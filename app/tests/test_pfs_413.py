"""The Form 413 statement: its arithmetic, and the contract it must not break.

The richer form replaces one that decided which assets were liquid by comparing
display labels against three frozen strings. `pfs_total_liquid_assets` gates
programme eligibility, so these tests pin both halves: that the five key facts
still mean exactly what they meant, and that liquidity now follows the schema
flag rather than the wording of a label.
"""

from __future__ import annotations

import re

import pytest

from app.services import dealer_forms_pdf, pfs_schema


def _body(**assets):
    body = pfs_schema.empty_body()
    body["assets"].update(assets)
    return body


def test_key_facts_keeps_the_shape_the_rest_of_the_system_reads():
    """Same five keys, same names. bucket_ai and the dealer screening logic
    read these by name, and one rename would silently blank a metric."""
    facts = pfs_schema.key_facts(_body(cash_on_hand=100), statement_date="2026-09-05")

    assert set(facts) == {
        "statement_date",
        "total_assets",
        "total_liabilities",
        "net_worth",
        "liquid_assets",
    }
    assert facts["statement_date"] == "2026-09-05"


def test_key_facts_matches_the_old_form_for_an_equivalent_statement():
    """Parity with `_pfs_key_facts`, computed the old way, on the same numbers.

    The old form's three liquid labels were cash, savings and marketable
    securities. A borrower entering the same figures must get the same metric
    out, or the same file screens differently before and after this change.
    """
    body = _body(
        cash_on_hand=25_000,
        savings_accounts=10_000,
        stocks_and_bonds=5_000,
        ira_or_retirement=90_000,   # explicitly NOT liquid, in either version
        real_estate=400_000,
    )
    body["liabilities"]["mortgages_on_real_estate"] = 250_000

    facts = pfs_schema.key_facts(body, statement_date="2026-09-05")

    assert facts["total_assets"] == 530_000.0
    assert facts["total_liabilities"] == 250_000.0
    assert facts["net_worth"] == 280_000.0
    # cash + savings + securities only — retirement and real estate excluded
    assert facts["liquid_assets"] == 40_000.0


def test_liquidity_follows_the_flag_not_the_label():
    """The whole point of the schema change.

    Renaming a label used to move an underwriting metric. Now the label is
    presentation and `liquid` is the fact.
    """
    assert pfs_schema.LIQUID_ASSET_KEYS == {
        "cash_on_hand",
        "savings_accounts",
        "stocks_and_bonds",
    }
    for row in pfs_schema.ASSET_ROWS:
        assert row.liquid == (row.key in pfs_schema.LIQUID_ASSET_KEYS)


@pytest.mark.parametrize("value", [None, "", "not a number", [], {}])
def test_a_blank_or_unusable_amount_counts_as_zero(value):
    """A borrower leaving a line empty is the normal case; totals are the wrong
    place to reject a form. Validation belongs on save, where it can point at
    the offending field."""
    facts = pfs_schema.key_facts(_body(cash_on_hand=value), statement_date="x")
    assert facts["total_assets"] == 0.0


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("1500.50", 1500.50),
        ("1,000", 1000.0),          # people put commas in money fields
        ("$5,000", 5000.0),         # and currency symbols
        (" 2 ", 2.0),
        ("$ 1,234.56", 1234.56),
    ],
)
def test_money_typed_the_way_people_type_it_is_counted(typed, expected):
    """Decimal rejects every one of these except the first.

    Left unhandled they parse as nothing and get silently counted as zero,
    understating a borrower's assets on a form whose totals gate eligibility.
    A wrong total is far worse than a blank one, because nobody looks twice.
    """
    facts = pfs_schema.key_facts(_body(cash_on_hand=typed), statement_date="x")
    assert facts["total_assets"] == expected
    assert facts["liquid_assets"] == expected


def test_contingent_liabilities_stay_out_of_total_liabilities():
    """Section 1's right-hand column is a disclosure, not debt on the balance
    sheet. Folding it in would overstate leverage on every file that has one."""
    body = pfs_schema.empty_body()
    body["liabilities"]["accounts_payable"] = 1_000
    body["contingent"]["legal_claims"] = 500_000

    facts = pfs_schema.key_facts(body, statement_date="x")
    assert facts["total_liabilities"] == 1_000.0
    assert facts["net_worth"] == -1_000.0


def test_the_schema_is_served_so_the_browser_need_not_duplicate_it():
    described = pfs_schema.describe()
    assert described["schema_version"] == pfs_schema.SCHEMA_VERSION
    assert described["collects_ssn"] is False
    assert [row["key"] for row in described["assets"]] == [
        row.key for row in pfs_schema.ASSET_ROWS
    ]
    assert any(row["liquid"] for row in described["assets"])


