"""The financial cockpit reads a file's numbers without a model.

Body Good Studio had six accepted statements, a signed authorization and no
tax return, and the desk could only see the figures by generating the lender
packet. The cockpit answers from the analyses on file: month by month, with
every figure naming its source, and the DSCR and net-profit conclusions
following the AI review's precedence (tax return, then P&L, then bank proxy).
"""

from __future__ import annotations

from app.services.financial_cockpit import (
    amortized_payment,
    bank_months,
    bank_summary,
    build_cockpit,
    debt_service,
    deposit_trend_pct,
    documented_debts,
    listing_summary,
    monthly_equivalent,
    tax_years,
)


def _file(
    file_id: str,
    classification: str,
    facts: dict,
    *,
    accepted: bool = True,
    classifications: list[str] | None = None,
) -> dict:
    return {
        "file_id": file_id,
        "file_name": f"{file_id}.pdf",
        "classification": classification,
        "classifications": classifications or [classification],
        "key_facts": facts,
        "accepted": accepted,
        "analyzed_at": None,
        "period_hint": None,
    }


def _bank(
    file_id: str,
    period: str,
    deposits: float,
    withdrawals: float,
    *,
    accepted: bool = True,
    bank: str = "Chase",
    last4: str = "1234",
    **facts,
) -> dict:
    return _file(
        file_id,
        "bank_statement",
        {
            "bank": bank,
            "account_last4": last4,
            "statement_period": period,
            "total_deposits_and_credits": deposits,
            "total_withdrawals_and_debits": withdrawals,
            **facts,
        },
        accepted=accepted,
    )


def _tax(file_id: str, year: str, **facts) -> dict:
    return _file(
        file_id,
        "tax_return",
        {
            "entity_name": "Body Good Studio LLC",
            "tax_year": year,
            "currency": "USD",
            "basis": "cash",
            **facts,
        },
    )


def _pnl(file_id: str = "pl", **facts) -> dict:
    base = {
        "business_name": "Body Good Studio LLC",
        "basis": "accrual",
        "currency": "USD",
        "period_start": "2026-01-01",
        "period_end": "2026-06-30",
        "gross_revenue": 300000,
        "net_income": 30000,
        "interest": 1000,
        "income_taxes": 2000,
        "depreciation_and_amortization": 3000,
    }
    return _file(file_id, "current_p_and_l", {**base, **facts})


SIX_MONTHS = [
    _bank(
        f"bs{i}",
        f"2026-{i + 2:02d}-28 to 2026-{i + 3:02d}-30",
        50000,
        45000,
        beginning_balance=10000 + i * 1000,
        ending_balance=15000 + i * 1000,
        average_ledger_balance=12000 + i * 1000,
        low_daily_balance=4000,
        nsf_or_overdraft_count=0,
    )
    for i in range(1, 7)
]
TAX_2025 = _tax(
    "t25",
    "2025",
    gross_receipts=600000,
    ordinary_business_income=60000,
    interest=3000,
    depreciation=7000,
)
TAX_2024 = _tax(
    "t24",
    "2024",
    gross_receipts=500000,
    ordinary_business_income=40000,
    interest=2000,
    depreciation=8000,
)
LOAN_ROW = {
    "id": "row1",
    "lender": "Bank A",
    "category": "loan",
    "monthly_payment": 2000,
    "balance": 50000,
    "count_in_dscr": True,
}
TERM_SHEET = {
    "source": "term_sheet",
    "label": "Term sheet v1",
    "amount": 100000,
    "rate_pct": 10,
    "term_months": 60,
    "monthly_payment": 1000,
}


def test_a_statement_files_under_its_closing_month_and_accounts_combine() -> None:
    months, accounts = bank_months(
        [
            _bank("a", "2026-05-30 to 2026-06-30", 10000, 8000, ending_balance=5000),
            _bank(
                "b",
                "2026-06-01 to 2026-06-30",
                2000,
                1000,
                ending_balance=1000,
                bank="Wells Fargo",
                last4="9876",
                accepted=False,
            ),
        ]
    )
    assert [m["month"] for m in months] == ["2026-06"]
    june = months[0]
    assert june["label"] == "Jun 2026"
    assert (june["deposits"], june["withdrawals"], june["net"], june["ending_balance"]) == (
        12000,
        9000,
        3000,
        6000,
    )
    assert june["accounts"] == ["Chase ••1234", "Wells Fargo ••9876"]
    assert june["accepted"] is True and sorted(june["file_ids"]) == ["a", "b"]
    assert [a["label"] for a in accounts] == ["Chase ••1234", "Wells Fargo ••9876"]
    assert accounts[1]["accepted_months"] == []


def test_a_statement_uploaded_twice_counts_once_and_keeps_the_fuller_read() -> None:
    months, _ = bank_months(
        [
            _bank("first", "2026-06-01 to 2026-06-30", 10000, 8000),
            _bank(
                "again",
                "2026-06-01 to 2026-06-30",
                10000,
                8000,
                ending_balance=5000,
                average_ledger_balance=4000,
            ),
        ]
    )
    assert len(months) == 1
    assert months[0]["deposits"] == 10000
    assert months[0]["ending_balance"] == 5000 and months[0]["average_balance"] == 4000


def test_summary_states_averages_annualized_figures_trend_and_gaps() -> None:
    months, _ = bank_months(
        [
            _bank("apr", "2026-04-01 to 2026-04-30", 40000, 30000),
            _bank("may", "2026-05-01 to 2026-05-31", 50000, 45000, nsf_or_overdraft_count=1),
            _bank("jul", "2026-07-01 to 2026-07-31", 60000, 50000),
            _bank(
                "aug",
                "2026-08-01 to 2026-08-31",
                70000,
                60000,
                negative_balance_dates=["2026-08-03"],
            ),
        ]
    )
    summary = bank_summary(months)
    assert summary["gaps"] == ["2026-06"]
    assert (summary["first_month"], summary["last_month"]) == ("2026-04", "2026-08")
    assert summary["avg_deposits"] == 55000 and summary["annualized_deposits"] == 660000
    assert summary["avg_net"] == 8750 and summary["annualized_net"] == 105000
    assert summary["deposit_trend_pct"] == 44.4  # (60k+70k)/2 against (40k+50k)/2
    assert summary["nsf_total"] == 1 and summary["negative_days_total"] == 1
    assert (summary["highest_month"], summary["lowest_month"]) == ("2026-08", "2026-04")
    assert summary["estimates_available"] is True


