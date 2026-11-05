"""Taking the lines of a debt schedule apart before they become the schedule.

A client uploaded a schedule an AI had built for them: seven advances, a
payment per business day or "recurring", a few balances, and no interest rate
or original amount anywhere. Some of those lines were guesses read off ACH
debits. Adopting every line made each of them an obligation in the debt
service. These tests pin the review that replaces it: the lines are listed,
the ones that are not loans come off (as dismissals, never deletes), and the
three answers a lender asks of every line are asked and stored where a reader
looks for them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os.models import DealerDebt
from app.enums import Role
from app.routers import application_profiles as routes
from app.schemas.debt_schedule_review import DebtReviewLineWrite, DebtScheduleReviewWrite
from app.schemas.financial_cockpit import DebtRowPatch
from app.services import debt_schedule_review as dsr
from app.services import financial_cockpit as cockpit
from app.services import financial_statements as fs

NOW = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)


# --- fixtures ---------------------------------------------------------------


def _file_lines():
    """The shape of the real upload: payment per payment, cadence in the
    analyzer's own words, monthly always null, no rate and no original amount."""
    return [
        {"lender": "Alpha Funding", "debt_type": "mca", "current_balance": 13665.71, "monthly_payment": None,
         "payment_frequency": "weekday", "per_payment_amount": 359.62, "payment_status": "current",
         "balance_note": "Oct 22 letter; not a verified payoff", "discounted_payoff": 13021.61,
         "discounted_payoff_note": "Expired Oct 22"},
        {"lender": "Credibly", "debt_type": "mca", "current_balance": 37388.38, "monthly_payment": None,
         "payment_frequency": "recurring", "per_payment_amount": 1133.18, "balance_note": "Reported Sept 30"},
        {"lender": "Mystery / 8449090040", "debt_type": "mca", "current_balance": None, "monthly_payment": None,
         "payment_frequency": "irregular", "per_payment_amount": 466.3, "balance_note": "Not confirmed"},
        {"lender": "Zeta Capital", "debt_type": "mca", "current_balance": None, "monthly_payment": None,
         "payment_frequency": None, "per_payment_amount": None, "balance_note": "Not confirmed"},
    ]


def _document(lines=None, *, file_id="file-new", name="Schedule.pdf", at="2026-11-05T14:02:06+00:00", **facts):
    key_facts = {"debts": lines if lines is not None else _file_lines(), "entity": "Acme Inc.",
                 "status": "Preliminary; not lender-certified", "schedule_date": "2026-11-05", **facts}
    return {"file_id": file_id, "file_name": name, "uploaded_by": "Terrell P.", "uploaded_at": at, "key_facts": key_facts}


def _row(lender, **over):
    fields = {"id": uuid4(), "profile_id": uuid4(), "lender": lender, "category": "mca", "origin": "client_form",
              "status": "active", "count_in_dscr": True, "evidence": None}
    fields.update(over)
    return DealerDebt(**fields)


def _by_key(review, key):
    return next(line for line in [*review["lines"], *review["removed"]] if line["key"] == key)


# --- reading the lines the file carries -------------------------------------


@pytest.mark.parametrize(
    ("words", "cadence"),
    [
        ("weekday", "daily"), ("Business days", "daily"), ("Daily ACH", "daily"), ("every day", "daily"),
        ("weekly", "weekly"), ("per week", "weekly"),
        ("bi-weekly", "biweekly"), ("every other week", "biweekly"), ("fortnightly", "biweekly"),
        ("monthly", "monthly"), ("per month", "monthly"),
        ("recurring", None), ("variable", None), ("irregular", None), ("semi-monthly", None),
        ("twice a month", None), ("", None), (None, None),
    ],
)
def test_the_words_a_schedule_uses_for_how_often_are_read_for_what_they_mean(words, cadence):
    assert dsr.cadence_of(words) == cadence


def test_a_payment_per_business_day_is_read_as_daily_and_priced_by_the_month():
    review = dsr.build_review([], [_document()])
    alpha = _by_key(review, "alpha-funding")
    assert (alpha["payment_amount"], alpha["payment_frequency"]) == (359.62, "daily")
    # Twenty-one business days, the same table the cockpit prices a daily advance with.
    assert alpha["monthly_payment"] == cockpit.monthly_equivalent(359.62, "daily") == 7552.02
    assert alpha["debt_type"] == "mca" and alpha["advance"] is True
    assert alpha["payment_status"] == "current"


