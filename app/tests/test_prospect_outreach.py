from __future__ import annotations

import importlib.util
import inspect
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pypdf import PdfWriter

from app.config import Settings
from app.dealer_os import prospect_outreach_router
from app.models.dealer_prospect import DealerProspect, DealerProspectStageDefinition
from app.models.notification import Notification
from app.models.prospect_outreach import DealerProspectEmailDraft, DealerProspectEmailDraftAsset
from app.models.user import User
from app.schemas.prospect_outreach import ProspectDraftAction
from app.services import prospect_outreach as outreach
from app.services.email import prospect_reply, ses_client


def _draft(*, status: str = "pending_review", version: int = 1) -> DealerProspectEmailDraft:
    draft_id = uuid.uuid4()
    return DealerProspectEmailDraft(
        id=draft_id,
        prospect_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        recipient_email="dealer@example.com",
        from_email="no-reply@qualifiedcommercial.com",
        from_name="Qualified Commercial Dealer Desk",
        reply_to_email="support+replytoken1234@qualifiedcommercial.com",
        reply_token_hash="a" * 64,
        unsubscribe_token_hash="b" * 64,
        rfc_message_id=f"<prospect-{draft_id}@qualifiedcommercial.com>",
        subject="Dealer financing resources",
        editable_body="Hi Alex,\n\nHere is the information we discussed.",
        locked_footer_text=(
            "Please reply directly to this email with any questions. Replies are monitored at "
            "support@qualifiedcommercial.com. You may also contact franco@qualifiedcommercial.com.\n\n"
            "Learn more: https://qualifiedcommercial.com/industries/auto\n\n"
            "---\nQualified Commercial · 14 53rd St #408N, Brooklyn, NY 11232\n"
            "Unsubscribe from Dealer Desk email: "
            "https://api.qualifiedcommercial.com/api/v1/dealer-os/prospect-email-unsubscribe/token"
        ),
        body_text="body",
        body_html="<div>body</div>",
        purpose="dealer_information",
        draft_source="fallback",
        catalog_version="c" * 64,
        catalog_snapshot=[],
        status=status,
        auto_send_at=datetime.now(UTC) - timedelta(seconds=1),
        idempotency_key=uuid.uuid4(),
        request_fingerprint="d" * 64,
        version=version,
        attachment_count=0,
        attachment_total_bytes=0,
        delivery_mode="attachments",
    )


def test_interactive_draft_actions_require_the_reviewed_version():
    with pytest.raises(ValidationError):
        ProspectDraftAction.model_validate({})

    assert ProspectDraftAction(expected_version=4).expected_version == 4

    for endpoint in (
        prospect_outreach_router.start_prospect_email_edit,
        prospect_outreach_router.approve_prospect_email_draft,
        prospect_outreach_router.cancel_prospect_email_draft,
        prospect_outreach_router.use_secure_bundle_for_prospect_email,
    ):
        payload = inspect.signature(endpoint).parameters["payload"]
        assert payload.default is inspect.Parameter.empty


