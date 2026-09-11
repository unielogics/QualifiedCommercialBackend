"""The four forms as one worksheet: what a read puts on the wire, what a cell
edit does to the file, and what a shared link is allowed to reach.

The rules being pinned here are the ones that are easy to lose:

- a read is *scoped* — a link that does not open the personal financial
  statement never receives its figures, and scope is applied on the server;
- an edit goes through the save function that already owns the form, so the
  draft→submitted latch and `save_debt_rows`' origin rule survive a grid where
  a save is a keystroke;
- a stale `base_rev` is normal and lands; only a client hundreds of revisions
  behind is refused, because 409ing on every keystroke would make the live
  design unusable;
- a worksheet share link draws its own token. The packet's `{base}.{kind}`
  derivation would let a link advertised as "P&L only" reach all four.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.financial_worksheet import SHEET_KINDS
from app.routers import application_profiles as router
from app.services import application_profiles as profiles
from app.services import business_statement_schema as bss
from app.services import business_statements, financial_statements, pfs_schema, sheet_layout, sheets


def _run(coro):
    return asyncio.run(coro)


def _user():
    return SimpleNamespace(
        id=uuid.uuid4(), role=Role.LOAN_EXEC, name="Jane Desk", email="jane@example.com"
    )


def _profile():
    return SimpleNamespace(
        id=uuid.uuid4(), primary_bucket_id=uuid.uuid4(), dealer_id=None, intake_id=None
    )


def _worksheet(revision=0, profile_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(), profile_id=profile_id or uuid.uuid4(), packet_id=None, revision=revision
    )


def _db(*, scalar=0, scalar_one=None, rows=()):
    result = SimpleNamespace(
        scalar=lambda: scalar,
        scalar_one_or_none=lambda: scalar_one,
        scalars=lambda: SimpleNamespace(all=lambda: list(rows), first=lambda: None),
    )
    return SimpleNamespace(
        execute=AsyncMock(return_value=result),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        get=AsyncMock(return_value=None),
        delete=AsyncMock(),
    )


def _pl_body(gross="100000"):
    body = bss.pl_empty_body()
    body["header"].update(period_start="2026-01-01", period_end="2026-06-30")
    body["sections"]["revenue"]["gross_revenue"] = gross
    body["sections"]["operating_expenses"]["supplies"] = "400"
    return body


def _statement(kind="p_and_l", status="draft", body=None):
    """A statement row with every attribute the real save writes, so the real
    save can be exercised rather than mocked."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind=kind,
        status=status,
        body=body if body is not None else _pl_body(),
        schema_version="qc_pl.v1",
        notes=None,
        period_start=None,
        period_end=None,
        as_of_date=None,
        statement_date=None,
        gross_revenue=None,
        net_income=None,
        ebitda=None,
        total_assets=None,
        total_liabilities=None,
        total_equity=None,
        net_worth=None,
        liquid_assets=None,
        submitted_at=None,
        submitted_by_user_id=None,
    )


def _debt_body(rows=()):
    return {
        "debts": [
            {
                "id": str(row["id"]),
                "editable": True,
                "owner": row.get("owner", "admin"),
                "lender": row.get("lender", ""),
                "debt_type": "",
                "original_amount": "",
                "balance": row.get("balance", ""),
                "rate": "",
                "monthly_payment": row.get("monthly_payment", ""),
                "originated_on": "",
                "maturity_on": "",
                "secured": "",
                "payment_status": "",
                "collateral": "",
                "notes": "",
            }
            for row in rows
        ]
    }


class _DebtStore:
    """The file's schedule, behaving the way the real pair does: a save writes
    only the rows the caller's origin owns, and the read hands back what was
    stored — normalised — rather than what was typed."""

    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.saves: list[tuple[list, str]] = []

    async def read(self, _db, _profile, *, origin=None):
        return _debt_body(self.rows)

    async def save(self, _db, _profile, rows, *, origin):
        """`save_debt_rows`' law in miniature: a row this origin owns is
        updated in place, a row another origin owns is matched so it is never
        re-inserted and otherwise left exactly as it was, an unmatched line is
        inserted under this origin, and an own-origin row nobody sent back is
        deleted."""
        self.saves.append((rows, origin))
        if rows is None:
            return
        by_id = {str(row["id"]): row for row in self.rows}
        claimed: set[str] = set()
        inserted = []
        for line in rows:
            target = by_id.get(str(line.get("id") or ""))
            if target is not None:
                claimed.add(str(target["id"]))
                if target.get("owner", "admin") == origin:
                    target["lender"] = line["lender"]
                    target["balance"] = line.get("balance") or ""
                    target["monthly_payment"] = line.get("monthly_payment") or ""
                continue
            inserted.append(
                {
                    "id": uuid.uuid4(),
                    "owner": origin,
                    "lender": line["lender"],
                    "balance": line.get("balance") or "",
                    "monthly_payment": line.get("monthly_payment") or "",
                }
            )
        self.rows = [
            row
            for row in self.rows
            if str(row["id"]) in claimed or row.get("owner", "admin") != origin
        ] + inserted


