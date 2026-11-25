"""Selected observations stay inside fresh, authenticated staff assistant turns."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.enums import Role
from app.services import application_programs, bucket_ai, internal_note_ai


def _note(content, *, selected=True, channel="internal"):
    return SimpleNamespace(
        id=uuid4(), author_name="Staff member", author_role=Role.LOAN_EXEC,
        content=content, created_at=datetime(2026, 1, 1, tzinfo=UTC),
        visibility="admin", channel=channel, share_id=None, vendor_access_id=None,
        include_in_ai_context=selected,
    )


def _bucket():
    return SimpleNamespace(
        id=uuid4(), name="Example file", client_name="Example company", purpose="Review",
        bucket_type="underwriting", ai_context={}, requested_documents=[], files=[],
        notes=[_note("SELECTED_PRIVATE_OBSERVATION"),
               _note("UNSELECTED_PRIVATE_OBSERVATION", selected=False),
               _note("EXISTING_PARTNER_DISCUSSION", channel="partner")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("audience,role,author_role", [
    ("admin", Role.SUPER_ADMIN, Role.LOAN_EXEC),
    ("admin", Role.LOAN_EXEC, Role.SUPER_ADMIN),
    ("admin", Role.BROKER, Role.LOAN_EXEC),
    ("admin", Role.REGIONAL_MANAGER, Role.LOAN_EXEC),
    ("admin", Role.DEALER_PARTNER, Role.LOAN_EXEC),
    ("admin", Role.PROFESSIONAL_REFERRAL_PARTNER, Role.LOAN_EXEC),
    ("admin", None, Role.LOAN_EXEC),
    ("uploader", Role.SUPER_ADMIN, Role.LOAN_EXEC),
    ("share", Role.SUPER_ADMIN, Role.LOAN_EXEC),
    ("vendor", Role.SUPER_ADMIN, Role.LOAN_EXEC),
    ("admin", Role.SUPER_ADMIN, Role.BROKER),
    ("admin", Role.LOAN_EXEC, Role.REGIONAL_MANAGER),
])
async def test_real_context_builder_requires_staff_identity_and_explicit_note_selection(
    monkeypatch, audience, role, author_role,
):
    bucket = _bucket()
    bucket.notes[0].author_role = author_role
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=AsyncMock(return_value=empty))
    monkeypatch.setattr(application_programs, "profile_for_chat_scope", AsyncMock(return_value=None))
    monkeypatch.setattr(bucket_ai, "_program_context_for_chat", AsyncMock(return_value=None))
    monkeypatch.setattr(bucket_ai, "latest_review", AsyncMock(return_value=None))
    monkeypatch.setattr(bucket_ai, "visible_action_items", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.financial_workspace.filter_current_files", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.financial_publication.current_version_context", AsyncMock(return_value={}))
    share = SimpleNamespace(id=uuid4(), recipient_name="Recipient", files=[],
                            can_see_internal_notes=True, can_view_ai_summary=False)
    vendor = SimpleNamespace(id=uuid4(), vendor=None, file_scope="all_active", files=[],
                             can_see_internal_notes=True, can_view_ai_summary=False)
    data = await bucket_ai._chat_context(
        db, bucket=bucket, audience=audience,
        upload_link=SimpleNamespace(id=uuid4(), recipient_name="Applicant") if audience == "uploader" else None,
        share=share if audience == "share" else None,
        vendor_access=vendor if audience == "vendor" else None,
        user=SimpleNamespace(role=role) if role else None,
    )
    encoded = json.dumps(data)
    staff_admin = audience == "admin" and role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}
    eligible_author = author_role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}
    assert ("SELECTED_PRIVATE_OBSERVATION" in encoded) is (staff_admin and eligible_author)
    assert "UNSELECTED_PRIVATE_OBSERVATION" not in encoded
    assert "SELECTED_PRIVATE_OBSERVATION" not in json.dumps(data.get("notes", []))
    assert ("internal_note_context" in data) is staff_admin
    if audience in {"admin", "share", "vendor"}:
        assert "EXISTING_PARTNER_DISCUSSION" in encoded


@pytest.mark.asyncio
async def test_selected_context_cannot_persist_facts_patches_or_shared_tasks(monkeypatch):
    bucket = _bucket()
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    packet = internal_note_ai.context(bucket, user, "admin")
    context = AsyncMock(return_value={"internal_note_context": packet})
    monkeypatch.setattr(bucket_ai, "_chat_context", context)
    current = SimpleNamespace(role="assistant", author_name="AI", content="CURRENT_SAFE_HISTORY",
                              metadata_json={"internal_note_context_fingerprint": packet["fingerprint"]})
    stale = SimpleNamespace(role="assistant", author_name="AI", content="STALE_PRIVATE_HISTORY",
                            metadata_json={"internal_note_context_fingerprint": "old-private-state"})
    legacy = SimpleNamespace(role="assistant", author_name="AI", content="UNSTAMPED_PRIVATE_HISTORY", metadata_json=None)
    monkeypatch.setattr(bucket_ai, "_recent_thread_messages", AsyncMock(return_value=[legacy, stale, current]))
    provider = AsyncMock(return_value=SimpleNamespace(model="example", content=[SimpleNamespace(
        type="text", text=json.dumps({
            "answer": "Discuss this unverified observation internally.",
            "proposed_context_patch": {"annual_revenue": 900000},
            "proposed_borrower_facts": {"annual_revenue": 900000},
            "proposed_action_items": [
                {"title": "Private observation", "instructions": "Private details", "route": route}
                for route in ["admin", "uploader", "share", "vendor"]
            ],
        }),
    )]))
    monkeypatch.setattr(bucket_ai, "tracked_messages_create", provider)
    monkeypatch.setattr(bucket_ai, "get_client", Mock(return_value=None))
    monkeypatch.setattr(bucket_ai, "model_light", Mock(return_value="example"))
    monkeypatch.setattr(bucket_ai, "is_admin_lookup", Mock(return_value=False))
    proposals = AsyncMock(return_value=[])
    monkeypatch.setattr(bucket_ai, "_create_proposals", proposals)
    monkeypatch.setattr(bucket_ai, "log_bucket_ai_activity", AsyncMock())
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())

    messages, _, _ = await bucket_ai.create_chat_reply(
        db, bucket=bucket, audience="admin", message="Explain the questions to discuss.",
        actor_name="Staff member", user=user,
    )

    assert context.await_args.kwargs["user"] is user
    provider_input = json.dumps(provider.await_args.kwargs["messages"])
    assert "SELECTED_PRIVATE_OBSERVATION" in provider_input
    assert "CURRENT_SAFE_HISTORY" in provider_input
    assert "STALE_PRIVATE_HISTORY" not in provider_input
    assert "UNSTAMPED_PRIVATE_HISTORY" not in provider_input
    assert internal_note_ai.NOTE_RULE in provider.await_args.kwargs["system"]
    assistant = messages[-1]
    assert assistant.proposed_context_patch is None
    assert assistant.metadata_json["raw"]["proposed_borrower_facts"] is None
    assert proposals.await_args.kwargs["parsed"]["proposed_action_items"] == []
    assert all(row.metadata_json["internal_note_context_fingerprint"] == packet["fingerprint"] for row in messages)


def test_shared_review_and_single_document_prompts_have_no_staff_note_injection():
    # The public synthesis feeds production PDFs and client summaries. It has
    # no private observation input; assistant context is a separate consumer.
    import inspect

    source = inspect.getsource(bucket_ai.run_bucket_ai_review)
    assert "internal_note_context" not in source
    assert "bucket.notes" not in source

