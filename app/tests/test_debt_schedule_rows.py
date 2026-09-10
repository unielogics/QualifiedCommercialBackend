"""The debt schedule must survive a round trip without growing, and a save
must never reach past the rows its own source owns.

The form is seeded with every active row on the file, whatever wrote it, and
saving used to delete only the rows sharing the saver's `origin` and then
insert everything the form sent back — including the other sources' rows it
had just been shown. A borrower submitting three debts and a desk officer
then pressing Save left the file claiming six and paying twice as much a
month. `count_in_dscr` defaults to true, so those phantom rows went straight
into the debt-service denominator and made coverage look worse than the
borrower's real position.

A first repair widened the delete to the whole file. Two reviewers rejected
it: an empty autosave from a no-login link could wipe every row on the file,
and a borrower could delete desk rows. The duplicate was never the delete's
fault, it was the re-insert. So rows are matched and updated in place, and a
save writes only what its own source owns. These tests are that guarantee.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.services import financial_statements as fs


class _Result:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return list(self._rows)


class _DB:
    """Enough session to exercise the matching, without a database.

    Filters and orders the way the real query does, so the profile scoping the
    fix relies on is executed rather than only grepped for.
    """

    def __init__(self, rows, *, profile_id):
        self.rows = list(rows)
        self.profile_id = profile_id

    async def execute(self, _stmt):
        rows = [r for r in self.rows if r.status == "active" and r.profile_id == self.profile_id]
        return _Result(sorted(rows, key=lambda r: (r.created_at, str(r.id))))

    async def delete(self, row): self.rows.remove(row)
    def add(self, row): self.rows.append(row)
    async def flush(self): return None


_CLOCK = [0]


def _stored(lender, balance, monthly, *, origin, profile_id, count_in_dscr=True, **extra):
    from app.dealer_os.models import DealerDebt

    _CLOCK[0] += 1
    row = DealerDebt(
        id=uuid.uuid4(), profile_id=profile_id, lender=lender, balance=balance,
        monthly_payment=monthly, origin=origin, status="active",
        count_in_dscr=count_in_dscr, category="loan",
    )
    row.created_at = _CLOCK[0]
    for key, value in extra.items():
        setattr(row, key, value)
    return row


def _profile():
    return SimpleNamespace(id=uuid.uuid4(), dealer_id=uuid.uuid4())


def _active(db, profile):
    return [r for r in db.rows if r.status == "active" and r.profile_id == profile.id]


def _monthly(db, profile):
    return float(sum(float(r.monthly_payment or 0) for r in _active(db, profile)))


async def _round_trip(db, profile, origin, *, edit=None):
    body = await fs.debt_body_for_profile(db, profile, origin=origin)
    if edit:
        edit(body)
    await fs.save_debt_rows(db, profile, fs.debt_rows_from_body(body), origin=origin)
    return body


# --- the bug, replayed ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_desk_save_after_a_borrower_submission_does_not_double_the_schedule():
    """The exact sequence that used to produce six debts out of three."""
    profile = _profile()
    db = _DB([
        _stored("First National", 12000, 450, origin="client_form", profile_id=profile.id),
        _stored("Ridge Equipment", 30000, 780, origin="client_form", profile_id=profile.id),
        _stored("Blue Card", 4000, 120, origin="client_form", profile_id=profile.id),
    ], profile_id=profile.id)
    before_ids = {str(r.id) for r in _active(db, profile)}
    before_monthly = _monthly(db, profile)

    await _round_trip(db, profile, "admin")

    assert len(_active(db, profile)) == 3, "the desk's save duplicated the borrower's rows"
    assert _monthly(db, profile) == before_monthly, "monthly debt service moved on a no-op save"
    assert {str(r.id) for r in _active(db, profile)} == before_ids, "rows lost their identity"
    assert [r.origin for r in _active(db, profile)] == ["client_form"] * 3


# --- a save writes only what it owns -----------------------------------------


@pytest.mark.asyncio
async def test_a_borrower_cannot_alter_or_delete_a_desk_row():
    """`origin='admin'` is never overwritten anywhere else in the system, and a
    no-login link must not be the exception."""
    profile = _profile()
    desk = _stored("Desk Note", 50000, 1200, origin="admin", profile_id=profile.id,
                   count_in_dscr=False, document_id=uuid.uuid4())
    db = _DB([desk, _stored("Mine", 4000, 120, origin="client_form", profile_id=profile.id)],
             profile_id=profile.id)

    def tamper(body):
        for row in body["debts"]:
            if row["lender"] == "Desk Note":
                row["monthly_payment"] = "1"        # an edit the save must discard
        body["debts"] = [r for r in body["debts"] if r["lender"] != "Mine"] + [
            {"lender": "Mine", "balance": "4000", "monthly_payment": "120"},
        ]

    body = await _round_trip(db, profile, "client_form", edit=tamper)

    assert desk in db.rows and float(desk.monthly_payment) == 1200
    assert desk.count_in_dscr is False and desk.document_id is not None
    shown = next(r for r in body["debts"] if r["lender"] == "Desk Note")
    assert shown["editable"] is False, "the form must know the desk owns this row"


@pytest.mark.asyncio
async def test_a_desk_save_leaves_a_borrower_row_untouched_too():
    """Symmetry: the desk's form is seeded with the borrower's rows and its
    save neither rewrites nor deletes them."""
    profile = _profile()
    theirs = _stored("Theirs", 9000, 300, origin="client_form", profile_id=profile.id)
    db = _DB([theirs, _stored("Ours", 20000, 600, origin="admin", profile_id=profile.id)],
             profile_id=profile.id)

    def drop_theirs(body):
        body["debts"] = [r for r in body["debts"] if r["lender"] != "Theirs"]

    await _round_trip(db, profile, "admin", edit=drop_theirs)
    assert theirs in db.rows, "the desk deleted a row it does not own"
    assert len(_active(db, profile)) == 2


# --- the empty body ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_body_with_no_debts_key_changes_nothing():
    """An autosave that fired before the form loaded sent nothing, and nothing
    must happen — not "delete everything"."""
    profile = _profile()
    db = _DB([
        _stored("Desk Note", 50000, 1200, origin="admin", profile_id=profile.id),
        _stored("Mine", 4000, 120, origin="client_form", profile_id=profile.id),
    ], profile_id=profile.id)

    assert fs.debt_rows_from_body({}) is None
    await fs.save_debt_rows(db, profile, fs.debt_rows_from_body({}), origin="client_form")
    await fs.save_debt_rows(db, profile, fs.debt_rows_from_body({"business_name": "x"}), origin="admin")
    assert len(_active(db, profile)) == 2


@pytest.mark.asyncio
async def test_an_explicit_empty_list_clears_only_the_savers_rows():
    profile = _profile()
    db = _DB([
        _stored("Desk Note", 50000, 1200, origin="admin", profile_id=profile.id),
        _stored("Mine", 4000, 120, origin="client_form", profile_id=profile.id),
    ], profile_id=profile.id)

    await fs.save_debt_rows(db, profile, fs.debt_rows_from_body({"debts": []}), origin="client_form")
    assert [r.lender for r in _active(db, profile)] == ["Desk Note"]


# --- what a save must preserve ---------------------------------------------


@pytest.mark.asyncio
async def test_editing_a_figure_keeps_the_dscr_exclusion_and_the_other_columns():
    """The point of the form is to correct a figure. Doing so must not turn
    the row into a stranger that comes back with every flag reset."""
    profile = _profile()
    db = _DB([
        _stored("Advance Co", 50000, 9000, origin="admin", profile_id=profile.id, count_in_dscr=False,
                term_months=9, payment_amount=420, payment_frequency="daily",
                factor_rate=1.32, payoff_amount=41000, vendor_key="advanceco",
                dealer_id=profile.dealer_id),
    ], profile_id=profile.id)
    original_id = str(_active(db, profile)[0].id)

    def correct(body):
        body["debts"][0]["monthly_payment"] = "9100"
        body["debts"][0]["balance"] = "48000"

    await _round_trip(db, profile, "admin", edit=correct)

    row = _active(db, profile)[0]
    assert str(row.id) == original_id
    assert float(row.monthly_payment) == 9100 and float(row.balance) == 48000
    assert row.count_in_dscr is False, "a DSCR exclusion was switched back on"
    assert (row.term_months, row.payment_frequency, float(row.factor_rate)) == (9, "daily", 1.32)
    assert float(row.payment_amount) == 420 and float(row.payoff_amount) == 41000
    assert row.vendor_key == "advanceco" and row.dealer_id == profile.dealer_id


@pytest.mark.asyncio
async def test_a_blank_monthly_payment_stays_unknown_not_zero():
    """Zero tells the DSCR engine the obligation costs nothing a month."""
    rows = fs.debt_rows_from_body({"debts": [{"lender": "Daily Advance", "balance": "50000", "monthly_payment": ""}]})
    assert rows[0]["monthly_payment"] is None
    facts = fs.debt_key_facts(rows)
    assert facts["debts"][0]["monthly_payment"] is None
    assert facts["total_outstanding_balance"] == 50000.0


# --- identity without ids ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_that_never_had_row_ids_still_updates_in_place_even_when_edited():
    """An older tab, still open, correcting a figure on the page it was served."""
    profile = _profile()
    db = _DB([
        _stored("First National", 12000, 450, origin="admin", profile_id=profile.id, count_in_dscr=False),
        _stored("Ridge Equipment", 30000, 780, origin="admin", profile_id=profile.id),
    ], profile_id=profile.id)
    ids = {str(r.id) for r in _active(db, profile)}

    def old_client(body):
        for row in body["debts"]:
            row.pop("id", None)
            row.pop("editable", None)
        body["debts"][0]["monthly_payment"] = "475"      # a real correction

    await _round_trip(db, profile, "admin", edit=old_client)

    assert {str(r.id) for r in _active(db, profile)} == ids, "an id-less edit re-created the row"
    assert [r.count_in_dscr for r in _active(db, profile)] == [False, True]
    assert float(_active(db, profile)[0].monthly_payment) == 475


@pytest.mark.asyncio
async def test_a_stale_id_falls_through_to_content_matching():
    """An id that resolves to nothing carries no claim."""
    profile = _profile()
    db = _DB([_stored("Acme", 10000, 300, origin="admin", profile_id=profile.id, count_in_dscr=False)],
             profile_id=profile.id)
    real_id = str(_active(db, profile)[0].id)

    def stale(body):
        body["debts"][0]["id"] = str(uuid.uuid4())

    await _round_trip(db, profile, "admin", edit=stale)
    row = _active(db, profile)[0]
    assert str(row.id) == real_id and row.count_in_dscr is False


@pytest.mark.asyncio
async def test_two_identical_debts_stay_two_without_ids():
    profile = _profile()
    db = _DB([
        _stored("Same Bank", 5000, 150, origin="admin", profile_id=profile.id),
        _stored("Same Bank", 5000, 150, origin="admin", profile_id=profile.id),
    ], profile_id=profile.id)

    def old_client(body):
        for row in body["debts"]:
            row.pop("id", None)

    await _round_trip(db, profile, "admin", edit=old_client)
    assert len(_active(db, profile)) == 2


# --- scope ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_files_row_id_is_ignored():
    """A body carrying some other profile's row id must neither steal nor
    delete that row."""
    mine, theirs = _profile(), _profile()
    foreign = _stored("Not Yours", 7000, 200, origin="admin", profile_id=theirs.id)
    db = _DB([foreign, _stored("Mine", 4000, 120, origin="admin", profile_id=mine.id)],
             profile_id=mine.id)

    body = await fs.debt_body_for_profile(db, mine, origin="admin")
    assert [r["lender"] for r in body["debts"]] == ["Mine"]
    body["debts"].append({"id": str(foreign.id), "lender": "Not Yours", "balance": "1", "monthly_payment": "1"})
    await fs.save_debt_rows(db, mine, fs.debt_rows_from_body(body), origin="admin")

    assert foreign in db.rows and float(foreign.balance) == 7000
    assert len(_active(db, mine)) == 2 and len(_active(db, theirs)) == 1


@pytest.mark.asyncio
async def test_a_row_added_on_the_form_is_inserted_under_the_saver():
    profile = _profile()
    db = _DB([_stored("First National", 12000, 450, origin="client_form", profile_id=profile.id)],
             profile_id=profile.id)

    def add(body):
        body["debts"].append({"lender": "New Note", "balance": "9000", "monthly_payment": "300"})

    await _round_trip(db, profile, "admin", edit=add)
    added = [r for r in _active(db, profile) if r.lender == "New Note"]
    assert len(added) == 1 and added[0].origin == "admin" and added[0].dealer_id == profile.dealer_id


@pytest.mark.asyncio
async def test_a_dismissed_row_is_neither_shown_nor_deleted():
    profile = _profile()
    dismissed = _stored("Old Note", 1000, 50, origin="admin", profile_id=profile.id)
    dismissed.status = "dismissed"
    db = _DB([_stored("First National", 12000, 450, origin="admin", profile_id=profile.id), dismissed],
             profile_id=profile.id)

    body = await _round_trip(db, profile, "admin")
    assert [r["lender"] for r in body["debts"]] == ["First National"]
    assert dismissed in db.rows


# --- the guard against the bug returning -------------------------------------


def test_the_read_and_the_write_agree_on_scope():
    """Both take every active row on this file. The write is never scoped by
    origin on its query; ownership is decided per row afterwards. The gap
    between an origin-scoped delete and an unscoped read was the duplicate."""
    read = inspect.getsource(fs.debt_body_for_profile)
    write = inspect.getsource(fs.save_debt_rows)
    for source in (read, write):
        assert "DealerDebt.profile_id == profile.id" in source
        assert 'DealerDebt.status == "active"' in source
        assert "DealerDebt.origin ==" not in source, (
            "the debt schedule query is scoped by origin again — that is the "
            "bug that doubled the schedule and inflated the DSCR denominator"
        )


@pytest.mark.asyncio
async def test_a_null_debts_key_is_nothing_sent_too():
    profile = _profile()
    db = _DB([_stored("Mine", 4000, 120, origin="client_form", profile_id=profile.id)], profile_id=profile.id)
    assert fs.debt_rows_from_body({"debts": None}) is None
    await fs.save_debt_rows(db, profile, fs.debt_rows_from_body({"debts": None}), origin="client_form")
    assert len(_active(db, profile)) == 1


@pytest.mark.asyncio
async def test_an_old_tab_adding_a_second_debt_at_a_desk_lender_inserts_rather_than_vanishes():
    """Content matching may only bind an id-less line to a foreign row when the
    figures still agree — that is the seeded copy. Different figures at the
    same lender are a new debt and must not be swallowed."""
    profile = _profile()
    desk = _stored("Big Bank", 50000, 1200, origin="admin", profile_id=profile.id)
    db = _DB([desk], profile_id=profile.id)

    def old_tab(body):
        for row in body["debts"]:
            for key in ("id", "editable", "owner"):
                row.pop(key, None)
        body["debts"].append({"lender": "Big Bank", "balance": "9000", "monthly_payment": "300"})

    await _round_trip(db, profile, "client_form", edit=old_tab)
    rows = _active(db, profile)
    assert len(rows) == 2 and desk in rows and float(desk.monthly_payment) == 1200
    assert [r.origin for r in rows if r is not desk] == ["client_form"]


def test_the_form_is_told_who_owns_a_locked_row():
    import asyncio
    profile = _profile()
    db = _DB([_stored("Desk Note", 1, 1, origin="admin", profile_id=profile.id)], profile_id=profile.id)
    body = asyncio.run(fs.debt_body_for_profile(db, profile, origin="client_form"))
    assert body["debts"][0]["editable"] is False and body["debts"][0]["owner"] == "admin"