def test_a_payment_whose_cadence_the_file_does_not_say_keeps_its_amount_and_asks_how_often():
    credibly = _by_key(dsr.build_review([], [_document()]), "credibly")
    assert credibly["payment_amount"] == 1133.18
    assert credibly["payment_frequency"] is None and credibly["monthly_payment"] is None
    assert "payment" in credibly["missing"]


def test_nothing_the_file_left_out_is_made_up():
    nothing = _by_key(dsr.build_review([], [_document()]), "zeta-capital")
    assert nothing["balance"] is None and nothing["payment_amount"] is None and nothing["monthly_payment"] is None
    assert nothing["original_amount"] is None and nothing["rate"] is None and nothing["factor_rate"] is None


def test_the_files_own_caveats_ride_beside_the_line_and_the_balance_one_sits_with_the_balance():
    lines = _file_lines()
    lines[0]["per_payment_note"] = "Observed Oct 29"
    alpha = _by_key(dsr.build_review([], [_document(lines)]), "alpha-funding")
    assert alpha["balance_note"] == "Oct 22 letter; not a verified payoff"
    assert alpha["payment_note"] == "Observed Oct 29"
    assert alpha["notes_from_file"] == ["Expired Oct 22"]  # the rest, once the two have gone beside their boxes
    assert _by_key(dsr.build_review([], [_document()]), "zeta-capital")["payment_note"] is None
    review = dsr.build_review([], [_document()])
    assert review["sources"][0]["status_note"] == "Preliminary; not lender-certified"
    assert review["sources"][0]["entity"] == "Acme Inc." and review["sources"][0]["line_count"] == 4


def test_a_line_names_the_answers_it_still_lacks_and_an_advance_is_asked_for_a_factor_not_a_rate():
    review = dsr.build_review([], [_document([
        {"lender": "Bank of Main", "debt_type": "term_loan", "current_balance": 10000, "monthly_payment": 500,
         "payment_status": "current", "interest_rate": 7.25, "original_amount": 50000},
        {"lender": "Bank of Two", "debt_type": "term_loan", "current_balance": 10000},
        {"lender": "Fast Advance", "debt_type": "mca", "factor_rate": 1.35, "current_balance": 9000,
         "original_amount": 30000, "monthly_payment": 4000, "payment_status": "current"},
        {"lender": "Typeless", "current_balance": 100},
    ])])
    assert _by_key(review, "bank-of-main")["missing"] == []
    assert _by_key(review, "bank-of-two")["missing"] == ["rate", "original_amount", "payment", "payment_status"]
    assert _by_key(review, "fast-advance")["missing"] == []  # a factor answers the advance's price question
    assert _by_key(review, "typeless")["missing"][0] == "debt_type"
    # Interest, original amount and balance still open across the lines that stay:
    # Bank of Two (rate, original amount) and Typeless (rate, original amount).
    assert review["counts"]["missing_answers"] == 4


def test_a_rate_the_file_gives_is_a_percent_and_a_factor_is_kept_apart_from_it():
    review = dsr.build_review([], [_document([
        {"lender": "Bank", "debt_type": "term_loan", "interest_rate": "7.25%"},
        {"lender": "Advance", "debt_type": "mca", "factor_rate": "1.35", "rate_pct": None},
        {"lender": "Nonsense", "debt_type": "mca", "factor_rate": 35},
    ])])
    assert _by_key(review, "bank")["rate"] == 7.25 and _by_key(review, "bank")["factor_rate"] is None
    assert _by_key(review, "advance")["factor_rate"] == 1.35 and _by_key(review, "advance")["rate"] is None
    assert _by_key(review, "nonsense")["factor_rate"] is None  # 35 is not a factor; it is not guessed into one


