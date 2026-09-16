from __future__ import annotations

import base64
import hashlib
import io
import sys
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter

from app.routers import application_offer_deliveries as offer_router
from app.routers.application_offer_deliveries import _request_fingerprint
from app.schemas.application_offer_delivery import (
    ManualOfferResponse,
    OfferDeliveryCreate,
    OfferDeliveryReconciliationRequest,
)
from app.services import application_offer_deliveries as offers
from app.services.email.gmail_client import build_message


def _resolved(*, kind: str = "production_term_sheet", title: str = "Financing Terms"):
    return offers.ResolvedOffer(
        kind=kind,
        source_id=uuid4(),
        version=3,
        source=object(),  # the body composer only consumes canonical fields
        label="Loan terms v3",
        title=title,
        file_name="Qualified-Commercial-Terms.pdf",
        lines=["Approved amount: $500,000.00", "Rate: 12.99%", "Term: 24 months"],
    )


def test_plain_and_html_offer_bodies_lock_terms_links_and_individual_deadlines() -> None:
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    loan = _resolved()
    merchant = _resolved(kind="merchant_offer", title="Merchant Processing Offer")
    merchant.label = "Merchant processing offer v2"
    merchant.lines = ["Estimated annual savings: $47,094.00"]
    links = {
        loan.key: "https://secure.example/room?item=loan&next=<review>",
        merchant.key: "https://secure.example/room?item=merchant",
    }
    expiries = {loan.key: now + timedelta(hours=12), merchant.key: now + timedelta(hours=48)}

    text = offers.compose_body(
        "Hello <Client>,",
        [loan, merchant],
        item_links=links,
        expires_at=expiries[loan.key],
        item_expiries=expiries,
    )
    html = offers.compose_body_html(
        "Hello <Client>,",
        [loan, merchant],
        item_links=links,
        expires_at=expiries[loan.key],
        item_expiries=expiries,
    )

    for required in (
        "$500,000.00",
        "12.99%",
        "$47,094.00",
        "within 48 hours",
        "Expired terms require reconfirmation",
        offers.LOAN_DISCLAIMER,
    ):
        assert required in text
        assert required in html
    assert "September 17, 2026 at 12:00 AM UTC" in text
    assert "September 18, 2026 at 12:00 PM UTC" in text
    assert "Hello &lt;Client&gt;" in html
    assert "next=&lt;review&gt;" in html


def test_offer_draft_fingerprint_is_stable_and_term_sensitive() -> None:
    row = _resolved()
    first = offers.draft_fingerprint([row])
    assert first == offers.draft_fingerprint([row])
    row.lines[-1] = "Term: 36 months"
    assert offers.draft_fingerprint([row]) != first


def test_production_revolving_summary_uses_canonical_cadence_and_separate_balloon() -> None:
    row = SimpleNamespace(
        facility_type="Revolving line of credit",
        approved_amount=500_000,
        min_activation_amount=1,
        rate_pct=10.5,
        term_months=24,
        monthly_debt_service=875,
        debt_service_is_level_payment=False,
        funding_party_kind="Lender",
        funding_party_name="Northstar Bank",
        conditions="Final verification.",
        extra={
            "facility_kind": "revolving_loc",
            "funder_type": "bank",
            "repayment_structure": "revolving_interest_only",
            "payment_frequency": "monthly",
            "rate_structure": "variable",
            "rate_index": "Prime",
            "rate_index_rate_pct": 8.5,
            "rate_margin_pct": 2,
            "rate_as_of": "2026-09-16",
            "apr_pct": 11.25,
            "initial_draw_amount": 100_000,
            "payment_basis_amount": 100_000,
            "monthly_program_coverage_amount": 1_000,
            "closing_estimate_days": 5,
            "expiration_days": 7,
            "payment_summary": {"lines": ["stale browser summary"], "assumptions": []},
        },
    )

    text = "\n".join(offers.production_term_lines(row))

    for required in (
        "Credit limit: $500,000.00",
        "Rate: 10.50% current (Prime 8.50% + 2.00%) as of 2026-09-16",
        "Lender-disclosed APR: 11.25%",
        "Repayment structure: Revolving interest only",
        "Initial draw: $100,000.00",
        "Estimated payment (Monthly): $875.00",
        "Annual scheduled debt service: $10,500.00",
        "Balloon due at maturity: $100,000.00",
        "Monthly program coverage amount: $1,000.00",
        "Offer validity: 7 days after issuance",
    ):
        assert required in text
    assert "stale browser summary" not in text
    assert "per month" not in text
    assert offers._production_expiry(row) is None