def _reading(*, pl=None, bs=None, pfs=None, debts=None, prefill=None, sheet_rev=0):
    """Patch the four body readers the worksheet composes, and nothing else."""
    pl_statement = pl if pl is not None else _statement("p_and_l")
    bs_statement = bs if bs is not None else _statement("balance_sheet", body=bss.bs_empty_body())

    async def _body_for_profile(_db, _profile, kind, _prefill=None):
        row = pl_statement if kind == "p_and_l" else bs_statement
        return dict(row.body), row

    return (
        patch.object(business_statements, "body_for_profile", AsyncMock(side_effect=_body_for_profile)),
        patch.object(financial_statements, "latest_for_profile", AsyncMock(return_value=pfs)),
        patch.object(
            financial_statements,
            "debt_body_for_profile",
            AsyncMock(return_value=debts if debts is not None else _debt_body()),
        ),
        patch.object(
            financial_statements,
            "form_prefill",
            AsyncMock(return_value=prefill if prefill is not None else {"business_name": "Acme LLC"}),
        ),
    )


def _read(*, kinds=None, can_edit=True, worksheet=None, origin="admin", **reading):
    profile = _profile()
    worksheet = worksheet or _worksheet(profile_id=profile.id)
    patches = _reading(**reading)
    with patches[0], patches[1], patches[2], patches[3]:
        return _run(
            sheets.read_sheets(
                _db(),
                profile,
                kinds=kinds,
                origin=origin,
                can_edit=can_edit,
                worksheet=worksheet,
            )
        )


# ── the read ────────────────────────────────────────────────────────────────


def test_the_read_returns_the_four_sheets_with_flat_values_and_computed_figures():
    payload = _read()
    assert [sheet["kind"] for sheet in payload["sheets"]] == list(sheet_layout.KINDS)
    assert payload["layout_version"] == sheet_layout.LAYOUT_VERSION
    assert payload["business_name"] == "Acme LLC"

    pl = payload["sheets"][0]
    # Flat, keyed the way the workbook's defined names are — the grid never
    # sees `sections.revenue.gross_revenue`.
    assert pl["values"]["gross_revenue"] == "100000"
    assert pl["values"]["supplies"] == "400"
    assert all("." not in key for key in pl["values"])
    # Computed comes from the schema's own totals, server-side, so the number
    # underwriting reads and the number on screen cannot disagree.
    assert pl["computed"]["net_income"] == 99600.0
    assert pl["computed"]["gross_profit"] == 100000.0
    assert pl["schema"]["kind"] == "p_and_l"
    assert pl["columns"] and pl["rows"] and pl["freeze"]["rows"] >= 0
    assert pl["status"] == "draft" and pl["completed"] is False


def test_every_sheet_carries_its_own_shape_schema_version_and_revision():
    payload = _read(worksheet=_worksheet(revision=7))
    by_kind = {sheet["kind"]: sheet for sheet in payload["sheets"]}
    assert set(by_kind) == set(SHEET_KINDS)
    for kind, sheet in by_kind.items():
        assert sheet["schema_version"] == sheet_layout.SCHEMA_VERSIONS[kind]
        assert sheet["title"] == sheet_layout.SHEET_TITLES[kind]
        assert isinstance(sheet["rev"], int)
    # The debt schedule has no statement row to hold a rev, so it reads the
    # workbook clock directly.
    assert by_kind["debt_schedule"]["rev"] == 7


def test_a_scoped_link_never_puts_the_sheets_it_omits_on_the_wire():
    payload = _read(kinds=["debt_schedule", "p_and_l"])
    assert [sheet["kind"] for sheet in payload["sheets"]] == ["p_and_l", "debt_schedule"]
    assert payload["scope"] == {
        "can_edit": True,
        "sheets": ["p_and_l", "debt_schedule"],
        "open_at": "p_and_l",
    }
    body = repr(payload)
    assert "pfs" not in [sheet["kind"] for sheet in payload["sheets"]]
    assert "net_worth" not in body


def test_a_scope_of_nothing_opens_nothing_rather_than_everything():
    payload = _read(kinds=[])
    # The dangerous default: a link whose stored scope rows were lost must
    # open no sheet at all, never all four.
    assert payload["sheets"] == []
    assert payload["scope"] == {"can_edit": True, "sheets": [], "open_at": None}


def test_a_view_only_scope_says_so_and_still_returns_the_figures():
    payload = _read(kinds=["pfs"], can_edit=False)
    assert payload["scope"]["can_edit"] is False and payload["scope"]["open_at"] == "pfs"
    assert payload["sheets"][0]["kind"] == "pfs"
    # Watching it change live is the accountant-over-your-shoulder case; the
    # refusal belongs on the write, not on the read.
    assert "computed" in payload["sheets"][0]