def test_outreach_migration_has_unique_columns_and_snapshot_scan_provenance(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0220_prospect_outreach.py"
    spec = importlib.util.spec_from_file_location("migration_0220_prospect_outreach", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    created: dict[str, list[str]] = {}

    class FakeOp:
        def create_table(self, name, *items):
            columns = [item.name for item in items if hasattr(item, "type") and item.name]
            assert len(columns) == len(set(columns)), f"duplicate column in {name}"
            created[name] = columns

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    monkeypatch.setattr(migration, "op", FakeOp())
    migration.upgrade()

    assert "validation_status" in created["marketing_collateral_assets"]
    assert "validation_status" in created["dealer_prospect_email_draft_assets"]
    assert "validation_status" in DealerProspectEmailDraftAsset.__table__.c


@pytest.mark.asyncio
async def test_edit_stops_durable_countdown_and_locked_footer_survives(monkeypatch):
    row = _draft(version=3)
    db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(outreach, "sync_draft_notifications", AsyncMock())

    async def load(*_args, **_kwargs):
        return row

    monkeypatch.setattr(outreach, "load_draft", load)
    edited = await outreach.edit_draft(
        db,
        row.id,
        expected_version=3,
        subject=" Updated subject ",
        # A browser may submit the previously rendered body. The server strips
        # and reattaches the exact locked suffix rather than duplicating it.
        body=f"New personal copy\n\n{row.locked_footer_text}",
    )

    assert edited.status == "editing"
    assert edited.auto_send_at is None
    assert edited.review_stopped_at is not None
    assert edited.version == 4
    assert edited.subject == "Updated subject"
    assert edited.editable_body == "New personal copy"
    assert edited.body_text.count(outreach.DEALER_WEBSITE) == 1
    assert edited.body_text.count("support@qualifiedcommercial.com") == 1
    assert edited.body_text.count("franco@qualifiedcommercial.com") == 1
    assert edited.body_text.endswith(edited.locked_footer_text)
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_never_dispatchable(monkeypatch):
    row = _draft(status="editing", version=5)
    row.delivery_mode = "secure_link"
    row.secure_bundle_token_hash = "f" * 64
    row.secure_bundle_expires_at = datetime.now(UTC) + timedelta(days=7)
    db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(outreach, "sync_draft_notifications", AsyncMock())

    async def load(*_args, **_kwargs):
        return row

    monkeypatch.setattr(outreach, "load_draft", load)
    cancelled = await outreach.cancel_draft(db, row.id, expected_version=5)
    assert cancelled.status == "cancelled"
    assert cancelled.auto_send_at is None
    assert cancelled.cancelled_at is not None
    assert cancelled.secure_bundle_token_hash is None
    assert cancelled.secure_bundle_expires_at is None

    again = await outreach.cancel_draft(db, row.id, expected_version=6)
    assert again is row
    assert row.version == 6


@pytest.mark.asyncio
async def test_cancelled_secure_bundle_is_not_publicly_downloadable():
    row = _draft(status="cancelled")
    row.delivery_mode = "secure_link"
    row.secure_bundle_token_hash = "f" * 64
    row.secure_bundle_expires_at = datetime.now(UTC) + timedelta(days=7)

    class ScalarResult:
        @staticmethod
        def scalar_one_or_none():
            return row

    db = SimpleNamespace(execute=AsyncMock(return_value=ScalarResult()))
    with pytest.raises(outreach.OutreachNotFound):
        await outreach.load_secure_bundle(db, "copied-before-cancel")
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_draft_can_be_cancelled_for_reversible_outcome_undo(monkeypatch):
    row = _draft(status="failed", version=2)
    db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(outreach, "sync_draft_notifications", AsyncMock())

    async def load(*_args, **_kwargs):
        return row

    monkeypatch.setattr(outreach, "load_draft", load)
    cancelled = await outreach.cancel_draft(db, row.id, expected_version=2)
    assert cancelled.status == "cancelled"
    assert cancelled.version == 3


@pytest.mark.asyncio
async def test_sending_claim_is_never_retried(monkeypatch):
    row = _draft(status="sending", version=8)
    db = SimpleNamespace(commit=AsyncMock())

    async def load(*_args, **_kwargs):
        return row

    suppression_check = AsyncMock()
    monkeypatch.setattr(outreach, "load_draft", load)
    monkeypatch.setattr(outreach, "is_suppressed", suppression_check)

    result = await outreach.dispatch_draft(db, row.id, automatic=True)
    assert result is row
    suppression_check.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_suppression_is_rechecked_immediately_before_claim(monkeypatch):
    row = _draft(status="pending_review", version=2)
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(outreach, "sync_draft_notifications", AsyncMock())

    async def load(*_args, **_kwargs):
        return row

    monkeypatch.setattr(outreach, "load_draft", load)
    monkeypatch.setattr(
        outreach,
        "is_suppressed",
        AsyncMock(return_value=SimpleNamespace(reason="complaint")),
    )

    result = await outreach.dispatch_draft(db, row.id, automatic=True)
    assert result.status == "blocked"
    assert result.failure_code == "email_suppressed"
    assert "complaint" in result.failure_detail
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_creator_access_is_rechecked_before_claim(monkeypatch):
    row = _draft(status="pending_review")
    prospect = DealerProspect(
        id=row.prospect_id,
        owner_user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        primary_contact_id=uuid.uuid4(),
        stage_definition_id=uuid.uuid4(),
        email_normalized=row.recipient_email,
        phone_normalized="+12025550100",
        dealer_name_normalized="dealer",
        do_not_contact=False,
        version=1,
    )
    actor = User(
        id=row.created_by_user_id,
        clerk_id="actor",
        email="rep@qualifiedcommercial.com",
        name="Rep",
        role="field_rep",
        account_status="active",
    )

    async def get(model, _key, **_kwargs):
        return prospect if model is DealerProspect else actor if model is User else None

    db = SimpleNamespace(get=get, commit=AsyncMock())
    monkeypatch.setattr(outreach, "sync_draft_notifications", AsyncMock())

    async def load(*_args, **_kwargs):
        return row

    monkeypatch.setattr(outreach, "load_draft", load)
    monkeypatch.setattr(outreach, "is_suppressed", AsyncMock(return_value=None))
    monkeypatch.setattr(
        outreach,
        "prospect_identity",
        AsyncMock(
            return_value=outreach.ProspectIdentity(
                contact_id=prospect.primary_contact_id,
                contact_name="Alex",
                dealer_name="Dealer",
                email=row.recipient_email,
                owner_user_id=prospect.owner_user_id,
            )
        ),
    )
    from app.dealer_os.services import prospects as core

    monkeypatch.setattr(
        core,
        "load_visible_prospect",
        AsyncMock(side_effect=HTTPException(404, "Prospect not found")),
    )

    result = await outreach.dispatch_draft(db, row.id, automatic=True)
    assert result.status == "blocked"
    assert result.failure_code == "creator_not_authorized"
    db.commit.assert_awaited_once()


def test_sba_microloan_and_unapproved_financial_claims_are_rejected():
    with pytest.raises(ValueError, match="published maximum"):
        outreach.validate_generated_copy(
            subject="Dealer information",
            body="SBA Microloans are available up to $350K.",
            catalog_snapshot=[],
        )
    with pytest.raises(ValueError, match="unapproved financial claim"):
        outreach.validate_generated_copy(
            subject="Dealer information",
            body="We can offer a 3.5% rate.",
            catalog_snapshot=[],
        )
    # Approved versioned catalog amounts may be personalized.
    outreach.validate_generated_copy(
        subject="Equipment financing information",
        body="The approved catalog includes options up to $50K, subject to lender review.",
        catalog_snapshot=[
            {
                "amount_min": 10_000,
                "amount_max": 50_000,
                "pricing": {},
                "copy": {"name": "Equipment financing"},
            }
        ],
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("Review our offer at https://example.com.", "contains a link"),
        ("Your approval is ready.", "approval or attachment"),
        ("See the attached approval letter.", "approval or attachment"),
        ("We offer cryptocurrency loans.", "unapproved product language"),
        ("We offer invoice factoring.", "unapproved product language"),
        ("Qualified Commercial can provide a quantum liquidity plan.", "capability language"),
    ],
)
def test_ai_copy_cannot_invent_links_attachments_approvals_or_products(body, message):
    with pytest.raises(ValueError, match=message):
        outreach.validate_generated_copy(
            subject="Dealer information",
            body=body,
            catalog_snapshot=[{"copy": {"name": "Equipment financing"}}],
        )


def test_program_section_is_rendered_only_from_the_versioned_catalog():
    rendered = outreach._with_approved_program_section(
        "Hi Alex,\n\nThanks for speaking with me.",
        [
            {"program_key": "equipment", "version": 4, "copy": {"name": "Equipment financing"}},
            {"program_key": "sba_7a", "version": 2, "copy": {"name": "SBA 7(a)"}},
        ],
    )

    assert "- Equipment financing" in rendered
    assert "- SBA 7(a)" in rendered
    assert "invoice factoring" not in rendered.lower()


@pytest.mark.asyncio
async def test_nova_micro_uses_bedrock_converse_and_records_usage(monkeypatch):
    settings = SimpleNamespace(
        ai_provider_enabled=True,
        prospect_bedrock_model="amazon.nova-micro-v1:0",
        aws_bearer_token_bedrock="",
        bedrock_runtime_region="us-east-1",
    )
    monkeypatch.setattr(outreach, "get_settings", lambda: settings)
    response = {
        "output": {
            "message": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "subject": "Resources for Sunrise Auto",
                                "body": "Hi Alex,\n\nTell us what you are planning and we can discuss next steps.",
                            }
                        )
                    }
                ]
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 30},
    }
    runtime = SimpleNamespace(converse=MagicMock(return_value=response))
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: runtime)
    from app.services.ai import usage

    monkeypatch.setattr(usage, "assert_ai_allowed", AsyncMock())
    record = AsyncMock()
    monkeypatch.setattr(usage, "record_ai_usage", record)
    prospect = SimpleNamespace(id=uuid.uuid4())
    identity = outreach.ProspectIdentity(
        contact_id=uuid.uuid4(),
        contact_name="Alex Smith",
        dealer_name="Sunrise Auto",
        email="alex@example.com",
        owner_user_id=uuid.uuid4(),
    )

    composed = await outreach._compose_with_nova(
        SimpleNamespace(),
        prospect=prospect,
        identity=identity,
        purpose="dealer_information",
        ai_instructions="Warm and concise",
        catalog_snapshot=[],
        actor_user_id=uuid.uuid4(),
    )

    assert composed.source == "ai"
    assert composed.model_id == "amazon.nova-micro-v1:0"
    runtime.converse.assert_called_once()
    assert runtime.converse.call_args.kwargs["modelId"] == "amazon.nova-micro-v1:0"
    record.assert_awaited_once()