def test_annualized_figures_need_three_months() -> None:
    months, _ = bank_months(
        [
            _bank("a", "2026-05-01 to 2026-05-31", 10000, 5000),
            _bank("b", "2026-06-01 to 2026-06-30", 20000, 5000),
        ]
    )
    summary = bank_summary(months)
    assert summary["avg_deposits"] == 15000
    assert summary["annualized_deposits"] is None and summary["annualized_net"] is None
    assert summary["estimates_available"] is False
    assert deposit_trend_pct([10000, 20000]) == 100.0
    assert deposit_trend_pct([None, 20000]) is None


def test_tax_years_rebuild_ebitda_and_year_over_year_change() -> None:
    years = tax_years(
        [TAX_2024, TAX_2025, _tax("t23", "2023", gross_receipts=400000, net_income=10000)]
    )
    assert [y["year"] for y in years] == ["2023", "2024", "2025"]
    first, prior, latest = years
    assert (first["income"], first["income_basis"], first["ebitda"], first["ebitda_complete"]) == (
        10000,
        "net income",
        10000,
        False,
    )
    assert (prior["ebitda"], latest["ebitda"]) == (50000, 70000)
    assert latest["income_basis"] == "ordinary business income"
    assert prior["gross_receipts_change_pct"] == 25.0
    assert latest["gross_receipts_change_pct"] == 20.0 and latest["income_change_pct"] == 50.0
    assert latest["net_margin"] == 0.1
    assert first["gross_receipts_change_pct"] is None


def test_payment_cadences_convert_to_a_monthly_figure() -> None:
    assert monthly_equivalent(1000, "weekly") == 4330.0
    assert monthly_equivalent(500, "daily") == 10500.0
    assert monthly_equivalent(1000, "biweekly") == 2170.0
    assert monthly_equivalent(1000, None) == 1000.0
    assert monthly_equivalent(1000, "quarterly") is None
    assert amortized_payment(100000, 12, 120) == 1434.71
    assert amortized_payment(100000, 0, 120) == 833.33
    assert amortized_payment(100000, 12, 0) is None


def test_debt_service_prefers_the_rows_then_the_document_then_advance_terms() -> None:
    rows = [
        LOAN_ROW,
        {
            "id": "row2",
            "lender": "Funder B",
            "category": "mca",
            "payment_amount": 300,
            "payment_frequency": "daily",
            "balance": 40000,
            "factor_rate": 1.3,
            "count_in_dscr": True,
        },
        {
            "id": "row3",
            "lender": "Card",
            "category": "card",
            "monthly_payment": 500,
            "count_in_dscr": False,
        },
    ]
    from app.services.financial_cockpit import debt_rows

    schedule = debt_rows(rows)
    assert [r["monthly_payment"] for r in schedule] == [2000, 6300.0, 500]
    assert [r["is_mca"] for r in schedule] == [False, True, False]
    service = debt_service(schedule, {"total_monthly_debt_service": 2500}, [])
    assert (service["monthly"], service["annual"], service["source"]) == (
        8300.0,
        99600.0,
        "debt_schedule_rows",
    )
    assert service["schedule_rows_counted"] == 2 and service["schedule_balance"] == 90000
    assert (service["mca_count"], service["mca_monthly"], service["mca_balance"]) == (
        1,
        6300.0,
        40000,
    )
    assert service["document_monthly"] == 2500

    from_document = debt_service(
        [], {"total_monthly_debt_service": 2500, "total_outstanding_balance": 80000}, []
    )
    assert (
        from_document["monthly"],
        from_document["source"],
        from_document["document_balance"],
    ) == (2500, "debt_schedule_document", 80000)

    documented = documented_debts(
        [
            _file(
                "terms",
                "debt_schedule",
                {
                    "debts": [
                        {
                            "lender": "Funder B",
                            "current_balance": 40000,
                            "payment_amount": 300,
                            "payment_frequency": "daily",
                        },
                        {"lender": "funder b", "current_balance": 35000, "monthly_payment": 5000},
                        {
                            "lender": "Funder C",
                            "current_balance": 20000,
                            "monthly_payment": 1200,
                            "factor_rate": 1.25,
                        },
                    ]
                },
            )
        ]
    )
    assert [(d["lender"], d["balance"], d["monthly_payment"], d["is_mca"]) for d in documented] == [
        ("Funder B", 40000, 6300.0, True),
        ("Funder C", 20000, 1200.0, True),
    ]
    from_terms = debt_service([], None, documented)
    assert (from_terms["monthly"], from_terms["source"], from_terms["mca_count"]) == (
        7500.0,
        "advance_terms",
        2,
    )

    nothing = debt_service([], None, [])
    assert (nothing["monthly"], nothing["source"]) == (None, "none")


def test_dscr_candidates_follow_the_review_precedence_and_name_their_basis() -> None:
    cockpit = build_cockpit(
        [*SIX_MONTHS, TAX_2024, TAX_2025, _pnl()], [LOAN_ROW], proposed_terms=TERM_SHEET
    )
    dscr = cockpit["dscr"]
    assert (
        dscr["annual_debt_service"] == 24000 and dscr["debt_service_source"] == "debt_schedule_rows"
    )
    assert [c["key"] for c in dscr["candidates"]] == [
        "tax_return",
        "profit_and_loss",
        "bank_cash_flow",
    ]
    tax, statement, bank = dscr["candidates"]
    assert (tax["value"], tax["status"], tax["confidence"]) == (2.92, "very_strong", "filed")
    assert tax["numerator"] == 70000 and "ordinary business income" in tax["numerator_label"]
    assert (tax["pro_forma_value"], tax["pro_forma_status"]) == (1.94, "very_strong")
    assert cockpit["profit_and_loss"]["annualized_ebitda"] == 72000
    assert statement["value"] == 3.0 and statement["period"] == "Jan–Jun 2026"
    assert bank["value"] == 3.5 and bank["confidence"] == "proxy" and "floor" in bank["note"]
    assert dscr["headline"]["key"] == "tax_return"
    assert (
        dscr["proposed"]["monthly_payment"] == 1000 and dscr["proposed"]["annual_payment"] == 12000
    )
    assert dscr["thresholds"] == {"acceptable": 1.0, "healthy": 1.25, "very_strong": 1.5}


def test_dscr_without_debt_service_explains_itself_and_still_shows_the_pro_forma() -> None:
    cockpit = build_cockpit([TAX_2025], [], proposed_terms=TERM_SHEET)
    dscr = cockpit["dscr"]
    assert dscr["headline"] is None
    assert "debt schedule" in dscr["reason"]
    assert dscr["pro_forma_headline"]["pro_forma_value"] == 5.83
    assert any("No debt service" in note for note in cockpit["notes"])

    nothing = build_cockpit([], [])
    assert nothing["dscr"]["headline"] is None and "income figure" in nothing["dscr"]["reason"]