def test_a_stored_debt_row_keeps_its_identity_and_blank_lines_follow_it():
    row_id = uuid.uuid4()
    payload = _read(debts=_debt_body([{"id": row_id, "lender": "Fifth Third", "balance": "1000"}]))
    debt = next(sheet for sheet in payload["sheets"] if sheet["kind"] == "debt_schedule")
    assert debt["values"][f"{row_id}.lender"] == "Fifth Third"
    data_rows = [row for row in debt["rows"] if row["kind"] == "data"]
    assert data_rows[0]["row_key"] == str(row_id)
    # Blank lines are addressed by the position each would occupy, so typing
    # into one appends rather than overwriting the stored row. The minimum is a
    # floor the sheet is drawn up to, not a tail added past every stored row:
    # one real line and two blanks, not one and three. A floor is what lets an
    # add or a remove move the count — see `apply_row_op`.
    assert [row["row_key"] for row in data_rows[1:]] == [
        f"r{n}" for n in range(2, sheets.BLANK_DEBT_ROWS + 1)
    ]
    assert len(data_rows) == sheets.BLANK_DEBT_ROWS


def test_the_read_says_which_debt_rows_this_caller_may_actually_write():
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    body = _debt_body(
        [
            {"id": mine, "owner": "admin", "lender": "Fifth Third"},
            {"id": theirs, "owner": "client_form", "lender": "Newtek"},
        ]
    )
    body["debts"][1]["editable"] = False
    payload = _read(debts=body)
    debt = next(sheet for sheet in payload["sheets"] if sheet["kind"] == "debt_schedule")
    assert debt["row_meta"][str(mine)] == {"editable": True, "owner": "admin"}
    assert debt["row_meta"][str(theirs)] == {"editable": False, "owner": "client_form"}
    # Only the schedule is a shared list; the rest are single documents.
    assert all(
        sheet["row_meta"] == {} for sheet in payload["sheets"] if sheet["kind"] != "debt_schedule"
    )


# ── writing cells ───────────────────────────────────────────────────────────


def _edit(edits, *, base_rev=None, revision=0, pl=None, debts=None, pfs=None, origin="admin", store=None):
    profile = _profile()
    worksheet = _worksheet(revision=revision, profile_id=profile.id)
    patches = _reading(pl=pl, pfs=pfs, debts=debts)
    saver = AsyncMock(side_effect=store.save) if store else AsyncMock()
    if store is not None:
        patches = (
            patches[0],
            patches[1],
            patch.object(financial_statements, "debt_body_for_profile", AsyncMock(side_effect=store.read)),
            patches[3],
        )
    with patches[0], patches[1], patches[2], patches[3], patch.object(
        financial_statements, "save_debt_rows", saver
    ):
        result = _run(
            sheets.apply_cell_edits(
                _db(),
                profile,
                edits,
                base_rev=base_rev or {},
                origin=origin,
                actor_user_id=uuid.uuid4(),
                worksheet=worksheet,
            )
        )
    return result, worksheet, saver


def test_an_edit_to_a_submitted_statement_leaves_it_submitted():
    submitted = _statement("p_and_l", status="submitted")
    submitted.submitted_at = "already"
    result, _, _ = _edit(
        [{"sheet": "p_and_l", "key": "gross_revenue", "value": "250000"}], pl=submitted
    )
    # The latch is `business_statements.save`'s, not reimplemented here — which
    # is the point of routing a cell edit through it.
    assert submitted.status == "submitted"
    assert submitted.submitted_at == "already"
    assert submitted.body["sections"]["revenue"]["gross_revenue"] == "250000"
    assert result["computed"]["p_and_l"]["gross_profit"] == 250000.0


def test_two_edits_to_different_cells_both_land():
    statement = _statement("p_and_l")
    result, _, _ = _edit(
        [
            {"sheet": "p_and_l", "key": "gross_revenue", "value": "5000"},
            {"sheet": "p_and_l", "key": "supplies", "value": "900"},
        ],
        pl=statement,
    )
    assert statement.body["sections"]["revenue"]["gross_revenue"] == "5000"
    assert statement.body["sections"]["operating_expenses"]["supplies"] == "900"
    assert result["resync"] == []


def test_an_untouched_cell_on_the_same_sheet_is_left_exactly_as_it_was():
    statement = _statement("p_and_l")
    _edit([{"sheet": "p_and_l", "key": "gross_revenue", "value": "5000"}], pl=statement)
    assert statement.body["sections"]["operating_expenses"]["supplies"] == "400"


def test_an_emptied_cell_is_stored_as_nothing_rather_than_as_nought():
    statement = _statement("p_and_l")
    _edit([{"sheet": "p_and_l", "key": "supplies", "value": ""}], pl=statement)
    assert statement.body["sections"]["operating_expenses"]["supplies"] is None


def test_the_revision_advances_once_per_batch_however_many_cells_it_carries():
    result, worksheet, _ = _edit(
        [
            {"sheet": "p_and_l", "key": "gross_revenue", "value": "1"},
            {"sheet": "p_and_l", "key": "supplies", "value": "2"},
        ],
        revision=41,
    )
    assert worksheet.revision == 42
    assert result["rev"]["p_and_l"] == 42
    # Always reported: the debt schedule reads the clock, so a client that only
    # edits the P&L would otherwise drift behind on it for no reason.
    assert result["rev"]["debt_schedule"] == 42