def test_pdf_validation_rejects_encryption_and_active_content(monkeypatch):
    monkeypatch.setattr(
        outreach,
        "get_settings",
        lambda: SimpleNamespace(prospect_email_max_attachment_bytes=7_000_000),
    )
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    raw = io.BytesIO()
    writer.write(raw)
    valid = raw.getvalue()
    result = outreach.validate_pdf(valid)
    assert result.status == "passed_static"
    assert result.size_bytes == len(valid)

    encrypted_writer = PdfWriter()
    encrypted_writer.add_blank_page(width=612, height=792)
    encrypted_writer.encrypt("secret")
    encrypted = io.BytesIO()
    encrypted_writer.write(encrypted)
    with pytest.raises(outreach.OutreachBlocked) as locked:
        outreach.validate_pdf(encrypted.getvalue())
    assert locked.value.code == "password_protected_pdf"

    with pytest.raises(outreach.OutreachBlocked) as active:
        outreach.validate_pdf(valid + b"/JavaScript")
    assert active.value.code == "unsafe_pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (b"stream: OK\0", None),
        (b"stream: Eicar-Signature FOUND\0", "unsafe_pdf"),
        (b"unexpected endpoint: OK\0", "malware_scanner_unavailable"),
        (b"stream: UNKNOWN ERROR\0", "malware_scanner_unavailable"),
    ],
)
async def test_collateral_malware_scan_is_fail_closed(monkeypatch, response, expected_code):
    writes = bytearray()

    class Reader:
        async def readuntil(self, _separator):
            return response

    class Writer:
        def write(self, value):
            writes.extend(value)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def connect(_host, _port):
        return Reader(), Writer()

    monkeypatch.setattr(
        outreach,
        "get_settings",
        lambda: SimpleNamespace(
            prospect_collateral_clamd_host="clamd.internal",
            prospect_collateral_clamd_port=3310,
            prospect_collateral_clamd_timeout_seconds=5,
        ),
    )
    monkeypatch.setattr(outreach.asyncio, "open_connection", connect)

    if expected_code:
        with pytest.raises(outreach.OutreachBlocked) as blocked:
            await outreach.scan_pdf_with_clamd(b"%PDF-safe")
        assert blocked.value.code == expected_code
    else:
        assert await outreach.scan_pdf_with_clamd(b"%PDF-safe") == "stream: OK"
        assert writes.startswith(b"zINSTREAM\0")
        assert writes.endswith(b"\0\0\0\0")