def test_draft_items_expose_authenticated_current_version_previews() -> None:
    profile_id = uuid4()
    row = _resolved()

    item = row.draft_item(profile_id)

    expected = (
        f"/api/v1/application-profiles/{profile_id}/offer-items/"
        f"{row.kind}/{row.source_id}/document?expected_version={row.version}"
    )
    assert item.preview_url == f"{expected}&disposition=inline"
    assert item.download_url == f"{expected}&disposition=attachment"


def test_locked_deadline_includes_provider_handoff_grace() -> None:
    prepared_at = datetime(2026, 9, 16, 12, tzinfo=UTC)

    expiry = offers.default_offer_expiry(prepared_at)

    assert expiry - prepared_at == timedelta(
        hours=offers.DEADLINE_HOURS,
        minutes=offers.HANDOFF_GRACE_MINUTES,
    )
    accepted_at = prepared_at + timedelta(minutes=offers.HANDOFF_GRACE_MINUTES)
    assert expiry - accepted_at >= timedelta(hours=offers.DEADLINE_HOURS)


def test_production_relative_validity_starts_at_send_and_caps_the_item_deadline() -> None:
    prepared_at = datetime(2026, 9, 16, 12, tzinfo=UTC)
    item = _resolved()
    item.source = SimpleNamespace(extra={"expiration_days": 1})

    assert offers.item_offer_expiry(item, prepared_at) == prepared_at + timedelta(days=1)

    item.source = SimpleNamespace(extra={"expiration_days": 7})
    assert offers.item_offer_expiry(item, prepared_at) == offers.default_offer_expiry(prepared_at)


def test_absolute_source_expiry_still_beats_relative_production_validity() -> None:
    prepared_at = datetime(2026, 9, 16, 12, tzinfo=UTC)
    item = _resolved()
    item.source = SimpleNamespace(extra={"expiration_days": 7})
    item.source_expires_at = prepared_at + timedelta(hours=6)

    assert offers.item_offer_expiry(item, prepared_at) == item.source_expires_at


def test_ai_personal_copy_cannot_author_financial_terms_or_links() -> None:
    assert offers._safe_ai_copy(
        "Your offer package is ready",
        "Hello Alex, your selected package is ready for review. Reply if we can help.",
    )
    assert not offers._safe_ai_copy(
        "Approved at 12.99%",
        "Hello Alex, your selected package is ready for review. Reply if we can help.",
    )
    assert not offers._safe_ai_copy(
        "Your offer package",
        "Open https://untrusted.example and accept the $500,000 amount.",
    )


def test_merchant_options_include_client_terms_without_desk_only_fields() -> None:
    lines = offers.merchant_option_lines(
        [
            {
                "label": "ConsumerChoice",
                "effective_rate_pct": 0.37,
                "monthly_fees": 778,
                "monthly_savings": 3924,
                "internal_margin_bps": 175,
                "desk_note": "never expose this",
            }
        ]
    )

    assert lines == [
        "Available option 1: ConsumerChoice · effective rate 0.37% · "
        "monthly fees $778.00 · estimated monthly savings $3,924.00"
    ]
    assert "margin" not in lines[0]
    assert "desk" not in lines[0]