def test_a_far_stale_base_rev_is_told_to_reload_and_a_near_stale_one_is_not():
    with pytest.raises(HTTPException) as far:
        _edit(
            [{"sheet": "p_and_l", "key": "gross_revenue", "value": "1"}],
            revision=1000,
            base_rev={"p_and_l": 1000 - sheets.STALE_LIMIT - 1},
        )
    assert far.value.status_code == 409
    assert far.value.detail == {"code": "stale_worksheet", "current_revision": 1000}

    result, worksheet, _ = _edit(
        [{"sheet": "p_and_l", "key": "gross_revenue", "value": "1"}],
        revision=1000,
        base_rev={"p_and_l": 1000 - sheets.STALE_LIMIT},
    )
    # A live grid cannot 409 on a keystroke: being a little behind is what
    # "somebody else is typing" looks like, and the write still lands.
    assert worksheet.revision == 1001 and result["rev"]["p_and_l"] == 1001


def test_an_unknown_cell_key_is_refused_rather_than_stored_somewhere_nobody_reads():
    with pytest.raises(HTTPException) as caught:
        _edit([{"sheet": "p_and_l", "key": "not_a_line", "value": "1"}])
    assert caught.value.status_code == 400

    with pytest.raises(HTTPException) as unknown_sheet:
        _edit([{"sheet": "cashflow", "key": "gross_revenue", "value": "1"}])
    assert unknown_sheet.value.status_code == 400


def test_a_debt_cell_goes_through_save_debt_rows_with_the_callers_own_origin():
    row_id = uuid.uuid4()
    store = _DebtStore([{"id": row_id, "owner": "admin", "lender": "Fifth Third", "balance": "1000"}])
    result, _, saver = _edit(
        [{"sheet": "debt_schedule", "key": f"{row_id}.balance", "value": "2,500"}],
        store=store,
        origin="admin",
    )
    saver.assert_awaited_once()
    assert saver.await_args.kwargs["origin"] == "admin"
    written = saver.await_args.args[2]
    assert [str(row["id"]) for row in written] == [str(row_id)]
    assert float(written[0]["balance"]) == 2500.0
    # The stored figure is "2500", not the "2,500" that was typed, so the grid
    # is told to take the server's answer rather than leave the comma on screen.
    assert result["resync"] == ["debt_schedule"]


def test_a_blank_debt_line_typed_into_becomes_a_row_this_origin_owns():
    desk_row = uuid.uuid4()
    store = _DebtStore([{"id": desk_row, "owner": "admin", "lender": "Fifth Third", "balance": "10"}])
    result, _, saver = _edit(
        [
            {"sheet": "debt_schedule", "key": "r2.lender", "value": "Newtek"},
            {"sheet": "debt_schedule", "key": "r2.balance", "value": "500"},
        ],
        store=store,
        origin="client_form",
    )
    written = saver.await_args.args[2]
    assert saver.await_args.kwargs["origin"] == "client_form"
    # The desk's row is sent back untouched so the save can match it rather
    # than insert a twin; the new line is the one this origin now owns.
    assert [row["lender"] for row in written] == ["Fifth Third", "Newtek"]
    assert {row["owner"] for row in store.rows} == {"admin", "client_form"}
    assert result["rev"]["debt_schedule"] >= 1


def test_a_desk_edit_to_a_borrowers_row_does_not_land_and_asks_for_a_resync():
    theirs = uuid.uuid4()
    store = _DebtStore(
        [{"id": theirs, "owner": "client_form", "lender": "Newtek", "balance": "1000"}]
    )
    result, _, _ = _edit(
        [{"sheet": "debt_schedule", "key": f"{theirs}.balance", "value": "9999"}],
        store=store,
        origin="admin",
    )
    # `save_debt_rows`' law: another origin's row is matched — so it is never
    # re-inserted, which is what used to double the schedule and inflate the
    # DSCR denominator — and otherwise left exactly as it was.
    assert store.rows[0]["balance"] == "1000"
    assert len(store.rows) == 1
    assert result["resync"] == ["debt_schedule"]


def test_a_desk_delete_cannot_remove_a_row_the_borrower_owns():
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    store = _DebtStore(
        [
            {"id": mine, "owner": "admin", "lender": "Fifth Third", "balance": "10"},
            {"id": theirs, "owner": "client_form", "lender": "Newtek", "balance": "20"},
        ]
    )
    _row_op(kind="debt_schedule", op="delete", row_id=str(theirs), store=store)
    assert sorted(str(row["id"]) for row in store.rows) == sorted([str(mine), str(theirs)])