@pytest.mark.asyncio
async def test_collateral_malware_scan_requires_a_configured_engine(monkeypatch):
    monkeypatch.setattr(
        outreach,
        "get_settings",
        lambda: SimpleNamespace(prospect_collateral_clamd_host=""),
    )

    with pytest.raises(outreach.OutreachBlocked) as blocked:
        await outreach.scan_pdf_with_clamd(b"%PDF-safe")
    assert blocked.value.code == "malware_scanner_unavailable"


@pytest.mark.asyncio
async def test_only_antivirus_scanned_collateral_can_be_approved():
    class Scalars:
        @staticmethod
        def all():
            return []

    class Result:
        @staticmethod
        def scalars():
            return Scalars()

    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result()),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    actor_id = uuid.uuid4()

    def collateral(validation_status):
        return SimpleNamespace(
            id=uuid.uuid4(),
            assignment=outreach.COLLATERAL_ASSIGNMENT,
            logical_key="dealer-overview",
            validation_status=validation_status,
            status="pending_approval",
            sort_order=0,
            approved_at=None,
            approved_by_user_id=None,
            retired_at=None,
            retired_by_user_id=None,
        )

    scanned = collateral("passed_antivirus")
    approved = await outreach.approve_collateral(db, scanned, actor_user_id=actor_id)
    assert approved.status == "active"
    assert approved.approved_by_user_id == actor_id

    legacy_static = collateral("passed_static")
    with pytest.raises(outreach.OutreachBlocked) as blocked:
        await outreach.approve_collateral(db, legacy_static, actor_user_id=actor_id)
    assert blocked.value.code == "collateral_not_validated"


