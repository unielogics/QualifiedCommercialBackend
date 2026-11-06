import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import loans


class _Read:
    def __init__(self, value):
        self.payload = value if isinstance(value, dict) else vars(value)

    @classmethod
    def model_validate(cls, value):
        return cls(value)

    def model_dump(self):
        return dict(self.payload)


def _result(value):
    return SimpleNamespace(
        scalar_one_or_none=lambda: value,
        scalars=lambda: SimpleNamespace(all=lambda: value),
    )


def test_inaccessible_loan_never_looks_up_profile():
    db = SimpleNamespace(execute=AsyncMock(return_value=_result(None)))
    with patch.object(loans, "_scope_query", lambda user, query: query):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(loans.get_loan(uuid4(), SimpleNamespace(), db))
    assert caught.value.status_code == 404
    assert db.execute.await_count == 1


def test_loan_read_exposes_only_an_existing_profile_and_never_provisions():
    loan = SimpleNamespace(id=uuid4(), source_intake_id=uuid4(), source_deal_id=None, amount=70_000)
    profile_id = uuid4()
    profile = SimpleNamespace(id=profile_id, current_funding_request={"requested_amount": "200000.00", "revision": 1})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_result(loan), _result(profile)]))
    with patch.object(loans, "_scope_query", lambda user, query: query), patch.object(loans, "LoanRead", _Read):
        result = asyncio.run(loans.get_loan(loan.id, SimpleNamespace(), db))
    assert result.payload["application_profile_id"] == profile_id
    assert result.payload["requested_amount"] == 200_000
    assert result.payload["requested_amount_source"] == "profile.current_funding_request"
    assert result.payload["amount"] == loan.amount == 70_000
    assert db.execute.await_count == 2


def test_list_mapping_prefers_direct_loan_identity_over_source_links():
    loan = SimpleNamespace(id=uuid4(), source_intake_id=uuid4(), source_deal_id=None, broker=None, client=None, amount=70_000)
    direct_id, source_id = uuid4(), uuid4()
    profiles = [
        SimpleNamespace(id=source_id, loan_id=None, intake_id=loan.source_intake_id, deal_id=None,
                        current_funding_request={"requested_amount": "300000.00", "revision": 1}),
        SimpleNamespace(id=direct_id, loan_id=loan.id, intake_id=None, deal_id=None,
                        current_funding_request={"requested_amount": "200000.00", "revision": 1}),
    ]
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_result([loan]), _result(profiles)]))
    with patch.object(loans, "_scope_query", lambda user, query: query), patch.object(loans, "LoanRead", _Read):
        result = asyncio.run(loans.list_loans(SimpleNamespace(), db))
    assert result[0].payload["application_profile_id"] == direct_id
    assert result[0].payload["requested_amount"] == 200_000
    assert result[0].payload["amount"] == loan.amount == 70_000


@pytest.mark.parametrize("profile", [None, SimpleNamespace(id=uuid4(), current_funding_request={})])
def test_loan_without_corrected_request_preserves_legacy_amount_and_does_not_invent_request(profile):
    loan = SimpleNamespace(id=uuid4(), source_intake_id=None, source_deal_id=None, amount=150_000)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_result(loan), _result(profile)]))
    with patch.object(loans, "_scope_query", lambda user, query: query), patch.object(loans, "LoanRead", _Read):
        result = asyncio.run(loans.get_loan(loan.id, SimpleNamespace(), db))
    assert result.payload["requested_amount"] is None
    assert result.payload["requested_amount_source"] is None
    assert result.payload["amount"] == loan.amount == 150_000