def test_a_pfs_cell_is_saved_through_the_413s_own_save_and_keeps_its_status():
    statement = _statement("pfs", status="submitted", body=pfs_schema.empty_body())
    statement.submitted_at = "already"
    result, _, _ = _edit(
        [{"sheet": "pfs", "key": "cash_on_hand", "value": "12000"}], pfs=statement
    )
    assert statement.body["assets"]["cash_on_hand"] == "12000"
    assert statement.status == "submitted" and statement.submitted_at == "already"
    assert result["computed"]["pfs"]["total_assets"] == 12000.0


# ── rows ────────────────────────────────────────────────────────────────────


def _lines(rows, block):
    """The typeable lines of one list, the way the grid counts them."""
    return [row for row in rows if row["kind"] == "data" and row.get("block") == block]


def _row_op(
    *,
    kind,
    op,
    row_id=None,
    after=None,
    block=None,
    debts=None,
    pfs=None,
    revision=3,
    store=None,
    visible=None,
):
    profile = _profile()
    worksheet = _worksheet(revision=revision, profile_id=profile.id)
    patches = _reading(debts=debts, pfs=pfs)
    saver = AsyncMock(side_effect=store.save) if store else AsyncMock()
    if store is not None:
        patches = (
            patches[0],
            patches[1],
            patch.object(financial_statements, "debt_body_for_profile", AsyncMock(side_effect=store.read)),
            patches[3],
        )
    with patches[0], patches[1], patches[2], patches[3], patch.object(
        financial_statements, "save_debt_rows", saver
    ):
        result = _run(
            sheets.apply_row_op(
                _db(),
                profile,
                kind=kind,
                op=op,
                row_id=row_id,
                after=after,
                block=block,
                origin="admin",
                actor_user_id=uuid.uuid4(),
                worksheet=worksheet,
                visible=visible,
            )
        )
    return result, worksheet, saver


def test_an_added_debt_line_is_addressable_but_is_not_yet_an_obligation():
    existing = uuid.uuid4()
    result, worksheet, saver = _row_op(
        kind="debt_schedule",
        op="insert",
        after=str(existing),
        debts=_debt_body([{"id": existing, "lender": "Fifth Third", "balance": "10"}]),
    )
    data_rows = [row for row in result["rows"] if row["kind"] == "data"]
    assert data_rows[0]["row_key"] == str(existing)
    assert len(data_rows) > 1
    # Nothing written: `count_in_dscr` defaults to true, so persisting an empty
    # line would put a debt nobody owes into the debt-service denominator.
    saver.assert_not_awaited()
    assert worksheet.revision == 3


def test_removing_a_debt_line_goes_through_save_debt_rows_with_origin():
    keep, drop = uuid.uuid4(), uuid.uuid4()
    result, worksheet, saver = _row_op(
        kind="debt_schedule",
        op="delete",
        row_id=str(drop),
        debts=_debt_body(
            [
                {"id": keep, "lender": "Fifth Third", "balance": "10"},
                {"id": drop, "lender": "Newtek", "balance": "20"},
            ]
        ),
    )
    saver.assert_awaited_once()
    assert saver.await_args.kwargs["origin"] == "admin"
    assert [str(row["id"]) for row in saver.await_args.args[2]] == [str(keep)]
    assert worksheet.revision == 4
    assert result["rev"]["debt_schedule"] == 4


def test_removing_a_row_that_is_not_there_is_a_404_not_a_silent_no_op():
    # Every line on this sheet is an obligation somebody entered, so an id
    # nobody recognises is a mistake and is told so — a stray id must never be
    # allowed to take out a real row just to make the count move.
    store = _DebtStore(
        [
            {"id": uuid.uuid4(), "lender": "Fifth Third", "balance": "10"},
            {"id": uuid.uuid4(), "lender": "Newtek", "balance": "20"},
            {"id": uuid.uuid4(), "lender": "Amex", "balance": "30"},
        ]
    )
    with pytest.raises(HTTPException) as caught:
        _row_op(kind="debt_schedule", op="delete", row_id=str(uuid.uuid4()), store=store)
    assert caught.value.status_code == 404
    assert len(store.rows) == 3


def test_a_sheet_with_a_fixed_row_list_refuses_a_row_operation():
    with pytest.raises(HTTPException) as caught:
        _row_op(kind="p_and_l", op="insert")
    assert caught.value.status_code == 400


def test_a_schedule_line_added_to_the_413_is_stored_and_advances_the_clock():
    statement = _statement("pfs", body=pfs_schema.empty_body())
    result, worksheet, _ = _row_op(
        kind="pfs", op="insert", block=pfs_schema.SCHEDULES[0].key, pfs=statement
    )
    stored = statement.body["schedules"][pfs_schema.SCHEDULES[0].key]
    # The lines the schedule was drawn up to are stored alongside the new one,
    # so the 413 remembers what the person is looking at and the next add moves
    # the count again. A blank line here carries no weight — unlike a debt row,
    # which would land in the DSCR denominator and is never persisted empty.
    assert len(stored) == sheets.BLANK_SCHEDULE_ROWS + 1
    assert all(row["id"] for row in stored)
    assert worksheet.revision == 4
    assert any(row["kind"] == "data" for row in result["rows"])