def test_attachment_limit_is_all_or_none(monkeypatch):
    monkeypatch.setattr(
        outreach,
        "get_settings",
        lambda: SimpleNamespace(prospect_email_max_attachment_bytes=7_000_000),
    )
    assert not outreach.attachment_bundle_too_large(7_000_000)
    assert outreach.attachment_bundle_too_large(7_000_001)


def test_booking_link_is_application_owned_and_locked_in_footer():
    url = "https://app.qualifiedcommercial.com/book/alex-rep"
    footer = outreach._locked_footer(
        signature=["Alex Rep", "Relationship Manager"],
        attachment_names=[],
        unsubscribe_url="https://api.qualifiedcommercial.com/unsubscribe/token",
        booking_url=url,
    )
    assert f"Book a time: {url}" in footer
    assert outreach.DEALER_WEBSITE in footer
    assert footer.count("support@qualifiedcommercial.com") == 1
    assert footer.count("franco@qualifiedcommercial.com") == 1
    assert footer.startswith("Please reply directly to this email with any questions.")
    assert outreach._render_body("Personal copy", footer).endswith(footer)


@pytest.mark.asyncio
async def test_oversized_draft_requires_explicit_secure_bundle_and_restarts_review(
    monkeypatch,
):
    row = _draft(status="blocked", version=4)
    monkeypatch.setattr(outreach, "sync_draft_notifications", AsyncMock())
    row.failure_code = "attachment_bundle_too_large"
    row.failure_detail = "too large"
    row.attachment_count = 2
    row.attachment_total_bytes = 8_000_000
    row.locked_footer_text = (
        f"Learn more: {outreach.DEALER_WEBSITE}\n"
        "Attached for reference: one.pdf, two.pdf\n\n"
        "---\nQualified Commercial · address\n"
        "Unsubscribe from Dealer Desk email: https://api.example.test/unsubscribe/token"
    )

    class CountResult:
        @staticmethod
        def scalar_one():
            return 2

    db = SimpleNamespace(
        execute=AsyncMock(return_value=CountResult()),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    async def load(*_args, **_kwargs):
        return row

    monkeypatch.setattr(outreach, "load_draft", load)
    monkeypatch.setattr(
        outreach,
        "get_settings",
        lambda: SimpleNamespace(
            prospect_secure_bundle_days=7,
            prospect_email_review_seconds=60,
            public_api_url="https://api.qualifiedcommercial.com",
        ),
    )
    selected = await outreach.select_secure_bundle(
        db,
        row.id,
        actor_user_id=uuid.uuid4(),
        expected_version=4,
    )
    assert selected.delivery_mode == "secure_link"
    assert selected.status == "pending_review"
    assert 55 <= (selected.auto_send_at - datetime.now(UTC)).total_seconds() <= 60
    assert selected.failure_code is None
    assert "Attached for reference:" not in selected.locked_footer_text
    assert "prospect-email-bundles/" in selected.locked_footer_text
    assert selected.secure_bundle_token_hash
    assert selected.secure_bundle_expires_at > datetime.now(UTC) + timedelta(days=6)
    db.flush.assert_awaited_once()


def test_secure_bundle_keeps_the_locked_reply_note_first():
    footer = outreach._locked_footer(
        signature=["Alex Rep", "Relationship Manager"],
        attachment_names=["dealer-guide.pdf"],
        unsubscribe_url="https://api.qualifiedcommercial.com/unsubscribe/token",
    )
    updated = outreach._footer_with_secure_bundle(
        footer,
        bundle_url="https://api.qualifiedcommercial.com/bundle/token",
        expires_at=datetime(2026, 9, 23, tzinfo=UTC),
    )
    lines = updated.splitlines()
    assert lines[0].startswith("Please reply directly to this email")
    assert lines.index("Learn more: https://qualifiedcommercial.com/industries/auto") < lines.index(
        "Secure dealer information bundle (expires 2026-09-23): "
        "https://api.qualifiedcommercial.com/bundle/token"
    )


def test_legacy_dealer_desk_addresses_migrate_to_selected_routing():
    settings = Settings(
        _env_file=None,
        prospect_from_email=" DEALERS@qualifiedcommercial.com ",
        prospect_reply_to_email="dealers@qualifiedcommercial.com",
    )
    assert settings.prospect_from_email == "no-reply@qualifiedcommercial.com"
    assert settings.prospect_reply_to_email == "support@qualifiedcommercial.com"
    assert settings.prospect_alternate_contact_email == "franco@qualifiedcommercial.com"


def test_tokenized_reply_address_is_correlated_without_subject(monkeypatch):
    monkeypatch.setattr(
        prospect_reply,
        "get_settings",
        lambda: SimpleNamespace(prospect_reply_to_email="support@qualifiedcommercial.com"),
    )
    assert prospect_reply.reply_tokens(
        ["Qualified Commercial <support+Abc_123456789@qualifiedcommercial.com>"]
    ) == ["abc_123456789"]
    assert prospect_reply.reply_tokens(["someone@example.com"]) == []


def test_ses_raw_message_keeps_firm_sender_reply_alias_and_one_click_headers(monkeypatch):
    settings = SimpleNamespace(
        ses_from_address="fallback@qualifiedcommercial.com",
        ses_region="us-east-1",
        ses_configuration_set="",
    )
    monkeypatch.setattr(ses_client, "get_settings", lambda: settings)
    client = SimpleNamespace(send_raw_email=MagicMock(return_value={"MessageId": "ses-message-1"}))
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: client)
    result = ses_client.send_raw_email(
        to_emails=["dealer@example.com"],
        subject="Information",
        body_text="Body",
        source_email="no-reply@qualifiedcommercial.com",
        source_name="Qualified Commercial Dealer Desk",
        reply_to="support+threadtoken@qualifiedcommercial.com",
        headers={
            "Message-ID": "<prospect-id@qualifiedcommercial.com>",
            "List-Unsubscribe": "<https://api.qualifiedcommercial.com/unsubscribe/token>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        attachments=[("one.pdf", b"%PDF-test", "application/pdf")],
    )
    assert result.ok
    raw = client.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message["From"] == "Qualified Commercial Dealer Desk <no-reply@qualifiedcommercial.com>"
    assert message["Reply-To"] == "support+threadtoken@qualifiedcommercial.com"
    assert message["Message-ID"] == "<prospect-id@qualifiedcommercial.com>"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert len(list(message.iter_attachments())) == 1


@pytest.mark.asyncio
async def test_draft_notification_is_created_then_updated_with_countdown_state():
    row = _draft()
    owner_id = uuid.uuid4()
    prospect = SimpleNamespace(id=row.prospect_id, owner_user_id=owner_id)

    class RowsResult:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    added = []
    db = SimpleNamespace(
        get=AsyncMock(return_value=prospect),
        execute=AsyncMock(return_value=RowsResult([])),
        add=added.append,
    )
    await outreach.sync_draft_notifications(db, row)
    notices = [item for item in added if isinstance(item, Notification)]
    assert {item.recipient_user_id for item in notices} == {row.created_by_user_id, owner_id}
    assert all(
        item.meta
        == {
            "draft_id": str(row.id),
            "prospect_id": str(row.prospect_id),
            "send_after": row.auto_send_at.isoformat(),
            "status": "pending_review",
        }
        for item in notices
    )

    row.status = "editing"
    row.auto_send_at = None
    db.execute = AsyncMock(return_value=RowsResult(notices))
    await outreach.sync_draft_notifications(db, row)
    assert all(item.meta["status"] == "editing" for item in notices)
    assert all(item.meta["send_after"] is None for item in notices)
    assert all("requires explicit approval" in item.body for item in notices)


@pytest.mark.asyncio
async def test_successful_first_email_advances_only_new_stage_with_versioned_activity():
    row = _draft(status="sent")
    prospect = DealerProspect(
        id=row.prospect_id,
        owner_user_id=row.created_by_user_id,
        company_id=uuid.uuid4(),
        primary_contact_id=uuid.uuid4(),
        stage_definition_id=uuid.uuid4(),
        email_normalized=row.recipient_email,
        phone_normalized="+12025550100",
        dealer_name_normalized="dealer",
        version=7,
    )
    current = DealerProspectStageDefinition(
        id=prospect.stage_definition_id,
        key="new",
        label="New",
        sort_order=0,
        is_active=True,
        is_system=True,
        behavior={},
    )
    emailed = DealerProspectStageDefinition(
        id=uuid.uuid4(),
        key="emailed",
        label="Emailed",
        sort_order=10,
        is_active=True,
        is_system=True,
        behavior={},
    )

    class ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    added = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[ScalarResult(prospect), ScalarResult(emailed)]),
        get=AsyncMock(return_value=current),
        add=added.append,
    )
    result = await outreach._advance_new_prospect_after_delivery(db, row)
    assert result is prospect
    assert prospect.stage_definition_id == emailed.id
    assert prospect.version == 8
    activity = added[-1]
    assert activity.metadata_json["version_before"] == 7
    assert activity.metadata_json["version_after"] == 8
    assert activity.metadata_json["draft_id"] == str(row.id)