def test_a_newer_schedule_replaces_an_older_ones_line_for_the_same_lender_but_one_file_may_repeat_a_lender():
    newer = _document([{"lender": "Credibly", "debt_type": "mca", "current_balance": 5}], file_id="new", at="2026-11-05T10:00:00+00:00")
    older = _document([
        {"lender": "Credibly Capital", "debt_type": "mca", "current_balance": 999},
        {"lender": "Gamma Bank", "debt_type": "term_loan"},
        {"lender": "Gamma Bank", "debt_type": "line_of_credit"},
    ], file_id="old", at="2026-10-01T10:00:00+00:00")
    review = dsr.build_review([], [newer, older])
    keys = [line["key"] for line in review["lines"]]
    assert keys == ["credibly", "gamma-bank", "gamma-bank-2"]
    assert _by_key(review, "credibly")["balance"] == 5 and _by_key(review, "credibly")["file_id"] == "new"
    assert [source["line_count"] for source in review["sources"]] == [1, 2]


# --- putting the file's lines beside the rows already on the file -----------


def test_a_line_with_a_row_on_the_schedule_is_on_the_schedule_and_the_row_is_the_authority():
    row = _row("Alpha Funding", balance=11000, rate=None, origin="admin", monthly_payment=6000)
    review = dsr.build_review([row], [_document()])
    alpha = _by_key(review, "alpha-funding")
    assert alpha["state"] == "on_schedule" and alpha["row_id"] == str(row.id)
    assert alpha["balance"] == 11000.0  # the row's figure, not the file's 13,665.71
    assert alpha["origin_label"] == "Recorded by the desk"
    # The row has a monthly figure, so the file's per-payment cadence is not mixed into it.
    assert alpha["monthly_payment"] == 6000.0 and alpha["payment_amount"] is None and alpha["payment_frequency"] is None


def test_a_row_with_no_payment_of_its_own_takes_the_files_whole_payment_not_a_piece_of_it():
    row = _row("Alpha Funding", balance=11000, origin="admin")
    alpha = _by_key(dsr.build_review([row], [_document()]), "alpha-funding")
    assert (alpha["payment_amount"], alpha["payment_frequency"], alpha["monthly_payment"]) == (359.62, "daily", 7552.02)


def test_a_line_whose_row_was_dismissed_is_listed_as_removed_not_asked_about_again():
    row = _row("Credibly", status="dismissed", evidence={"removed": {"reason": "paid_off", "by_name": "Franco", "at": "2026-11-05T15:00:00+00:00"}})
    review = dsr.build_review([row], [_document()])
    assert "credibly" not in [line["key"] for line in review["lines"]]
    credibly = _by_key(review, "credibly")
    assert credibly["state"] == "removed" and credibly["removed_reason"] == "paid_off" and credibly["removed_by"] == "Franco"
    assert review["counts"]["read"] == 3 and review["counts"]["removed"] == 1


def test_a_row_is_matched_to_one_line_only_and_a_second_debt_at_the_same_lender_stays_a_line():
    row = _row("Gamma Bank", category="term_loan", balance=1000)
    doc = _document([{"lender": "Gamma Bank", "debt_type": "term_loan"}, {"lender": "Gamma Bank", "debt_type": "line_of_credit"}])
    review = dsr.build_review([row], [doc])
    states = {line["key"]: line["state"] for line in review["lines"]}
    assert states == {"gamma-bank": "on_schedule", "gamma-bank-2": "read"}


def test_a_row_the_file_does_not_mention_stays_on_the_schedule_and_old_dismissals_are_listed_as_removed():
    kept = _row("Local Credit Union", category="term_loan", origin="client_form")
    gone = _row("Old Lender", status="dismissed", evidence={"removed": {"reason": "duplicate"}})
    review = dsr.build_review([kept, gone], [])
    assert [line["key"] for line in review["lines"]] == [f"row:{kept.id}"]
    assert review["lines"][0]["origin_label"] == "Typed by the client"
    assert [line["key"] for line in review["removed"]] == [f"row:{gone.id}"]


def test_the_removed_list_is_the_newest_thirty():
    rows = [_row(f"Lender {n}", status="dismissed") for n in range(45)]
    assert len(dsr.build_review(rows, [])["removed"]) == 30


def test_a_removed_file_only_line_shows_the_figures_it_had_from_the_snapshot_not_the_columns():
    row = _row("Credibly", status="dismissed", count_in_dscr=False,
               evidence={"source": "debt_schedule_review", "proposed": {"balance": 37388.38, "monthly_payment": 4000.0}})
    removed = dsr.build_review([row], [])["removed"][0]
    assert removed["balance"] == 37388.38 and removed["monthly_payment"] == 4000.0