# ── the count answers to the button ─────────────────────────────────────────
#
# The bug these pin: an empty schedule holds no rows, the sheet is drawn up to
# a minimum of blank lines so there is somewhere to type, and "Add a line" used
# to be swallowed by that padding — the row list came back the same length and
# the button looked broken. The minimum is a floor for a sheet nobody has
# touched; a deliberate add or remove always moves the count, and a remove may
# take it below the floor, because lines somebody removed are meant to be gone.


def _read_lines(**reading):
    payload = _read(**reading)
    debt = next(sheet for sheet in payload["sheets"] if sheet["kind"] == "debt_schedule")
    pfs = next(sheet for sheet in payload["sheets"] if sheet["kind"] == "pfs")
    return debt, pfs


def test_a_fresh_read_of_an_untouched_sheet_still_offers_the_blank_minimum():
    debt, pfs = _read_lines()
    assert len(_lines(debt["rows"], "debts")) == sheets.BLANK_DEBT_ROWS
    for spec in pfs_schema.SCHEDULES:
        assert len(_lines(pfs["rows"], spec.key)) == sheets.BLANK_SCHEDULE_ROWS


def test_adding_a_line_to_an_empty_debt_schedule_returns_one_more_than_the_read():
    debt, _ = _read_lines()
    seen = len(_lines(debt["rows"], "debts"))
    result, _, saver = _row_op(kind="debt_schedule", op="insert", block="debts")
    lines = _lines(result["rows"], "debts")
    assert len(lines) == seen + 1
    # Every visible line is addressable and owned by the caller, so the first
    # keystroke saves through `save_debt_rows` like any other edit.
    assert all(result["row_meta"][row["row_key"]] == {"editable": True, "owner": "admin"}
               for row in lines)
    # Nothing written: an empty line is not an obligation.
    saver.assert_not_awaited()


def test_each_further_add_moves_the_count_again_when_the_client_says_what_it_sees():
    # A debt row is deliberately not stored until it is typed into, so the file
    # looks identical after every add; the count the client is looking at is
    # what the next add has to land on.
    debt, _ = _read_lines()
    seen = len(_lines(debt["rows"], "debts"))
    for _ in range(3):
        result, _, saver = _row_op(kind="debt_schedule", op="insert", block="debts", visible=seen)
        lines = _lines(result["rows"], "debts")
        assert len(lines) == seen + 1
        saver.assert_not_awaited()
        seen = len(lines)
    assert seen == sheets.BLANK_DEBT_ROWS + 3


def test_a_client_cannot_ask_for_an_unbounded_sheet_by_claiming_to_see_one():
    result, _, _ = _row_op(kind="debt_schedule", op="insert", block="debts", visible=10_000_000)
    assert len(_lines(result["rows"], "debts")) == sheets.MAX_BLANK_FILL + 1


def test_removing_a_line_returns_one_fewer_and_may_go_below_the_blank_minimum():
    debt, _ = _read_lines()
    seen = len(_lines(debt["rows"], "debts"))
    result, _, saver = _row_op(kind="debt_schedule", op="delete", row_id="r2", block="debts")
    assert len(_lines(result["rows"], "debts")) == seen - 1
    # Below the floor on purpose: the minimum is what a sheet nobody has
    # touched is drawn at, not a quota a person is held to.
    assert seen - 1 < sheets.BLANK_DEBT_ROWS
    # And still nothing blank on the file.
    assert saver.await_args.args[2] == []

    result, _, _ = _row_op(
        kind="debt_schedule", op="delete", row_id="r1", block="debts", visible=seen - 1
    )
    assert len(_lines(result["rows"], "debts")) == seen - 2


def test_deleting_a_line_that_only_exists_on_screen_is_not_a_404():
    # The likeliest delete there is: somebody adds a line, changes their mind,
    # and removes it before typing. The server never stored it, so the id means
    # nothing to the file — and the answer they are owed is the shorter list.
    debt, _ = _read_lines()
    seen = len(_lines(debt["rows"], "debts"))
    added, _, _ = _row_op(kind="debt_schedule", op="insert", block="debts")
    minted = _lines(added["rows"], "debts")[-1]["row_key"]
    result, _, _ = _row_op(
        kind="debt_schedule", op="delete", row_id=minted, block="debts", visible=seen + 1
    )
    assert len(_lines(result["rows"], "debts")) == seen


def test_a_removed_line_does_not_take_a_real_obligation_with_it():
    keep = uuid.uuid4()
    store = _DebtStore([{"id": keep, "owner": "admin", "lender": "Fifth Third", "balance": "10"}])
    result, _, saver = _row_op(kind="debt_schedule", op="delete", row_id="r2", store=store)
    lines = _lines(result["rows"], "debts")
    # One fewer than the floor the read drew, and the obligation is still first.
    assert len(lines) == sheets.BLANK_DEBT_ROWS - 1
    assert lines[0]["row_key"] == str(keep)
    assert [str(row["id"]) for row in store.rows] == [str(keep)]
    # The blank lines the caller was looking at are not obligations and are
    # never handed to the save.
    assert [row["id"] for row in saver.await_args.args[2]] == [str(keep)]


