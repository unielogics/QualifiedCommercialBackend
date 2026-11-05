"""Accepted debt schedules clear collection and outrank unreviewed updates."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import financial_version_selection as selection
from app.services import financial_workspace as ws
from app.services.dealer_forms_pdf import render_debt_schedule_pdf


class Rows:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self.scalar


def body(**values):
    return {"debts": [{"id": str(uuid4()), "lender": "Kapfi", "balance": "40000", "monthly_payment": "1000", **values}]}


def version(*, body=None, status="published", source="form", **values):
    return SimpleNamespace(id=uuid4(), family_id=uuid4(), source=source, status=status,
                           body=body if body is not None else globals()["body"](),
                           pdf_file_id=uuid4(), source_file_id=None, version_number=2, **values)


def test_completion_requires_only_named_lender_balance_and_known_monthly():
    schedule = body(balance="0", monthly_payment="0", rate=None, factor_rate=None, debt_type=None)
    assert selection.debt_schedule_completion(schedule)["complete"]
    row_id = schedule["debts"][0]["id"]
    schedule["debts"][0]["monthly_payment"] = None
    result = selection.debt_schedule_completion(schedule)
    assert not result["complete"] and result["missing_fields"] == [f"{row_id}.monthly_payment"]
    schedule["debts"][0].update(payment_amount="0", payment_frequency="daily")
    assert selection.debt_schedule_completion(schedule)["complete"]


def test_an_untouched_empty_schedule_is_unknown_but_explicit_no_debt_is_complete_zero():
    empty = ws.empty_body("debt_schedule")
    assert not selection.debt_schedule_completion(empty)["complete"]
    assert ws.facts_for_body("debt_schedule", empty)["total_monthly_debt_service"] is None
    empty["no_debt"] = True
    assert selection.debt_schedule_completion(empty)["complete"]
    facts = ws.facts_for_body("debt_schedule", empty)
    assert facts["total_outstanding_balance"] == facts["total_monthly_debt_service"] == 0
    assert facts["no_debt"] and facts["debt_schedule_complete"]


def test_debt_free_pdf_records_explicit_declaration():
    renderer = Mock(return_value=SimpleNamespace(write_pdf=lambda: b"%PDF declaration"))
    with patch.dict("sys.modules", {"weasyprint": SimpleNamespace(HTML=renderer)}):
        render_debt_schedule_pdf(business_name="Little Star Therapy", rows=[], total_balance=0,
                                 total_monthly=0, no_debt=True)
    assert "declares that it has no outstanding business debt" in renderer.call_args.kwargs["string"]


@pytest.mark.parametrize("declaration", ["true", 1, None])
def test_no_debt_declaration_requires_boolean(declaration):
    with pytest.raises(HTTPException):
        ws.validate_body("debt_schedule", {"debts": [], "no_debt": declaration})


def test_no_debt_cannot_contradict_active_rows_but_ignores_excluded_history():
    schedule = {**body(), "no_debt": True}
    with pytest.raises(HTTPException):
        ws.validate_body("debt_schedule", schedule)
    schedule["debts"][0]["status"] = "dismissed"
    assert selection.debt_schedule_completion(ws.validate_body("debt_schedule", schedule))["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,source,schedule,accepted", [
    ("published", "form", body(), True),
    ("filed", "form", {"debts": [], "no_debt": True}, True),
    ("original", "original", {}, False),
    ("published", "form", body(monthly_payment=None), False),
    ("draft", "form", body(), False),
])
async def test_authority_uses_current_complete_immutable_body(status, source, schedule, accepted):
    saved = version(body=schedule, status=status, source=source)
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([saved])))
    result = await selection.accepted_debt_schedule_snapshot(db, uuid4())
    assert (result is saved) is accepted
    query = db.execute.await_args.args[0]
    assert "current_version_id" in str(query) and "debt_schedule" in query.compile().params.values()


@pytest.mark.asyncio
async def test_newer_incomplete_separate_period_does_not_remove_accepted_authority():
    incomplete, complete = version(body=body(monthly_payment=None)), version()
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([incomplete, complete])))
    assert await selection.accepted_debt_schedule_snapshot(db, uuid4()) is complete


def test_shared_monthly_preserves_two_same_lender_loans_and_explicit_zero():
    rows = body()["debts"] + body(monthly_payment="0")["debts"] + body(monthly_payment="500")["debts"]
    rows[1]["count_in_dscr"] = False
    schedule = {"debts": rows}
    assert len(selection.active_debt_schedule_rows(schedule)) == 3
    assert selection.debt_schedule_monthly(schedule) == 1500
    assert selection.debt_schedule_monthly(schedule, exclude_debt_ids={rows[0]["id"]}) == 500
    rows[2]["monthly_payment"] = None
    assert selection.debt_schedule_monthly(schedule) is None


@pytest.mark.asyncio
async def test_dealer_authority_requires_unambiguous_profile_scope():
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([uuid4(), uuid4()])))
    assert not await selection.accepted_debt_schedule_authority_for_dealer(db, uuid4())
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_old_complete_published_schedule_is_coverage_without_mutating_or_resaving():
    accepted = version()
    original = version(body={}, status="original", source="original")
    original.family_id, original.version_number = accepted.family_id, 1
    original.source_file_id = original.pdf_file_id
    pending = version(body={}, status="original", source="original")
    pending.family_id, pending.version_number = accepted.family_id, 3
    pending.source_file_id = pending.pdf_file_id
    unrelated = uuid4()
    db = SimpleNamespace(execute=AsyncMock(return_value=Rows([
        (original, accepted.id, "debt_schedule"), (accepted, accepted.id, "debt_schedule"),
        (pending, accepted.id, "debt_schedule"),
    ])))
    result = await selection.financial_coverage_file_ids(db, {original.pdf_file_id, accepted.pdf_file_id, pending.pdf_file_id, unrelated})
    assert result == {accepted.pdf_file_id, unrelated}
    assert accepted.status == "published" and pending.status == "original"


@pytest.mark.asyncio
async def test_incomplete_saved_debt_and_unfiled_other_forms_do_not_satisfy_coverage():
    debt = version(body=body(monthly_payment=None))
    pfs = version()
    db = SimpleNamespace(execute=AsyncMock(return_value=Rows([
        (debt, debt.id, "debt_schedule"), (pfs, pfs.id, "pfs"),
    ])))
    assert await selection.financial_coverage_file_ids(db, {debt.pdf_file_id, pfs.pdf_file_id}) == set()