def test_the_answer_is_always_a_valid_response_whatever_legacy_rows_hold():
    from app.schemas.debt_schedule_review import DebtScheduleReviewRead

    odd = _row("Legacy Lender", payment_frequency="semimonthly", payment_status="late", rate=12.5, origin="somebody_new")
    review = dsr.build_review([odd], [_document()])
    review["profile_id"] = str(uuid4())
    read = DebtScheduleReviewRead(**review)
    legacy = next(line for line in read.lines if line.lender == "Legacy Lender")
    assert legacy.payment_frequency is None and legacy.payment_status is None and legacy.rate == 12.5
    assert legacy.origin_label == "On the schedule"
    assert [line.key for line in read.lines][:1] == ["alpha-funding"]


# --- what looks like it is not a loan ---------------------------------------


@pytest.mark.parametrize(
    ("lender", "kind"),
    [("ADP Payroll Services", "payroll"), ("Duke Energy", "utilities"), ("State Farm Insurance", "insurance"), ("Landlord LLC rent", "rent")],
)
def test_a_line_that_looks_like_payroll_or_a_utility_is_flagged_and_never_removed_for_it(lender, kind):
    review = dsr.build_review([], [_document([{"lender": lender, "debt_type": "other", "current_balance": 10}])])
    line = review["lines"][0]
    assert line["hint"]["kind"] == kind and line["state"] == "read"  # advice, not an action


def test_funders_and_unknown_names_are_not_flagged():
    review = dsr.build_review([], [_document()])
    assert all(line["hint"] is None for line in review["lines"])


def test_a_tax_installment_plan_is_not_flagged_as_a_tax_payment():
    review = dsr.build_review([], [_document([{"lender": "IRS", "debt_type": "tax_payment_plan"}, {"lender": "IRS Direct", "debt_type": "other"}])])
    assert review["lines"][0]["hint"] is None
    assert review["lines"][1]["hint"]["kind"] == "tax"


# --- the documents the lines are read from -----------------------------------


@pytest.mark.asyncio
async def test_a_schedule_the_desk_unlinked_and_our_own_pdf_of_the_rows_are_not_read_as_lines(monkeypatch):
    from app.services import application_programs as programs

    def f(name):
        return SimpleNamespace(id=uuid4(), file_name=name, created_at=NOW, uploaded_by_name="Client")

    live, unlinked, drafted = f("live.pdf"), f("old.xlsx"), f("Business Debt Schedule.pdf")
    files = [live, unlinked, drafted]
    latest = {
        live.id: SimpleNamespace(provider="bedrock"),
        unlinked.id: SimpleNamespace(provider="bedrock"),
        drafted.id: SimpleNamespace(provider="drafted_form"),
    }

    async def inventory(db, profile):
        return files, latest, set()

    async def aside(db, profile):
        return {str(unlinked.id)}

    rows = [
        {"file_id": str(item.id), "file_name": item.file_name, "classification": "debt_schedule",
         "classifications": ["debt_schedule"], "key_facts": {"debts": []}}
        for item in files
    ]
    monkeypatch.setattr(programs, "_evidence_inventory", inventory)
    monkeypatch.setattr(cockpit, "set_aside_schedule_files", aside)
    monkeypatch.setattr(cockpit, "_analysis_rows", lambda *args: rows)
    documents = await dsr.schedule_documents(None, SimpleNamespace(id=uuid4()))
    assert [document["file_name"] for document in documents] == ["live.pdf"]


# --- saving the desk's decisions --------------------------------------------


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        return None


@pytest.fixture
def desk(monkeypatch):
    state = SimpleNamespace(rows=[], documents=[_document()])

    async def rows(db, profile):
        return list(state.rows)

    async def documents(db, profile):
        return list(state.documents)

    monkeypatch.setattr(dsr, "load_rows", rows)
    monkeypatch.setattr(dsr, "schedule_documents", documents)
    state.db = _DB(state.rows)
    state.profile = SimpleNamespace(id=uuid4(), dealer_id=uuid4())
    state.user = SimpleNamespace(id=uuid4(), name="Franco", email="franco@example.test", role=Role.SUPER_ADMIN)
    return state


def _line(key, lender, **over):
    fields = {"key": key, "lender": lender, "keep": True}
    fields.update(over)
    return DebtReviewLineWrite(**fields)


