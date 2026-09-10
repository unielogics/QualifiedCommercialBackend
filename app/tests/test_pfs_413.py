"""The Form 413 statement: its arithmetic, and the contract it must not break.

The richer form replaces one that decided which assets were liquid by comparing
display labels against three frozen strings. `pfs_total_liquid_assets` gates
programme eligibility, so these tests pin both halves: that the five key facts
still mean exactly what they meant, and that liquidity now follows the schema
flag rather than the wording of a label.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

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


# --- the share link --------------------------------------------------------


def test_the_token_is_stored_only_as_a_hash():
    """The link carries no access code, so the URL is the whole credential.

    A database read — a backup, a support query, a leaked dump — must not hand
    somebody a working link.
    """
    from app.services import financial_statements

    token = "a-secret-token"
    digest = financial_statements.hash_token(token)

    assert digest != token
    assert len(digest) == 64
    assert financial_statements.hash_token(token) == digest  # stable
    assert financial_statements.hash_token("a-secret-tokem") != digest


def test_a_link_closes_on_expiry_and_on_revocation_independently():
    """Two different facts: a deadline, and somebody deciding. Either shuts it."""
    from datetime import timedelta

    from app.models.financial_form_link import FinancialFormLink

    live = FinancialFormLink(kind="pfs", token_hash="x")
    assert live.is_open is True

    expired = FinancialFormLink(
        kind="pfs", token_hash="x", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert expired.is_open is False

    revoked = FinancialFormLink(
        kind="pfs",
        token_hash="x",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        revoked_at=datetime.now(UTC),
    )
    assert revoked.is_open is False


def test_links_expire_by_default():
    """An open link with no end date is a permanent credential to someone's
    finances living in whatever inbox it was forwarded to."""
    from app.services import financial_statements

    assert financial_statements.DEFAULT_LINK_TTL_DAYS > 0


# --- the debt schedule form ------------------------------------------------


def test_blank_debt_rows_are_dropped_not_stored():
    """Someone tabbing through an empty line is not an obligation."""
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body(
        {
            "debts": [
                {"lender": "First National", "balance": "12,000", "monthly_payment": "$450"},
                {"lender": "", "balance": "", "monthly_payment": ""},
                {"lender": "  ", "balance": None, "monthly_payment": None},
            ]
        }
    )
    assert len(rows) == 1
    assert rows[0]["lender"] == "First National"
    # Commas and currency symbols survive the same way they do on the 413.
    assert float(rows[0]["balance"]) == 12000.0
    assert float(rows[0]["monthly_payment"]) == 450.0


def test_a_row_with_figures_but_no_lender_is_still_an_obligation():
    """Dropping it would quietly understate what the borrower owes."""
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body({"debts": [{"lender": "", "balance": "5000", "monthly_payment": "200"}]})
    assert len(rows) == 1
    assert rows[0]["lender"] == "Unnamed lender"


def test_debt_key_facts_keep_the_shape_the_dscr_reads():
    """`extract_debt_schedule` and the DSCR metric read these by name."""
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body(
        {
            "debts": [
                {"lender": "A", "balance": "1000", "monthly_payment": "100"},
                {"lender": "B", "balance": "2000", "monthly_payment": "250"},
            ]
        }
    )
    facts = fs.debt_key_facts(rows)

    assert set(facts) == {"debts", "total_monthly_debt_service", "total_outstanding_balance"}
    assert facts["total_monthly_debt_service"] == 350.0
    assert facts["total_outstanding_balance"] == 3000.0
    assert set(facts["debts"][0]) == {
        "lender", "original_amount", "current_balance", "monthly_payment", "maturity_date",
    }


# ---------------------------------------------------------------------------
# Figures read off an uploaded document.
#
# An upload is the other way either form gets satisfied, and the analyzer has
# always read it — a PFS yields assets/liabilities/net worth, a debt schedule a
# debts array and its totals. Those figures were being discarded by the status
# endpoint, which then told the desk there was nothing behind the document.
# ---------------------------------------------------------------------------


def _pfs_analysis(**facts):
    return {"classification": "personal_financial_statement", "key_facts": facts}


def _debt_analysis(**facts):
    return {"classification": "debt_schedule", "key_facts": facts}


def test_uploaded_statement_carries_every_figure_including_liquidity():
    from app.routers.application_profiles import _uploaded_statements

    rows = _uploaded_statements(
        [_pfs_analysis(
            statement_date="2026-06-30",
            total_assets=2_450_000,
            total_liabilities=910_000,
            net_worth=1_540_000,
            liquid_assets=220_000,
        )]
    )
    assert len(rows) == 1
    assert rows[0].net_worth == 1_540_000
    assert rows[0].total_assets == 2_450_000
    assert rows[0].total_liabilities == 910_000
    # Liquidity gates programme eligibility elsewhere; dropping it here would
    # make the panel disagree with the underwriting screen.
    assert rows[0].liquid_assets == 220_000


def test_two_statements_stay_two_people():
    """A PFS belongs to an individual. Husband and wife each file one, and a
    combined net worth would be a household balance sheet neither signed."""
    from app.routers.application_profiles import _uploaded_statements

    rows = _uploaded_statements([
        _pfs_analysis(net_worth=6_365_000, total_assets=6_365_000, total_liabilities=0),
        _pfs_analysis(net_worth=2_845_779, total_assets=3_601_100, total_liabilities=755_321),
    ])
    assert [row.net_worth for row in rows] == [6_365_000, 2_845_779]


def test_debt_schedule_prefers_the_documents_own_totals():
    from app.routers.application_profiles import _uploaded_debt_figures

    figures = _uploaded_debt_figures([
        _debt_analysis(
            total_monthly_debt_service=18_400,
            total_outstanding_balance=742_000,
            debts=[{"lender": "Ally"}, {"lender": "Westlake"}],
        )
    ])
    assert figures == (2, 18_400.0, 742_000.0)


def test_debt_schedule_sums_the_rows_when_the_document_never_totals_them():
    """And survives a figure the analyzer left as printed text."""
    from app.routers.application_profiles import _uploaded_debt_figures

    figures = _uploaded_debt_figures([
        _debt_analysis(debts=[
            {"lender": "A", "current_balance": "$100,000", "monthly_payment": "2,500"},
            {"lender": "B", "current_balance": 50_000, "monthly_payment": 900},
        ])
    ])
    assert figures == (2, 3_400.0, 150_000.0)


def test_a_document_with_no_figures_reports_none_not_zero():
    """"$0 a month" is a finding about the borrower. "We could not read it" is
    a finding about the document, and the panel must not confuse the two."""
    from app.routers.application_profiles import _uploaded_debt_figures

    assert _uploaded_debt_figures([_debt_analysis()]) is None
    # An MCA contract dropped into the Debts slot is not a schedule, and its
    # figures must not be counted as one — the DSCR denominator depends on it.
    assert _uploaded_debt_figures(
        [{"classification": "floorplan_mca_inventory", "key_facts": {"total_monthly_debt_service": 9_999}}]
    ) is None


def test_amounts_survive_currency_formatting():
    from app.routers.application_profiles import _form_amount

    assert _form_amount("$1,200.50") == 1200.50
    assert _form_amount(300) == 300.0
    assert _form_amount(None) == 0.0
    assert _form_amount("not a number") == 0.0


# ---------------------------------------------------------------------------
# Prefill, and the fuller debt row.
# ---------------------------------------------------------------------------


def test_prefill_seeds_blanks_and_never_overwrites_what_was_typed():
    """The file's copy of a name can be stale. The person on the form is the
    better authority, so their typing wins and a correction must survive a
    reload of the link."""
    from app.services import financial_statements as fs

    prefill = {
        "owner_name": "Ashraf Kassim",
        "business_name": "Kassim Motors LLC",
        "home_address": "12 Main St, Miami, FL 33101",
        "business_phone": "305-555-0100",
    }
    seeded = fs.seed_pfs_applicant(pfs_schema.empty_body(), prefill)
    assert seeded["applicant"]["name"] == "Ashraf Kassim"
    assert seeded["applicant"]["business_name"] == "Kassim Motors LLC"

    corrected = {**seeded, "applicant": {**seeded["applicant"], "name": "Karim Kassim"}}
    again = fs.seed_pfs_applicant(corrected, prefill)
    assert again["applicant"]["name"] == "Karim Kassim"


def test_a_debt_row_carries_everything_a_schedule_states():
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body({"debts": [{
        "lender": "Ally", "debt_type": "Floorplan",
        "original_amount": "$250,000", "balance": "180,000",
        "rate": "7.25%", "monthly_payment": "4,200",
        "originated_on": "2024-03-01", "maturity_on": "2029-03-01",
        "secured": "Secured", "payment_status": "CURRENT",
        "collateral": "Inventory", "notes": "Curtailment monthly",
    }]})
    row = rows[0]
    assert float(row["original_amount"]) == 250000.0
    # "7.25%" and "7.25" mean the same thing to someone filling in a form.
    assert row["rate"] == 7.25
    assert row["originated_on"].isoformat() == "2024-03-01"
    assert row["maturity_on"].isoformat() == "2029-03-01"
    # Case is the borrower's business, not the schema's.
    assert row["secured"] == "secured"
    assert row["payment_status"] == "current"
    assert row["collateral"] == "Inventory"


def test_the_two_choice_fields_refuse_anything_they_do_not_recognize():
    """These render on a schedule we hand a lender. A stray value must not
    arrive there looking like something the borrower stated."""
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body({"debts": [{
        "lender": "A", "balance": "1000", "monthly_payment": "50",
        "secured": "probably", "payment_status": "<script>",
    }]})
    assert rows[0]["secured"] is None
    assert rows[0]["payment_status"] is None


def test_an_unparseable_date_is_dropped_rather_than_raised():
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body({"debts": [{
        "lender": "A", "balance": "1000", "monthly_payment": "50",
        "originated_on": "last spring", "maturity_on": "",
    }]})
    assert rows[0]["originated_on"] is None
    assert rows[0]["maturity_on"] is None


def test_key_facts_now_carry_the_figures_that_used_to_be_hardcoded_null():
    from app.services import financial_statements as fs

    rows = fs.debt_rows_from_body({"debts": [{
        "lender": "Ally", "original_amount": "250000", "balance": "180000",
        "monthly_payment": "4200", "maturity_on": "2029-03-01",
    }]})
    facts = fs.debt_key_facts(rows)
    assert facts["debts"][0]["original_amount"] == 250000.0
    assert facts["debts"][0]["maturity_date"] == "2029-03-01"


# ---------------------------------------------------------------------------
# The schedule PDF.
#
# write_pdf needs Pango, which is in the prod container and not on this host —
# hence the HTML half being separable, the same split render_pfs_413_pdf uses.
# ---------------------------------------------------------------------------


def test_the_schedule_pdf_carries_every_column_the_borrower_filled():
    from app.services import dealer_forms_pdf as pdf

    row = pdf._schedule_row_html({
        "lender": "Ally Financial", "debt_type": "Floorplan",
        "original_amount": "250000", "balance": "180000", "rate": 7.25,
        "monthly_payment": "4200", "originated_on": "2024-03-01",
        "maturity_on": "2029-03-01", "secured": "secured",
        "payment_status": "current", "collateral": "Inventory",
    })
    for expected in ("Ally Financial", "Floorplan", "$250,000", "$180,000",
                     "7.25%", "$4,200", "2024-03-01", "2029-03-01",
                     "Secured", "Current", "Inventory"):
        assert expected in row, expected


def test_a_note_hangs_under_its_own_obligation():
    """Spanning the table rather than squeezing a twelfth column, so a long
    note wraps without shrinking every figure on the row."""
    from app.services import dealer_forms_pdf as pdf

    row = pdf._schedule_row_html({"lender": "A", "notes": "Balloon due in 2027"})
    assert "colspan='11'" in row
    assert "Note: Balloon due in 2027" in row


def test_a_field_the_borrower_left_blank_prints_a_dash():
    """A blank cell on a printed schedule reads as a rendering fault. A dash
    reads as "not stated", which is what it means."""
    from app.services import dealer_forms_pdf as pdf

    row = pdf._schedule_row_html({"lender": "A", "balance": "1000"})
    assert row.count("&mdash;") >= 8


def test_the_schedule_pdf_escapes_what_the_borrower_typed():
    from app.services import dealer_forms_pdf as pdf

    row = pdf._schedule_row_html({"lender": "<script>alert(1)</script>", "collateral": "A & B"})
    assert "<script>" not in row
    assert "&lt;script&gt;" in row
    assert "A &amp; B" in row


def test_no_model_declares_the_same_column_twice():
    """SQLAlchemy lets a second `x: Mapped[...]` in one class silently replace
    the first, so a column that already exists reads as missing to anyone
    grepping a window of the class — and the migration written to "add" it
    fails on deploy with DuplicateColumn, after the image has been built and
    pulled. Which is exactly how `collateral` got added to dos_debts twice.

    Parsed rather than introspected: `__table__.columns` deduplicates, so by
    the time SQLAlchemy has built the table the evidence is gone.
    """
    import ast
    from pathlib import Path

    offenders: list[str] = []
    for path in sorted(Path("app").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not our files to fix
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            seen: set[str] = set()
            for item in node.body:
                if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                    continue
                # Only mapped columns: a plain annotation may legitimately be
                # narrowed twice in a class body.
                source = ast.unparse(item.annotation)
                if "Mapped[" not in source:
                    continue
                name = item.target.id
                if name in seen:
                    offenders.append(f"{path}:{item.lineno} {node.name}.{name}")
                seen.add(name)

    assert not offenders, "column declared twice: " + "; ".join(offenders)