def test_proposed_payment_amortizes_when_the_sheet_has_no_monthly_figure() -> None:
    cockpit = build_cockpit(
        [TAX_2025],
        [LOAN_ROW],
        proposed_terms={
            "source": "term_sheet",
            "amount": 100000,
            "rate_pct": 12,
            "term_months": 120,
        },
    )
    assert cockpit["dscr"]["proposed"]["monthly_payment"] == 1434.71
    assert build_cockpit([], [], proposed_terms=None)["dscr"]["proposed"] is None


def test_net_profit_concludes_from_the_tax_return_then_the_statement_then_the_bank_proxy() -> None:
    everything = build_cockpit([*SIX_MONTHS, TAX_2025, _pnl()], [LOAN_ROW])
    conclusion = everything["net_profit"]["conclusion"]
    assert (conclusion["key"], conclusion["value"], conclusion["basis"], conclusion["period"]) == (
        "tax_return",
        60000,
        "ordinary business income",
        "2025",
    )
    assert conclusion["margin"] == 0.1 and everything["net_profit"]["note"] is None
    assert [c["key"] for c in everything["net_profit"]["candidates"]] == [
        "tax_return",
        "profit_and_loss",
        "bank_cash_flow",
    ]

    statement_only = build_cockpit([*SIX_MONTHS, _pnl()], [LOAN_ROW])
    conclusion = statement_only["net_profit"]["conclusion"]
    assert (conclusion["key"], conclusion["value"]) == ("profit_and_loss", 60000)
    assert (
        "annualized" in conclusion["basis"]
        and "No tax return" in statement_only["net_profit"]["note"]
    )

    bank_only = build_cockpit(SIX_MONTHS, [LOAN_ROW])
    conclusion = bank_only["net_profit"]["conclusion"]
    assert (conclusion["key"], conclusion["value"], conclusion["revenue"]) == (
        "bank_cash_flow",
        60000,
        600000,
    )
    assert "cash retained" in bank_only["net_profit"]["note"]


def test_expense_lines_name_their_source_and_period() -> None:
    cockpit = build_cockpit(
        [
            *SIX_MONTHS,
            TAX_2025,
            _pnl(cost_of_goods_sold=100000, total_operating_expenses=150000, owner_salaries=40000),
        ],
        [LOAN_ROW],
    )
    lines = {line["key"]: line for line in cockpit["expenses"]["lines"]}
    cogs = lines["pl_cost_of_goods_sold"]
    assert (
        cogs["amount"],
        cogs["annualized"],
        cogs["period"],
        cogs["source"],
        cogs["ratio_to_revenue"],
    ) == (100000, 200000, "Jan–Jun 2026", "profit_and_loss", 0.3333)
    assert lines["pl_owner_salaries"]["annualized"] == 80000
    assert (
        lines["tax_depreciation"]["amount"],
        lines["tax_depreciation"]["period"],
        lines["tax_depreciation"]["source"],
    ) == (7000, "2025", "tax_return")
    assert (
        lines["bank_outflows"]["amount"],
        lines["bank_outflows"]["annualized"],
        lines["bank_outflows"]["ratio_to_revenue"],
    ) == (45000, 540000, 0.9)
    assert (
        lines["debt_service"]["amount"],
        lines["debt_service"]["annualized"],
        lines["debt_service"]["source"],
    ) == (2000, 24000, "debt_schedule_rows")
    assert cockpit["expenses"]["opex_ratio"] == 0.5 and cockpit["expenses"]["outflow_ratio"] == 0.9
    assert cockpit["expenses"]["debt_service_to_deposits"] == 0.04
    assert cockpit["profit_and_loss"]["gross_margin"] == round(200000 / 300000, 4)


def test_an_empty_file_builds_a_cockpit_that_says_what_is_missing() -> None:
    cockpit = build_cockpit([], [])
    assert cockpit["coverage"]["bank_months"] == 0 and cockpit["bank"]["months"] == []
    assert (
        cockpit["tax"]["years"] == []
        and cockpit["profit_and_loss"] is None
        and cockpit["balance_sheet"] is None
    )
    assert cockpit["net_profit"]["conclusion"] is None
    joined = " ".join(cockpit["notes"])
    assert (
        "No bank statement" in joined
        and "No tax return" in joined
        and "No debt service" in joined
        and "No loan terms" in joined
    )


def test_statements_only_file_reads_like_body_good_studio() -> None:
    terms = _file(
        "advance-terms",
        "debt_schedule",
        {
            "debts": [
                {
                    "lender": "Funder B",
                    "current_balance": 40000,
                    "payment_amount": 300,
                    "payment_frequency": "daily",
                },
                {
                    "lender": "Funder C",
                    "current_balance": 20000,
                    "payment_amount": 1000,
                    "payment_frequency": "weekly",
                },
            ]
        },
    )
    cockpit = build_cockpit([*SIX_MONTHS, terms], [], requested_amount=70000)
    coverage = cockpit["coverage"]
    assert (
        coverage["bank_months"],
        coverage["bank_months_accepted"],
        coverage["bank_months_required"],
    ) == (6, 6, 6)
    assert (
        coverage["tax_years"] == []
        and coverage["has_debt_schedule_document"] is True
        and coverage["documented_debts"] == 2
    )
    assert cockpit["requested_amount"] == 70000
    summary = cockpit["bank"]["summary"]
    assert (
        summary["beginning_balance"],
        summary["ending_balance"],
        summary["average_balance"],
    ) == (11000, 21000, 15500)
    assert summary["lowest_balance"] == 4000 and summary["annualized_deposits"] == 600000
    service = cockpit["debt"]["service"]
    assert (service["source"], service["monthly"], service["mca_count"]) == (
        "advance_terms",
        10630.0,
        2,
    )
    headline = cockpit["dscr"]["headline"]
    assert headline["key"] == "bank_cash_flow" and headline["value"] == round(
        (5000 + 10630.0) * 12 / (10630.0 * 12), 2
    )
    assert cockpit["net_profit"]["conclusion"]["key"] == "bank_cash_flow"
    assert any("advance terms" in note for note in cockpit["notes"])
    assert any("No tax return" in note for note in cockpit["notes"])

    compact = listing_summary(cockpit)
    assert (
        compact["bank_months"] == 6
        and compact["avg_monthly_deposits"] == 50000
        and compact["annualized_deposits"] == 600000
    )
    assert (
        compact["dscr_basis"] == "bank_cash_flow"
        and compact["net_profit_basis"] == "bank_cash_flow"
    )
    assert compact["net_profit"] == 60000 and compact["tax_year"] is None


