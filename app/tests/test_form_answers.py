"""A filled-in form read back in short: headline, answered lines, the file."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.routers import application_profiles as routes
from app.services import application_programs, drafted_forms, form_answers, pfs_schema
from app.services import business_statement_schema as bss

NOW = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)


def _pfs_statement(**over):
    body = pfs_schema.empty_body()
    body["applicant"]["name"] = "Terrell Parker"
    body["as_of"] = "2026-10-31"
    body["assets"].update({"cash_on_hand": "$25,000", "savings_accounts": 10000, "real_estate": "250,000", "automobiles": "(500)"})
    body["liabilities"].update({"mortgages_on_real_estate": "180,000", "installment_auto": 0})
    body["income"]["salary"] = "90,000"
    body["schedules"]["real_estate"] = [
        {"property_address": "1 Main St", "type": "Residence", "present_market_value": "250000", "mortgage_balance": "180000", "date_purchased": "2019-03-01"},
        {"property_address": "", "type": ""},
    ]
    body["notes"] = "Rental is leased through 2027."
    row = {"body": body, "status": "submitted", "statement_date": None, "submitted_by_user_id": None, "updated_at": NOW, "bucket_file_id": None}
    row.update(over)
    return SimpleNamespace(**row)


def _debt(**over):
    row = {
        "lender": "Kapfi Equity LLC",
        "category": "mca",
        "original_amount": Decimal("60000"),
        "balance": Decimal("41250.50"),
        "rate": None,
        "monthly_payment": Decimal("3100"),
        "originated_on": date(2026, 4, 2),
        "maturity_on": None,
        "secured": "unsecured",
        "payment_status": "current",
        "collateral": None,
        "mca_modified": True,
        "mca_modification_note": "Cut to $2,800 in September",
        "count_in_dscr": True,
        "notes": None,
        "origin": "client_form",
        "updated_at": NOW,
    }
    row.update(over)
    return SimpleNamespace(**row)


# --- values -------------------------------------------------------------------


def test_money_reads_what_people_type() -> None:
    assert form_answers.format_money("$1,250") == "$1,250"
    assert form_answers.format_money("(500)") == "-$500"
    assert form_answers.format_money(1234.5) == "$1,234.50"
    assert form_answers.format_money(0) == "$0"
    assert form_answers.format_money("") is None
    assert form_answers.format_money(None) is None


def test_dates_and_rates_read_as_words() -> None:
    assert form_answers.format_date("2026-11-05") == "Nov 5, 2026"
    assert form_answers.format_date(date(2026, 1, 9)) == "Jan 9, 2026"
    assert form_answers.format_date("sometime in June") == "sometime in June"
    assert form_answers.format_date("") is None
    assert form_answers.format_percent("26.990") == "26.99%"
    assert form_answers.format_percent(10) == "10%"
    assert form_answers.format_percent(None) is None


# --- personal financial statement ------------------------------------------------


def test_a_filled_pfs_shows_the_headline_and_only_the_answered_lines() -> None:
    answers = form_answers.pfs_answers_from_statement(_pfs_statement())

    assert answers["source"] == "filled" and answers["figures_from"] == "form" and answers["draft"] is False
    assert answers["who"] == "Terrell Parker" and answers["as_of"] == "Oct 31, 2026"
    assert answers["filled_by"] == "the borrower"
    headline = {line["label"]: line["value"] for line in answers["headline"]}
    assert headline == {
        "Net worth": "$104,500",
        "Total assets": "$284,500",
        "Total liabilities": "$180,000",
        "Liquid assets": "$35,000",
    }
    assets = next(section for section in answers["sections"] if section["key"] == "assets")
    assert [(row["label"], row["value"]) for row in assets["rows"]] == [
        ("Cash on hand and in banks", "$25,000"),
        ("Savings accounts", "$10,000"),
        ("Real estate", "$250,000"),
        ("Automobiles — present value", "-$500"),
    ]
    assert assets["rows"][0]["note"] == "Counts as liquid" and assets["rows"][2]["note"] is None
    assert assets["total"]["value"] == "$284,500" and assets["left_blank"] == 6
    liabilities = next(section for section in answers["sections"] if section["key"] == "liabilities")
    # A zero that was typed is an answer; a line left empty is not.
    assert [row["label"] for row in liabilities["rows"]] == ["Installment account (auto)", "Mortgages on real estate"]
    assert [section["key"] for section in answers["sections"]] == ["assets", "liabilities", "income"]
    assert answers["notes"] == "Rental is leased through 2027."


def test_a_filled_pfs_lists_each_schedule_that_has_a_row_and_drops_blank_rows() -> None:
    answers = form_answers.pfs_answers_from_statement(_pfs_statement())

    assert [table["key"] for table in answers["tables"]] == ["real_estate"]
    table = answers["tables"][0]
    assert table["columns"][0] == "Property address"
    assert table["rows"] == [["1 Main St", "Residence", "Mar 1, 2019", "", "$250,000", "$180,000", "", ""]]


def test_a_pfs_filed_by_the_desk_says_so_and_a_draft_is_not_called_filled() -> None:
    desk = form_answers.pfs_answers_from_statement(_pfs_statement(submitted_by_user_id=uuid4()))
    assert desk["filled_by"] == "the desk"

    draft = form_answers.pfs_answers_from_statement(_pfs_statement(status="draft"))
    assert draft["source"] == "none" and draft["draft"] is True and draft["filled_by"] is None
    assert draft["headline"], "a draft still shows what has been typed"


def test_an_opened_but_empty_pfs_says_nothing_was_entered() -> None:
    empty = form_answers.pfs_answers_from_statement(_pfs_statement(body=pfs_schema.empty_body(), status="draft"))

    assert empty["headline"] == [] and empty["sections"] == []
    assert empty["empty_reason"] == "The form was opened but nothing has been entered yet."


def test_uploaded_statements_are_one_block_per_person_and_never_summed() -> None:
    one = form_answers.pfs_answers_from_uploads(
        [{"statement_date": "2026-09-30", "total_assets": 500000, "total_liabilities": 200000, "net_worth": 300000, "liquid_assets": 40000}]
    )
    assert one["source"] == "uploaded" and one["figures_from"] == "document" and one["filled_by"] == "a document we read"
    assert one["as_of"] == "Sep 30, 2026"
    assert [line["label"] for line in one["headline"]] == ["Net worth", "Total assets", "Total liabilities", "Liquid assets"]

    two = form_answers.pfs_answers_from_uploads(
        [
            {"statement_date": None, "total_assets": 1, "total_liabilities": None, "net_worth": 1, "liquid_assets": None},
            {"statement_date": "2026-01-01", "total_assets": 2, "total_liabilities": None, "net_worth": 2, "liquid_assets": None},
        ]
    )
    assert two["headline"] == [] and [section["title"] for section in two["sections"]] == ["Statement 1", "Statement 2 · as of Jan 1, 2026"]

    unread = form_answers.pfs_answers_from_uploads([{"statement_date": None, "total_assets": None, "total_liabilities": None, "net_worth": None, "liquid_assets": None}])
    assert unread["source"] == "uploaded" and "no figures could be read" in unread["empty_reason"]


# --- business debt schedule --------------------------------------------------------


def test_the_debt_schedule_leads_with_totals_and_a_glance_table() -> None:
    answers = form_answers.debt_answers_from_rows([_debt(), _debt(lender="First Home Bank SBA", category="sba_loan", balance=Decimal("100000"), monthly_payment=Decimal("1400"), rate=Decimal("10.250"), origin="admin", mca_modified=None)])

    assert answers["source"] == "filled" and answers["filled_by"] == "the borrower and the desk"
    assert [(line["label"], line["value"]) for line in answers["headline"]] == [
        ("Obligations", "2"),
        ("Monthly payments", "$4,500"),
        ("Total owed", "$141,250.50"),
    ]
    glance = answers["tables"][0]
    assert glance["columns"] == ["Lender", "Type", "Balance", "Monthly", "Rate", "Status"]
    assert glance["rows"][0] == ["Kapfi Equity LLC", "Merchant cash advance (MCA)", "$41,250.50", "$3,100", "", "Current"]
    assert glance["rows"][1][4] == "10.25%"
    assert glance["total_row"] == ["Total", "", "$141,250.50", "$4,500", "", ""]


def test_each_obligation_has_a_folded_detail_card_and_the_advance_question_only_on_an_advance() -> None:
    answers = form_answers.debt_answers_from_rows(
        [_debt(), _debt(lender="Bank", category="term_loan", mca_modified=True, count_in_dscr=False, notes="Paid early")]
    )

    advance, term = answers["sections"]
    assert advance["collapsed"] and advance["title"] == "Kapfi Equity LLC"
    labels = [row["label"] for row in advance["rows"]]
    assert "Funder lowered the payment because sales fell" in labels and "What changed" in labels
    assert dict((row["label"], row["value"]) for row in advance["rows"])["Entered by"] == "the borrower"
    term_labels = {row["label"]: row["value"] for row in term["rows"]}
    assert "Funder lowered the payment because sales fell" not in term_labels
    assert term_labels["Counted in DSCR"] == "No, left out of the DSCR" and term_labels["Notes"] == "Paid early"
    assert term_labels["Type"] == "Term loan"


def test_an_empty_schedule_and_an_upload_with_nothing_readable_say_so() -> None:
    assert form_answers.debt_answers_from_rows([])["empty_reason"] == "No obligations are on the schedule yet."
    unread = form_answers.debt_answers_from_upload({"debts": [], "total_monthly_debt_service": None, "total_outstanding_balance": None})
    assert unread["source"] == "uploaded" and "no obligations could be read" in unread["empty_reason"]


def test_an_uploaded_schedule_lists_what_the_ai_read_with_the_documents_own_totals() -> None:
    answers = form_answers.debt_answers_from_upload(
        {
            "debts": [{"lender": "OnDeck", "debt_type": "term_loan", "current_balance": 12000, "monthly_payment": 900, "interest_rate": 18}],
            "total_monthly_debt_service": 900,
            "total_outstanding_balance": 12000,
        }
    )

    assert answers["figures_from"] == "document"
    assert [(line["label"], line["value"]) for line in answers["headline"]] == [("Obligations", "1"), ("Monthly payments", "$900"), ("Total owed", "$12,000")]
    assert answers["tables"][0]["rows"] == [["OnDeck", "Term loan", "$12,000", "$900", "18%", ""]]


# --- profit and loss, balance sheet ----------------------------------------------------


def test_a_filled_profit_and_loss_shows_the_answered_lines_and_its_computed_headline() -> None:
    body = bss.pl_empty_body()
    body["header"].update({"business_name": "Earthly Inc", "period_start": "2026-01-01", "period_end": "2026-06-30"})
    body["sections"]["revenue"].update({"gross_revenue": "600,000", "cost_of_goods_sold": "200,000"})
    body["sections"]["operating_expenses"].update({"payroll": "150,000", "depreciation_and_amortization": "10,000"})
    statement = SimpleNamespace(body=body, status="submitted", submitted_by_user_id=None, updated_at=NOW)

    answers = form_answers.business_answers_from_statement("p_and_l", statement)

    assert answers["who"] == "Earthly Inc" and answers["as_of"] == "Jan–Jun 2026"
    revenue = next(section for section in answers["sections"] if section["key"] == "revenue")
    assert [row["value"] for row in revenue["rows"]] == ["$600,000", "$200,000"]
    assert revenue["total"]["label"] == "Gross profit" and revenue["total"]["value"] == "$400,000"
    labels = [line["label"] for line in answers["headline"]]
    assert "Net income" in labels and "EBITDA (memo)" in labels


def test_a_balance_sheet_headline_uses_ratios_as_ratios() -> None:
    body = bss.SCHEMA_FOR["balance_sheet"].empty_body()
    body["header"]["as_of_date"] = "2026-06-30"
    body["sections"]["current_assets"].update({"cash_in_bank": "50,000"})
    statement = SimpleNamespace(body=body, status="submitted", submitted_by_user_id=uuid4(), updated_at=NOW)

    answers = form_answers.business_answers_from_statement("balance_sheet", statement)

    assert answers["as_of"] == "as of 2026-06-30" and answers["filled_by"] == "the desk"
    assert answers["headline"] and all("$" in line["value"] for line in answers["headline"])


def test_an_uploaded_statement_shows_the_figures_the_ai_read() -> None:
    answers = form_answers.business_answers_from_upload(
        "p_and_l", {"gross_revenue": 900000, "net_income": 75000, "business_name": "Earthly Inc", "period_start": "2026-01-01", "period_end": "2026-06-30"}
    )
    assert answers["figures_from"] == "document" and answers["as_of"] == "Jan 1, 2026 to Jun 30, 2026"
    assert [line["label"] for line in answers["headline"]] == ["Gross revenue", "Net income"]
    assert form_answers.business_answers_from_upload("balance_sheet", {})["empty_reason"]


# --- where a file came from ------------------------------------------------------------


def test_a_drafted_form_is_told_apart_from_an_upload_and_a_signed_certificate() -> None:
    plain = SimpleNamespace(source_kind="client_room", source_detail=None)
    assert application_programs.evidence_origin(plain, None) == ("uploaded", None)
    assert application_programs.evidence_origin(plain, SimpleNamespace(provider="openai", classification="debt_schedule")) == ("uploaded", None)
    drafted = SimpleNamespace(provider="drafted_form", classification="personal_financial_statement")
    assert application_programs.evidence_origin(plain, drafted) == ("filled_form", "pfs")
    assert application_programs.evidence_origin(plain, SimpleNamespace(provider="drafted_form", classification="debt_schedule")) == ("filled_form", "debt_schedule")
    assert application_programs.evidence_origin(plain, SimpleNamespace(provider="drafted_form", classification="current_p_and_l")) == ("filled_form", "p_and_l")
    signed = SimpleNamespace(source_kind="generated", source_detail="signed_document")
    assert application_programs.evidence_origin(signed, None) == ("signed", None)


def test_the_provider_word_matches_the_one_the_drafting_code_writes() -> None:
    assert application_programs._DRAFTED_FORM_PROVIDER == drafted_forms.DRAFTED_FORM_PROVIDER


# --- the route -----------------------------------------------------------------------------


def test_the_answers_route_is_registered_for_staff_reading() -> None:
    contract = {(route.path, method) for route in routes.router.routes for method in getattr(route, "methods", set())}

    assert any(path.endswith("/{profile_id}/financial-forms/{kind}/answers") and method == "GET" for path, method in contract)


@pytest.fixture
def route_stubs(monkeypatch):
    profile = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(routes, "_require_statement_staff", Mock())
    monkeypatch.setattr(routes, "_form_answer_files", AsyncMock(return_value=[]))
    return profile


async def test_the_route_prefers_a_submitted_statement_over_an_upload(monkeypatch, route_stubs) -> None:
    monkeypatch.setattr(routes, "_requested_slot", AsyncMock(return_value=SimpleNamespace(status="uploaded")))
    monkeypatch.setattr(routes.financial_statements, "latest_for_profile", AsyncMock(return_value=_pfs_statement()))
    slot_reader = AsyncMock()
    monkeypatch.setattr(routes, "_slot_analyses", slot_reader)

    answers = await routes.financial_form_answers(route_stubs.id, "pfs", SimpleNamespace(), SimpleNamespace())

    assert answers.source == "filled" and answers.figures_from == "form"
    slot_reader.assert_not_awaited()


async def test_the_route_falls_back_to_what_the_ai_read_off_an_upload(monkeypatch, route_stubs) -> None:
    monkeypatch.setattr(routes, "_requested_slot", AsyncMock(return_value=SimpleNamespace(status="uploaded")))
    monkeypatch.setattr(routes.financial_statements, "latest_for_profile", AsyncMock(return_value=None))
    analyses = [{"classification": "personal_financial_statement", "key_facts": {"net_worth": 300000, "total_assets": 500000}}]
    monkeypatch.setattr(routes, "_slot_analyses", AsyncMock(return_value=(analyses, False)))

    answers = await routes.financial_form_answers(route_stubs.id, "pfs", SimpleNamespace(), SimpleNamespace())

    assert answers.source == "uploaded" and answers.figures_from == "document"
    assert answers.headline[0].value == "$300,000"


async def test_the_route_says_when_nothing_is_there_and_refuses_an_unknown_form(monkeypatch, route_stubs) -> None:
    monkeypatch.setattr(routes, "_requested_slot", AsyncMock(return_value=None))
    monkeypatch.setattr(routes.financial_statements, "latest_for_profile", AsyncMock(return_value=None))

    answers = await routes.financial_form_answers(route_stubs.id, "pfs", SimpleNamespace(), SimpleNamespace())
    assert answers.source == "none" and answers.empty_reason == "Nothing has been filled in or uploaded yet."

    with pytest.raises(routes.HTTPException) as refusal:
        await routes.financial_form_answers(route_stubs.id, "tax_return", SimpleNamespace(), SimpleNamespace())
    assert refusal.value.status_code == 404