def test_the_413s_schedules_answer_to_the_buttons_the_same_way():
    block = pfs_schema.SCHEDULES[0].key
    statement = _statement("pfs", body=pfs_schema.empty_body())
    _, pfs_sheet = _read_lines(pfs=statement)
    seen = len(_lines(pfs_sheet["rows"], block))
    assert seen == sheets.BLANK_SCHEDULE_ROWS

    for _ in range(2):
        result, _, _ = _row_op(kind="pfs", op="insert", block=block, pfs=statement)
        lines = _lines(result["rows"], block)
        assert len(lines) == seen + 1
        seen = len(lines)
    # The other schedules are untouched and still show their own floor.
    other = pfs_schema.SCHEDULES[1].key
    assert len(_lines(result["rows"], other)) == sheets.BLANK_SCHEDULE_ROWS

    while seen:
        result, _, _ = _row_op(
            kind="pfs",
            op="delete",
            row_id=_lines(result["rows"], block)[-1]["row_key"],
            block=block,
            pfs=statement,
            # Past the floor the server can no longer derive what is on screen
            # — a client that has deleted its way below the minimum says so.
            visible=seen,
        )
        assert len(_lines(result["rows"], block)) == seen - 1
        seen -= 1
    # All the way to nothing, well below the minimum a fresh read would draw.
    assert len(_lines(result["rows"], block)) == 0
    # And the floor comes back for the next person to open it: the minimum is
    # about a sheet nobody has touched, not about this one.
    _, reopened = _read_lines(pfs=statement)
    assert len(_lines(reopened["rows"], block)) == sheets.BLANK_SCHEDULE_ROWS


def test_no_blank_debt_line_is_ever_written_to_the_file():
    store = _DebtStore()
    _, _, saver = _row_op(kind="debt_schedule", op="insert", block="debts", store=store)
    saver.assert_not_awaited()

    _, _, saver = _row_op(
        kind="debt_schedule", op="delete", row_id="r1", block="debts", store=store
    )
    for written in saver.await_args.args[2] or []:
        assert written["lender"] or written["balance"] or written["monthly_payment"]
    assert store.rows == []


# ── sharing ─────────────────────────────────────────────────────────────────


def _mint(payload, *, worksheet=None, profile=None, user=None):
    profile = profile or _profile()
    user = user or _user()
    worksheet = worksheet or _worksheet(profile_id=profile.id)
    link = SimpleNamespace(
        id=uuid.uuid4(),
        expires_at="2026-10-10",
        worksheet_id=None,
        packet_id=None,
        permission=None,
        revoked_at=None,
    )
    db = _db()
    db.get = AsyncMock(return_value=worksheet)
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(
            financial_statements, "mint_link", AsyncMock(return_value=(link, "tok"))
        ) as mint,
        patch.object(profiles, "log_profile_action", AsyncMock()) as logged,
    ):
        result = _run(
            router.mint_worksheet_link(profile.id, worksheet.id, payload, user, db)
        )
    added = [call.args[0] for call in db.add.call_args_list]
    return result, link, mint, added, logged, db


def test_minting_stores_which_sheets_the_link_opens_and_what_it_may_do():
    payload = router.WorksheetLinkCreate(
        permission="view", sheets=["debt_schedule", "p_and_l", "balance_sheet"]
    )
    result, link, mint, added, logged, db = _mint(payload)
    assert sorted(row.sheet_kind for row in added) == [
        "balance_sheet",
        "debt_schedule",
        "p_and_l",
    ]
    assert all(row.link_id == link.id for row in added)
    assert link.permission == "view"
    assert link.worksheet_id == result["worksheet_id"]
    assert result["sheets"] == ["p_and_l", "balance_sheet", "debt_schedule"]
    assert result["link_id"] == link.id and result["expires_at"] == "2026-10-10"
    assert logged.await_args.args[3] == "financial_form.worksheet_link_minted"
    db.commit.assert_awaited_once()


def test_the_token_is_drawn_on_its_own_rather_than_derived_from_another_link():
    payload = router.WorksheetLinkCreate(permission="edit", sheets=["p_and_l"])
    _, _, first, _, _, _ = _mint(payload)
    _, _, second, _, _, _ = _mint(payload)
    one = first.await_args.kwargs["token"]
    two = second.await_args.kwargs["token"]
    # `{base}.{kind}` is what makes per-sheet scope impossible: any child token
    # yields the base by rsplit, and the base yields all four by concatenation.
    assert "." not in one and "." not in two
    assert one != two and len(one) >= 40
    assert first.await_args.kwargs["kind"] == "worksheet"


def test_a_link_that_opens_nothing_is_refused_rather_than_stored():
    with pytest.raises(HTTPException) as caught:
        _mint(router.WorksheetLinkCreate(permission="view", sheets=[]))
    assert caught.value.status_code == 400