# ---------------------------------------------------------------------------
# Recurring payments and the reconciliation of what the client said
# ---------------------------------------------------------------------------

from app.services.financial_cockpit import (  # noqa: E402
    debt_rows,
    reconcile_advances,
    recurring_payments,
)


def _recurring_month(period: str, debits: list[dict], credits: list[dict] | None = None, overdrafts: list[dict] | None = None, **extra) -> dict:
    return {
        "statement_period": period,
        "total_deposits_and_credits": 50000,
        "total_withdrawals_and_debits": 45000,
        "recurring_debits": debits,
        "recurring_credits": credits or [],
        "overdraft_items": overdrafts or [],
        **extra,
    }


def _statement_with_recurring(file_id: str, months: list[dict]) -> dict:
    return _file(file_id, "bank_statement", {"bank": "Chase", "account_last4": "1204", "statement_period": months[0]["statement_period"], "months": months})


ONDECK = {"payee": "ONDECK ACH DEBIT 4471", "occurrences": 21, "total_amount": 6300, "average_amount": 300, "cadence": "daily", "same_amount": True, "kind_guess": "merchant_cash_advance"}
CHASE_CARD = {"payee": "CHASE CARD ONLINE PAYMENT", "occurrences": 1, "total_amount": 1200, "average_amount": 1200, "cadence": "monthly", "same_amount": False, "kind_guess": "credit_card"}
GUSTO = {"payee": "GUSTO PAYROLL", "occurrences": 2, "total_amount": 9000, "average_amount": 4500, "cadence": "biweekly", "same_amount": False, "kind_guess": "payroll"}
MYSTERY = {"payee": "FORWARD FINANCING LLC", "occurrences": 4, "total_amount": 4000, "average_amount": 1000, "cadence": "weekly", "same_amount": True, "kind_guess": "other"}


def test_recurring_payments_roll_up_across_months_and_classify_the_payee() -> None:
    analyses = [
        _statement_with_recurring("s1", [_recurring_month("2026-05-01 to 2026-05-31", [ONDECK, CHASE_CARD, GUSTO]), _recurring_month("2026-06-01 to 2026-06-30", [ONDECK, MYSTERY])]),
        _bank("old", "2026-07-01 to 2026-07-31", 50000, 45000),  # read before the extraction existed
    ]
    recurring = recurring_payments(analyses)
    assert recurring["months_read"] == ["2026-05", "2026-06"]
    assert recurring["files_need_reread"] == ["old"]
    by_key = {p["payee"]: p for p in recurring["payees"]}
    ondeck = by_key["ONDECK ACH DEBIT 4471"]
    assert (ondeck["kind"], ondeck["occurrences"], ondeck["total"], ondeck["average_amount"], ondeck["monthly_average"], ondeck["months_seen"]) == ("merchant_cash_advance", 42, 12600, 300, 6300, 2)
    assert ondeck["cadence"] == "daily" and ondeck["same_amount"] is True and ondeck["is_debt"]
    assert by_key["FORWARD FINANCING LLC"]["kind"] == "merchant_cash_advance"
    assert by_key["CHASE CARD ONLINE PAYMENT"]["kind"] == "credit_card" and by_key["CHASE CARD ONLINE PAYMENT"]["is_card"]
    assert by_key["GUSTO PAYROLL"]["kind"] == "payroll"
    assert [p["payee"] for p in recurring["payees"][:2]] == ["ONDECK ACH DEBIT 4471", "FORWARD FINANCING LLC"]  # debt first
    assert recurring["observed_debt_service_monthly"] == 8300.0  # 6300 + 4000/2
    assert recurring["observed_mca_monthly"] == 8300.0 and recurring["observed_loan_monthly"] is None
    assert recurring["observed_card_monthly"] == 600.0


def test_statement_debits_become_the_debt_service_when_nothing_else_is_on_file() -> None:
    cockpit = build_cockpit([_statement_with_recurring("s1", [_recurring_month("2026-05-01 to 2026-05-31", [ONDECK]), _recurring_month("2026-06-01 to 2026-06-30", [ONDECK]), _recurring_month("2026-07-01 to 2026-07-31", [ONDECK])])], [])
    service = cockpit["debt"]["service"]
    assert (service["source"], service["monthly"], service["observed_mca_monthly"]) == ("bank_observed", 6300.0, 6300.0)
    assert cockpit["dscr"]["candidates"][0]["annual_debt_service"] == 75600.0
    assert any("statements show going to lenders" in note for note in cockpit["notes"])
    assert cockpit["coverage"]["recurring_months_read"] == 3 and cockpit["coverage"]["statements_need_reread"] == 0


def test_a_lender_the_schedule_forgot_is_added_to_the_rows_denominator() -> None:
    rows = debt_rows([{"id": "r1", "lender": "OnDeck Capital", "category": "mca", "payment_amount": 300, "payment_frequency": "daily", "count_in_dscr": True}])
    recurring = recurring_payments([_statement_with_recurring("s1", [_recurring_month("2026-05-01 to 2026-05-31", [ONDECK, MYSTERY])])])
    service = debt_service(rows, None, [], recurring)
    assert service["source"] == "debt_schedule_rows+bank_observed"
    assert service["monthly"] == 6300.0 + 4000.0 and service["unexplained_observed_monthly"] == 4000.0