async def _apply(desk, *lines):
    return await dsr.apply_review(desk.db, desk.profile, desk.user, list(lines))


def _active(desk):
    return [row for row in desk.rows if row.status == "active"]


@pytest.mark.asyncio
async def test_a_kept_line_becomes_a_row_under_the_desks_origin_with_its_answers(desk):
    saved = await _apply(desk, _line(
        "alpha-funding", "Alpha Funding", debt_type="mca", original_amount=30000, balance=13665.71, factor_rate=1.35,
        payment_amount=359.62, payment_frequency="daily", payment_status="current",
    ))
    assert saved == {"kept": 1, "added": 1, "updated": 0, "removed": 0, "restored": 0}
    row = desk.rows[0]
    assert (row.origin, row.status, row.profile_id, row.dealer_id) == ("admin", "active", desk.profile.id, desk.profile.dealer_id)
    assert (row.category, float(row.original_amount), float(row.balance), float(row.factor_rate)) == ("mca", 30000.0, 13665.71, 1.35)
    assert row.rate is None  # the factor is not a percent
    assert (float(row.payment_amount), row.payment_frequency, float(row.monthly_payment)) == (359.62, "daily", 7552.02)
    assert row.count_in_dscr is True and row.payment_status == "current"
    assert row.evidence["source"] == "debt_schedule_review" and row.evidence["file_id"] == "file-new"
    assert row.evidence["line"]["lender"] == "Alpha Funding" and row.evidence["reviewed"]["by"] == str(desk.user.id)
    assert row.notes == dsr.SAVED_NOTE


@pytest.mark.asyncio
async def test_a_card_a_kept_line_is_still_not_counted_in_the_debt_service_by_default(desk):
    await _apply(desk, _line("card", "Corporate Card", debt_type="credit_card"))
    assert desk.rows[0].count_in_dscr is False


@pytest.mark.asyncio
async def test_the_answers_correct_a_row_in_place_and_it_keeps_its_origin_and_identity(desk):
    row = _row("Alpha Funding", origin="client_form", balance=11000)
    desk.rows.append(row)
    saved = await _apply(desk, _line(
        "alpha-funding", "Alpha Funding", row_id=row.id, debt_type="mca", original_amount=25000, balance=9000, factor_rate=1.4,
        monthly_payment=4200, payment_status="delinquent",
    ))
    assert saved["updated"] == 1 and saved["added"] == 0 and len(desk.rows) == 1
    assert (row.origin, str(row.id)) == ("client_form", str(row.id))
    assert (float(row.original_amount), float(row.balance), float(row.factor_rate), float(row.monthly_payment)) == (25000.0, 9000.0, 1.4, 4200.0)
    assert row.payment_status == "delinquent" and row.evidence["reviewed"]["by"] == str(desk.user.id)


@pytest.mark.asyncio
async def test_a_percent_rate_goes_in_the_rate_column_for_a_loan(desk):
    await _apply(desk, _line("bank", "Bank of Main", debt_type="term_loan", rate=7.25, original_amount=50000, balance=40000, monthly_payment=800))
    row = desk.rows[0]
    assert float(row.rate) == 7.25 and row.factor_rate is None and float(row.monthly_payment) == 800.0
    assert row.payment_amount is None and row.payment_frequency is None


@pytest.mark.asyncio
async def test_a_monthly_payment_is_the_month_and_a_per_payment_amount_keeps_its_cadence(desk):
    await _apply(desk, _line("a", "Monthly Bank", payment_amount=800, payment_frequency="monthly"),
                 _line("b", "Weekly Bank", payment_amount=1000, payment_frequency="weekly"),
                 _line("c", "Month Only Bank", monthly_payment=950))
    monthly, weekly, only = desk.rows
    assert (float(monthly.monthly_payment), monthly.payment_amount, monthly.payment_frequency) == (800.0, None, None)
    assert (float(weekly.payment_amount), weekly.payment_frequency, float(weekly.monthly_payment)) == (1000.0, "weekly", 4330.0)
    assert (float(only.monthly_payment), only.payment_amount) == (950.0, None)


