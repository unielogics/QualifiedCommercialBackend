from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.router import _assert_owner_email_unique, _normalized_owner_email
from app.dealer_os.schemas import (
    BulkCreditInviteResult,
    PublicPlaidItemRead,
    RoomFeaturesRead,
    VerificationRead,
)
from app.dealer_os.services.decision import assess_verification


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, existing_owner_id=None):
        self.existing_owner_id = existing_owner_id
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.existing_owner_id)


def test_owner_email_normalization_is_stable() -> None:
    assert _normalized_owner_email(" Owner@Example.COM ") == "owner@example.com"
    assert _normalized_owner_email("   ") is None
    assert _normalized_owner_email(None) is None


@pytest.mark.asyncio
async def test_duplicate_owner_email_is_rejected() -> None:
    db = _Db(existing_owner_id=uuid4())

    with pytest.raises(HTTPException) as exc:
        await _assert_owner_email_unique(db, uuid4(), "owner@example.com")

    assert exc.value.status_code == 422
    assert "different email" in str(exc.value.detail)


def test_verification_requires_complete_ownership_and_all_required_credit() -> None:
    pending_id = uuid4()
    incomplete = assess_verification(
        bank_linked=True,
        credit_returned=False,
        ownership_total=80,
        ownership_complete=False,
        required_credit_owner_count=2,
        completed_credit_owner_count=1,
        pending_credit_owner_ids=[pending_id],
    )
    assert incomplete.unlocked is False
    assert "80.00%" in incomplete.reason
    assert incomplete.pending_credit_owner_ids == [pending_id]

    complete = assess_verification(
        bank_linked=True,
        credit_returned=True,
        ownership_total=100,
        ownership_complete=True,
        required_credit_owner_count=2,
        completed_credit_owner_count=2,
    )
    assert complete.unlocked is True
    assert complete.stage == "underwriting"


def test_new_list_defaults_are_isolated() -> None:
    first = VerificationRead()
    second = VerificationRead()
    first.pending_credit_owner_ids.append(uuid4())
    assert second.pending_credit_owner_ids == []

    first_room = RoomFeaturesRead(business_name="First Dealer")
    second_room = RoomFeaturesRead(business_name="Second Dealer")
    first_room.bank_connections.append(
        PublicPlaidItemRead(
            id=uuid4(),
            institution_name="Test Bank",
            accounts_label=None,
            status="active",
            is_primary_operating=True,
            last_pulled_at=datetime.now(timezone.utc),
            statement_months=[],
        )
    )
    assert second_room.bank_connections == []
    assert BulkCreditInviteResult().items == []