def test_reconciliation_marks_each_declared_advance_against_statements_and_contracts() -> None:
    rows = debt_rows(
        [
            {"id": "r1", "lender": "OnDeck", "category": "mca", "payment_amount": 310, "payment_frequency": "daily", "balance": 40000, "count_in_dscr": True},
            {"id": "r2", "lender": "Kapitus", "category": "mca", "payment_amount": 2000, "payment_frequency": "weekly", "count_in_dscr": True},
            {"id": "r3", "lender": "Credibly", "category": "mca", "payment_amount": 500, "payment_frequency": "daily", "count_in_dscr": True},
        ]
    )
    documented = [{"lender": "ON DECK CAPITAL", "balance": 41000, "monthly_payment": 6300.0, "payment_frequency": "daily", "is_mca": True}]
    recurring = recurring_payments([_statement_with_recurring("s1", [_recurring_month("2026-05-01 to 2026-05-31", [ONDECK, MYSTERY, {"payee": "KAPITUS ACH", "occurrences": 4, "total_amount": 4000, "average_amount": 1000, "cadence": "weekly", "same_amount": True}])])])
    result = reconcile_advances(rows, documented, recurring)
    by_lender = {r["lender"]: r for r in result["rows"]}
    ondeck = by_lender["OnDeck"]
    assert ondeck["status"] == "matches" and ondeck["observed"]["payee"] == "ONDECK ACH DEBIT 4471"
    assert ondeck["observed_delta_pct"] == 3.3  # 6510 declared vs 6300 seen
    assert ondeck["documented"]["balance"] == 41000 and ondeck["documented_balance_delta_pct"] == -2.4
    kapitus = by_lender["Kapitus"]
    assert kapitus["status"] == "differs" and kapitus["observed_delta_pct"] == 116.5  # 8660 declared vs 4000 seen
    assert by_lender["Credibly"]["status"] == "not_seen"
    assert [p["payee"] for p in result["unmatched_observed"]] == ["FORWARD FINANCING LLC"]
    assert result["unmatched_documented"] == []
    assert (result["matches"], result["differs"], result["not_seen"]) == (1, 1, 1)

    no_statements = reconcile_advances(rows, [], {"payees": [], "months_read": []})
    assert {r["status"] for r in no_statements["rows"]} == {"no_statements_read"}


def test_the_statement_prompt_asks_for_recurring_debits() -> None:
    from app.services import bucket_ai

    assert any("recurring_debits" in value and "kind_guess" in value for value in vars(bucket_ai).values() if isinstance(value, str))


# ---------------------------------------------------------------------------
# Accounts, per-month amounts, credits, overdrafts
# ---------------------------------------------------------------------------


def test_each_account_keeps_its_own_series_and_summary() -> None:
    cockpit = build_cockpit(
        [
            _bank("c1", "2026-05-01 to 2026-05-31", 30000, 25000, ending_balance=5000),
            _bank("c2", "2026-06-01 to 2026-06-30", 32000, 26000, ending_balance=6000),
            _bank("s1", "2026-05-01 to 2026-05-31", 2000, 500, ending_balance=9000, bank="Chase", last4="7777"),
        ],
        [],
    )
    accounts = cockpit["bank"]["accounts"]
    assert [a["label"] for a in accounts] == ["Chase ••1234", "Chase ••7777"]
    checking = accounts[0]
    assert [m["month"] for m in checking["series"]] == ["2026-05", "2026-06"]
    assert checking["series"][0]["net"] == 5000 and checking["series"][0]["accounts"] == ["Chase ••1234"]
    assert checking["summary"]["avg_deposits"] == 31000 and checking["summary"]["months"] == 2
    savings = accounts[1]
    assert savings["series"][0]["ending_balance"] == 9000 and savings["summary"]["avg_net"] == 1500
    combined = cockpit["bank"]["months"]
    assert combined[0]["deposits"] == 32000 and combined[0]["ending_balance"] == 14000  # both accounts in May


def test_payees_carry_the_amount_seen_each_month_and_their_account() -> None:
    recurring = recurring_payments([_statement_with_recurring("s1", [_recurring_month("2026-05-01 to 2026-05-31", [ONDECK]), _recurring_month("2026-06-01 to 2026-06-30", [ONDECK, MYSTERY])])])
    by = {p["payee"]: p for p in recurring["payees"]}
    assert by["ONDECK ACH DEBIT 4471"]["monthly"] == {"2026-05": 6300.0, "2026-06": 6300.0}
    assert by["FORWARD FINANCING LLC"]["monthly"] == {"2026-06": 4000.0}
    assert by["ONDECK ACH DEBIT 4471"]["accounts"] == ["Chase ••1204"]
    assert recurring["files_need_reread"] == []  # every field present
    assert recurring["total_monthly"] == 8300.0

    partial = recurring_payments([_statement_with_recurring("p", [{"statement_period": "2026-05-01 to 2026-05-31", "recurring_debits": [ONDECK]}])])
    assert partial["files_need_reread"] == ["p"]  # credits and overdraft items never read


def test_recurring_credits_name_where_the_money_comes_from() -> None:
    credits = recurring_payments(
        [
            _statement_with_recurring(
                "s1",
                [
                    _recurring_month(
                        "2026-05-01 to 2026-05-31",
                        [],
                        credits=[
                            {"payee": "STRIPE TRANSFER", "occurrences": 20, "total_amount": 40000, "average_amount": 2000, "cadence": "daily", "kind_guess": "card_settlement"},
                            {"payee": "ONDECK CAPITAL", "occurrences": 1, "total_amount": 80000, "average_amount": 80000, "cadence": "irregular", "kind_guess": "other"},
                            {"payee": "ACME CORP", "occurrences": 2, "total_amount": 9000, "average_amount": 4500, "cadence": "biweekly", "kind_guess": "customer"},
                        ],
                    )
                ],
            )
        ],
        field="recurring_credits",
        direction=1,
    )
    by = {p["payee"]: p for p in credits["payees"]}
    assert by["STRIPE TRANSFER"]["kind"] == "card_settlement"
    assert by["ONDECK CAPITAL"]["kind"] == "advance_funding" and by["ONDECK CAPITAL"]["kind_label"] == "Advance funded"
    assert by["ACME CORP"]["kind"] == "customer"
    assert all(not p["is_debt"] and not p["is_card"] for p in credits["payees"])
    assert credits["observed_debt_service_monthly"] is None and credits["total_monthly"] == 129000.0


def test_negative_days_and_overdraft_items_are_called_out() -> None:
    cockpit = build_cockpit(
        [
            _statement_with_recurring(
                "s1",
                [
                    _recurring_month(
                        "2026-05-01 to 2026-05-31",
                        [ONDECK],
                        overdrafts=[
                            {"date": "2026-05-12", "description": "OVERDRAFT FEE", "amount": 34, "type": "overdraft_fee"},
                            {"date": "2026-05-12", "description": "Balance -$412.10", "amount": None, "type": "negative_balance"},
                            {"date": "2026-05-20", "description": "RETURNED ITEM FEE", "amount": 34, "type": "returned_item"},
                        ],
                        negative_balance_dates=["2026-05-12", "2026-05-13"],
                        low_daily_balance=-412.1,
                        nsf_or_overdraft_count=2,
                    ),
                    _recurring_month("2026-06-01 to 2026-06-30", [ONDECK], low_daily_balance=800),
                ],
            )
        ],
        [],
    )
    may = cockpit["bank"]["months"][0]
    assert may["negative_dates"] == ["2026-05-12", "2026-05-13"] and may["negative_days"] == 2 and may["fees"] == 68.0
    overdrafts = cockpit["bank"]["overdrafts"]
    assert (overdrafts["negative_days_total"], overdrafts["nsf_total"], overdrafts["fees_total"], overdrafts["lowest_balance"]) == (2, 2, 68.0, -412.1)
    assert overdrafts["negative_months"] == ["2026-05"]
    assert [i["type"] for i in overdrafts["items"]] == ["overdraft_fee", "negative_balance", "returned_item"]
    assert overdrafts["items"][0]["month"] == "2026-05" and overdrafts["items"][0]["account"] == "Chase ••1204"
    assert cockpit["bank"]["summary"]["negative_days_total"] == 2


