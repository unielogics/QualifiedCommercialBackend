"""The desk records an advance from the client's words and the save touches
only the desk's own rows: the client's typed terms and the AI's draft stay."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.dealer_os.models import DealerDebt
from app.services import mca_advances


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    async def execute(self, _stmt):
        # The real query orders by lender.
        return _Result(sorted((r for r in self.rows if r.status == "active"), key=lambda r: r.lender.casefold()))

    def add(self, row):
        self.rows.append(row)

    async def delete(self, row):
        self.rows.remove(row)
        self.deleted.append(row)

    async def flush(self):
        return None


def _row(lender, *, origin="admin", category="mca", **fields) -> DealerDebt:
    row = DealerDebt(id=uuid.uuid4(), profile_id=PROFILE.id, lender=lender, origin=origin, category=category, status="active")
    for key, value in fields.items():
        setattr(row, key, value)
    if getattr(row, "count_in_dscr", None) is None:
        row.count_in_dscr = True
    return row


PROFILE = SimpleNamespace(id=uuid.uuid4(), dealer_id=None)


@pytest.mark.asyncio
async def test_a_verbal_breakdown_becomes_rows_with_a_monthly_figure() -> None:
    db = _DB([])
    rows = await mca_advances.save_advances(
        db,
        PROFILE,
        [
            {"lender": "OnDeck", "original_amount": 80000, "balance": 40000, "payment_amount": 300, "payment_frequency": "daily"},
            {"lender": "Kapitus", "balance": 20000, "payment_amount": 1000, "payment_frequency": "weekly", "factor_rate": 1.3, "notes": "told on the call"},
        ],
    )
    assert [(r.lender, r.origin, r.category, r.monthly_payment) for r in rows] == [("Kapitus", "admin", "mca", 4330.0), ("OnDeck", "admin", "mca", 6300.0)]
    read = [mca_advances.advance_read(r) for r in rows]
    assert read[0]["editable"] is True and read[0]["source"] == "desk" and read[0]["factor_rate"] == 1.3
    assert read[1]["original_amount"] == 80000 and read[1]["count_in_dscr"] is True


@pytest.mark.asyncio
async def test_the_save_updates_its_own_rows_and_leaves_the_clients_alone() -> None:
    mine = _row("OnDeck", payment_amount=300, payment_frequency="daily", monthly_payment=6300.0, balance=40000)
    theirs = _row("Kapitus", origin="borrower", payment_amount=1000, payment_frequency="weekly", monthly_payment=4330.0, balance=20000)
    db = _DB([mine, theirs])
    rows = await mca_advances.save_advances(
        db,
        PROFILE,
        [
            {"id": str(mine.id), "lender": "OnDeck", "balance": 35000, "payment_amount": 300, "payment_frequency": "daily"},
            {"id": str(theirs.id), "lender": "Kapitus", "balance": 1, "payment_amount": 1, "payment_frequency": "daily"},
        ],
    )
    assert mine.balance == 35000 and mine.origin == "admin"
    assert theirs.balance == 20000 and theirs.monthly_payment == 4330.0  # untouched
    assert len(rows) == 2 and db.deleted == []
    assert mca_advances.advance_read(theirs)["editable"] is False and mca_advances.advance_read(theirs)["source"] == "client"


@pytest.mark.asyncio
async def test_dropping_a_row_deletes_only_the_desks_own_and_matches_by_lender_without_an_id() -> None:
    mine = _row("OnDeck", payment_amount=300, payment_frequency="daily", monthly_payment=6300.0)
    gone = _row("Fora Financial", payment_amount=200, payment_frequency="daily", monthly_payment=4200.0)
    theirs = _row("Kapitus", origin="ai_draft", payment_amount=1000, payment_frequency="weekly", monthly_payment=4330.0)
    db = _DB([mine, gone, theirs])
    rows = await mca_advances.save_advances(db, PROFILE, [{"lender": "ONDECK", "payment_amount": 310, "payment_frequency": "daily"}])
    assert db.deleted == [gone]
    assert mine.payment_amount == 310 and mine.monthly_payment == 6510.0
    assert {r.lender for r in rows} == {"ONDECK", "Kapitus"}  # the desk's latest spelling wins on its own row
    assert await mca_advances.save_advances(db, PROFILE, None) == rows  # nothing sent, nothing changes


def test_only_advance_shaped_rows_count() -> None:
    assert mca_advances.is_advance_row(SimpleNamespace(category="mca", payment_frequency=None, factor_rate=None))
    assert mca_advances.is_advance_row(SimpleNamespace(category="loan", payment_frequency="daily", factor_rate=None))
    assert mca_advances.is_advance_row(SimpleNamespace(category="loan", payment_frequency=None, factor_rate=1.25))
    assert not mca_advances.is_advance_row(SimpleNamespace(category="loan", payment_frequency="monthly", factor_rate=None))
