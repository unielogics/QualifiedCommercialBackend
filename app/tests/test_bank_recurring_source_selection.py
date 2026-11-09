"""A duplicate statement never creates another payment or payoff benefit."""

from copy import deepcopy

from app.services import financial_cockpit as cockpit
from app.services import financial_debt_review, financial_plan


def statement(file_id, month="2026-05", *, last4="1111", accepted=True, amount=400, **extra):
    return {"file_id": file_id, "file_name": f"{file_id}.pdf", "classification": "bank_statement", "accepted": accepted,
            "key_facts": {"bank": "Example Bank", "account_last4": last4, "statement_period": month,
                          "total_deposits_and_credits": 5000, "total_withdrawals_and_debits": 4000,
                          "recurring_debits": [{"payee": "Example funding", "kind_guess": "loan", "total_amount": amount,
                                                "occurrences": 4, "cadence": "weekly"}],
                          "recurring_credits": [{"payer": "Customer", "kind_guess": "customer", "total_amount": 500,
                                                 "occurrences": 1}], "overdraft_items": [], **extra}}


def payee(result, name="Example funding"):
    return next(row for row in result["payees"] if row["payee"] == name)


def test_duplicate_upload_does_not_double_debits_credits_or_payoff_cash_relief():
    sources = [statement("original"), statement("copy")]
    recurring = cockpit.recurring_payments(sources)
    assert payee(recurring)["total"] == payee(recurring)["monthly_average"] == 400
    assert payee(recurring)["occurrences"] == 4
    assert payee(recurring)["file_ids"] == ["original"]
    credits = cockpit.recurring_payments(sources, field="recurring_credits", direction=1)
    assert payee(credits, "Customer")["total"] == 500
    months, _accounts = cockpit.bank_months(sources)
    observed = payee(recurring)
    replay = financial_plan.replay_months(months, [{"linked_payee_key": observed["key"], "monthly": 400}],
                                         {observed["key"]: observed}, recurring["months_read"], None)
    assert replay[0]["removed"] == 400
    assert replay[0]["net_after"] == 1400


def test_accepted_source_wins_tie_and_richer_source_wins_the_same_way_as_cash_totals():
    old = statement("old", accepted=False, amount=800)
    accepted = statement("accepted", accepted=True, amount=400)
    assert payee(cockpit.recurring_payments([old, accepted]))["total"] == 400
    richer = statement("richer", accepted=False, amount=600, beginning_balance=100, ending_balance=1100)
    sources = [accepted, richer]
    months, _ = cockpit.bank_months(sources)
    assert months[0]["file_ids"] == ["richer"]
    assert payee(cockpit.recurring_payments(sources))["file_ids"] == ["richer"]
    assert payee(cockpit.recurring_payments(sources))["total"] == 600


def test_distinct_accounts_remain_distinct_and_explicit_same_account_alias_deduplicates():
    sources = [statement("one"), statement("two", last4="2222")]
    assert payee(cockpit.recurring_payments(sources))["total"] == 800
    aliases = {cockpit.account_key_of(sources[1]["key_facts"]): cockpit.account_key_of(sources[0]["key_facts"])}
    assert payee(cockpit.recurring_payments(sources, aliases=aliases))["total"] == 400
    result = cockpit.build_cockpit(sources, [], account_aliases=aliases)
    assert result["bank"]["recurring"]["observed_debt_service_monthly"] == 400


def test_partial_replacement_retains_per_month_values_without_counting_replaced_month_twice():
    old = statement("combined", months=[
        {"statement_period": "2026-05", "recurring_debits": [{"payee": "Example funding", "total_amount": 100, "occurrences": 1}]},
        {"statement_period": "2026-06", "recurring_debits": [{"payee": "Example funding", "total_amount": 200, "occurrences": 2}]},
    ])
    newer = statement("may", amount=300, beginning_balance=100)
    sources = [old, newer]
    unchanged = deepcopy(sources)
    result = cockpit.recurring_payments(sources)
    assert payee(result)["monthly"] == {"2026-05": 300, "2026-06": 200}
    assert payee(result)["total"] == 500
    assert payee(result)["monthly_average"] == 250
    assert payee(result)["occurrences"] == 6
    assert sources == unchanged
    _, accounts = cockpit.bank_months(sources)
    candidates = financial_debt_review.build_candidates({"debts": []}, sources, accounts)
    candidate = next(row for row in candidates if row["payee"] == "Example funding")
    assert candidate["monthly"] == payee(result)["monthly"]


def test_partly_replaced_file_wide_aggregate_is_unknown_for_surviving_month_and_requests_reread():
    combined = statement("combined", amount=1200, months=[
        {"statement_period": "2026-05"}, {"statement_period": "2026-06"},
    ])
    newer = statement("may", amount=300, beginning_balance=100)
    result = cockpit.recurring_payments([combined, newer])
    assert payee(result)["monthly"] == {"2026-05": 300}
    assert result["months_read"] == ["2026-05"]
    assert result["files_need_reread"] == ["combined"]
    assert payee(result)["total"] == 300  # Never invent half of an aggregate or redistribute it.


def test_wholly_selected_file_keeps_legacy_aggregate_but_a_winner_without_details_is_unknown():
    combined = statement("combined", amount=1200, months=[
        {"statement_period": "2026-05"}, {"statement_period": "2026-06"},
    ])
    assert payee(cockpit.recurring_payments([combined]))["monthly"] == {"2026-05": 600, "2026-06": 600}
    old = statement("old")
    new = statement("new", beginning_balance=100)
    new["key_facts"].pop("recurring_debits")
    result = cockpit.recurring_payments([old, new])
    assert result["payees"] == [] and result["observed_debt_service_monthly"] is None
    assert result["files_need_reread"] == ["new"]


def test_wholly_superseded_file_without_details_does_not_create_a_phantom_reread():
    old = statement("old", accepted=False)
    old["key_facts"].pop("recurring_debits")
    current = statement("current")
    result = cockpit.recurring_payments([old, current])
    assert result["files_need_reread"] == []
    assert result["observed_debt_service_monthly"] == 400