@pytest.mark.asyncio
async def test_an_answer_left_blank_stays_unknown_and_is_never_written_as_nought(desk):
    await _apply(desk, _line("x", "Blank Lender"))
    row = desk.rows[0]
    assert row.balance is None and row.original_amount is None and row.rate is None and row.monthly_payment is None


@pytest.mark.asyncio
async def test_taking_a_line_off_dismisses_its_row_and_remembers_why_and_who(desk):
    row = _row("Credibly", balance=37388.38, monthly_payment=4000)
    desk.rows.append(row)
    saved = await _apply(desk, _line("credibly", "Credibly", row_id=row.id, keep=False, remove_reason="paid_off", remove_note="Paid in October"))
    assert saved["removed"] == 1 and saved["kept"] == 0
    assert row.status == "dismissed"  # never deleted
    removed = row.evidence["removed"]
    assert (removed["reason"], removed["note"], removed["by_name"]) == ("paid_off", "Paid in October", "Franco")
    assert float(row.balance) == 37388.38  # the row itself is left as it was


@pytest.mark.asyncio
async def test_a_line_that_was_only_in_the_file_and_is_taken_off_becomes_a_dismissed_row_with_empty_columns(desk):
    saved = await _apply(desk, _line("mystery-8449090040", "Mystery / 8449090040", keep=False, remove_reason="not_a_loan",
                                    debt_type="mca", balance=500, monthly_payment=1000))
    assert saved["removed"] == 1
    row = desk.rows[0]
    assert row.status == "dismissed" and row.count_in_dscr is False and row.origin == "admin"
    # A reader that sums the columns without looking at status still counts nothing.
    assert row.balance is None and row.monthly_payment is None and row.rate is None
    assert row.evidence["proposed"]["balance"] == 500 and row.evidence["removed"]["reason"] == "not_a_loan"
    review = dsr.build_review(desk.rows, desk.documents)
    assert "mystery-8449090040" not in [line["key"] for line in review["lines"]]  # it does not come back as a line to review
    assert _by_key(review, "mystery-8449090040")["balance"] == 500  # and the person who put it back sees what it had


@pytest.mark.asyncio
async def test_a_removed_line_put_back_is_active_again_with_its_figures_in_the_columns_and_counting(desk):
    await _apply(desk, _line("credibly", "Credibly", keep=False, debt_type="mca", balance=37388.38))
    assert _active(desk) == []
    saved = await _apply(desk, _line("credibly", "Credibly", debt_type="mca", balance=37388.38, factor_rate=1.3))
    assert saved["restored"] == 1 and saved["added"] == 0 and len(desk.rows) == 1
    row = desk.rows[0]
    assert row.status == "active" and row.count_in_dscr is True and float(row.balance) == 37388.38
    assert "removed" not in row.evidence and "proposed" not in row.evidence


@pytest.mark.asyncio
async def test_a_row_dismissed_somewhere_else_keeps_its_own_counting_flag_when_put_back(desk):
    row = _row("Old Lender", status="dismissed", count_in_dscr=False, evidence=None)
    desk.rows.append(row)
    await _apply(desk, _line("row:x", "Old Lender", row_id=row.id))
    assert row.status == "active" and row.count_in_dscr is False  # that was somebody's choice, not ours


@pytest.mark.asyncio
async def test_saving_the_same_review_twice_does_not_add_the_lines_twice(desk):
    lines = [_line("alpha-funding", "Alpha Funding", balance=1), _line("credibly", "Credibly", keep=False)]
    first = await _apply(desk, *lines)
    second = await _apply(desk, *lines)
    assert first["added"] == 1 and first["removed"] == 1
    assert (second["added"], second["removed"]) == (0, 0) and len(desk.rows) == 2


@pytest.mark.asyncio
async def test_two_real_debts_at_one_lender_are_two_rows_not_one(desk):
    await _apply(desk, _line("gamma-bank", "Gamma Bank", debt_type="term_loan", balance=1), _line("gamma-bank-2", "Gamma Bank", debt_type="line_of_credit", balance=2))
    assert len(_active(desk)) == 2


@pytest.mark.asyncio
async def test_a_line_the_reading_has_moved_on_from_is_saved_as_sent(desk):
    desk.documents = []  # the file was unlinked between the read and the save
    saved = await _apply(desk, _line("ghost", "Ghost Lender", balance=5))
    assert saved["added"] == 1 and desk.rows[0].evidence["line"] == {} and desk.rows[0].notes is None