def test_counted_accounts_narrow_the_combined_months_but_keep_every_account_visible() -> None:
    from app.services.financial_cockpit import bank_file_counted

    analyses = [
        _bank("c1", "2026-05-01 to 2026-05-31", 30000, 25000, ending_balance=5000),
        _bank("s1", "2026-05-01 to 2026-05-31", 2000, 500, ending_balance=9000, bank="Chase", last4="7777"),
    ]
    cockpit = build_cockpit(analyses, [], counted_accounts=["chase ••1234"])
    assert cockpit["bank"]["months"][0]["deposits"] == 30000  # only the counted account
    assert cockpit["bank"]["counted_accounts"] == ["chase ••1234"]
    assert [(a["label"], a["counted"]) for a in cockpit["bank"]["accounts"]] == [("Chase ••1234", True), ("Chase ••7777", False)]
    assert cockpit["bank"]["all_months"][0]["deposits"] == 32000 and cockpit["bank"]["all_summary"]["avg_deposits"] == 32000

    everything = build_cockpit(analyses, [], counted_accounts=[])
    assert everything["bank"]["months"][0]["deposits"] == 32000 and everything["bank"]["all_months"] is None

    stale = build_cockpit(analyses, [], counted_accounts=["gone ••0000"])  # a key no statement matches: every account counts
    assert stale["bank"]["months"][0]["deposits"] == 32000

    assert bank_file_counted({"key_facts": {"bank": "Chase", "account_last4": "7777"}}, ["chase ••7777"]) is True
    assert bank_file_counted({"key_facts": {"bank": "Chase", "account_last4": "7777"}}, ["chase ••1234"]) is False
    assert bank_file_counted(None, ["chase ••1234"]) is False
    assert bank_file_counted(None, []) is True


def test_an_account_keeps_one_identity_however_the_reader_prints_the_bank() -> None:
    from app.services.financial_cockpit import account_key_of

    assert account_key_of({"bank": "JPMorgan Chase Bank, N.A.", "account_last4": "1204"}) == "jpmorgan ••1204"
    assert account_key_of({"bank": "Pinnacle Bank (Horizon 75)", "account_last4": "9519"}) == account_key_of({"bank": "Pinnacle Bank", "account_last4": "9519"})
    assert account_key_of({"bank": "Citizens Bank, N.A.", "account_last4": "x4884"}) == "citizens ••4884"
    assert account_key_of({"bank": "The Bank of Tampa", "account_last4": "0001"}) == "tampa ••0001"
    months, accounts = bank_months(
        [
            _bank("a", "2026-05-01 to 2026-05-31", 10000, 5000, bank="Pinnacle Bank", last4="9519"),
            _bank("b", "2026-06-01 to 2026-06-30", 12000, 6000, bank="Pinnacle Bank (Horizon 75)", last4="9519"),
            _bank("c", "2026-07-01 to 2026-07-31", 9000, 4000, bank="Pinnacle Bank", last4=""),  # the reader missed the digits
        ]
    )
    assert [a["key"] for a in accounts] == ["pinnacle ••9519"]
    assert accounts[0]["months"] == ["2026-05", "2026-06", "2026-07"]
    assert len(months) == 3

    _months, two = bank_months(
        [
            _bank("a", "2026-05-01 to 2026-05-31", 10000, 5000, bank="KeyBank", last4="2108"),
            _bank("b", "2026-05-01 to 2026-05-31", 7000, 1000, bank="KeyBank", last4="4835"),
            _bank("c", "2026-06-01 to 2026-06-30", 9000, 4000, bank="KeyBank", last4=""),  # two candidates: stays apart
        ]
    )
    assert [a["key"] for a in two] == ["keybank", "keybank ••2108", "keybank ••4835"]


def test_the_desk_can_declare_detected_accounts_to_be_one() -> None:
    from app.services.financial_cockpit import resolve_alias

    analyses = [
        _bank("a", "2026-03-01 to 2026-03-31", 10000, 5000, bank="Navy Federal Credit Union", last4="0865"),
        _bank("b", "2026-04-01 to 2026-04-30", 12000, 6000, bank="Navy Federal Credit Union", last4="8547"),
        _bank("c", "2026-05-01 to 2026-05-31", 9000, 4000, bank="Navy Federal Credit Union", last4=""),
        _bank("d", "2026-05-01 to 2026-05-31", 2000, 100, bank="Atlantic Union Bank", last4="5045"),
    ]
    _m, split = bank_months(analyses)
    assert [a["key"] for a in split] == ["atlantic ••5045", "navy", "navy ••0865", "navy ••8547"]
    aliases = {"navy ••0865": "navy ••8547", "navy": "navy ••8547"}
    months, merged = bank_months(analyses, None, aliases)
    assert [a["key"] for a in merged] == ["atlantic ••5045", "navy ••8547"]
    navy = merged[1]
    assert navy["months"] == ["2026-03", "2026-04", "2026-05"] and navy["members"] == ["navy", "navy ••0865", "navy ••8547"]
    assert months[0]["deposits"] == 10000 and months[2]["deposits"] == 11000  # May: navy 9000 + atlantic 2000
    assert resolve_alias("navy ••0865", aliases) == "navy ••8547" and resolve_alias("x", {"x": "y", "y": "x"}) == "y"
    cockpit = build_cockpit(analyses, [], account_aliases=aliases, counted_accounts=["navy ••8547"])
    # A counted key that was folded into another account still counts that account.
    _m, merged_stale = bank_months(analyses, ["navy ••0865"], aliases)
    assert [a["key"] for a in merged_stale if a["counted"]] == ["navy ••8547"]
    assert cockpit["bank"]["months"][0]["deposits"] == 10000 and len(cockpit["bank"]["accounts"]) == 2