@pytest.mark.asyncio
async def test_collateral_reorder_is_complete_atomic_and_audited():
    first = SimpleNamespace(id=uuid.uuid4(), sort_order=0)
    second = SimpleNamespace(id=uuid.uuid4(), sort_order=10)

    class RowsResult:
        def scalars(self):
            return self

        @staticmethod
        def all():
            return [first, second]

    added = []
    db = SimpleNamespace(
        execute=AsyncMock(return_value=RowsResult()),
        add=added.append,
        flush=AsyncMock(),
    )
    rows = await outreach.reorder_collateral(
        db,
        actor_user_id=uuid.uuid4(),
        expected_ids=[first.id, second.id],
        ordered_ids=[second.id, first.id],
    )
    assert rows == [second, first]
    assert [second.sort_order, first.sort_order] == [0, 10]
    assert len(added) == 2
    assert all(item.event_type == "reordered" for item in added)
    assert all(item.details["atomic"] is True for item in added)


def test_rollout_gate_keeps_public_links_and_collateral_setup_available(monkeypatch):
    guard = MagicMock()
    monkeypatch.setattr(
        prospect_outreach_router.prospect_service,
        "require_pipeline_enabled",
        guard,
    )
    for path in (
        "/api/v1/dealer-os/prospect-email-unsubscribe/already-issued-token",
        "/api/v1/dealer-os/prospect-email-bundles/already-issued-token",
        "/api/v1/dealer-os/marketing-collateral",
        f"/api/v1/dealer-os/marketing-collateral/{uuid.uuid4()}/approve",
    ):
        prospect_outreach_router._require_outreach_enabled(
            SimpleNamespace(url=SimpleNamespace(path=path))
        )
    guard.assert_not_called()

    prospect_outreach_router._require_outreach_enabled(
        SimpleNamespace(url=SimpleNamespace(path="/api/v1/dealer-os/prospect-email-drafts"))
    )
    guard.assert_called_once_with()


@pytest.mark.asyncio
async def test_collateral_setup_remains_config_admin_only(monkeypatch):
    denied = MagicMock(side_effect=HTTPException(status_code=403, detail="admin required"))
    monkeypatch.setattr(
        prospect_outreach_router.prospect_service,
        "require_config_admin",
        denied,
    )
    db = SimpleNamespace(execute=AsyncMock())
    user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await prospect_outreach_router.list_marketing_collateral(user=user, db=db)

    assert exc.value.status_code == 403
    denied.assert_called_once_with(user)
    db.execute.assert_not_awaited()