def test_response_appendix_is_a_valid_extra_pdf_page_with_exact_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    source = io.BytesIO()
    writer.write(source)
    deadline = datetime(2026, 9, 18, 16, 30, tzinfo=UTC)
    rendered_html: list[str] = []

    class _FakeHTML:
        def __init__(self, *, string: str):
            rendered_html.append(string)

        def write_pdf(self) -> bytes:
            appendix = PdfWriter()
            appendix.add_blank_page(width=612, height=792)
            target = io.BytesIO()
            appendix.write(target)
            return target.getvalue()

    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_FakeHTML))

    rendered = offers.append_response_page(
        source.getvalue(),
        title="Financing Terms",
        response_url="https://secure.example/respond/abc",
        expires_at=deadline,
    )

    reader = PdfReader(io.BytesIO(rendered))
    assert len(reader.pages) == 2
    assert "Review and respond securely" in rendered_html[0]
    assert "September 18, 2026 at 04:30 PM UTC" in rendered_html[0]
    assert "https://secure.example/respond/abc" in rendered_html[0]


def test_delivery_expiry_is_independent_per_item() -> None:
    now = datetime.now(UTC)
    delivery = SimpleNamespace(status="sent")
    delivery.items = [
        SimpleNamespace(
            kind="production_term_sheet",
            decision_status="pending",
            expires_at=now - timedelta(minutes=1),
        ),
        SimpleNamespace(
            kind="merchant_offer",
            decision_status="pending",
            expires_at=now + timedelta(hours=24),
        ),
    ]

    offers.refresh_delivery_status(delivery)

    assert delivery.items[0].decision_status == "expired"
    assert delivery.items[1].decision_status == "pending"
    assert delivery.status == "partially_decided"


def test_manual_response_requires_timezone_aware_received_at() -> None:
    payload = {
        "response": "accepted",
        "responder_name": "Alex Client",
        "channel": "phone",
        "received_at": "2026-09-16T14:30:00",
        "attestation": "I attest this accurately records the client's response.",
    }
    with pytest.raises(ValidationError, match="timezone"):
        ManualOfferResponse.model_validate(payload)

    payload["received_at"] = "2026-09-16T14:30:00-04:00"
    validated = ManualOfferResponse.model_validate(payload)
    assert validated.received_at.utcoffset() == timedelta(hours=-4)


def test_idempotency_fingerprint_covers_recipient_message_and_package() -> None:
    term_id = uuid4()
    values = {
        "idempotency_key": uuid4(),
        "to_contact_id": "client",
        "subject": "Your offer package",
        "personal_message": "Hello, your offer package is ready for review.",
        "items": [
            {
                "kind": "application_term_sheet",
                "term_sheet_id": term_id,
                "expected_version": 3,
            }
        ],
    }
    original = OfferDeliveryCreate.model_validate(values)
    changed = OfferDeliveryCreate.model_validate(
        {**values, "personal_message": "Hello, this is a different saved message."}
    )

    assert _request_fingerprint(original) != _request_fingerprint(changed)


