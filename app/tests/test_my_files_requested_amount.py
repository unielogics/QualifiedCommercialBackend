"""Borrower loan rows expose a current request without changing loan terms."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import LoanStage, LoanType, Role
from app.routers import me


def result(rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [LoanStage.COLLECTING_DOCS, LoanStage.FUNDED])
async def test_borrower_table_current_request_preserves_original_and_funded_amount(stage):
    client_id, intake_id, loan_id = uuid4(), uuid4(), uuid4()
    loan = SimpleNamespace(
        id=loan_id, client_id=client_id, source_intake_id=intake_id, source_deal_id=None,
        stage=stage, deal_id="L-1234", address="Test address", city=None, type=LoanType.DSCR,
        amount=70_000, living_profile=None, status_summary=None, updated_at=datetime.now(UTC),
    )
    profiles = [
        SimpleNamespace(id=uuid4(), loan_id=None, intake_id=intake_id, deal_id=None,
                        current_funding_request={"requested_amount": "300000.00", "revision": 1}),
        SimpleNamespace(id=uuid4(), loan_id=loan_id, intake_id=None, deal_id=None,
                        current_funding_request={"requested_amount": "200000.00", "revision": 1}),
    ]
    db = SimpleNamespace(execute=AsyncMock(side_effect=[result([loan]), result(profiles), result([])]))
    rows = await me.my_files(SimpleNamespace(role=Role.CLIENT, client=SimpleNamespace(id=client_id)), db)
    assert len(rows) == 1
    assert rows[0].requested_amount == 200_000
    assert rows[0].requested_amount_source == "profile.current_funding_request"
    assert rows[0].amount == loan.amount == 70_000
    assert rows[0].status == ("funded" if stage == LoanStage.FUNDED else "in_funding")


@pytest.mark.asyncio
async def test_borrower_without_loans_does_not_query_other_application_profiles():
    db = SimpleNamespace(execute=AsyncMock(side_effect=[result([]), result([])]))
    rows = await me.my_files(SimpleNamespace(role=Role.CLIENT, client=SimpleNamespace(id=uuid4())), db)
    assert rows == []
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_non_borrower_never_queries_profile_or_loan_rows():
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await me.my_files(SimpleNamespace(role=Role.LOAN_EXEC, client=None), db)
    assert error.value.status_code == 403
    db.execute.assert_not_awaited()