def test_the_debt_schedule_is_a_standing_container_that_says_where_it_stands() -> None:
    from app.services.financial_cockpit import schedule_block

    assert schedule_block([], None, [], None)["status"] == "missing"
    requested = {
        "requirement_present": True,
        "requirement_status": "requested",
        "requested_at": "2026-10-01T00:00:00+00:00",
    }
    assert schedule_block([], None, [], requested)["status"] == "requested"
    received = {"requirement_present": True, "requirement_status": "received_unverified"}
    assert schedule_block([], None, [], received)["status"] == "received"
    assert schedule_block([LOAN_ROW], None, [], None)["status"] == "rows"
    voided = {"reason": "No debt on the business", "by": "Jane Desk", "at": "2026-11-03T12:00:00+00:00"}
    block = schedule_block(
        [], None, [], {"requirement_present": True, "requirement_status": "not_applicable", "voided": voided}
    )
    assert block["status"] == "voided" and block["voided"]["by"] == "Jane Desk"
    assert block["can_void"] is False
    assert schedule_block([], None, [], None)["can_void"] is True
    assert schedule_block([LOAN_ROW], None, [], None)["can_void"] is False


def test_a_voided_schedule_means_no_debt_service_unless_the_statements_disagree() -> None:
    service = debt_service([], None, [], None, declared_none=True)
    assert (service["monthly"], service["annual"], service["source"]) == (0.0, 0.0, "declared_none")
    assert service["declaration_conflict"] is False
    seen = {"observed_debt_service_monthly": 1200.0, "payees": []}
    conflicted = debt_service([], None, [], seen, declared_none=True)
    assert conflicted["source"] == "bank_observed" and conflicted["declaration_conflict"] is True

    voided = {"reason": "No debt on the business", "by": "Jane Desk", "at": "2026-11-03T12:00:00+00:00"}
    state = {"requirement_present": True, "requirement_status": "not_applicable", "voided": voided}
    cockpit = build_cockpit([TAX_2025], [], proposed_terms=TERM_SHEET, schedule_state=state)
    assert cockpit["debt"]["schedule"]["status"] == "voided"
    assert cockpit["coverage"]["debt_schedule_status"] == "voided"
    assert cockpit["dscr"]["no_debt"] is True and cockpit["dscr"]["headline"] is None
    assert "voided" in cockpit["dscr"]["reason"]
    assert cockpit["dscr"]["pro_forma_headline"]["pro_forma_value"] == 5.83
    assert any("Jane Desk" in note for note in cockpit["notes"])
    assert not any("No debt service on file" in note for note in cockpit["notes"])

    plain = build_cockpit([TAX_2025], [], proposed_terms=TERM_SHEET)
    assert plain["debt"]["schedule"]["status"] == "missing" and plain["dscr"]["no_debt"] is False
    assert any("not been requested" in note for note in plain["notes"])


def test_a_consolidated_statement_is_one_account_whatever_it_lists() -> None:
    """The Navy Federal shape: one member statement lists six share accounts.
    Only the statement's own account becomes an account; the rest are named."""
    from app.services.financial_cockpit import account_key_of, bank_file_counted

    july_period = "2026-07-01 to 2026-07-31"
    zero = {"beginning_balance": 0.0, "ending_balance": 0.0, "total_deposits_and_credits": 0.0, "total_withdrawals_and_debits": 0.0}
    july = _file("jul", "bank_statement", {"bank": "Navy Federal Credit Union", "statement_period": july_period, "months": [
        {"account_label": "Bus Plus Checking", "account_last4": "0865", "statement_period": july_period, "beginning_balance": 43583.17, "ending_balance": 25959.07, "total_deposits_and_credits": 495315.14, "total_withdrawals_and_debits": 512939.24},
        *[{"account_label": f"Business Checking {d}", "account_last4": d, "statement_period": july_period, **zero} for d in ("8513", "8539", "8547", "8554")],
        {"account_label": "Mbr Business Savings", "account_last4": "5087", "statement_period": july_period, "beginning_balance": 5.06, "ending_balance": 5.06, "total_deposits_and_credits": 0.0, "total_withdrawals_and_debits": 0.0},
    ]})
    march = _file("mar", "bank_statement", {"bank": "Navy Federal Credit Union", "months": [{"statement_period": "2026-03-01 to 2026-03-31", "accounts": [
        {"account_name": "Bus Plus Checking", "account_last4": "0865", "beginning_balance": 133084.58, "ending_balance": 124421.34, "total_deposits_and_credits": 700070.86, "total_withdrawals_and_debits": 708734.1},
        {"account_name": "Business Checking 8513", "account_last4": "8513", **zero},
    ], "combined_totals": {"ending_balance": 124426.4, "previous_balance": 133089.64, "total_deposits_and_credits": 700070.86, "total_withdrawals_and_debits": 708734.1}}]})
    april = _file("apr", "bank_statement", {"bank": "Navy Federal Credit Union", "primary_account": "Bus Plus Checking – 7000000865", "statement_period": "2026-04-01 to 2026-04-30", "beginning_balance": 124421.34, "ending_balance": 23942.24, "total_deposits_and_credits": 738065.75, "total_withdrawals_and_debits": 838544.85})
    august = _file("aug", "bank_statement", {"bank": "Navy Federal Credit Union", "months": [{"statement_period": "2026-08-01 to 2026-08-31", "accounts_summary": [
        {"account_type": "Business Checking", "account_last4": "8513", **zero}],
        "combined_totals": {"ending_balance": 60533.07, "beginning_balance": 25964.13, "total_deposits_and_credits": 548596.23, "total_withdrawals_and_debits": 514027.29}}]})
    atlantic = [_bank(f"au{i}", f"2026-{i:02d}-01 to 2026-{i:02d}-28", 2000000 + i, 1900000, bank="Atlantic Union Bank", last4="5045") for i in range(3, 9)]

    months, accounts = bank_months([july, march, april, august, *atlantic])
    assert [a["key"] for a in accounts] == ["atlantic ••5045", "navy ••0865"]
    navy = accounts[1]
    assert navy["months"] == ["2026-03", "2026-04", "2026-07", "2026-08"]
    by_month = {m["month"]: m for m in navy["series"]}
    assert by_month["2026-07"]["deposits"] == 495315.14 and by_month["2026-07"]["ending_balance"] == 25959.07
    assert by_month["2026-03"]["deposits"] == 700070.86
    assert by_month["2026-08"]["deposits"] == 548596.23  # the combined figure stands in for the missing own line
    assert {s["last4"] for s in navy["sub_accounts"]} == {"8513", "8539", "8547", "8554", "5087"}
    assert months[0]["month"] == "2026-03" and months[0]["deposits"] == 2000003 + 700070.86
    assert account_key_of({"bank": "Navy Federal Credit Union", "primary_account": "Bus Plus Checking – 7000000865"}) == "navy ••0865"
    assert account_key_of(august["key_facts"]) == "navy"  # an empty listed account never names the statement
    assert bank_file_counted({"key_facts": august["key_facts"]}, ["navy ••0865"]) is True
    assert bank_file_counted({"key_facts": august["key_facts"]}, ["atlantic ••5045"]) is False


