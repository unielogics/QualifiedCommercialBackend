from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import debt_payee_links as links


def setup_row():
    profile = SimpleNamespace(id=uuid4(), dealer_id=uuid4())
    row = SimpleNamespace(id=uuid4(), profile_id=profile.id, dealer_id=profile.dealer_id,
                          lender="Oakwood", status="active", duplicate_of_id=None,
                          vendor_key=None, monthly_payment=Decimal("32453.35"), balance=Decimal("80000"),
                          count_in_dscr=True, evidence={"contract": "retained"})
    result = SimpleNamespace(scalar_one_or_none=lambda: row, scalars=lambda: SimpleNamespace(all=lambda: [row]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock())
    return db, profile, row


@pytest.mark.asyncio
async def test_linking_bank_payee_changes_only_evidence_and_preserves_financial_figures():
    db, profile, row = setup_row()
    actor = SimpleNamespace(id=uuid4())
    before = (row.monthly_payment, row.balance, row.count_in_dscr)
    version = SimpleNamespace(body={"debts": [{"id": str(row.id), "lender": row.lender}]})
    saved_body = deepcopy(version.body)
    with patch.object(links, "accepted_debt_schedule_snapshot", AsyncMock(return_value=version)), \
         patch.object(links, "_payees", AsyncMock(return_value=[SimpleNamespace(key="OAKWOOD ACH")])), \
         patch.object(links, "inventory", AsyncMock(return_value={"schedule_complete": True})):
        await links.link_payee(db, profile, row.id, "OAKWOOD ACH", actor=actor)
    assert row.vendor_key == "OAKWOOD ACH"
    assert row.evidence["contract"] == "retained"
    assert row.evidence["payee_link"]["linked_by_user_id"] == str(actor.id)
    assert (row.monthly_payment, row.balance, row.count_in_dscr) == before
    assert version.body == saved_body


@pytest.mark.asyncio
async def test_unlinking_a_missing_bank_payee_does_not_require_it_to_reappear_in_statements():
    db, profile, row = setup_row()
    row.vendor_key = "OLD PAYEE"
    with patch.object(links, "accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(links, "_payees", AsyncMock()) as payees, \
         patch.object(links, "inventory", AsyncMock(return_value={})):
        await links.link_payee(db, profile, row.id, None)
    payees.assert_not_awaited()
    assert row.vendor_key is None
    assert row.evidence["payee_link"]["vendor_key"] is None


@pytest.mark.asyncio
async def test_even_same_dealer_does_not_allow_another_profiles_owned_obligation():
    db, profile, row = setup_row()
    row.profile_id = uuid4()
    with pytest.raises(HTTPException) as error:
        await links.link_payee(db, profile, row.id, "KNOWN PAYEE")
    assert error.value.status_code == 404
    assert row.vendor_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,duplicate", [("dismissed", False), ("active", True)])
async def test_excluded_obligations_cannot_be_linked_as_active_loans(status, duplicate):
    db, profile, row = setup_row()
    row.status = status
    row.duplicate_of_id = uuid4() if duplicate else None
    with pytest.raises(HTTPException) as error:
        await links.link_payee(db, profile, row.id, "KNOWN PAYEE")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_unobserved_payee_cannot_be_submitted_by_guessing_its_key():
    db, profile, row = setup_row()
    with patch.object(links, "accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(links, "_payees", AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as error:
            await links.link_payee(db, profile, row.id, "ANOTHER CLIENTS PAYEE")
    assert error.value.status_code == 422
    assert row.vendor_key is None


@pytest.mark.asyncio
async def test_completed_no_debt_schedule_does_not_offer_extra_automated_debts_for_matching():
    db, profile, row = setup_row()
    version = SimpleNamespace(body={"no_debt": True, "debts": []})
    with patch.object(links, "accepted_debt_schedule_snapshot", AsyncMock(return_value=version)), \
         patch.object(links, "_payees", AsyncMock(return_value=[])):
        result = await links.inventory(db, profile)
        with pytest.raises(HTTPException) as error:
            await links.link_payee(db, profile, row.id, None)
    assert result["schedule_complete"] is True and result["obligations"] == []
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_banking_uses_accepted_terms_even_if_an_older_projection_was_replaced():
    db, profile, row = setup_row()
    row.status = "dismissed"
    row.monthly_payment = Decimal("9999")
    accepted = SimpleNamespace(body={"debts": [{"id": str(row.id), "lender": row.lender,
                                              "balance": "1000", "monthly_payment": "100"}]})
    payee = SimpleNamespace(key="OAKWOOD ACH", sample_description="Oakwood ACH", category="loan",
                            monthly_average=-9999, months=2, count=4)
    with patch.object(links, "accepted_debt_schedule_snapshot", AsyncMock(return_value=accepted)), \
         patch.object(links, "_payees", AsyncMock(return_value=[payee])):
        result = await links.link_payee(db, profile, row.id, payee.key)
    assert len(result["obligations"]) == 1
    assert Decimal(result["obligations"][0]["monthly_payment"]) == 100
    assert result["obligations"][0]["vendor_key"] == payee.key
    assert row.monthly_payment == 9999 and row.status == "dismissed"


@pytest.mark.asyncio
async def test_bank_report_reuses_canonical_rollup_and_only_offers_outgoing_payees():
    db, profile, row = setup_row()
    dealer = SimpleNamespace(id=profile.dealer_id)
    db.get = AsyncMock(return_value=dealer)
    incoming = SimpleNamespace(key="CLIENT RECEIPTS", direction=1)
    outgoing = SimpleNamespace(key="OAKWOOD ACH", direction=-1)
    with patch("app.dealer_os.router._vendor_rollup", AsyncMock(return_value=([incoming, outgoing], 9))) as rollup:
        assert await links._payees(db, profile) == [outgoing]
    rollup.assert_awaited_once_with(db, dealer)
