"""The debt schedule asks for what a lender reads, and a submit is complete.

The owner's list: "lender name, type of debt, original amount, balance,
interest rate, monthly payment, current or delinquent we need it"; the dates,
secured, collateral and the note may stay blank; and "if is an MCA category
... we ask if the MCA has been modified due to lower sales/revenue". The type
is a closed list now, stored as a slug in `dos_debts.category`, and what was
typed before the list existed ("LOC", "Term") is read for the slug it means.
These tests are that contract — the vocabulary, the advance question, the
completeness check and the PDF that shows all of it.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import debt_types, sheet_layout
from app.services import financial_statements as fs
from app.services.dealer_forms_pdf import build_debt_schedule_html
from app.tests.test_debt_schedule_rows import _DB, _profile, _stored

# --- the vocabulary -----------------------------------------------------------


def test_every_slug_fits_the_category_column_and_has_a_label():
    for slug, label in debt_types.DEBT_TYPES:
        assert len(slug) <= debt_types.CATEGORY_WIDTH
        assert label and label[0].isupper()
    assert "mca" in debt_types.DEBT_TYPE_SLUGS
    assert debt_types.ADVANCE_DEBT_TYPES <= set(debt_types.DEBT_TYPE_SLUGS)


@pytest.mark.parametrize(
    ("typed", "slug"),
    [
        ("LOC", "line_of_credit"),
        ("loc", "line_of_credit"),
        ("Line of Credit", "line_of_credit"),
        ("Term", "term_loan"),
        ("term loan", "term_loan"),
        ("SBA 7(a) term loan", "sba_loan"),
        ("Floorplan", "floorplan"),
        ("mca", "mca"),
        ("Merchant Cash Advance (MCA)", "mca"),
        ("merchant cash advance", "mca"),
        ("Equipment lease", "equipment"),
        ("credit_card", "credit_card"),
        ("Business credit card", "credit_card"),
        ("Auto loan", "vehicle"),
        ("Commercial mortgage", "real_estate"),
        ("Other", "other"),
    ],
)
def test_what_was_typed_before_the_list_existed_is_read_for_the_slug_it_means(typed, slug):
    assert debt_types.normalize_debt_type(typed) == slug


def test_the_columns_historic_default_and_a_blank_are_not_stated():
    assert debt_types.normalize_debt_type("loan") is None
    assert debt_types.normalize_debt_type("") is None
    assert debt_types.normalize_debt_type(None) is None
    assert debt_types.debt_type_label("loan") is None


def test_an_unrecognised_type_is_kept_as_typed_and_trimmed_to_the_column():
    assert debt_types.normalize_debt_type("Bridge note") == "Bridge note"
    long = "A very long description of a debt nobody has a word for"
    assert len(debt_types.normalize_debt_type(long)) == debt_types.CATEGORY_WIDTH
    assert debt_types.debt_type_label("Bridge note") == "Bridge note"


def test_word_boundaries_keep_automatic_from_reading_as_a_vehicle():
    assert debt_types.normalize_debt_type("automatic payment plan") != "vehicle"
    assert debt_types.normalize_debt_type("Auto") == "vehicle"


def test_an_advance_under_either_name_is_asked_the_question():
    assert debt_types.is_advance_type("mca")
    assert debt_types.is_advance_type("Merchant Cash Advance")
    assert debt_types.is_advance_type("revenue_based")
    assert debt_types.is_advance_type("merchant_cash_advance")
    assert not debt_types.is_advance_type("term_loan")
    assert not debt_types.is_advance_type("equipment")


@pytest.mark.parametrize(
    ("typed", "slug"),
    [
        ("merchant_cash_advance", "mca"),
        ("cash_advance", "mca"),
        ("advance", "mca"),
        ("Property tax plan", "tax_payment_plan"),
        ("Revolving credit card", "credit_card"),
        ("SBA EIDL", "sba_loan"),
        ("Term note secured by truck", "vehicle"),
        ("Equipment lease", "equipment"),
    ],
)
def test_the_systems_own_aliases_and_the_specific_before_the_generic(typed, slug):
    assert debt_types.normalize_debt_type(typed) == slug


def test_a_word_that_could_mean_anything_is_kept_as_typed():
    for text in ("Gift card payable", "Auto-pay loan", "Property management fee"):
        assert debt_types.normalize_debt_type(text) == text


# --- reading and writing the row ------------------------------------------------


def _row(**over):
    base = {
        "lender": "Newtek",
        "debt_type": "term_loan",
        "original_amount": "50,000",
        "balance": "32,500",
        "rate": "9.5%",
        "monthly_payment": "1,100",
        "payment_status": "current",
    }
    base.update(over)
    return base


def test_the_two_new_columns_are_on_the_schedule_in_order():
    assert fs.DEBT_COLUMNS.index("mca_modified") == fs.DEBT_COLUMNS.index("collateral") + 1
    assert (
        fs.DEBT_COLUMNS.index("mca_modification_note") == fs.DEBT_COLUMNS.index("mca_modified") + 1
    )
    assert fs.DEBT_COLUMNS[fs.DEBT_COLUMNS.index("mca_modification_note") + 1] == "notes"
    assert fs.DEBT_COLUMNS[-3:] == ("factor_rate", "payment_amount", "payment_frequency")
    assert len(fs.DEBT_COLUMN_LABELS) == len(fs.DEBT_COLUMNS)


def test_a_typed_type_is_stored_as_its_slug():
    (row,) = fs.debt_rows_from_body({"debts": [_row(debt_type="LOC")]})
    assert row["debt_type"] == "line_of_credit"


def test_the_advance_question_travels_with_an_advance():
    (row,) = fs.debt_rows_from_body(
        {
            "debts": [
                _row(
                    debt_type="mca",
                    mca_modified="yes",
                    mca_modification_note="Remittance cut to $300/day in June",
                )
            ]
        }
    )
    assert row["mca_modified"] is True
    assert row["mca_modification_note"] == "Remittance cut to $300/day in June"
    (row,) = fs.debt_rows_from_body({"debts": [_row(debt_type="mca", mca_modified="no")]})
    assert row["mca_modified"] is False and row["mca_modification_note"] is None
    (row,) = fs.debt_rows_from_body({"debts": [_row(debt_type="mca", mca_modified=True)]})
    assert row["mca_modified"] is True


def test_the_advance_answer_is_dropped_when_the_row_is_not_an_advance():
    """A type changed from MCA to a term loan leaves an answer to a question
    nobody asked; it must not be stored or printed."""
    (row,) = fs.debt_rows_from_body(
        {
            "debts": [
                _row(
                    debt_type="term_loan",
                    mca_modified="yes",
                    mca_modification_note="was an advance",
                )
            ]
        }
    )
    assert row["mca_modified"] is None and row["mca_modification_note"] is None


def test_an_unanswered_advance_question_is_unknown_not_no():
    (row,) = fs.debt_rows_from_body({"debts": [_row(debt_type="mca", mca_modified="")]})
    assert row["mca_modified"] is None


def test_the_request_is_not_believed_about_which_rows_it_may_write():
    """A body that marks its own rows read-only must not skip the check."""
    (row,) = fs.debt_rows_from_body({"debts": [_row(editable=False)]})
    assert row["editable"] is True
    target = SimpleNamespace()
    fs._apply_debt_form_fields(target, row)
    assert not hasattr(target, "editable")
    assert target.category == "term_loan"
    assert target.mca_modified is None


def test_editability_is_read_off_the_file_not_the_request():
    profile = _profile()
    theirs = _stored("Desk Note", 50000, 1200, origin="admin", profile_id=profile.id)
    mine = _stored("Mine", 4000, 120, origin="client_form", profile_id=profile.id)
    rows = fs.debt_rows_from_body(
        {
            "debts": [
                {**_row(lender="Desk Note", debt_type="", rate=""), "id": str(theirs.id), "editable": True},
                {**_row(lender="Mine", debt_type="", rate=""), "id": str(mine.id), "editable": False},
                _row(lender="New one", rate=""),
            ]
        }
    )
    fs.mark_foreign_rows(rows, [theirs, mine], origin="client_form")
    assert [row["editable"] for row in rows] == [False, True, True]
    problems = fs.debt_row_problems(rows)
    assert [p["lender"] for p in problems] == ["Mine", "New one"]


@pytest.mark.asyncio
async def test_debt_submit_problems_skips_only_another_sources_rows():
    profile = _profile()
    theirs = _stored("Desk Note", 50000, 1200, origin="admin", profile_id=profile.id)
    db = _DB([theirs], profile_id=profile.id)
    rows = fs.debt_rows_from_body(
        {"debts": [{**_row(lender="Desk Note", debt_type="", rate=""), "id": str(theirs.id)}, _row(rate="")]}
    )
    problems = await fs.debt_submit_problems(db, profile, rows, origin="client_form")
    assert problems == [{"index": 2, "lender": "Newtek", "missing": ["rate"]}]


@pytest.mark.asyncio
async def test_the_advance_answer_round_trips_through_the_file():
    profile = _profile()
    db = _DB([], profile_id=profile.id)
    rows = fs.debt_rows_from_body(
        {
            "debts": [
                _row(
                    lender="Fundbox",
                    debt_type="Merchant cash advance",
                    mca_modified="yes",
                    mca_modification_note="Reduced to 10% of sales",
                )
            ]
        }
    )
    await fs.save_debt_rows(db, profile, rows, origin="client_form")
    (stored,) = db.rows
    assert stored.category == "mca"
    assert stored.mca_modified is True
    assert stored.mca_modification_note == "Reduced to 10% of sales"

    body = await fs.debt_body_for_profile(db, profile, origin="client_form")
    (seeded,) = body["debts"]
    assert seeded["debt_type"] == "mca"
    assert seeded["mca_modified"] == "yes"
    assert seeded["mca_modification_note"] == "Reduced to 10% of sales"


@pytest.mark.asyncio
async def test_a_legacy_typed_category_seeds_as_the_slug_the_list_shows():
    profile = _profile()
    legacy = _stored("Fifth Third", 1000, 50, origin="admin", profile_id=profile.id, category="LOC")
    legacy.mca_modified = None
    legacy.mca_modification_note = None
    db = _DB([legacy], profile_id=profile.id)
    body = await fs.debt_body_for_profile(db, profile, origin="admin")
    assert body["debts"][0]["debt_type"] == "line_of_credit"
    assert body["debts"][0]["mca_modified"] == ""


def test_the_key_facts_carry_the_rest_of_the_row_beside_the_old_keys():
    rows = fs.debt_rows_from_body(
        {
            "debts": [
                _row(
                    debt_type="mca",
                    originated_on="2025-03-01",
                    maturity_on="2026-03-01",
                    secured="unsecured",
                    collateral="",
                    mca_modified="yes",
                    mca_modification_note="Lower daily pull",
                )
            ]
        }
    )
    facts = fs.debt_key_facts(rows)
    (debt,) = facts["debts"]
    # The keys the analyzer and the readers already use, unchanged.
    assert debt["lender"] == "Newtek"
    assert debt["current_balance"] == 32500.0 and debt["monthly_payment"] == 1100.0
    assert debt["original_amount"] == 50000.0 and debt["maturity_date"] == "2026-03-01"
    # And the rest of what the PDF shows.
    assert debt["debt_type"] == "mca" and debt["debt_type_label"] == "Merchant cash advance (MCA)"
    assert debt["interest_rate"] == 9.5 and debt["payment_status"] == "current"
    assert debt["originated_date"] == "2025-03-01" and debt["secured"] == "unsecured"
    assert debt["mca_modified"] is True and debt["mca_modification_note"] == "Lower daily pull"
    assert facts["total_monthly_debt_service"] == 1100.0
    assert facts["total_outstanding_balance"] == 32500.0


# --- the completeness check -------------------------------------------------------


def test_a_complete_row_has_no_problems():
    rows = fs.debt_rows_from_body({"debts": [_row()]})
    assert fs.debt_row_problems(rows) == []


def test_the_owners_required_fields_are_exactly_these():
    assert fs.REQUIRED_DEBT_FIELDS == (
        "lender",
        "debt_type",
        "original_amount",
        "balance",
        "rate",
        "monthly_payment",
        "payment_status",
    )
    for optional in ("originated_on", "maturity_on", "secured", "collateral", "notes"):
        assert optional not in fs.REQUIRED_DEBT_FIELDS


def test_each_missing_required_answer_is_named():
    rows = fs.debt_rows_from_body(
        {
            "debts": [
                _row(debt_type="", rate="", payment_status=""),
                _row(lender="", original_amount=""),
            ]
        }
    )
    problems = fs.debt_row_problems(rows)
    assert problems == [
        {"index": 1, "lender": "Newtek", "missing": ["debt_type", "rate", "payment_status"]},
        {"index": 2, "lender": None, "missing": ["lender", "original_amount"]},
    ]
    text = fs.describe_debt_problems(problems)
    assert text == (
        "Obligation 1 (Newtek) still needs: type of debt, interest rate, current or delinquent. "
        "Obligation 2 still needs: lender name, original amount."
    )


def test_a_zero_is_an_answer_and_a_blank_is_not():
    rows = fs.debt_rows_from_body({"debts": [_row(rate="0", balance="0")]})
    assert fs.debt_row_problems(rows) == []


def test_an_advance_must_also_say_whether_it_was_modified():
    rows = fs.debt_rows_from_body({"debts": [_row(debt_type="mca")]})
    assert fs.debt_row_problems(rows) == [
        {"index": 1, "lender": "Newtek", "missing": ["mca_modified"]}
    ]
    assert "whether the advance terms or payments were modified" in fs.describe_debt_problems(
        fs.debt_row_problems(rows)
    )
    rows = fs.debt_rows_from_body({"debts": [_row(debt_type="mca", mca_modified="no")]})
    assert fs.debt_row_problems(rows) == []


def test_a_term_loan_is_not_asked_the_advance_question():
    rows = fs.debt_rows_from_body({"debts": [_row(debt_type="term_loan")]})
    assert fs.debt_row_problems(rows) == []


def test_a_row_the_saver_cannot_write_does_not_hold_up_the_submit():
    """A desk row shown to the borrower for context has no type or rate; the
    borrower has no way to complete it and must not be refused for it."""
    rows = fs.debt_rows_from_body({"debts": [_row(debt_type="", rate=""), _row()]})
    rows[0]["editable"] = False
    assert fs.debt_row_problems(rows) == []


def test_a_figure_that_is_not_a_number_is_unknown_not_nought():
    rows = fs.debt_rows_from_body({"debts": [_row(monthly_payment="TBD", balance="n/a")]})
    assert rows[0]["monthly_payment"] is None and rows[0]["balance"] is None
    assert fs.debt_row_problems(rows) == [{"index": 1, "lender": "Newtek", "missing": ["balance", "monthly_payment"]}]
    assert fs._money_or_none("(500)") == -500 and fs._money_or_none("$1,250.50") == Decimal("1250.50")
    assert fs._money_or_none(0) == 0 and fs._money_or_none("") is None


def test_a_numeric_zero_rate_or_original_amount_is_an_answer_too():
    """The modal sends numbers; 0 off a 0% promotion is not a blank."""
    rows = fs.debt_rows_from_body({"debts": [{**_row(), "rate": 0.0, "original_amount": 0}]})
    assert rows[0]["rate"] == 0.0 and rows[0]["original_amount"] == 0
    assert fs.debt_row_problems(rows) == []


def test_a_rate_past_the_column_is_unknown_rather_than_an_overflow():
    rows = fs.debt_rows_from_body({"debts": [_row(rate="12000")]})
    assert rows[0]["rate"] is None
    assert fs.debt_row_problems(rows) == [{"index": 1, "lender": "Newtek", "missing": ["rate"]}]


def test_a_line_with_only_a_type_or_an_amount_is_kept_and_checked():
    """It used to be dropped (and so never checked) unless it had a lender,
    a balance or a payment; what someone typed is not thrown away."""
    rows = fs.debt_rows_from_body({"debts": [{"debt_type": "mca", "original_amount": "50,000"}, {}]})
    assert len(rows) == 1 and rows[0]["lender"] == "Unnamed lender"
    (problem,) = fs.debt_row_problems(rows)
    assert "lender" in problem["missing"] and problem["lender"] is None


@pytest.mark.asyncio
async def test_a_stored_zero_seeds_as_zero_not_blank():
    profile = _profile()
    row = _stored("Promo", 0, 0, origin="admin", profile_id=profile.id, rate=0, original_amount=0)
    row.mca_modified = None
    row.mca_modification_note = None
    db = _DB([row], profile_id=profile.id)
    body = await fs.debt_body_for_profile(db, profile, origin="admin")
    assert body["debts"][0]["rate"] == "0" and body["debts"][0]["original_amount"] == "0"
    assert body["debts"][0]["balance"] == "0"


@pytest.mark.asyncio
async def test_a_removed_obligation_is_dismissed_not_deleted():
    """dos_debts is a retention-guarded table: a physical delete trips the 0239
    trigger and 500s the borrower's link. The row is retired instead, which
    every schedule reader already filters out."""
    profile = _profile()
    gone = _stored("Old Note", 1000, 50, origin="client_form", profile_id=profile.id)
    kept = _stored("Kept", 2000, 80, origin="client_form", profile_id=profile.id)
    db = _DB([gone, kept], profile_id=profile.id)
    rows = fs.debt_rows_from_body({"debts": [{**_row(lender="Kept"), "id": str(kept.id)}]})
    await fs.save_debt_rows(db, profile, rows, origin="client_form")
    assert gone in db.rows and gone.status == "dismissed" and kept.status == "active"
    assert "db.delete" not in inspect.getsource(fs.save_debt_rows)


def test_the_dates_and_collateral_may_stay_blank():
    rows = fs.debt_rows_from_body(
        {"debts": [_row(originated_on="", maturity_on="", secured="", collateral="")]}
    )
    assert fs.debt_row_problems(rows) == []


def test_nothing_sent_and_an_empty_schedule_have_no_problems():
    assert fs.debt_row_problems(None) == []
    assert fs.debt_row_problems([]) == []


# --- the PDF -------------------------------------------------------------------


def test_the_pdf_prints_the_type_label_us_dates_and_the_advance_answer():
    rows = fs.debt_rows_from_body(
        {
            "debts": [
                _row(
                    lender="Fundbox",
                    debt_type="mca",
                    originated_on="2025-03-01",
                    maturity_on="2026-03-15",
                    secured="unsecured",
                    collateral="Future receivables",
                    mca_modified="yes",
                    mca_modification_note="Remittance cut to $300/day",
                    notes="Payoff letter requested",
                ),
                _row(lender="Chase", debt_type="LOC"),
            ]
        }
    )
    facts = fs.debt_key_facts(rows)
    html = build_debt_schedule_html(
        business_name="Acme Plumbing LLC",
        rows=rows,
        total_balance=facts["total_outstanding_balance"],
        total_monthly=facts["total_monthly_debt_service"],
    )
    assert "Merchant cash advance (MCA)" in html and "Line of credit" in html
    assert ">mca<" not in html and "line_of_credit" not in html
    # Numbered the way the owner's list and the completeness message number them.
    assert "<th>#</th>" in html and "<td class='num'>1</td>" in html and "<td class='num'>2</td>" in html
    assert "03/01/2025" in html and "03/15/2026" in html and "2025-03-01" not in html
    assert (
        "Modified by the funder for lower sales or revenue: Yes &mdash; Remittance cut to $300/day"
        in html
    )
    assert "Note: Payoff letter requested" in html
    assert "Current" in html and "Unsecured" in html and "Future receivables" in html
    # The term-style row gets no advance line.
    assert html.count("Modified by the funder") == 1


def test_an_advance_that_was_not_modified_says_so_and_one_never_asked_says_not_stated():
    rows = fs.debt_rows_from_body({"debts": [_row(debt_type="mca", mca_modified="no")]})
    html = build_debt_schedule_html(business_name="x", rows=rows, total_balance=0, total_monthly=0)
    assert "Modified by the funder for lower sales or revenue: No" in html
    stale = [{**rows[0], "mca_modified": None}]
    html = build_debt_schedule_html(business_name="x", rows=stale, total_balance=0, total_monthly=0)
    assert "Modified by the funder for lower sales or revenue: not stated" in html


def test_the_narrow_three_tuple_table_still_renders_for_the_advance_terms_form():
    html = build_debt_schedule_html(
        business_name="x",
        debts=[("Funder A", 1000.0, 250.0)],
        total_balance=1000.0,
        total_monthly=250.0,
    )
    assert "Funder A" in html and "Modified by the funder" not in html


def test_the_html_builder_needs_no_weasyprint_and_a_date_object_prints_too():
    from app.services.dealer_forms_pdf import _date_cell

    assert _date_cell(date(2026, 1, 9)) == "01/09/2026"
    assert _date_cell("2026-01-09") == "01/09/2026"
    assert _date_cell("") == "&mdash;"
    assert _date_cell("next spring") == "next spring"


# --- the worksheet and the workbook ------------------------------------------------


def test_the_grid_offers_the_type_list_and_the_yes_no_as_labelled_choices():
    sheet = sheet_layout.layout("debt_schedule", debt_rows=["r1"])
    data = next(row for row in sheet.rows if row.kind == "data")
    by_key = {cell.key.split(".", 1)[1]: cell for cell in data.cells if cell.key}
    assert by_key["debt_type"].type == "select"
    assert by_key["debt_type"].options == debt_types.DEBT_TYPE_SLUGS
    assert dict(by_key["debt_type"].option_labels)["mca"] == "Merchant cash advance (MCA)"
    assert by_key["mca_modified"].type == "select"
    assert by_key["mca_modified"].options == ("yes", "no")
    assert by_key["mca_modification_note"].type == "text"
    assert by_key["secured"].option_labels is None


def test_the_grid_payload_carries_the_labels_beside_the_options():
    from app.services import sheets

    sheet = sheet_layout.layout("debt_schedule", debt_rows=["r1"])
    data = next(row for row in sheet.rows if row.kind == "data")
    cell = next(c for c in data.cells if c.key == "r1.debt_type")
    payload = sheets._cell_payload(cell)
    assert payload["options"] == list(debt_types.DEBT_TYPE_SLUGS)
    assert payload["option_labels"]["term_loan"] == "Term loan"


def test_the_workbook_lists_show_labels_a_person_reads_and_fit_excels_limit():
    """Excel strips an inline list formula past 255 characters on open; the
    type list runs long, so it lives down a hidden column and the validation
    points at the range."""
    from io import BytesIO

    from openpyxl import load_workbook

    from app.services.financial_templates import template_for_requirement
    from app.services.financial_templates_xlsx import build_packet_workbook, build_workbook

    books = (
        build_workbook("debt_schedule"),
        build_packet_workbook(),
        template_for_requirement("business_debt_schedule").content,
    )
    for content in books:
        wb = load_workbook(BytesIO(content))
        checked = 0
        for ws in wb.worksheets:
            for dv in ws.data_validations.dataValidation:
                assert len(dv.formula1) <= 255, (ws.title, dv.formula1[:60])
                assert "term_loan" not in dv.formula1, "slugs reached a pick-list"
                if "!" in dv.formula1:
                    sheet_name, _, ref = dv.formula1.lstrip("=").rpartition("!")
                    target = wb[sheet_name.strip("'")]
                    cells = [c.value for row in target[ref.replace("$", "")] for c in row]
                    assert "Merchant cash advance (MCA)" in cells
                    checked += 1
        assert checked >= 1


# --- the on-screen modal's row --------------------------------------------------------


def test_the_modal_row_reads_like_the_links_row():
    from app.routers.dealer_ai_intake import (
        DealerDebtScheduleRow,
        DealerDebtScheduleSubmission,
        _debt_schedule_rows,
    )

    payload = DealerDebtScheduleSubmission(
        business_name="Acme",
        acknowledgment=True,
        debts=[
            DealerDebtScheduleRow(
                lender="Fundbox",
                balance=12000,
                monthly_payment=900,
                debt_type="mca",
                original_amount=20000,
                rate=1.3,
                payment_status="current",
                mca_modified=True,
                mca_modification_note="Daily pull reduced",
            ),
            # The three fields the old modal collected still parse — and now
            # fail the completeness check instead of filing a half-empty PDF.
            DealerDebtScheduleRow(lender="Chase", balance=5000, monthly_payment=200),
        ],
    )
    rows = _debt_schedule_rows(payload)
    assert rows[0]["debt_type"] == "mca" and rows[0]["mca_modified"] is True
    assert rows[0]["original_amount"] == 20000 and rows[0]["rate"] == 1.3
    problems = fs.debt_row_problems(rows)
    assert [p["index"] for p in problems] == [2]
    assert set(problems[0]["missing"]) == {"debt_type", "original_amount", "rate", "payment_status"}


# --- adopting an uploaded schedule keeps every answer the AI read -------------


def _adopted(*lines: dict) -> list[dict]:
    from app.services.financial_statements import debt_rows_from_body, document_debts_as_body

    rows = debt_rows_from_body(document_debts_as_body({"debts": list(lines)}))
    assert rows is not None
    return rows


def test_adopting_reads_the_older_extraction_shape_and_keeps_delinquent() -> None:
    late, ok, unknown = _adopted(
        {"lender": "Ally", "current": False, "rate_pct": 11.89, "secured_by": "2025 RAM 1500", "monthly_payment": 1090.38},
        {"lender": "Stellantis", "current": True, "rate_pct": 10.11, "monthly_payment": 1166.74},
        {"lender": "Chase", "current": None, "monthly_payment": 500},
    )
    # A delinquent debt must arrive delinquent. It used to arrive blank, which
    # reads as "nobody asked" instead of "behind on payments".
    assert late["payment_status"] == "delinquent"
    assert ok["payment_status"] == "current"
    assert unknown["payment_status"] is None
    assert late["secured"] == "secured" and late["collateral"] == "2025 RAM 1500"
    assert ok["secured"] is None
    assert late["rate"] == 11.89


def test_adopting_reads_the_current_extraction_shape() -> None:
    advance, term = _adopted(
        {
            "lender": "Fox Funding", "debt_type": "mca", "interest_rate": 0, "payment_status": "delinquent",
            "secured": "unsecured", "mca_modified": True, "monthly_payment": 4200, "current_balance": 31000,
            "maturity_date": "2027-03-01", "origination_date": "2026-03-01",
        },
        {"lender": "First Bank", "debt_type": "Term loan", "interest_rate": "7.25%", "payment_status": "current",
         "secured": "secured", "mca_modified": True, "monthly_payment": 2100},
    )
    assert advance["debt_type"] == "mca" and advance["payment_status"] == "delinquent"
    assert advance["secured"] == "unsecured"
    # A 0% rate is an answer, not a gap.
    assert advance["rate"] == 0
    assert advance["mca_modified"] is True
    assert advance["maturity_on"] is not None and advance["originated_on"] is not None
    assert term["rate"] == 7.25 and term["payment_status"] == "current" and term["secured"] == "secured"
    # The modification question belongs to advances; the cleaner drops it here.
    assert term["mca_modified"] is None


def test_adopting_turns_free_text_status_and_flags_into_the_form_vocabulary() -> None:
    past_due, related, boolean_secured, yes_secured, no_secured = _adopted(
        {"lender": "A", "payment_status": "30 days past due"},
        {"lender": "B", "payment_status": "related party"},
        {"lender": "C", "secured": False},
        {"lender": "D", "secured": "yes"},
        {"lender": "E", "secured": "no"},
    )
    assert past_due["payment_status"] == "delinquent"
    assert related["payment_status"] is None
    assert boolean_secured["secured"] == "unsecured"
    assert yes_secured["secured"] == "secured" and no_secured["secured"] == "unsecured"