def test_a_multi_month_file_whose_rows_agree_on_the_digits_is_that_account() -> None:
    six = _file("six", "bank_statement", {"bank": "Chase", "months": [
        {"account_last4": "1204", "statement_period": f"2026-{i:02d}-01 to 2026-{i:02d}-28", "total_deposits_and_credits": 1000 * i, "total_withdrawals_and_debits": 500}
        for i in range(1, 7)
    ]})
    _months, accounts = bank_months([six])
    assert [a["key"] for a in accounts] == ["chase ••1204"] and accounts[0]["months"] == [f"2026-{i:02d}" for i in range(1, 7)]
    assert accounts[0]["sub_accounts"] == []


def test_the_cockpit_carries_the_plan_in_words_and_the_listing_summary_repeats_it() -> None:
    from app.services.financial_cockpit import plan_brief

    plan = {"retire": [], "add_backs": [{"kind": "owner_salary", "amount_monthly": 5000, "note": "Discretionary owner pay"}], "new_loan_amount": 100000, "new_loan_rate_pct": 10, "new_loan_interest_only": True}
    cockpit = build_cockpit([TAX_2025, *SIX_MONTHS], [LOAN_ROW], plan=plan, counted_accounts=None)
    block = cockpit["plan"]
    assert [m["kind"] for m in block["modifications"]] == ["add_back", "loan"]
    assert block["impact"]["active"] and block["impact"]["dscr"]["basis"] == "tax_return"
    assert block["impact"]["loan"]["interest_only"] is True and block["impact"]["loan"]["payment_monthly"] == 833.33
    summary = listing_summary(cockpit)
    assert summary["plan"]["active"] and summary["dscr_after_plan"] == block["evaluation"]["headline_after"]
    assert summary["debt_service_after_monthly"] == block["evaluation"]["debt_service_after_monthly"]
    brief = plan_brief(cockpit)
    assert brief["active"] and brief["dscr_before"] == cockpit["dscr"]["headline"]["value"] and brief["dscr_after"] == summary["dscr_after_plan"]
    assert any(line.startswith("Add back") for line in brief["modifications"])
    quiet = build_cockpit([TAX_2025], [LOAN_ROW])
    assert quiet["plan"]["impact"]["active"] is False and listing_summary(quiet)["plan"]["active"] is False


def test_the_uploaded_schedule_lines_become_the_desk_form_and_the_plan_obligations() -> None:
    from app.services.financial_plan import obligations
    from app.services.financial_statements import debt_rows_from_body, document_debts_as_body

    document = {
        "total_monthly_debt_service": 19607.18,
        "debts": [
            {"lender": "Ally", "current": None, "rate_pct": 11.89, "debt_type": "Vehicle loan", "secured_by": "2025 RAM 1500 VIN 68897", "maturity_date": "2030-09-03", "current_balance": 49139.2, "monthly_payment": 1090.38, "original_amount": 55937.3},
            {"lender": "", "monthly_payment": 1},
            {"lender": "Stellantis Financial Services", "current": True, "rate_pct": 10.11, "debt_type": "Vehicle loan", "current_balance": 54771.35, "monthly_payment": 1166.74},
        ],
    }
    body = document_debts_as_body(document)
    assert [d["lender"] for d in body["debts"]] == ["Ally", "Stellantis Financial Services"]
    rows = debt_rows_from_body(body)
    assert rows is not None and len(rows) == 2
    assert float(rows[0]["balance"]) == 49139.2 and float(rows[0]["monthly_payment"]) == 1090.38
    assert rows[0]["collateral"] == "2025 RAM 1500 VIN 68897"
    assert rows[0]["maturity_on"] is not None and rows[0]["rate"] is not None
    assert rows[1]["payment_status"] == "current"
    listed = obligations({"rows": [], "service": {"source": "debt_schedule_document"}, "document": document, "documented": []}, {"payees": []})
    assert [o["id"] for o in listed] == ["sched:0", "sched:2"]
    assert listed[0]["lender"] == "Ally" and listed[0]["monthly"] == 1090.38 and listed[0]["payoff"] == 49139.2 and listed[0]["in_debt_service"] is True
    assert listed[0]["kind"] == "schedule_document" and listed[0]["origin"] == "uploaded_schedule"
    assert obligations({"rows": [], "service": {"source": "none"}, "document": document, "documented": []}, {"payees": []}) == []


def test_a_schedule_row_edited_in_place_takes_the_figures_and_the_payee_link() -> None:
    from types import SimpleNamespace

    from app.services.financial_statements import apply_debt_row_patch

    row = SimpleNamespace(lender="Arya", category="loan", monthly_payment=None, balance=17688.2, payoff_amount=None, payment_amount=450.0, payment_frequency="daily", count_in_dscr=True, vendor_key=None, status="active")
    changed = apply_debt_row_patch(row, {"monthly_payment": 9000, "category": "Merchant cash advance", "vendor_key": "arya capital", "payoff_amount": 17000})
    assert row.monthly_payment == 9000.0 and row.payoff_amount == 17000.0 and row.vendor_key == "arya capital"
    assert row.category == "mca"  # the closed vocabulary's key for an advance
    assert row.payment_amount is None and row.payment_frequency is None  # the typed month replaces the daily cadence
    assert set(changed) == {"monthly_payment", "category", "vendor_key", "payoff_amount", "payment_amount"}
    assert apply_debt_row_patch(row, {"monthly_payment": 9000}) == []  # nothing new
    apply_debt_row_patch(row, {"vendor_key": "", "status": "dismissed", "count_in_dscr": False})
    assert row.vendor_key is None and row.status == "dismissed" and row.count_in_dscr is False
    apply_debt_row_patch(row, {"lender": "  "})
    assert row.lender == "Arya"  # a blank name keeps the old one
    row.mca_modified, row.mca_modification_note = True, "renegotiated in May"
    assert "mca_modified" in apply_debt_row_patch(row, {"category": "term_loan"})
    assert row.category == "term_loan" and row.mca_modified is None and row.mca_modification_note is None  # the advance questions leave with the type
    row.mca_modified = True
    apply_debt_row_patch(row, {"category": "mca"})
    assert row.mca_modified is True  # an advance keeps its answer