def test_a_worksheet_on_another_file_is_a_404():
    worksheet = _worksheet(profile_id=uuid.uuid4())
    with pytest.raises(HTTPException) as caught:
        _mint(router.WorksheetLinkCreate(sheets=["pfs"]), worksheet=worksheet)
    assert caught.value.status_code == 404


def test_revoking_closes_the_link_and_leaves_an_already_closed_one_alone():
    profile = _profile()
    link = SimpleNamespace(
        id=uuid.uuid4(), revoked_at=None, worksheet_id=uuid.uuid4(), kind="worksheet"
    )
    db = _db(scalar_one=link)
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(profiles, "log_profile_action", AsyncMock()) as logged,
    ):
        result = _run(router.revoke_worksheet_link(profile.id, link.id, _user(), db))
    assert result == {"revoked": True}
    assert link.revoked_at is not None
    assert logged.await_args.args[3] == "financial_form.worksheet_link_revoked"

    closed_at = link.revoked_at
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(profiles, "log_profile_action", AsyncMock()),
    ):
        _run(router.revoke_worksheet_link(profile.id, link.id, _user(), _db(scalar_one=link)))
    assert link.revoked_at == closed_at


def test_revoking_a_link_that_is_not_on_this_file_is_a_404():
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=_profile())),
        patch.object(profiles, "log_profile_action", AsyncMock()),
        pytest.raises(HTTPException) as caught,
    ):
        _run(router.revoke_worksheet_link(uuid.uuid4(), uuid.uuid4(), _user(), _db()))
    assert caught.value.status_code == 404


# ── the vocabulary the model and the layout must agree on ───────────────────


def test_the_model_and_the_layout_name_the_same_four_sheets():
    assert set(SHEET_KINDS) == set(sheet_layout.KINDS) == set(sheets.KINDS)


# ── the wire between the two row routes and the service ─────────────────────
#
# The service got `visible` right and shipped, and a second "Add a line" still
# did nothing, because neither route had a field to put it in. Both halves
# were correct and the seam between them was empty. A test per field would
# have been written after the fact and only for that field, so this one is
# about the seam itself: whatever a row body and `apply_row_op` turn out to
# have in common has to actually travel between them.


def _row_op_parameters() -> set[str]:
    import inspect

    return set(inspect.signature(sheets.apply_row_op).parameters)


def _forwarded_kwargs(func) -> set[str]:
    """The keyword names the route hands to `sheets.apply_row_op`."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "apply_row_op":
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError(f"{func.__name__} never calls apply_row_op")


def test_both_row_routes_forward_every_field_they_share_with_the_service():
    """A field on the body whose name the service also takes must be passed.

    `sheet` is the one rename — the wire calls it `sheet` and the service
    calls it `kind` — and `name` is deliberately not a service argument: it is
    the self-declared name on the audit row and stops at the router.
    """
    from app.routers import worksheets as public_router

    accepted = _row_op_parameters()
    assert "visible" in accepted

    for route, body in (
        (router.write_worksheet_row, router.WorksheetRowOp),
        (public_router.write_worksheet_rows, public_router.RowsBody),
    ):
        fields = set(body.model_fields)
        shared = (fields & accepted) - {"sheet"}
        missing = shared - _forwarded_kwargs(route)
        assert not missing, f"{route.__name__} drops {sorted(missing)} on the way to apply_row_op"
        assert "visible" in shared, f"{body.__name__} has no visible field"


def test_a_row_body_refuses_a_line_count_that_is_not_one():
    """The count is a count. A negative one is a client bug, not a row to add."""
    from pydantic import ValidationError

    from app.routers import worksheets as public_router

    for body in (router.WorksheetRowOp, public_router.RowsBody):
        assert body(sheet="debt_schedule", op="insert").visible is None
        assert body(sheet="debt_schedule", op="insert", visible=7).visible == 7
        with pytest.raises(ValidationError):
            body(sheet="debt_schedule", op="insert", visible=-1)


def test_a_formula_cell_carries_its_formula_onto_the_wire():
    """The grid needs the formula text, not only the name of its answer.

    It reads the references to decide which subtotals are made of nothing but
    blank cells and shows those as an em dash — a zero somebody typed and a
    zero nobody typed are different claims, and on a debt schedule the second
    one reads as "this business has no debt". The client's own tests passed on
    a fixture that carried `formula` while the wire did not, so the feature
    was dead in the browser and green in CI. Hence a test on the payload.
    """
    for kind in ("p_and_l", "balance_sheet", "debt_schedule", "pfs"):
        sheet = sheets._layout_for(kind, {})
        formulas = [
            cell
            for row in sheet.rows
            for cell in row.cells
            if cell.type == "formula" and cell.formula
        ]
        assert formulas, f"{kind} has no formula cells to check"
        for cell in formulas:
            wire = sheets._cell_payload(cell)
            assert wire.get("formula") == cell.formula, f"{kind}/{cell.xlsx_name}"
            # Both halves of what `indexFormulas` keys on.
            assert wire.get("xlsx_name"), f"{kind}/{cell.c} has a formula and no name"
