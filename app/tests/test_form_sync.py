"""The four financial forms, agreeing with each other.

The rule every test here defends: an identity fact is shared, a figure is
cross-checked and *offered*. Nothing in `form_sync` writes a number into a
body, and nothing overwrites a word somebody typed — so the tests that matter
most are the ones that prove a disagreement comes back as a sentence rather
than as a silent edit, and that a file nobody has filled in yet produces
nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services import business_statement_schema as bss
from app.services import form_sync, pfs_schema

# ── bodies ──────────────────────────────────────────────────────────────────


def _pl(**lines):
    """A P&L body with lines spread across the sections by key; anything left
    over is a header field."""
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


def _ds(*debts, business_name=""):
    return {"business_name": business_name, "debts": list(debts)}


def _debt(balance="", monthly_payment="", lender="First Bank"):
    return {
        "id": "row-1",
        "lender": lender,
        "balance": balance,
        "monthly_payment": monthly_payment,
    }


def _pfs(**applicant):
    body = pfs_schema.empty_body()
    body["applicant"].update(applicant)
    return body


def _blank_file():
    return {
        "p_and_l": bss.pl_empty_body(),
        "balance_sheet": bss.bs_empty_body(),
        "debt_schedule": _ds(),
        "pfs": pfs_schema.empty_body(),
    }


def _codes(checks):
    return [check.code for check in checks]


def _only(checks, code):
    matching = [check for check in checks if check.code == code]
    assert len(matching) == 1, _codes(checks)
    return matching[0]


# ── a fresh file says nothing ───────────────────────────────────────────────


def test_blank_forms_produce_no_checks_at_all():
    """The whole point of the severity rules. A file opened for the first time
    must not greet the desk with a panel of warnings."""
    assert form_sync.checks(_blank_file()) == []
    assert form_sync.checks({}) == []
    assert form_sync.checks(None) == []
    # Null bodies — a form row that exists but was never filled.
    assert form_sync.checks({kind: None for kind in form_sync.SHEET_KINDS}) == []


def test_a_form_nobody_opened_takes_no_part():
    """Absent is not blank. A debt schedule with no `debts` key is a form that
    never loaded, and it must not be read as "this borrower owes nobody"."""
    bodies = {
        "balance_sheet": _bs(cash_in_bank="50000", long_term_loans="300000"),
        "debt_schedule": {"business_name": "Acme LLC"},
    }
    assert form_sync.checks(bodies) == []
    # An explicit empty list *is* an answer, and now the two sides differ.
    bodies["debt_schedule"] = _ds(business_name="Acme LLC")
    assert _codes(form_sync.checks(bodies)) == [form_sync.CODE_DEBT_BALANCE_ONE_SIDED]


def test_a_p_and_l_nobody_filled_in_is_never_compared():
    """Dates typed, figures not. Net income of zero is not an answer yet."""
    bodies = {
        "p_and_l": _pl(period_start="2026-01-01", period_end="2026-06-30"),
        "balance_sheet": _bs(as_of_date="2026-03-31", current_period_net_income="40000"),
    }
    assert form_sync.checks(bodies) == []


# ── shared identity ─────────────────────────────────────────────────────────


def test_identity_fills_blanks_and_only_blanks():
    bodies = {
        "p_and_l": _pl(business_name="Acme Holdings LLC", prepared_by="R. Diaz", basis="accrual"),
        "balance_sheet": _bs(),
        "debt_schedule": _ds(),
        "pfs": _pfs(),
    }
    out = form_sync.apply_identity(bodies)

    assert out["balance_sheet"]["header"]["business_name"] == "Acme Holdings LLC"
    assert out["balance_sheet"]["header"]["prepared_by"] == "R. Diaz"
    assert out["balance_sheet"]["header"]["basis"] == "accrual"
    assert out["debt_schedule"]["business_name"] == "Acme Holdings LLC"
    assert out["pfs"]["applicant"]["business_name"] == "Acme Holdings LLC"
    # Nothing else on any form moved.
    assert out["balance_sheet"]["sections"] == bodies["balance_sheet"]["sections"]
    assert out["pfs"]["assets"] == bodies["pfs"]["assets"]
    # The bodies handed in are untouched: a new document comes back.
    assert bodies["debt_schedule"]["business_name"] == ""


def test_identity_never_overwrites_a_value_somebody_typed():
    """The harm this module exists to avoid, stated as a test."""
    bodies = {
        "p_and_l": _pl(business_name="Acme Holdings LLC", basis="accrual"),
        "balance_sheet": _bs(business_name="Acme Holding Co", basis="cash"),
        "debt_schedule": _ds(business_name="Acme Holding Company"),
        "pfs": _pfs(business_name="Acme"),
    }
    out = form_sync.apply_identity(bodies)

    assert out["p_and_l"]["header"]["business_name"] == "Acme Holdings LLC"
    assert out["balance_sheet"]["header"]["business_name"] == "Acme Holding Co"
    assert out["balance_sheet"]["header"]["basis"] == "cash"
    assert out["debt_schedule"]["business_name"] == "Acme Holding Company"
    assert out["pfs"]["applicant"]["business_name"] == "Acme"
    # Nothing was blank, so nothing was rewritten — the very same objects.
    for kind, body in bodies.items():
        assert out[kind] is body


def test_a_disagreement_surfaces_as_a_check_rather_than_a_silent_pick():
    bodies = {
        "p_and_l": _pl(business_name="Acme Holdings LLC"),
        "balance_sheet": _bs(business_name="Acme Holding Co"),
    }
    checks = form_sync.checks(bodies)
    disagreement = _only(checks, form_sync.CODE_IDENTITY_DISAGREEMENT)

    assert disagreement.severity == "warn"
    # Reported against the form that differs, pointing at the exact field.
    assert disagreement.sheet == "balance_sheet"
    assert disagreement.key == "header.business_name"
    assert disagreement.from_sheet == "p_and_l"
    # Offered, never applied.
    assert disagreement.suggested_value == "Acme Holdings LLC"
    assert form_sync.apply_identity(bodies)["balance_sheet"]["header"]["business_name"] == (
        "Acme Holding Co"
    )
    # Both sides and both values are in the sentence, and nobody is accused.
    assert "Acme Holding Co" in disagreement.message
    assert "Acme Holdings LLC" in disagreement.message
    assert "balance sheet" in disagreement.message
    assert "profit and loss" in disagreement.message


def test_case_and_spacing_are_how_people_type_not_a_disagreement():
    bodies = {
        "p_and_l": _pl(business_name="Acme Holdings LLC"),
        "balance_sheet": _bs(business_name="  acme   holdings llc "),
    }
    assert form_sync.checks(bodies) == []


def test_a_basis_disagreement_is_reported_in_its_own_words():
    bodies = {"p_and_l": _pl(basis="accrual"), "balance_sheet": _bs(basis="cash")}
    check = _only(form_sync.checks(bodies), form_sync.CODE_IDENTITY_DISAGREEMENT)
    assert check.key == "header.basis"
    assert "cash" in check.message and "accrual" in check.message


def test_shared_identity_prefers_the_most_recently_edited_copy():
    now = datetime(2026, 9, 10, 12, 0, 0)
    bodies = {
        "p_and_l": _pl(business_name="Acme Holdings LLC"),
        "balance_sheet": _bs(business_name="Acme Holdings LLC, Inc."),
    }
    edited_at = {"p_and_l": now - timedelta(days=3), "balance_sheet": now}
    assert form_sync.shared_identity(bodies, edited_at=edited_at) == {
        "business_name": "Acme Holdings LLC, Inc."
    }
    # ISO strings, which is how a caller usually holds them.
    edited_at = {"p_and_l": now.isoformat(), "balance_sheet": (now - timedelta(days=3)).isoformat()}
    assert form_sync.shared_identity(bodies, edited_at=edited_at) == {
        "business_name": "Acme Holdings LLC"
    }
    # Nothing known about when: slot order decides, and the P&L is first.
    assert form_sync.shared_identity(bodies) == {"business_name": "Acme Holdings LLC"}
    assert form_sync.shared_identity(bodies, edited_at={"balance_sheet": "not a date"}) == {
        "business_name": "Acme Holdings LLC"
    }


def test_shared_identity_leaves_out_what_nobody_stated():
    bodies = {"p_and_l": _pl(business_name="Acme Holdings LLC"), "balance_sheet": _bs()}
    assert form_sync.shared_identity(bodies) == {"business_name": "Acme Holdings LLC"}
    assert form_sync.shared_identity(_blank_file()) == {}
    # And a fact with nowhere to go is not invented: the 413 has no basis line.
    out = form_sync.apply_identity({"p_and_l": _pl(basis="cash"), "pfs": _pfs()})
    assert "basis" not in out["pfs"]["applicant"]


def test_a_malformed_block_is_left_exactly_as_it_was_found():
    """Filling a blank must never discard what is already there, whatever
    shape a stored body has drifted into."""
    bodies = {
        "p_and_l": _pl(business_name="Acme Holdings LLC"),
        "pfs": {"applicant": "typed as a string somehow"},
    }
    assert form_sync.apply_identity(bodies)["pfs"] == bodies["pfs"]


def test_apply_identity_takes_a_caller_s_value_and_still_only_fills_blanks():
    bodies = {"p_and_l": _pl(business_name="Typed By A Person"), "debt_schedule": _ds()}
    out = form_sync.apply_identity(bodies, {"business_name": "From The File"})
    assert out["p_and_l"]["header"]["business_name"] == "Typed By A Person"
    assert out["debt_schedule"]["business_name"] == "From The File"


# ── net income: the P&L against the balance sheet's equity line ─────────────

_PL_PERIOD = {"period_start": "2026-01-01", "period_end": "2026-06-30"}


def _profitable_pl(**extra):
    return _pl(gross_revenue="1000000", cost_of_goods_sold="400000", **_PL_PERIOD, **extra)


def test_net_income_is_checked_only_when_the_dates_line_up():
    """A balance sheet dated outside the P&L's period holds a different figure
    on purpose."""
    pl = _profitable_pl()  # net income 600,000
    for as_of in ("2025-12-31", "2026-07-01"):
        bodies = {
            "p_and_l": pl,
            "balance_sheet": _bs(as_of_date=as_of, current_period_net_income="10"),
        }
        assert form_sync.checks(bodies) == [], as_of
    # No date at all on either side is the same answer: say nothing.
    assert form_sync.checks({"p_and_l": pl, "balance_sheet": _bs(current_period_net_income="10")}) == []
    assert (
        form_sync.checks(
            {
                "p_and_l": _pl(gross_revenue="1000000"),
                "balance_sheet": _bs(as_of_date="2026-06-30", current_period_net_income="10"),
            }
        )
        == []
    )
    # Inside the period — including the last day of it — and it is checked.
    bodies = {
        "p_and_l": pl,
        "balance_sheet": _bs(as_of_date="2026-06-30", current_period_net_income="10"),
    }
    assert _codes(form_sync.checks(bodies)) == [form_sync.CODE_NET_INCOME_MISMATCH]


def test_a_blank_equity_line_is_offered_the_p_and_l_s_figure():
    bodies = {
        "p_and_l": _profitable_pl(),
        "balance_sheet": _bs(as_of_date="2026-03-31", cash_in_bank="50000"),
    }
    check = _only(form_sync.checks(bodies), form_sync.CODE_NET_INCOME_AVAILABLE)
    assert check.severity == "info"
    assert check.sheet == "balance_sheet"
    assert check.key == "sections.equity.current_period_net_income"
    assert check.from_sheet == "p_and_l" and check.from_key == "net_income"
    # Offered as the string that would go in the box — not "$600,000".
    assert check.suggested_value == "600000"
    assert "$600,000" in check.message


def test_a_differing_equity_line_is_a_warning_and_is_never_offered_a_value():
    bodies = {
        "p_and_l": _profitable_pl(),
        "balance_sheet": _bs(as_of_date="2026-06-30", current_period_net_income="540,000"),
    }
    check = _only(form_sync.checks(bodies), form_sync.CODE_NET_INCOME_MISMATCH)
    assert check.severity == "warn"
    # A typed figure is an answer; the desk decides which one stands.
    assert check.suggested_value is None
    assert "$540,000" in check.message and "$600,000" in check.message
    assert "$60,000" in check.message


def test_net_income_agreeing_to_the_rounding_is_silent():
    """Whole dollars on one form, cents on the other, is not a disagreement."""
    pl = _profitable_pl(other_income="0.40")
    bodies = {
        "p_and_l": pl,
        "balance_sheet": _bs(as_of_date="2026-06-30", current_period_net_income="$600,000"),
    }
    assert form_sync.checks(bodies) == []


# ── the debt schedule against the balance sheet ─────────────────────────────


def test_the_debt_total_reads_every_interest_bearing_line_and_no_others():
    """The eight lines come off the schema's flag, so a liability added there
    joins this check by itself. Payables and accrued taxes are debts, but they
    have no lender and no line on a schedule."""
    assert set(bss.BS_INTEREST_BEARING_KEYS) == {
        "credit_cards",
        "lines_of_credit",
        "short_term_loans",
        "current_portion_long_term_debt",
        "long_term_loans",
        "equipment_loans",
        "vehicle_loans",
        "real_estate_loans",
    }
    body = _bs(
        credit_cards="1000",
        lines_of_credit="2000",
        short_term_loans="3000",
        current_portion_long_term_debt="4000",
        long_term_loans="5000",
        equipment_loans="6000",
        vehicle_loans="7000",
        real_estate_loans="8000",
        accounts_payable="99000",
        other_current_liabilities="99000",
        other_long_term_liabilities="99000",
    )
    assert bss.bs_totals(body)["interest_bearing_debt"] == 36000
    # And the flag is served, so the form can mark the lines it cross-checks.
    rows = {
        row["key"]: row["interest_bearing"]
        for section in bss.describe("balance_sheet")["sections"]
        for row in section["rows"]
    }
    assert rows["credit_cards"] is True
    assert rows["accounts_payable"] is False


def test_the_debt_total_tolerance_is_a_hundred_dollars_or_a_percent():
    def _diff(schedule_balance, sheet_balance):
        return form_sync.checks(
            {
                "debt_schedule": _ds(_debt(balance=schedule_balance)),
                "balance_sheet": _bs(long_term_loans=sheet_balance),
            }
        )

    # The floor carries the small sheets: $100 on a $5,000 loan is inside it.
    assert _diff("5090", "5000") == []
    assert _codes(_diff("5150", "5000")) == [form_sync.CODE_DEBT_BALANCE_MISMATCH]
    # The share carries the large ones: 1% of $300,000 is $3,000.
    assert _diff("302500", "300000") == []
    check = _only(_diff("310000", "300000"), form_sync.CODE_DEBT_BALANCE_MISMATCH)
    assert check.severity == "warn"
    assert check.sheet == "balance_sheet" and check.key == "interest_bearing_debt"
    assert check.from_sheet == "debt_schedule" and check.from_key == "total_balance"
    # Two loans stated twice is a difference to look at, not a value to paste.
    assert check.suggested_value is None
    assert "$310,000" in check.message and "$300,000" in check.message
    assert "1 obligation" in check.message


def test_the_debt_total_reads_money_the_one_way_the_forms_do():
    bodies = {
        "debt_schedule": _ds(
            _debt(balance="$300,000.00"), _debt(balance=" 12,000 ", lender="Card")
        ),
        "balance_sheet": _bs(long_term_loans="300000", credit_cards="$12,000"),
    }
    assert form_sync.checks(bodies) == []
    assert form_sync.debt_totals(bodies["debt_schedule"])["rows_with_balance"] == 2


def test_one_empty_side_is_information_not_a_warning():
    """A form somebody has not got to yet is not a contradiction."""
    schedule_only = {
        "debt_schedule": _ds(_debt(balance="300000")),
        "balance_sheet": _bs(cash_in_bank="50000"),
    }
    check = _only(form_sync.checks(schedule_only), form_sync.CODE_DEBT_BALANCE_ONE_SIDED)
    assert check.severity == "info"
    assert check.sheet == "balance_sheet"
    assert "$300,000" in check.message
    # A balance sheet nobody has started says nothing either way.
    assert form_sync.checks({**schedule_only, "balance_sheet": _bs()}) == []

    sheet_only = {"debt_schedule": _ds(), "balance_sheet": _bs(long_term_loans="300000")}
    check = _only(form_sync.checks(sheet_only), form_sync.CODE_DEBT_BALANCE_ONE_SIDED)
    assert check.severity == "info"
    assert check.sheet == "debt_schedule" and check.key == "debts"
    assert "$300,000" in check.message


# ── debt service against the interest line ──────────────────────────────────


def test_debt_service_against_interest_is_information_and_only_ever_that():
    """A payment is principal as well as interest. The two are not meant to be
    equal, so a warning here would be wrong every single time."""
    bodies = {
        "p_and_l": _profitable_pl(interest="46000"),
        "debt_schedule": _ds(_debt(balance="300000", monthly_payment="5,200")),
        "balance_sheet": _bs(long_term_loans="300000"),
    }
    checks = form_sync.checks(bodies)
    assert [check.severity for check in checks] == ["info"]
    check = _only(checks, form_sync.CODE_DEBT_SERVICE_VS_INTEREST)
    assert check.sheet == "p_and_l"
    assert check.key == "sections.operating_expenses.interest"
    assert check.from_sheet == "debt_schedule"
    # Never offered: pasting a payment total into the interest line would be a
    # bookkeeping error, which is exactly why this is info.
    assert check.suggested_value is None
    assert "$5,200" in check.message
    assert "$31,200" in check.message  # six months of it, the period the P&L covers
    assert "$46,000" in check.message
    assert "principal" in check.message

    # Wildly out of line — still info, still no warning.
    bodies["p_and_l"] = _profitable_pl(interest="1")
    assert [check.severity for check in form_sync.checks(bodies)] == ["info"]

    # And it says nothing when either side is empty.
    assert (
        form_sync.CODE_DEBT_SERVICE_VS_INTEREST
        not in _codes(form_sync.checks({**bodies, "p_and_l": _profitable_pl()}))
    )
    assert (
        form_sync.CODE_DEBT_SERVICE_VS_INTEREST
        not in _codes(form_sync.checks({**bodies, "debt_schedule": _ds(_debt(balance="300000"))}))
    )


def test_debt_service_annualises_when_the_p_and_l_has_no_dates():
    bodies = {
        "p_and_l": _pl(gross_revenue="1000", interest="1200"),
        "debt_schedule": _ds(_debt(balance="300000", monthly_payment="1000")),
    }
    check = _only(form_sync.checks(bodies), form_sync.CODE_DEBT_SERVICE_VS_INTEREST)
    assert "$12,000 over a year" in check.message


# ── the shape the integrator wires up ───────────────────────────────────────


def test_checks_come_back_in_a_declared_order_and_serialise():
    bodies = {
        "p_and_l": _profitable_pl(business_name="Acme Holdings LLC", interest="46000"),
        "balance_sheet": _bs(
            business_name="Acme Holding Co",
            as_of_date="2026-06-30",
            current_period_net_income="540000",
            long_term_loans="200000",
        ),
        "debt_schedule": _ds(_debt(balance="300000", monthly_payment="5200")),
        "pfs": _pfs(),
    }
    assert _codes(form_sync.checks(bodies)) == [
        form_sync.CODE_IDENTITY_DISAGREEMENT,
        form_sync.CODE_NET_INCOME_MISMATCH,
        form_sync.CODE_DEBT_BALANCE_MISMATCH,
        form_sync.CODE_DEBT_SERVICE_VS_INTEREST,
    ]
    payload = form_sync.checks_payload(bodies)
    assert [row["code"] for row in payload] == _codes(form_sync.checks(bodies))
    for row in payload:
        assert set(row) == {
            "code",
            "severity",
            "sheet",
            "key",
            "message",
            "suggested_value",
            "from_sheet",
            "from_key",
        }
        assert row["severity"] in {"info", "warn"}
        assert row["sheet"] in form_sync.SHEET_KINDS
        assert row["message"] and row["message"][0].isupper()
        # Every message ends a sentence, and none of them accuses anybody.
        assert row["message"].rstrip().endswith(".")


def test_every_identity_slot_addresses_a_field_a_blank_form_actually_has():
    """The declaration is the only place that knows where a fact lives; if a
    form's shape moves, this is what catches it."""
    blank = _blank_file()
    for field in form_sync.IDENTITY_FIELDS:
        for slot in field.slots:
            body = blank[slot.sheet]
            for step in slot.path[:-1]:
                assert step in body, (slot.sheet, slot.path)
                body = body[step]
            assert slot.path[-1] in body, (slot.sheet, slot.path)