# --- the rendered sheet ----------------------------------------------------


def test_only_schedules_with_rows_are_printed():
    """A partner should not scan past seven empty tables to find the one that
    was filled in."""
    body = pfs_schema.empty_body()
    body["schedules"]["real_estate"] = [{"Property address": "12 Main St"}]

    html_doc = dealer_forms_pdf.build_pfs_413_html(body=body, statement_date="2026-09-05")
    headings = set(re.findall(r"<h2>([^<]+)</h2>", html_doc))

    assert "Real estate owned" in headings
    assert "Unpaid taxes" not in headings  # empty schedule, omitted
    assert "Life insurance held" not in headings


def test_the_sheet_states_that_no_ssn_was_collected():
    """Form 413 has the field. A partner reading ours should see why it is
    blank rather than assume the borrower skipped it."""
    html_doc = dealer_forms_pdf.build_pfs_413_html(
        body=pfs_schema.empty_body(), statement_date="2026-09-05"
    )
    assert "No Social Security Number was collected" in html_doc


def test_borrower_text_cannot_inject_markup_into_the_sheet():
    body = pfs_schema.empty_body()
    body["applicant"]["name"] = "<script>alert(1)</script>"
    body["schedules"]["other_liabilities"] = [{"Description": "<b>bold</b>", "Amount": "1"}]

    html_doc = dealer_forms_pdf.build_pfs_413_html(body=body, statement_date="x")

    assert "<script>" not in html_doc
    assert "&lt;script&gt;" in html_doc
    assert "<b>bold</b>" not in html_doc


# --- the legacy eight-row form, carried forward ----------------------------


class _Row:
    """Stands in for DealerPfsAssetRow / DealerPfsLiabilityRow."""

    def __init__(self, label: str, amount: float):
        self.label = label
        self.amount = amount


def _legacy_body(assets, liabilities):
    from app.services import financial_statements

    return financial_statements.from_legacy_submission(
        assets=assets,
        liabilities=liabilities,
        owner_full_name="John Grace",
        statement_date="2026-09-05",
    )


def test_a_legacy_submission_keeps_its_net_worth_and_liquidity():
    """The old form is mapped onto 413 lines positionally.

    Net worth and liquid assets are what the screening logic reads, so a
    borrower's file must not screen differently just because the sheet it was
    typed on changed shape.
    """
    assets = [
        _Row("Cash on hand and in banks", 25_000),
        _Row("Savings accounts", 10_000),
        _Row("Stocks and bonds / other marketable securities", 5_000),
        _Row("Retirement accounts (401k, IRA, etc.)", 90_000),
        _Row("Real estate equity (market value less mortgages)", 150_000),
        _Row("Vehicles", 20_000),
        _Row("Business ownership / equity", 300_000),
        _Row("Other assets", 1_000),
    ]
    liabilities = [
        _Row("Mortgages on real estate", 0),
        _Row("Auto loans", 12_000),
        _Row("Credit cards", 4_000),
        _Row("Personal loans", 3_000),
        _Row("Student loans", 8_000),
        _Row("Other liabilities", 500),
    ]

    facts = pfs_schema.key_facts(_legacy_body(assets, liabilities), statement_date="2026-09-05")

    assert facts["total_assets"] == 601_000.0
    assert facts["total_liabilities"] == 27_500.0
    assert facts["net_worth"] == 573_500.0
    # The three liquid rows, unchanged in meaning by the move.
    assert facts["liquid_assets"] == 40_000.0


def test_legacy_liabilities_that_share_a_413_line_are_summed_not_overwritten():
    """Auto loans, credit cards, personal loans and student loans collapse onto
    two 413 instalment lines. Assigning instead of adding would silently drop
    two of them."""
    liabilities = [
        _Row("Mortgages on real estate", 0),
        _Row("Auto loans", 1_000),
        _Row("Credit cards", 2_000),
        _Row("Personal loans", 4_000),
        _Row("Student loans", 8_000),
        _Row("Other liabilities", 0),
    ]
    body = _legacy_body([_Row("Cash on hand and in banks", 0)], liabilities)

    assert float(body["liabilities"]["installment_auto"]) == 1_000.0
    # credit cards + personal + student, all on "installment (other)"
    assert float(body["liabilities"]["installment_other"]) == 14_000.0
    assert pfs_schema.key_facts(body, statement_date="x")["total_liabilities"] == 15_000.0


def test_the_legacy_mapping_is_positional_not_by_label():
    """The labels are display strings that have been reworded once already.
    Matching on them is the fragility this change exists to remove."""
    body = _legacy_body([_Row("Renamed by a designer", 7_000)], [])
    assert float(body["assets"]["cash_on_hand"]) == 7_000.0