@pytest.mark.asyncio
async def test_a_review_round_trip_leaves_nothing_to_read_and_the_rest_on_the_schedule(desk):
    keys = {"alpha-funding": True, "credibly": True, "mystery-8449090040": False, "zeta-capital": False}
    lines = [_line(key, name, keep=keep, balance=10 if keep else None) for (key, keep), name in zip(
        keys.items(), ["Alpha Funding", "Credibly", "Mystery / 8449090040", "Zeta Capital"], strict=True)]
    saved = await _apply(desk, *lines)
    assert saved == {"kept": 2, "added": 2, "updated": 0, "removed": 2, "restored": 0}
    review = dsr.build_review(desk.rows, desk.documents)
    assert review["counts"]["read"] == 0 and review["counts"]["on_schedule"] == 2 and review["counts"]["removed"] == 2


@pytest.mark.asyncio
async def test_a_removal_reason_defaults_to_not_a_loan(desk):
    await _apply(desk, _line("zeta-capital", "Zeta Capital", keep=False))
    assert desk.rows[0].evidence["removed"]["reason"] == "not_a_loan"


@pytest.mark.asyncio
async def test_rows_written_by_a_review_are_ordinary_schedule_rows_to_every_reader(desk):
    await _apply(desk, _line("alpha-funding", "Alpha Funding", debt_type="mca", balance=100, monthly_payment=50))
    body = fs.debt_rows_from_body({"debts": [{
        "lender": desk.rows[0].lender, "debt_type": desk.rows[0].category, "balance": str(desk.rows[0].balance),
        "monthly_payment": str(desk.rows[0].monthly_payment),
    }]})
    assert body[0]["lender"] == "Alpha Funding" and float(body[0]["balance"]) == 100.0


# --- the request shapes ------------------------------------------------------


def test_the_write_schema_keeps_every_answer_inside_what_a_schedule_can_hold():
    DebtReviewLineWrite(key="k", lender="L", rate=0, factor_rate=1, original_amount=0, balance=1_000_000_000, monthly_payment=0)
    DebtReviewLineWrite(key="k", lender="L", rate=999.999, factor_rate=5)
    for bad in ({"rate": -0.1}, {"rate": 1000}, {"factor_rate": 0.99}, {"factor_rate": 35}, {"balance": -1}, {"balance": 1_000_000_001},
                {"original_amount": -5}, {"payment_frequency": "hourly"}, {"payment_status": "late"}, {"lender": ""}, {"remove_reason": "because"}):
        with pytest.raises(ValidationError):
            DebtReviewLineWrite(key="k", **{"lender": "L", **bad})


def test_an_amount_per_payment_must_say_how_often_but_a_removed_line_need_not():
    with pytest.raises(ValidationError, match="how often"):
        DebtReviewLineWrite(key="k", lender="Alpha", payment_amount=359.62)
    DebtReviewLineWrite(key="k", lender="Alpha", payment_amount=359.62, payment_frequency="daily")
    DebtReviewLineWrite(key="k", lender="Alpha", payment_amount=359.62, monthly_payment=7552.02)
    DebtReviewLineWrite(key="k", lender="Alpha", keep=False, payment_amount=359.62)


def test_a_review_is_at_most_eighty_lines():
    DebtScheduleReviewWrite(lines=[{"key": str(n), "lender": "L"} for n in range(80)])
    with pytest.raises(ValidationError):
        DebtScheduleReviewWrite(lines=[{"key": str(n), "lender": "L"} for n in range(81)])


# --- the row patch learned the three answers ---------------------------------


def test_a_schedule_row_can_now_be_corrected_on_its_rate_original_amount_and_status():
    row = SimpleNamespace(lender="Bank", category="term_loan", monthly_payment=None, balance=None, payoff_amount=None, payment_amount=None,
                          payment_frequency=None, count_in_dscr=True, vendor_key=None, status="active", original_amount=None, rate=None,
                          factor_rate=None, payment_status=None, mca_modified=None, mca_modification_note=None)
    changed = fs.apply_debt_row_patch(row, {"original_amount": 50000.456, "rate": 7.25049, "factor_rate": 1.35, "payment_status": "Current"})
    assert set(changed) == {"original_amount", "rate", "factor_rate", "payment_status"}
    assert (row.original_amount, row.rate, row.factor_rate, row.payment_status) == (50000.46, 7.25, 1.35, "current")
    assert fs.apply_debt_row_patch(row, {"payment_status": "whenever"}) == [] and row.payment_status == "current"  # unreadable: left alone
    fs.apply_debt_row_patch(row, {"rate": None})
    assert row.rate is None


