"""Saved goal/loan summaries must not be reconstructed from live or legacy inputs."""
from app.services.operating_plan_pdf import build_html
from app.tests.test_earnings_improvement import body


def test_legacy_pdf_does_not_invent_an_annual_goal():
    html = build_html(body=body())
    assert "Annual NOI goal - saved" not in html
    assert "$250,000" not in html


def test_captured_goal_keeps_loss_unknown_projection_and_signed_cash_separate():
    snapshot = {"goal_progress": {"goal_revision": 3, "currency": "USD", "current_noi": -15000,
        "zero": 0, "target": 18000, "proposed_noi": None, "proposed_gap": None},
        "cash_bridge": {"currency": "USD", "net_loan_cash_change_monthly": -500,
            "modeled_operations_monthly": 1000, "combined_incremental_monthly": 500}}
    html = build_html(body=body(), financial_totals={"improvement_plan": snapshot})
    assert "Annual NOI goal - saved revision 3" in html
    assert "-$15,000.00" in html and "$18,000.00" in html
    assert "Proposed annual NOI</td><td class='num'>Not available" in html
    assert "Net loan cash change</td><td class='num'>-$500.00" in html
    assert "Combined incremental change</td><td class='num'>$500.00" in html
    assert "Recurring released-cash-flow allocation" not in html
