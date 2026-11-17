from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import resource_nurture as nurture


def capture(**overrides):
    return SimpleNamespace(id=uuid4(), client_id=uuid4(), goal="merchant_cash_advance", locale="en",
                           contact={"email": "owner@example.com"}, email_marketing_consent=True,
                           application_started_at=None, created_at=datetime(2026, 11, 1, tzinfo=UTC),
                           token_expires_at=datetime(2026, 11, 15, tzinfo=UTC), **overrides)


def fake_db():
    claims = {}
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock(), execute=AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    db.add = lambda row: claims.update({row.id: row})
    db.get = AsyncMock(side_effect=lambda model, identifier: claims.get(identifier))
    return db


def test_only_explicit_opt_in_and_unconverted_unexpired_leads_are_eligible():
    row = capture()
    now = row.created_at
    assert nurture.eligible(row, now)
    row.email_marketing_consent = False
    assert not nurture.eligible(row, now)
    row.email_marketing_consent = True
    row.application_started_at = now
    assert not nurture.eligible(row, now)
    row.application_started_at = None
    assert not nurture.eligible(row, row.token_expires_at)


def test_unsubscribe_capability_cannot_be_used_as_report_capability():
    row = capture()
    with patch.object(nurture.resource_assessments, "_secret", return_value=b"test-secret"):
        token = nurture.mint_unsubscribe_token(row)
        assert nurture.unsubscribe_id(token) == row.id
        resource_token = nurture.resource_assessments.token_for(row.id)
        with pytest.raises(HTTPException):
            nurture.unsubscribe_id(resource_token)
        with pytest.raises(HTTPException):
            nurture.unsubscribe_id(token + "tampered")


def test_templates_have_scoped_result_link_and_both_unsubscribe_headers():
    row = capture()
    settings = SimpleNamespace(public_api_url="https://api.example.com", prospect_mailing_address="14 53rd St #408N, Brooklyn, NY 11232",
                               resource_marketing_url="https://qualifiedcommercial.com")
    with patch.object(nurture, "get_settings", return_value=settings), patch.object(nurture.resource_assessments, "_secret", return_value=b"secret"):
        draft = nurture.compose_email(row, 2)
    assert "/business-funding-check#resource=" in draft.body_text
    assert "six complete months" in draft.body_text
    assert draft.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert len(draft.secrets) == 2
    row.locale = "es"
    with patch.object(nurture, "get_settings", return_value=settings), patch.object(nurture.resource_assessments, "_secret", return_value=b"secret"):
        draft = nurture.compose_email(row, 0)
    assert "/es/business-funding-check#resource=" in draft.body_text
    assert "Cancelar suscripción" in draft.body_text


def test_follow_up_explains_the_selected_product_and_its_own_documents():
    row = capture()
    _, fast = nurture.email_content(row, 1)
    assert "factor" in fast and "APR" in fast
    row.goal = "term_loan"
    _, term = nurture.email_content(row, 1)
    assert "coverage buffer" in term and "MCA" not in term
    row.goal = "mca_refinance"
    _, refi = nurture.email_content(row, 2)
    assert "payoff letters" in refi and "six complete months" in refi


@pytest.mark.asyncio
async def test_scheduler_repetition_claims_once_and_staggers_delayed_steps():
    row, db = capture(), fake_db()
    sent = AsyncMock(return_value=SimpleNamespace(ok=True, row=SimpleNamespace(id=uuid4())))
    with patch.object(nurture, "is_suppressed", new=AsyncMock(return_value=None)), \
         patch.object(nurture, "compose_email", return_value=SimpleNamespace()), \
         patch.object(nurture.outbox, "deliver_email", new=sent):
        assert await nurture.dispatch_one(db, row, now=row.created_at + timedelta(days=2))
        assert not await nurture.dispatch_one(db, row, now=row.created_at + timedelta(days=2))
        assert await nurture.dispatch_one(db, row, now=row.created_at + timedelta(days=3))
        assert not await nurture.dispatch_one(db, row, now=row.created_at + timedelta(days=4))
    assert sent.await_count == 2
    assert sent.call_args.kwargs["durable_handoff"] is True


@pytest.mark.asyncio
async def test_opt_out_suppression_and_application_start_prevent_send():
    row, db = capture(), fake_db()
    with patch.object(nurture, "is_suppressed", new=AsyncMock(return_value=object())), \
         patch.object(nurture.outbox, "deliver_email", new=AsyncMock()) as send:
        assert not await nurture.dispatch_one(db, row, now=row.created_at)
        send.assert_not_awaited()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: uuid4()))
    with patch.object(nurture, "is_suppressed", new=AsyncMock(return_value=None)), \
         patch.object(nurture.outbox, "deliver_email", new=AsyncMock()) as send:
        assert not await nurture.dispatch_one(db, row, now=row.created_at)
        assert row.application_started_at == row.created_at
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_delivery_is_not_automatically_retried():
    row, db = capture(), fake_db()
    with patch.object(nurture, "is_suppressed", new=AsyncMock(return_value=None)), \
         patch.object(nurture, "compose_email", return_value=SimpleNamespace()), \
         patch.object(nurture.outbox, "deliver_email", new=AsyncMock(return_value=SimpleNamespace(ok=False, row=None))) as send:
        assert not await nurture.dispatch_one(db, row, now=row.created_at)
        assert not await nurture.dispatch_one(db, row, now=row.created_at)
        send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_email_provider_does_not_touch_capture_or_block_report():
    with patch.object(nurture, "get_settings", return_value=SimpleNamespace(resource_nurture_enabled=True)), \
         patch.object(nurture, "ses_configured", return_value=False):
        assert await nurture.drain_pending() == {"checked": 0, "sent": 0}


@pytest.mark.asyncio
async def test_four_steps_advance_due_cursor_and_complete_without_starving_other_leads():
    row, db = capture(), fake_db()
    with patch.object(nurture, "is_suppressed", new=AsyncMock(return_value=None)), \
         patch.object(nurture, "compose_email", return_value=SimpleNamespace()), \
         patch.object(nurture.outbox, "deliver_email", new=AsyncMock(return_value=SimpleNamespace(ok=True, row=None))) as send:
        for step, offset in enumerate(nurture.OFFSETS):
            assert await nurture.dispatch_one(db, row, now=row.created_at + timedelta(days=offset))
            expected = row.created_at + timedelta(days=nurture.OFFSETS[step + 1]) if step < 3 else None
            assert row.nurture_next_at == expected
        assert not await nurture.dispatch_one(db, row, now=row.created_at + timedelta(days=8))
        assert row.nurture_next_at is None
        assert send.await_count == 4