def test_the_cockpit_patch_schema_bounds_the_new_answers_the_same_way():
    DebtRowPatch(rate=0, factor_rate=1.35, original_amount=1, payment_status="delinquent")
    for bad in ({"rate": 1000}, {"factor_rate": 0.5}, {"original_amount": -1}, {"payment_status": "late"}):
        with pytest.raises(ValidationError):
            DebtRowPatch(**bad)


# --- unlinking really detaches ------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _SetAsideDB:
    def __init__(self, links):
        self.links = links

    async def execute(self, _statement):
        return _Rows(self.links)


@pytest.mark.asyncio
async def test_a_file_is_set_aside_when_its_link_was_removed_and_nothing_links_it_now():
    gone, back, never = uuid4(), uuid4(), uuid4()
    db = _SetAsideDB([(gone, NOW), (back, NOW), (back, None), (never, None)])
    assert await cockpit.set_aside_schedule_files(db, SimpleNamespace(id=uuid4())) == {str(gone)}


def _schedule_file(file_id, lender, *, also=None):
    classes = ["debt_schedule", *(also or [])]
    return {"file_id": file_id, "file_name": f"{file_id}.pdf", "classification": "debt_schedule", "classifications": classes,
            "key_facts": {"debts": [{"lender": lender, "current_balance": 1000, "monthly_payment": 100}], "total_monthly_debt_service": 100},
            "accepted": True, "analyzed_at": None, "period_hint": None}


def test_a_set_aside_schedule_is_not_read_as_the_schedule_and_its_replacement_is_not_added_to_it():
    old, new = _schedule_file("old", "Alpha"), _schedule_file("new", "Beta")
    both = cockpit.build_cockpit([old, new], [])
    assert both["coverage"]["documented_debts"] == 2 and both["debt"]["document"]["total_monthly_debt_service"] == 200
    only_new = cockpit.build_cockpit([old, new], [], schedule_set_aside={"old"})
    assert only_new["coverage"]["documented_debts"] == 1 and only_new["debt"]["document"]["total_monthly_debt_service"] == 100
    assert [line["lender"] for line in only_new["debt"]["document"]["debts"]] == ["Beta"]
    none_left = cockpit.build_cockpit([old], [], schedule_set_aside={"old"})
    assert none_left["coverage"]["has_debt_schedule_document"] is False


def test_setting_a_file_aside_takes_only_its_schedule_reading_away_not_its_other_readings():
    both = _schedule_file("combo", "Alpha", also=["payoff_letter"])
    kept = cockpit.without_set_aside_schedules([both], {"combo"})[0]
    assert "debt_schedule" not in kept["classifications"] and "payoff_letter" in kept["classifications"]
    assert both["classifications"] == ["debt_schedule", "payoff_letter"]  # the input is not changed
    assert cockpit.without_set_aside_schedules([both], None) == [both] and cockpit.without_set_aside_schedules([both], set()) == [both]


# --- who may read and save ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.BROKER, Role.CLIENT, Role.REGIONAL_MANAGER, Role.DEALER_PARTNER, Role.FIELD_REP])
async def test_only_underwriting_roles_can_save_a_review(role):
    user = SimpleNamespace(id=uuid4(), role=role)
    with pytest.raises(HTTPException) as refused:
        await routes.apply_debt_schedule_review(uuid4(), DebtScheduleReviewWrite(lines=[]), user, db=None)
    assert refused.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER])
async def test_the_review_is_not_readable_by_the_client_or_outside_parties(role, monkeypatch):
    async def load(db, profile_id, user):
        return SimpleNamespace(id=profile_id)

    monkeypatch.setattr(routes.profiles, "load_profile", load)
    with pytest.raises(HTTPException) as refused:
        await routes.get_debt_schedule_review(uuid4(), SimpleNamespace(id=uuid4(), role=role), db=None)
    assert refused.value.status_code == 403
