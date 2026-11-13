"""Saved financing bank replay remains distinct from operating assumptions."""
import json

import pytest

from app.services import earnings_improvement
from app.services.operating_plan_guidance import client_evaluation
from app.tests.test_earnings_improvement import NOW, body
from app.tests.test_refinance_reinvestment_independent import saved_loan


def bank_context():
    cockpit = saved_loan()
    cockpit["plan"]["evaluation"].update(
        debt_service_after_annual=72000, replay_projection_available=True,
        replay=[
            {"net": -1000, "net_after": 3000, "ending": 2000, "ending_after": 6000,
             "lowest": -300, "lowest_after": 1700},
            {"net": 400, "net_after": 4400, "ending": 2400, "ending_after": 10400,
             "lowest": 0, "lowest_after": 4000},
        ],
    )
    return cockpit


def compare(cockpit, raw=None):
    return earnings_improvement.evaluate(raw if raw is not None else body(), cockpit, now=NOW, include_expense_changes=True)


def test_saved_financing_replay_reports_numeric_before_and_after_without_operating_rewrites():
    current = bank_context()
    raw = body()
    raw["adjustments"] = []
    read = compare(current, raw)["comparison"]["bank_replay"]
    assert read == {
        "scope": "saved_financing_plan_only", "currency": "USD", "replay_available": True,
        "replay_reason": None, "month_count": 2, "complete": True,
        "before": {"net_monthly": -300, "ending_balance": 2400, "negative_months": 1,
            "net_months_observed": 2, "ending_months_observed": 2, "low_balance_months_observed": 2},
        "after": {"net_monthly": 3700, "ending_balance": 10400, "negative_months": 0,
            "net_months_observed": 2, "ending_months_observed": 2, "low_balance_months_observed": 2},
    }
    changed = body()
    changed["adjustments"][0]["proposed_amount"] = 500
    assert compare(current, changed)["comparison"]["bank_replay"] == read


def test_partial_observations_never_turn_missing_balances_or_negative_months_into_zero():
    current = bank_context()
    rows = current["plan"]["evaluation"]["replay"]
    rows[0].update(lowest=None, lowest_after=None)
    rows[1].update(net=None, ending=None, lowest=None, net_after=None, ending_after=None, lowest_after=None)
    read = compare(current)["comparison"]["bank_replay"]
    assert read["replay_available"] and not read["complete"]
    assert read["before"] == {"net_monthly": -1000, "ending_balance": None, "negative_months": None,
        "net_months_observed": 1, "ending_months_observed": 1, "low_balance_months_observed": 0}
    assert read["after"]["ending_balance"] is None
    assert read["after"]["negative_months"] is None
    assert read["after"]["net_months_observed"] == 1


@pytest.mark.parametrize(("mutation", "reason"), [
    ("not_saved", "not_saved"), ("no_bank_months", "no_bank_months"),
    ("replay_blocked", "financing_inputs_incomplete"), ("currency", "currency_unconfirmed"),
])
def test_unavailable_financing_replay_preserves_unknown_outcomes(mutation, reason):
    current = bank_context()
    evaluation = current["plan"]["evaluation"]
    if mutation == "not_saved":
        current["plan"]["record"].pop("updated_at")
    elif mutation == "no_bank_months":
        evaluation["replay"] = []
    elif mutation == "replay_blocked":
        evaluation.update(replay_projection_available=False, replay_unavailable_reason="Private borrower detail")
    else:
        current["debt"]["service"]["currency"] = "EUR"
    read = compare(current)["comparison"]["bank_replay"]
    assert not read["replay_available"] and not read["complete"]
    assert read["replay_reason"] == reason
    assert all(read["after"][key] is None for key in ("net_monthly", "ending_balance", "negative_months"))
    assert read["after"]["net_months_observed"] == 0
    if mutation == "no_bank_months":
        assert read["before"]["negative_months"] is None


def test_public_bank_comparison_is_numeric_metadata_without_identifiers_or_free_text():
    current = bank_context()
    loan = current["plan"]["evaluation"]
    loan["private_borrower_name"] = "Fictional Borrower"
    loan["replay"][0].update(month="2026-01", lender="Fictional Bank", profile_id="private-profile")
    result = compare(current)
    public = client_evaluation(result)["comparison"]["bank_replay"]
    assert public == result["comparison"]["bank_replay"]
    encoded = json.dumps(public)
    assert all(value not in encoded for value in ("Fictional", "private-profile", "2026-01", "profile_id"))
    assert set(public["before"]) == {"net_monthly", "ending_balance", "negative_months",
        "net_months_observed", "ending_months_observed", "low_balance_months_observed"}
