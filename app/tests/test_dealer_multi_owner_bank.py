from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os.models import DealerOwner
from app.dealer_os.router import (
    _assert_owner_email_unique,
    _normalized_owner_email,
    grant_bank_consent,
    plaid_exchange,
    plaid_link_token,
    plaid_patch,
    plaid_remove,
)
from app.dealer_os.schemas import (
    BankConsentGrant,
    BulkCreditInviteResult,
    DealerCreate,
    PlaidExchange,
    PlaidItemPatch,
    PublicPlaidItemRead,
    RoomFeaturesRead,
    VerificationRead,
)
from app.dealer_os.services import plaid_client
from app.dealer_os.services.decision import assess_verification
from app.enums import Role


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


def test_new_application_requires_request_but_not_ein_address_or_trading_since() -> None:
    payload = DealerCreate(
        name="Optional Fields LLC",
        entity_type="Limited liability company",
        funding_goal=250_000,
        funding_purpose="working_capital",
        use_of_proceeds_note="Purchase equipment and retain operating liquidity.",
    )

    assert payload.ein is None
    assert payload.address is None


@pytest.mark.parametrize("field", ["funding_goal", "funding_purpose", "use_of_proceeds_note"])
def test_new_application_rejects_missing_required_request_fields(field: str) -> None:
    data = {
        "name": "Incomplete LLC",
        "entity_type": "Limited liability company",
        "funding_goal": 250_000,
        "funding_purpose": "working_capital",
        "use_of_proceeds_note": "Working capital for payroll and inventory.",
    }
    data.pop(field)
    with pytest.raises(ValidationError):
        DealerCreate.model_validate(data)


def test_plaid_invalid_environment_never_falls_back_to_sandbox(monkeypatch) -> None:
    monkeypatch.setenv("DEALER_OS_PLAID_ENV", "prodution")
    with pytest.raises(plaid_client.PlaidUnavailable):
        plaid_client.environment()


def test_credit_requirement_uses_inclusive_twenty_percent_threshold() -> None:
    below = DealerOwner(first_name="Below", last_name="Threshold", ownership_pct=19.99)
    exact = DealerOwner(first_name="Exact", last_name="Threshold", ownership_pct=20)

    assert below.credit_required is False
    assert exact.credit_required is True


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
        owner_contact_complete=False,
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
        owner_contact_complete=True,
        required_credit_owner_count=2,
        completed_credit_owner_count=2,
    )
    assert complete.unlocked is True
    assert complete.stage == "underwriting"


def test_verification_blocks_missing_required_owner_contact() -> None:
    owner_id = uuid4()
    result = assess_verification(
        bank_linked=True,
        credit_returned=False,
        ownership_total=100,
        ownership_complete=True,
        owner_contact_complete=False,
        missing_credit_contact_owner_ids=[owner_id],
        required_credit_owner_count=1,
        completed_credit_owner_count=1,
    )

    assert result.unlocked is False
    assert result.owner_contact_complete is False
    assert result.missing_credit_contact_owner_ids == [owner_id]
    assert "email and phone" in result.reason


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
            last_pulled_at=datetime.now(UTC),
            statement_months=[],
        )
    )
    assert second_room.bank_connections == []
    assert BulkCreditInviteResult().items == []


def _user(role: Role):
    return SimpleNamespace(id=uuid4(), role=role, name="Test User")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP])
async def test_non_clients_cannot_start_authenticated_plaid(role: Role) -> None:
    user = _user(role)
    dealer_id = uuid4()

    with pytest.raises(HTTPException) as link_error:
        await plaid_link_token(dealer_id, user, None)
    assert link_error.value.status_code == 403

    with pytest.raises(HTTPException) as consent_error:
        await grant_bank_consent(
            dealer_id,
            BankConsentGrant(consenter_name="Client Owner"),
            SimpleNamespace(client=None, headers={}),
            user,
            None,
        )
    assert consent_error.value.status_code == 403

    with pytest.raises(HTTPException) as exchange_error:
        await plaid_exchange(
            dealer_id,
            PlaidExchange(public_token="public-token"),
            SimpleNamespace(add_task=lambda *args: None),
            user,
            None,
        )
    assert exchange_error.value.status_code == 403


@pytest.mark.asyncio
async def test_staff_cannot_choose_primary_and_only_super_admin_can_disconnect() -> None:
    dealer_id = uuid4()
    item_id = uuid4()

    with pytest.raises(HTTPException) as primary_error:
        await plaid_patch(
            dealer_id,
            item_id,
            PlaidItemPatch(is_primary_operating=True),
            _user(Role.LOAN_EXEC),
            None,
        )
    assert primary_error.value.status_code == 403

    with pytest.raises(HTTPException) as disconnect_error:
        await plaid_remove(dealer_id, item_id, _user(Role.LOAN_EXEC), None)
    assert disconnect_error.value.status_code == 403


def test_bank_consent_requires_client_identity() -> None:
    with pytest.raises(ValidationError):
        BankConsentGrant(consenter_name="")