@pytest.mark.asyncio
async def test_manual_response_cannot_predate_provider_acceptance() -> None:
    sent_at = datetime.now(UTC) - timedelta(hours=1)
    delivery_id = uuid4()
    profile_id = uuid4()
    delivery = SimpleNamespace(
        id=delivery_id,
        profile_id=profile_id,
        sent_at=sent_at,
        items=[],
    )
    item = SimpleNamespace(
        id=uuid4(),
        delivery_id=delivery_id,
        kind="application_term_sheet",
        decision_status="pending",
        expires_at=sent_at + timedelta(hours=48),
    )
    delivery.items = [item]
    profile = SimpleNamespace(id=profile_id)

    with pytest.raises(HTTPException, match="earlier than the email delivery") as exc:
        await offers.record_response(
            SimpleNamespace(),
            profile=profile,
            delivery=delivery,
            item=item,
            response="accepted",
            responder_name="Alex Client",
            reason=None,
            channel="phone",
            responded_at=sent_at - timedelta(minutes=1),
            ip_address=None,
            user_agent=None,
            user_id=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_repeated_identical_response_is_an_idempotent_noop() -> None:
    delivery_id = uuid4()
    profile = SimpleNamespace(id=uuid4())
    item = SimpleNamespace(
        delivery_id=delivery_id,
        kind="application_term_sheet",
        decision_status="accepted",
    )
    delivery = SimpleNamespace(id=delivery_id, profile_id=profile.id)

    changed = await offers.record_response(
        SimpleNamespace(),
        profile=profile,
        delivery=delivery,
        item=item,
        response="accepted",
        responder_name="Alex Client",
        reason=None,
        channel="authenticated_client",
        responded_at=datetime.now(UTC),
        ip_address=None,
        user_agent=None,
        user_id=uuid4(),
    )

    assert changed is False


@pytest.mark.asyncio
async def test_delivery_pin_snapshot_survives_room_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id = uuid4()
    delivery_id = uuid4()
    link = SimpleNamespace(
        status="active",
        expires_at=None,
        passcode_hash="new-pin-hash",
        bucket_id=uuid4(),
    )
    candidate = SimpleNamespace(
        id=delivery_id,
        profile_id=profile_id,
        access_passcode_hash="old-pin-hash",
        status="sent",
        published_at=datetime.now(UTC),
    )
    second_delivery_id = uuid4()
    second_candidate = SimpleNamespace(
        id=second_delivery_id,
        profile_id=profile_id,
        access_passcode_hash="old-pin-hash",
        status="sending",
        published_at=None,
    )
    first_result = SimpleNamespace(scalar_one_or_none=lambda: link)
    second_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [candidate, second_candidate])
    )
    profile = SimpleNamespace(id=profile_id)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[first_result, second_result]),
        get=AsyncMock(return_value=profile),
    )
    monkeypatch.setattr(offer_router, "_ip", lambda _request: "test-client")
    checked: list[str] = []

    def verify(passcode, digest, *, attempt_scope):
        checked.append(digest)
        return passcode == "112233" and digest == "old-pin-hash"

    monkeypatch.setattr(offer_router, "_verify_passcode", verify)

    resolved_profile, allowed, uncertain = await offer_router._public_profile(
        db,
        "historical-room-token",
        "112233",
        SimpleNamespace(),
    )

    assert resolved_profile is profile
    assert allowed == {delivery_id, second_delivery_id}
    assert uncertain == {second_delivery_id}
    assert checked == ["new-pin-hash", "old-pin-hash"]


