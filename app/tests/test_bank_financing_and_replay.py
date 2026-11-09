"""Borrowed cash and combined lender observations do not create earnings or duplicate payoff benefit."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import financial_cockpit as cockpit
from app.services import financial_metric_composition as composition
from app.services import financial_plan


def rows():
    return [{"id": "weekly", "lender": "Example Lender", "monthly_payment": 5253.34, "balance": 40000,
             "is_mca": False, "count_in_dscr": True, "origin": "admin"},
            {"id": "line", "lender": "Example Lender", "monthly_payment": 3343.17, "balance": 24000,
             "is_mca": False, "count_in_dscr": True, "origin": "admin"}]


def observed():
    return {"key": "combined", "payee": "Example Lender", "monthly_average": 6127.01,
            "monthly": {"2026-09": 8649.81}, "cadence": "irregular", "is_debt": True}


def evaluate(obligations, retire):
    return financial_plan.evaluate_plan(financial_plan.normalize_plan({"retire": retire}), obligations_list=obligations,
        recurring={"payees": [observed()], "months_read": ["2026-09"]},
        months=[{"month": "2026-09", "deposits": 10000, "withdrawals": 9000, "net": 1000,
                 "ending_balance": 2000, "low_balance": 100}],
        dscr={"headline": {"key": "tax_return", "numerator": -54369, "value": -.26, "currency_comparable": True},
              "candidates": [{"key": "tax_return", "numerator": -54369, "value": -.26, "currency_comparable": True}]},
        service={"monthly": 17578.26}, proposed=None, requested_amount=None)


@pytest.mark.parametrize("selected", [["row:weekly"], ["row:weekly", "row:line"]])
def test_two_same_lender_loans_do_not_reuse_or_double_remove_one_combined_observation(selected):
    obligations = financial_plan.obligations({"rows": rows(), "service": {"source": "accepted_debt_schedule", "schedule_authoritative": True}},
                                             {"payees": [observed()]})
    assert all(row["replay_requires_review"] and row["linked_payee_key"] is None for row in obligations)
    assert all(row["candidate_payee_keys"] == ["combined"] for row in obligations)
    result = evaluate(obligations, selected)
    assert result["retired_monthly"] == sum(row["monthly"] for row in obligations if row["id"] in selected)
    assert result["debt_service_after_monthly"] == round(17578.26 - result["retired_monthly"], 2)
    assert result["income_before_annual"] == result["income_after_annual"] == -54369
    assert result["retirement_replay_unknown"]
    assert result["replay_projection_available"] is False
    assert result["replay_unavailable_reason"] == financial_plan.AMBIGUOUS_REPLAY_REASON
    for name in ("net_after", "withdrawals_after", "ending_after", "lowest_after", "removed", "added", "cumulative"):
        assert result["replay"][0][name] is None
    assert result["cash_kept"] is None and result["months_dipping_negative_after"] is None
    assert result["replay"][0]["net"] == 1000


def test_unique_explicit_payee_bindings_preserve_separate_statement_streams():
    separate_rows = rows()
    separate_rows[0]["vendor_key"], separate_rows[1]["vendor_key"] = "weekly-stream", "monthly-stream"
    payees = [{**observed(), "key": "weekly-stream"}, {**observed(), "key": "monthly-stream"}]
    obligations = financial_plan.obligations({"rows": separate_rows, "service": {"source": "accepted_debt_schedule", "schedule_authoritative": True}}, {"payees": payees})
    assert [row["linked_payee_key"] for row in obligations] == ["weekly-stream", "monthly-stream"]
    assert not any(row["replay_requires_review"] for row in obligations)


def test_stale_duplicate_explicit_observation_keys_are_unsafe_even_when_only_one_loan_is_selected():
    obligations = [{"id": str(index), "monthly": 100, "linked_payee_key": "combined", "in_debt_service": True} for index in range(2)]
    result = evaluate(obligations, ["0"])
    assert result["retirement_replay_unknown"] and result["replay_review_required_ids"] == ["0"]
    assert result["replay"][0]["removed"] is None
    assert result["retired_monthly"] == 100


def statement(index, *, funding=0, account="1111", empty=False):
    facts = {"bank": "Example Bank", "account_last4": account, "currency": "USD", "statement_period": f"2026-0{index}",
             "total_deposits_and_credits": 10000, "total_withdrawals_and_debits": 8000,
             "recurring_debits": [], "recurring_credits": [] if not funding else [
                 {"payee": "Example Loan Funding", "kind_guess": "loan_proceeds", "total_amount": funding, "occurrences": 1}],
             "overdraft_items": []}
    if empty:
        facts.pop("recurring_credits")
    return {"file_id": f"{account}-{index}", "classification": "bank_statement", "accepted": True, "key_facts": facts}


def bank_cockpit(sources, state=None, counted=None):
    return cockpit.build_cockpit(sources, [{"id": "loan", "lender": "Contract Lender", "monthly_payment": 1000, "balance": 10000, "origin": "admin"}],
                                  metric_review_state=state, counted_accounts=counted)


def bank_candidate(value):
    return next(row for row in value["dscr"]["candidates"] if row["key"] == "bank_cash_flow")


def test_identified_financing_is_excluded_from_bank_earnings_without_changing_cash_or_contractual_debt():
    sources = [statement(1, funding=6000), statement(2), statement(3)]
    original = deepcopy(sources)
    result = bank_cockpit(sources)
    candidate = bank_candidate(result)
    assert result["bank"]["summary"]["avg_net"] == 2000
    assert result["debt"]["service"]["monthly"] == 1000
    assert candidate["numerator"] == 12000  # (cash retained 2000 + scheduled service1000 - financing2000) ×12.
    assert candidate["value"] == 1
    assert "scheduled debt service" in candidate["numerator_label"]
    assert "floor" not in candidate["note"]
    assert candidate["financing_adjustment"]["identified_total"] == 6000
    assert sources == original


def test_known_funding_is_spread_over_bank_net_months_not_only_months_with_read_credit_groups():
    sources = [statement(index, funding=28000 if index == 5 else 0, empty=index < 5) for index in range(1, 7)]
    result = bank_cockpit(sources)
    candidate = bank_candidate(result)
    assert candidate["financing_adjustment"]["identified_total"] == 28000
    assert candidate["financing_adjustment"]["identified_monthly"] == 28000 / 6
    assert candidate["financing_adjustment"]["unread_credit_months"] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert candidate["numerator"] == -20000
    calculation = next(row for row in result["metric_review"]["calculations"] if row["key"] == "dscr.bank_cash_flow")
    recipe = calculation["recipe"]
    assert recipe["annualize_before_rounding"]
    assert recipe["reviewed_numerator"] == candidate["numerator"]


def test_funding_in_an_excluded_account_does_not_reduce_the_counted_account_cash_proxy():
    selected = [statement(index) for index in range(1, 4)]
    excluded = [statement(index, funding=9000, account="2222") for index in range(1, 4)]
    result = bank_cockpit(selected + excluded, counted=[cockpit.account_key_of(selected[0]["key_facts"])])
    assert bank_candidate(result)["numerator"] == 36000
    assert bank_candidate(result)["financing_adjustment"]["identified_total"] == 0


def test_funding_recipe_deduction_is_readonly_mandatory_and_old_recipe_goes_stale_when_source_changes():
    sources = [statement(1, funding=6000), statement(2), statement(3)]
    current = bank_cockpit(sources)
    review = current["metric_review"]
    calc = next(row for row in review["calculations"] if row["key"] == "dscr.bank_cash_flow")
    recipe = calc["recipe"]
    funding = next(row for row in recipe["components"] if row["input_key"] == "bank.identified_financing_credits_monthly")
    assert funding["required"] and funding["coefficient"] == -1 and funding["contribution"] == -2000
    source = next(row for row in review["inputs"] if row["key"] == funding["input_key"])
    assert source["editable"] is False
    command = {"calculation_key": "dscr.bank_cash_flow", "action": "compose", "components": [
        {"input_key": row["input_key"], "included": row["included"]} for row in recipe["components"]], "adjustments": [],
        "reason": "Reviewed the source loan funding", "expected_revision": review["revision"],
        "source_stamp": recipe["source_stamp"], "idempotency_key": "funding-review"}
    actor = SimpleNamespace(id="staff", full_name="Staff", role=Role.SUPER_ADMIN)
    missing = deepcopy(command)
    next(row for row in missing["components"] if row["input_key"] == funding["input_key"])["included"] = False
    with pytest.raises(HTTPException, match="Identified financing credits must remain excluded"):
        composition.change({}, review, actor=actor, **missing)
    state, _ = composition.change({}, review, actor=actor, **command)
    changed = deepcopy(sources)
    changed[0]["key_facts"]["recurring_credits"][0]["total_amount"] = 9000
    next_result = bank_cockpit(changed, state)
    changed_calc = next(row for row in next_result["metric_review"]["calculations"] if row["key"] == "dscr.bank_cash_flow")
    assert changed_calc["recipe"]["stale"] and changed_calc["recipe"]["current_event"] is None
    assert bank_candidate(next_result)["numerator"] == 0
