"""The profit-and-loss and balance-sheet schemas, their arithmetic, the
contracts they serve, and the service that saves and files them.

The one rule these pin above all others: EBITDA reads the `addback` flag, so
"Taxes and licenses" never comes back and "Income taxes" always does — the
single biggest error the owner's one-line "Taxes" invited.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import business_statement_schema as bss
from app.services import business_statements, dealer_forms_pdf, drafted_forms, file_events


def _run(coro):
    return asyncio.run(coro)


def _pl(**lines):
    """A P&L body with lines spread across the sections by key."""
    body = bss.pl_empty_body()
    for section in bss.PL_SECTIONS:
        for row in section.rows:
            if row.key in lines:
                body["sections"][section.key][row.key] = lines.pop(row.key)
    body["header"].update(lines)
    return body


def _bs(**lines):
    body = bss.bs_empty_body()
    for section in bss.BS_SECTIONS:
        for row in section.rows:
            if row.key in lines:
                body["sections"][section.key][row.key] = lines.pop(row.key)
    body["header"].update(lines)
    return body


# ── arithmetic ──────────────────────────────────────────────────────────────


def test_ebitda_reads_the_flags_not_the_labels():
    body = _pl(
        gross_revenue="100000", cost_of_goods_sold="40000",
        interest="1000", depreciation_and_amortization="2000",
        taxes_and_licenses="500", owner_salaries="9000",
        income_taxes="3000", other_income="100",
    )
    totals = bss.pl_totals(body)
    assert totals["gross_profit"] == Decimal("60000")
    assert totals["total_operating_expenses"] == Decimal("12500")
    assert totals["operating_income"] == Decimal("47500")
    assert totals["net_income"] == Decimal("44600")
    # net income + interest + income taxes + D&A; taxes_and_licenses stays out
    assert totals["ebitda"] == Decimal("50600")
    assert totals["addbacks"] == Decimal("6000")
    assert totals["owner_compensation"] == Decimal("9000")


def test_taxes_and_licenses_never_adds_back_and_income_taxes_always_does():
    rows = {row.key: row for section in bss.PL_SECTIONS for row in section.rows}
    assert rows["taxes_and_licenses"].addback is False
    assert rows["income_taxes"].addback is True
    assert {key for key, row in rows.items() if row.addback} == {
        "depreciation_and_amortization", "interest", "income_taxes",
    }
    assert {key for key, row in rows.items() if row.owner_comp} == {"owner_salaries"}


def test_other_income_sits_below_gross_profit_and_net_income_is_unchanged():
    body = _pl(gross_revenue="1000", other_income="50")
    totals = bss.pl_totals(body)
    assert totals["gross_profit"] == Decimal("1000")
    assert totals["net_income"] == Decimal("1050")


def test_owner_lines_are_kept_in_the_owners_order_with_the_two_corrections():
    keys = [row.key for row in bss.PL_SECTIONS[1].rows if not row.text]
    assert keys[:2] == ["supplies", "depreciation_and_amortization"]
    assert keys[10] == "taxes_and_licenses"
    assert keys[-1] == "other"
    assert len(keys) == 19


def test_the_other_expenses_description_is_a_text_row_beside_the_line_it_describes():
    """It used to be written into the body by hand and absent from the schema,
    so `describe()` never served it and nothing rendered it. Now it is a row
    like any other, flagged as words rather than money."""
    rows = bss.PL_SECTIONS[1].rows
    assert [row.key for row in rows][-2:] == ["other", "other_description"]
    description = rows[-1]
    assert description.text is True
    assert not (description.addback or description.contra or description.owner_comp)
    # Seeded by the schema, not by a special case in pl_empty_body.
    assert "other_description" in bss.pl_empty_body()["sections"]["operating_expenses"]
    served = bss.describe("p_and_l")
    opex = next(section for section in served["sections"] if section["key"] == "operating_expenses")
    assert {row["key"]: row["text"] for row in opex["rows"]}["other_description"] is True
    assert sum(row["text"] for row in opex["rows"]) == 1


def test_a_number_typed_in_the_description_row_is_never_summed():
    """Safe before only because `_amount("words") == 0`. A borrower who types
    "500" there must not add $500 to operating expenses, nor to any add-back."""
    body = _pl(gross_revenue="10000", supplies="100", other="50", other_description="500")
    totals = bss.pl_totals(body)
    assert totals["total_operating_expenses"] == Decimal("150")
    assert totals["addbacks"] == Decimal("0")
    assert totals["owner_compensation"] == Decimal("0")
    assert totals["net_income"] == Decimal("9850")


def test_every_section_declares_what_it_is():
    """The browser rolled the balance sheet up by sniffing section keys for
    "liabilit" and "_equity". The role is a fact on the section now."""
    by_key = {section.key: section.role for section in bss.BS_SECTIONS}
    assert by_key == {
        "current_assets": "asset",
        "fixed_assets": "asset",
        "other_assets": "asset",
        "current_liabilities": "liability",
        "long_term_liabilities": "liability",
        "equity": "equity",
    }
    assert {section.key: section.role for section in bss.PL_SECTIONS} == {
        "revenue": "income",
        "operating_expenses": "expense",
        "below_the_line": "other",
    }
    for kind in bss.KINDS:
        served = {section["key"]: section["role"] for section in bss.describe(kind)["sections"]}
        assert served == {section.key: section.role for section in bss.SCHEMA_FOR[kind].sections}


def test_computed_lines_say_how_they_are_shown():
    """A ratio through a currency formatter is "$1.50", which is why the
    ratios and the month count were quietly dropped by the renderer."""
    formats = {item.key: item.format for item in bss.PL_COMPUTED + bss.BS_COMPUTED}
    assert formats["months_covered"] == "count"
    assert formats["current_ratio"] == "ratio"
    assert formats["debt_to_equity"] == "ratio"
    assert all(
        fmt == "money"
        for key, fmt in formats.items()
        if key not in {"months_covered", "current_ratio", "debt_to_equity"}
    )
    for kind in bss.KINDS:
        served = {item["key"]: item["format"] for item in bss.describe(kind)["computed"]}
        assert served == {item.key: item.format for item in bss.SCHEMA_FOR[kind].computed}


@pytest.mark.parametrize(
    ("start", "end", "months"),
    [
        ("2026-01-01", "2026-06-30", 6),
        ("2025-01-01", "2025-12-31", 12),
        ("2026-03-01", "2026-03-31", 1),
        ("2025-11-01", "2026-02-28", 4),
        ("", "2026-06-30", None),
        ("2026-06-30", "2026-01-01", None),
        ("not a date", "2026-06-30", None),
    ],
)
def test_months_covered_is_inclusive_and_none_when_unknown(start, end, months):
    assert bss.months_between(start, end) == months
    assert bss.pl_totals(_pl(period_start=start, period_end=end))["months_covered"] == months


def test_equity_is_implied_when_the_whole_section_is_blank():
    body = _bs(cash_in_bank="1000", accounts_payable="400")
    totals = bss.bs_totals(body)
    assert totals["equity_implied"] is True
    assert totals["total_equity"] == Decimal("600")
    assert totals["imbalance"] == Decimal("0")
    assert totals["balances"] is True
    assert totals["total_liabilities_and_equity"] == Decimal("1000")


def test_typed_equity_is_used_and_the_difference_is_reported():
    body = _bs(cash_in_bank="1000", accounts_payable="400", owner_capital="100")
    totals = bss.bs_totals(body)
    assert totals["equity_implied"] is False
    assert totals["total_equity"] == Decimal("100")
    assert totals["imbalance"] == Decimal("500")
    assert totals["balances"] is False
    # A zero typed on a line counts as typed.
    assert bss.bs_totals(_bs(cash_in_bank="1000", owner_capital="0"))["equity_implied"] is False


def test_the_balance_tolerance_is_the_larger_of_100_and_one_percent():
    small = _bs(cash_in_bank="1000", owner_capital="920")   # off by 80 on $1,000
    assert bss.bs_totals(small)["balances"] is True
    large = _bs(cash_in_bank="1000000", owner_capital="992000")   # off by 8,000 on $1m
    assert bss.bs_totals(large)["balances"] is True
    too_far = _bs(cash_in_bank="1000000", owner_capital="980000")   # off by 20,000
    assert bss.bs_totals(too_far)["balances"] is False


def test_contra_rows_subtract_within_their_section():
    body = _bs(equipment="10000", accumulated_depreciation="2500", owner_capital="9000", owner_draws="1500")
    totals = bss.bs_totals(body)
    assert totals["total_fixed_assets"] == Decimal("7500")
    assert totals["total_equity"] == Decimal("7500")


def test_ratios_return_none_rather_than_divide_by_zero():
    totals = bss.bs_totals(_bs(cash_in_bank="1000"))
    assert totals["current_ratio"] is None
    totals = bss.bs_totals(_bs(cash_in_bank="1000", accounts_payable="2000"))
    assert totals["debt_to_equity"] is None   # equity ≤ 0
    assert totals["current_ratio"] == Decimal("0.50")
    totals = bss.bs_totals(_bs(cash_in_bank="1000", petty_cash="200", accounts_payable="400"))
    assert totals["cash"] == Decimal("1200")
    assert totals["working_capital"] == Decimal("800")
    assert totals["debt_to_equity"] == Decimal("0.50")


def test_money_typed_the_way_people_type_it_is_counted():
    body = _pl(gross_revenue="$1,250,000.50", cost_of_goods_sold=" 250,000 ")
    assert bss.pl_totals(body)["gross_profit"] == Decimal("1000000.50")


# ── the served schema and the key-facts contracts ───────────────────────────


def test_describe_carries_every_key_the_form_renders():
    for kind in bss.KINDS:
        served = bss.describe(kind)
        assert served["kind"] == kind
        assert served["collects_ssn"] is False
        assert served["schema_version"] == bss.SCHEMA_FOR[kind].schema_version
        for field in served["header"]:
            assert set(field) >= {"key", "label", "input"}
            assert field["input"] in {"text", "date", "select"}
            if field["input"] == "select":
                assert field["options"]
        for section in served["sections"]:
            assert set(section) == {"key", "label", "role", "rows", "subtotal"}
            assert section["role"] in {"asset", "liability", "equity", "income", "expense", "other"}
            assert set(section["subtotal"]) == {"key", "label"}
            for row in section["rows"]:
                assert set(row) == {
                    "key", "label", "addback", "contra", "owner_comp", "text", "hint",
                }
        for item in served["computed"]:
            assert set(item) == {"key", "label", "emphasis", "format"}
            assert item["format"] in {"money", "ratio", "count"}
        body = bss.empty_body(kind)
        assert set(body["header"]) == {field["key"] for field in served["header"]}
        assert set(body["sections"]) == {section["key"] for section in served["sections"]}


def test_the_basis_select_and_the_dates_are_never_seeded():
    body = bss.pl_empty_body()
    assert body["header"]["period_start"] is None
    assert body["header"]["period_end"] is None
    assert body["header"]["basis"] is None
    assert bss.bs_empty_body()["header"]["as_of_date"] is None


def test_key_facts_shapes_are_exact_and_prompt_keys_are_a_subset():
    pl = bss.pl_key_facts(_pl(gross_revenue="10"))
    assert tuple(pl) == bss.KEY_FACT_KEYS["p_and_l"]
    assert pl["source_form"] == "qc_pl.v1"
    bs = bss.bs_key_facts(_bs(cash_in_bank="10"))
    assert tuple(bs) == bss.KEY_FACT_KEYS["balance_sheet"]
    assert bs["source_form"] == "qc_bs.v1"
    for kind in bss.KINDS:
        assert set(bss.PROMPT_KEYS[kind]) < set(bss.KEY_FACT_KEYS[kind])
        assert not {"months_covered", "ebitda", "balances", "imbalance", "source_form"} & set(bss.PROMPT_KEYS[kind])


def test_prompt_keys_are_exactly_what_the_typed_blocks_name():
    assert bss.PROMPT_KEYS["p_and_l"] == (
        "business_name", "period_start", "period_end", "basis", "gross_revenue",
        "cost_of_goods_sold", "gross_profit", "other_income", "total_operating_expenses",
        "operating_income", "depreciation_and_amortization", "interest", "income_taxes",
        "taxes_and_licenses", "owner_salaries", "net_income",
    )
    assert bss.PROMPT_KEYS["balance_sheet"] == (
        "business_name", "as_of_date", "basis", "cash", "accounts_receivable", "inventory",
        "total_current_assets", "total_fixed_assets", "total_other_assets", "total_assets",
        "accounts_payable", "current_portion_long_term_debt", "total_current_liabilities",
        "total_long_term_liabilities", "total_liabilities", "total_equity",
    )


def test_prompt_keys_match_the_blocks_bucket_ai_sends_the_model():
    """The extraction slice names the same keys in the prompt. Skipped until it
    lands; fails loudly the day the two drift."""
    from app.services import bucket_ai

    if not hasattr(bucket_ai, "PL_PROMPT_KEYS") or not hasattr(bucket_ai, "BS_PROMPT_KEYS"):
        pytest.skip("bucket_ai has no typed P&L / balance-sheet blocks yet")
    assert tuple(bucket_ai.PL_PROMPT_KEYS) == bss.PROMPT_KEYS["p_and_l"]
    assert tuple(bucket_ai.BS_PROMPT_KEYS) == bss.PROMPT_KEYS["balance_sheet"]


def test_key_facts_carry_dates_as_iso_and_blanks_as_none():
    facts = bss.pl_key_facts(_pl(period_start="2026-01-01", period_end="2026-06-30", basis="cash"))
    assert facts["period_start"] == "2026-01-01"
    assert facts["months_covered"] == 6
    assert facts["basis"] == "cash"
    assert facts["business_name"] is None
    facts = bss.bs_key_facts(_bs(as_of_date="garbage"))
    assert facts["as_of_date"] is None


@pytest.mark.parametrize(
    ("kind", "header", "label"),
    [
        ("p_and_l", {"period_start": "2026-01-01", "period_end": "2026-06-30"}, "Jan–Jun 2026"),
        ("p_and_l", {"period_start": "2025-01-01", "period_end": "2025-12-31"}, "FY 2025"),
        ("p_and_l", {"period_start": "2026-03-01", "period_end": "2026-03-31"}, "Mar 2026"),
        ("p_and_l", {"period_start": "2025-07-01", "period_end": "2026-06-30"}, "Jul 2025–Jun 2026"),
        ("p_and_l", {"period_start": "", "period_end": "2026-06-30"}, None),
        ("balance_sheet", {"as_of_date": "2026-06-30"}, "as of 2026-06-30"),
        ("balance_sheet", {}, None),
    ],
)
def test_period_label(kind, header, label):
    assert bss.period_label(kind, {"header": header}) == label


# ── the PDFs ────────────────────────────────────────────────────────────────


def test_the_p_and_l_sheet_prints_the_ebitda_memo_and_escapes_what_was_typed():
    body = _pl(
        business_name="Acme <script>alert(1)</script>",
        period_start="2026-01-01", period_end="2026-06-30",
        gross_revenue="1000", interest="10", taxes_and_licenses="7",
    )
    html = dealer_forms_pdf.build_p_and_l_html(body=body)
    assert "EBITDA (memo)" in html
    assert "Add: interest" in html
    assert "Add: taxes and licenses" not in html
    assert "Owner compensation" in html
    assert "Jan–Jun 2026" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert html.count("add-back</span>") == 3


def test_the_balance_sheet_prints_the_identity_line_and_any_difference():
    balanced = dealer_forms_pdf.build_balance_sheet_html(body=_bs(cash_in_bank="1000", accounts_payable="400"))
    assert "Total liabilities and equity" in balanced
    assert "Unreconciled difference" not in balanced
    assert "Equity implied" in balanced
    off = dealer_forms_pdf.build_balance_sheet_html(body=_bs(cash_in_bank="1000", owner_capital="5"))
    assert "Unreconciled difference" in off
    assert "$995.00" in off


def test_renders_are_split_from_layouts_the_way_the_413_is():
    for name in ("render_p_and_l_pdf", "render_balance_sheet_pdf"):
        src = inspect.getsource(getattr(dealer_forms_pdf, name))
        assert "from weasyprint import HTML" in src
        assert "build_" in src


# ── the service ─────────────────────────────────────────────────────────────


def _db(rows=None, scalar=None):
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: list(rows or []), first=lambda: (rows or [None])[0]),
        scalar_one_or_none=lambda: scalar,
    )
    return SimpleNamespace(
        execute=AsyncMock(return_value=result),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        get=AsyncMock(return_value=None),
    )


def _profile(bucket=True):
    return SimpleNamespace(id=uuid.uuid4(), primary_bucket_id=uuid.uuid4() if bucket else None, dealer_id=None)


def test_save_writes_the_derived_columns_and_leaves_the_borrower_anonymous():
    db = _db(scalar=None)
    body = _pl(period_start="2026-01-01", period_end="2026-06-30", gross_revenue="1000", interest="10")
    statement = _run(business_statements.save(db, _profile(), kind="p_and_l", body=body, status="submitted", actor_user_id=None))
    db.add.assert_called_once_with(statement)
    assert statement.kind == "p_and_l"
    assert statement.schema_version == "qc_pl.v1"
    assert statement.gross_revenue == Decimal("1000")
    assert statement.net_income == Decimal("990")
    assert statement.ebitda == Decimal("1000")
    assert statement.period_start.isoformat() == "2026-01-01"
    assert statement.status == "submitted"
    assert statement.submitted_at is not None
    assert statement.submitted_by_user_id is None   # the borrower did it


def test_save_stamps_staff_and_keeps_submitted_on_later_edits():
    staff = uuid.uuid4()
    db = _db(scalar=None)
    statement = _run(business_statements.save(db, _profile(), kind="balance_sheet", body=_bs(cash_in_bank="5", as_of_date="2026-06-30"), status="submitted", actor_user_id=staff))
    assert statement.submitted_by_user_id == staff
    assert statement.total_assets == Decimal("5")
    assert statement.total_equity == Decimal("5")
    assert statement.as_of_date.isoformat() == "2026-06-30"
    first_submit = statement.submitted_at
    # A later draft save on a submitted statement stays submitted — the PFS rule.
    again = _run(business_statements.save(db, _profile(), kind="balance_sheet", body=_bs(cash_in_bank="9"), status="draft", actor_user_id=uuid.uuid4(), statement=statement))
    assert again is statement
    assert again.status == "submitted"
    assert again.submitted_at == first_submit
    assert again.submitted_by_user_id == staff
    assert again.total_assets == Decimal("9")


def test_save_rejects_an_unknown_kind():
    with pytest.raises(HTTPException) as caught:
        _run(business_statements.save(_db(), _profile(), kind="packet", body={}))
    assert caught.value.status_code == 404


def test_seed_business_name_fills_blanks_only():
    body = bss.pl_empty_body()
    seeded = business_statements.seed_business_name(body, {"business_name": "Acme"})
    assert seeded["header"]["business_name"] == "Acme"
    assert body["header"]["business_name"] is None   # not mutated
    typed = business_statements.seed_business_name({**body, "header": {"business_name": "Mine"}}, {"business_name": "Acme"})
    assert typed["header"]["business_name"] == "Mine"


def _slot(name, category="Financials", status="requested"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, category=category, status=status)


def test_slot_for_kind_prefers_the_dedicated_row_over_the_combined_one():
    combined = _slot("Year-to-date P&L and balance sheet")
    dedicated = _slot("Profit and loss statement")
    tax = _slot("Business tax returns", "Financials")
    db = _db(rows=[tax, combined, dedicated])
    assert _run(business_statements.slot_for_kind(db, _profile(), "p_and_l")) is dedicated


def test_slot_for_kind_falls_back_to_the_combined_row_and_never_to_the_tax_row():
    combined = _slot("Year-to-date P&L and balance sheet")
    tax = _slot("Business tax returns", "Financials")
    db = _db(rows=[tax, combined])
    assert _run(business_statements.slot_for_kind(db, _profile(), "p_and_l")) is combined
    # The dealer checklist's P&L-only row is not a balance-sheet row.
    db = _db(rows=[tax, _slot("Current year P&L")])
    assert _run(business_statements.slot_for_kind(db, _profile(), "balance_sheet")) is None
    assert _run(business_statements.slot_for_kind(_db(rows=[combined]), _profile(bucket=False), "p_and_l")) is None


def test_ensure_slot_is_idempotent_creates_when_missing_and_409s_without_a_room():
    existing = _slot("Balance sheet")
    db = _db(rows=[existing])
    assert _run(business_statements.ensure_slot(db, _profile(), "balance_sheet", required=True)) is existing
    db.add.assert_not_called()

    db = _db(rows=[])
    created = _run(business_statements.ensure_slot(db, _profile(), "balance_sheet", required=False))
    db.add.assert_called_once_with(created)
    assert created.name == "Balance sheet"
    assert created.category == "Financials"
    assert created.required is False
    assert created.status == "requested"

    with pytest.raises(HTTPException) as caught:
        _run(business_statements.ensure_slot(_db(), _profile(bucket=False), "p_and_l", required=False))
    assert caught.value.status_code == 409
    assert "no document room" in caught.value.detail


def test_file_pdf_files_the_sheet_with_typed_key_facts_and_tells_the_timeline():
    profile = _profile()
    body = _pl(period_start="2026-01-01", period_end="2026-06-30", gross_revenue="1000")
    statement = SimpleNamespace(id=uuid.uuid4(), kind="p_and_l", body=body, bucket_file_id=None)
    slot = _slot("Year-to-date P&L and balance sheet")
    stored = SimpleNamespace(id=uuid.uuid4())
    with (
        patch.object(drafted_forms, "store_form_pdf", AsyncMock(return_value=stored)) as store,
        patch.object(dealer_forms_pdf, "render_p_and_l_pdf", return_value=b"%PDF") as render,
        patch.object(file_events, "emit", AsyncMock()) as emit,
    ):
        result = _run(business_statements.file_pdf(_db(), profile, statement, slot=slot, actor_user_id=None, actor_name="Borrower", actor_email=""))
    assert result is stored
    assert statement.bucket_file_id == stored.id
    render.assert_called_once_with(body=body)
    kwargs = store.await_args.kwargs
    assert kwargs["requested_document"] is slot
    assert kwargs["bucket_id"] == profile.primary_bucket_id
    assert kwargs["classification"] == "current_p_and_l"
    assert kwargs["key_facts"]["source_form"] == "qc_pl.v1"
    assert kwargs["key_facts"]["gross_revenue"] == 1000.0
    assert kwargs["file_label"] == "Profit and loss statement · Jan–Jun 2026"
    assert kwargs["summary"].endswith("submitted by the borrower through their own link.")
    event = emit.await_args.kwargs
    assert event["kind"] == "document.received"
    assert event["visibility"] == file_events.VISIBILITY_CLIENT
    assert event["title"] == "Profit and loss statement was submitted"
    assert event["actor"] is None
    assert event["meta"] == {"kind": "p_and_l"}


def test_file_pdf_names_the_staff_member_when_the_desk_did_it():
    statement = SimpleNamespace(id=uuid.uuid4(), kind="balance_sheet", body=_bs(cash_in_bank="1"), bucket_file_id=None)
    user = SimpleNamespace(id=uuid.uuid4())
    with (
        patch.object(drafted_forms, "store_form_pdf", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))) as store,
        patch.object(dealer_forms_pdf, "render_balance_sheet_pdf", return_value=b"%PDF"),
        patch.object(file_events, "emit", AsyncMock()) as emit,
    ):
        _run(business_statements.file_pdf(_db(), _profile(), statement, slot=_slot("Balance sheet"), actor_user_id=user.id, actor_name="Jane Desk", actor_email="jane@example.com", actor=user))
    kwargs = store.await_args.kwargs
    assert kwargs["classification"] == "balance_sheet"
    assert kwargs["file_label"] == "Balance sheet"   # no date typed, no period on the label
    assert kwargs["summary"] == "Balance sheet completed by Jane Desk on the borrower's behalf."
    assert emit.await_args.kwargs["actor"] is user


def test_the_link_check_admits_all_four_kinds_and_the_migration_agrees():
    from pathlib import Path

    from app.models.financial_form_link import FinancialFormLink

    checks = [c.sqltext.text for c in FinancialFormLink.__table__.constraints if getattr(c, "name", None) == "ck_financial_form_links_kind"]
    # 'worksheet' joined the list in 0204 — a fifth kind of link, over all four
    # forms at once. The four form kinds are still exactly these four.
    assert checks == ["kind in ('pfs','debt_schedule','p_and_l','balance_sheet','worksheet')"]
    assert FinancialFormLink.__table__.c.packet_id.nullable
    migration = Path("alembic/versions/0203_business_statements_and_packets.py").read_text()
    assert "kind in ('pfs','debt_schedule','p_and_l','balance_sheet')" in migration
    assert 'down_revision = "0202_debt_schedule_full_row"' in migration
    assert "business_financial_statements" in migration
    assert "packet_id" in migration
    widened = Path("alembic/versions/0204_financial_worksheets_and_link_scopes.py").read_text()
    assert "'worksheet'" in widened


def test_the_statement_table_is_its_own_and_pins_its_kinds():
    from app.models.business_financial_statement import BusinessFinancialStatement

    assert BusinessFinancialStatement.__tablename__ == "business_financial_statements"
    names = {getattr(c, "name", None) for c in BusinessFinancialStatement.__table__.constraints}
    assert {"ck_business_financial_statements_kind", "ck_business_financial_statements_status"} <= names
    columns = set(BusinessFinancialStatement.__table__.c.keys())
    assert {"gross_revenue", "net_income", "ebitda", "total_assets", "total_liabilities", "total_equity", "period_start", "period_end", "as_of_date", "bucket_file_id"} <= columns