@pytest.mark.asyncio
async def test_evidence_attachment_rejects_raw_merchant_source_by_foreign_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    file_id = uuid4()
    row = SimpleNamespace(
        id=file_id,
        deleted_at=None,
        status="uploaded",
        source_detail="ordinary-looking filename",
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=row),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: uuid4())),
    )
    monkeypatch.setattr(
        offers.application_profiles,
        "evidence_state",
        AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)])),
    )
    monkeypatch.setattr(
        offers.merchant_processing,
        "is_offer_document",
        lambda _row: False,
    )

    with pytest.raises(HTTPException, match="partner source PDF") as exc:
        await offers.evidence_snapshot(db, profile=profile, file_id=file_id)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_provider_acceptance_is_persisted_before_ancillary_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def persist(*_args, **_kwargs):
        events.append("provider-accepted")

    async def effects(*_args, **_kwargs):
        events.append("effects")
        raise RuntimeError("audit subsystem unavailable")

    monkeypatch.setattr(offer_router, "_persist_provider_acceptance", persist)
    monkeypatch.setattr(offer_router, "_apply_delivery_effects", effects)
    db = SimpleNamespace(rollback=AsyncMock())
    delivery = SimpleNamespace(id=uuid4())

    await offer_router._finalize_delivery_success(
        db,
        profile=SimpleNamespace(id=uuid4()),
        delivery=delivery,
        message=SimpleNamespace(),
        user=SimpleNamespace(id=uuid4()),
    )

    assert events == ["provider-accepted", "effects"]
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_retry_rejects_a_changed_verified_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    delivery = SimpleNamespace(
        to_contact_id="client",
        cc_contact_ids=["owner:1"],
        recipient_emails=["old-client@example.com", "owner@example.com"],
        cc_emails=["owner@example.com"],
    )
    contacts = [
        SimpleNamespace(id="client", email="corrected-client@example.com"),
        SimpleNamespace(id="owner:1", email="owner@example.com"),
    ]
    monkeypatch.setattr(
        offer_router.communications,
        "_contacts",
        AsyncMock(return_value=(contacts, [])),
    )

    with pytest.raises(HTTPException, match="Recipient details changed") as exc:
        await offer_router._revalidate_saved_recipients(SimpleNamespace(), profile, delivery)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_failed_retry_rechecks_current_contact_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = HTTPException(409, "Direct client contact is now suppressed")
    monkeypatch.setattr(
        offer_router.communications,
        "_enforce_direct_client_contact",
        AsyncMock(side_effect=blocked),
    )
    revalidate = AsyncMock()
    monkeypatch.setattr(offer_router, "_revalidate_saved_recipients", revalidate)

    with pytest.raises(HTTPException, match="now suppressed"):
        await offer_router._retry_failed_delivery(
            SimpleNamespace(),
            profile=SimpleNamespace(id=uuid4()),
            delivery=SimpleNamespace(id=uuid4()),
            user=SimpleNamespace(id=uuid4()),
        )

    revalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_provider_lock_failure_is_retryable_and_locks_in_stable_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    delivery = SimpleNamespace(id=uuid4())
    persisted = SimpleNamespace(
        id=delivery.id,
        status="sending",
        provider_message_id=None,
        provider_detail=None,
    )
    merchant = SimpleNamespace(kind="merchant_offer", offer_id=uuid4())
    terms = SimpleNamespace(kind="production_term_sheet", term_sheet_id=uuid4())
    resolve = AsyncMock(side_effect=RuntimeError("transient database deadlock"))
    monkeypatch.setattr(offer_router.offers, "resolve_offers", resolve)
    monkeypatch.setattr(
        offer_router,
        "_load_delivery",
        AsyncMock(return_value=persisted),
    )
    db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())

    with pytest.raises(RuntimeError, match="deadlock"):
        await offer_router._lock_sources_for_provider_handoff(
            db,
            profile=profile,
            refs=[merchant, terms],
            delivery=delivery,
        )

    locked_refs = resolve.await_args.args[2]
    assert [ref.kind for ref in locked_refs] == [
        "production_term_sheet",
        "merchant_offer",
    ]
    assert persisted.status == "failed"
    assert persisted.provider_message_id is None
    assert "retry is safe" in persisted.provider_detail
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliation_rejects_an_active_provider_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    profile = SimpleNamespace(id=uuid4())
    delivery = SimpleNamespace(
        id=uuid4(),
        status="sending",
        published_at=None,
        provider_handoff_started_at=now - timedelta(minutes=1),
    )
    monkeypatch.setattr(offer_router, "_operator_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(offer_router, "_load_delivery", AsyncMock(return_value=delivery))
    payload = OfferDeliveryReconciliationRequest(
        outcome="confirmed_not_sent",
        attestation="I verified the provider logs and no send occurred.",
    )

    with pytest.raises(HTTPException, match="may still be active") as exc:
        await offer_router.reconcile_offer_delivery(
            profile.id,
            delivery.id,
            payload,
            SimpleNamespace(id=uuid4()),
            SimpleNamespace(),
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_audited_reconciliation_publishes_provider_accepted_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    profile = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4())
    delivery = SimpleNamespace(
        id=uuid4(),
        status="sending",
        published_at=None,
        provider=None,
        provider_message_id=None,
        provider_detail=None,
        provider_correlation_id="qc-offer-correlation-1",
        provider_handoff_started_at=now - timedelta(minutes=30),
        sent_at=None,
        created_at=now - timedelta(hours=1),
        reconciled_at=None,
        reconciled_by_user_id=None,
        reconciliation_outcome=None,
        reconciliation_attestation=None,
    )
    monkeypatch.setattr(offer_router, "_operator_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(offer_router, "_load_delivery", AsyncMock(return_value=delivery))
    effects = AsyncMock()
    monkeypatch.setattr(offer_router, "_apply_delivery_effects", effects)
    audit = AsyncMock()
    monkeypatch.setattr(offer_router.profiles, "log_profile_action", audit)
    duplicate_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=duplicate_result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    accepted_at = now - timedelta(minutes=29)
    payload = OfferDeliveryReconciliationRequest(
        outcome="provider_accepted",
        provider="gmail",
        provider_message_id="gmail-provider-123",
        accepted_at=accepted_at,
        attestation="I matched the stable QC correlation in the provider sent mailbox.",
    )

    result = await offer_router.reconcile_offer_delivery(
        profile.id,
        delivery.id,
        payload,
        user,
        db,
    )

    assert result.status == "sent"
    assert result.provider_message_id == "gmail-provider-123"
    assert delivery.published_at is not None
    assert delivery.reconciliation_outcome == "provider_accepted"
    assert delivery.reconciled_by_user_id == user.id
    assert "matched the stable QC correlation" in delivery.reconciliation_attestation
    effects.assert_awaited_once_with(
        db,
        profile=profile,
        delivery_id=delivery.id,
        user=user,
    )
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_audited_reconciliation_can_confirm_no_provider_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    profile = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4())
    delivery = SimpleNamespace(
        id=uuid4(),
        status="sending",
        published_at=None,
        provider=None,
        provider_message_id=None,
        provider_detail=None,
        provider_correlation_id="qc-offer-correlation-2",
        provider_handoff_started_at=now - timedelta(minutes=30),
        sent_at=None,
        reconciled_at=None,
        reconciled_by_user_id=None,
        reconciliation_outcome=None,
        reconciliation_attestation=None,
    )
    monkeypatch.setattr(offer_router, "_operator_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(offer_router, "_load_delivery", AsyncMock(return_value=delivery))
    audit = AsyncMock()
    monkeypatch.setattr(offer_router.profiles, "log_profile_action", audit)
    db = SimpleNamespace(commit=AsyncMock())
    payload = OfferDeliveryReconciliationRequest(
        outcome="confirmed_not_sent",
        attestation="I checked both provider logs and the sender mailbox; no send occurred.",
    )

    result = await offer_router.reconcile_offer_delivery(
        profile.id,
        delivery.id,
        payload,
        user,
        db,
    )

    assert result.status == "failed"
    assert delivery.reconciliation_outcome == "confirmed_not_sent"
    assert delivery.reconciled_by_user_id == user.id
    audit.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_key_retry_stays_blocked_while_provider_outcome_is_uncertain() -> None:
    profile = SimpleNamespace(id=uuid4())
    delivery = SimpleNamespace(
        id=uuid4(),
        profile_id=profile.id,
        request_fingerprint="fingerprint",
        status="sending",
        provider_message_id=None,
        published_at=None,
    )

    with pytest.raises(HTTPException, match="already in progress") as exc:
        await offer_router._resume_existing_delivery(
            SimpleNamespace(),
            profile=profile,
            delivery=delivery,
            request_fingerprint="fingerprint",
            user=SimpleNamespace(id=uuid4()),
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_retry_exception_after_handoff_remains_uncertain_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    claimed = SimpleNamespace(
        id=uuid4(),
        profile_id=profile.id,
        request_fingerprint="fingerprint",
        status="failed",
        provider_message_id=None,
        provider_detail="provider rejected previous attempt",
        provider_handoff_started_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    async def ambiguous_retry(*_args, **_kwargs):
        claimed.provider_handoff_started_at = datetime.now(UTC)
        raise RuntimeError("transport outcome unknown")

    monkeypatch.setattr(offer_router, "_retry_failed_delivery", ambiguous_retry)
    monkeypatch.setattr(
        offer_router,
        "_load_delivery",
        AsyncMock(side_effect=[claimed, claimed]),
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(RuntimeError, match="outcome unknown"):
        await offer_router._resume_existing_delivery(
            db,
            profile=profile,
            delivery=claimed,
            request_fingerprint="fingerprint",
            user=SimpleNamespace(id=uuid4()),
        )

    assert claimed.status == "sending"
    assert claimed.provider_handoff_started_at is not None
    assert "provider acceptance" not in claimed.provider_detail


@pytest.mark.asyncio
async def test_transport_timeout_is_persisted_as_uncertain_not_failed() -> None:
    delivery = SimpleNamespace(
        status="sending",
        provider=None,
        provider_detail=None,
        message_send_id=None,
    )
    message = SimpleNamespace(
        provider="gmail",
        provider_error="gmail_send_failed: timed out waiting for response",
        message_send_id=uuid4(),
    )
    db = SimpleNamespace(commit=AsyncMock())

    assert offer_router._provider_outcome_is_uncertain(message)
    await offer_router._persist_uncertain_provider_outcome(
        db,
        delivery=delivery,
        message=message,
    )

    assert delivery.status == "sending"
    assert delivery.provider == "gmail"
    assert "outcome is uncertain" in delivery.provider_detail
    assert delivery.message_send_id == message.message_send_id
    db.commit.assert_awaited_once()


def test_definite_pre_transport_failure_remains_retryable() -> None:
    assert not offer_router._provider_outcome_is_uncertain(
        SimpleNamespace(provider_error="not_configured")
    )


@pytest.mark.asyncio
async def test_delivery_scoped_pin_can_view_uncertain_pdf_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    delivery_id = uuid4()
    item = SimpleNamespace(
        id=uuid4(),
        file_name="Terms.pdf",
        content_type="application/pdf",
        sha256=hashlib.sha256(b"locked-pdf").hexdigest(),
    )
    delivery = SimpleNamespace(
        id=delivery_id,
        status="sending",
        published_at=None,
        items=[item],
    )
    monkeypatch.setattr(
        offer_router,
        "_public_profile",
        AsyncMock(return_value=(profile, {delivery_id}, {delivery_id})),
    )
    monkeypatch.setattr(offer_router, "_load_delivery", AsyncMock(return_value=delivery))
    monkeypatch.setattr(offer_router.offers, "snapshot_bytes", AsyncMock(return_value=b"locked-pdf"))

    response = await offer_router.get_public_offer_document(
        "delivery-room-token",
        delivery_id,
        item.id,
        SimpleNamespace(passcode="112233"),
        SimpleNamespace(),
        "inline",
        SimpleNamespace(),
    )

    assert response.body == b"locked-pdf"
    assert response.media_type == "application/pdf"


def test_uncertain_delivery_read_exposes_pdf_but_no_decision_action() -> None:
    item = SimpleNamespace(
        id=uuid4(),
        item_key="production_term_sheet:source:v1",
        kind="production_term_sheet",
        label="Loan terms v1",
        title="Financing Terms",
        file_name="Terms.pdf",
        content_type="application/pdf",
        size_bytes=123,
        decision_status="pending",
        responded_at=None,
        responded_name=None,
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )

    read = offers.item_read(
        item,
        base_url="/api/v1/public/delivery/1",
        decisions_enabled=False,
    )

    assert read.preview_url
    assert read.download_url
    assert read.response_label is None


def test_gmail_message_contains_plain_html_and_pdf_snapshot() -> None:
    built = build_message(
        to="client@example.com",
        subject="Your offer",
        body="Plain locked terms: 12.99%",
        body_html="<p>HTML locked terms: <strong>12.99%</strong></p>",
        from_email="advisor@example.com",
        attachments=[
            {
                "filename": "Offer.pdf",
                "mime_type": "application/pdf",
                "data": b"immutable-pdf-bytes",
            }
        ],
        headers={
            "Message-ID": "<qc-offer-123@qualifiedcommercial.com>",
            "X-QC-Offer-Correlation": "qc-offer-123",
        },
    )
    raw = base64.urlsafe_b64decode(built.raw_base64)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = list(message.walk())

    assert any(
        part.get_content_type() == "text/plain" and "12.99%" in part.get_content() for part in parts
    )
    assert any(
        part.get_content_type() == "text/html" and "<strong>12.99%</strong>" in part.get_content()
        for part in parts
    )
    attachment = next(part for part in parts if part.get_filename() == "Offer.pdf")
    assert attachment.get_payload(decode=True) == b"immutable-pdf-bytes"
    assert message["Message-ID"] == "<qc-offer-123@qualifiedcommercial.com>"
    assert message["X-QC-Offer-Correlation"] == "qc-offer-123"


def test_document_disposition_safely_quotes_unicode_filename() -> None:
    header = offer_router._content_disposition('Client "final" résumé.pdf', "inline")

    assert 'filename="Client _final_ rsum.pdf"' in header
    assert "filename*=UTF-8''Client%20%22final%22%20r%C3%A9sum%C3%A9.pdf" in header
