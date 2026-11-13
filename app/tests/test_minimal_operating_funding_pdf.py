"""Saved PDF funding readout uses the same captured pool as the interface."""
from app.services.operating_plan_pdf import _goal_tracking


def snapshot(**cash):
    return {"goal_progress": {"goal_revision": 1, "currency": "USD", "current_noi": -100,
        "zero": 0, "proposed_noi": 1100, "target": 2000, "proposed_gap": 900}, "cash_bridge": cash}


def test_saved_funding_pool_replaces_loan_only_assignment_totals_without_recomputing():
    captured = snapshot(currency="USD", net_loan_cash_change_monthly=3500, modeled_operations_monthly=100,
        combined_incremental_monthly=3600, allocated_monthly=3600, unallocated_monthly=-100,
        funding_capacity_monthly=3600, funding_allocated_monthly=3600, funding_unallocated_monthly=0,
        operating_savings_monthly=100)
    value = _goal_tracking(captured, {"released_cash_flow": {"allocations": [{"id": "other", "amount_monthly": 99999}]}})
    assert "Plan funding pool</td><td class='num'>$3,600.00" in value
    assert "Validated cash expense savings</td><td class='num'>$100.00" in value
    assert "Still to assign</td><td class='num'>$0.00" in value
    assert "Net loan cash change</td><td class='num'>$3,500.00" in value
    assert "Assigned loan cash relief" not in value and "Still to assign</td><td class='num'>-$100.00" not in value and "$99,999.00" not in value


def test_missing_pool_is_not_replaced_by_a_known_subtotal_or_old_loan_capacity():
    value = _goal_tracking(snapshot(currency="USD", funding_capacity_monthly=None,
        funding_known_capacity_monthly=3500, recurring_cash_released_monthly=3500,
        funding_unallocated_monthly=None, unallocated_monthly=100), {})
    assert "Plan funding pool</td><td class='num'>Not available" in value
    assert "Still to assign</td><td class='num'>Not available" in value
    assert "$3,500.00" not in value


def test_ops_only_pool_uses_its_explicit_captured_currency():
    value = _goal_tracking(snapshot(currency=None, funding_currency="USD", funding_capacity_monthly=100,
        funding_allocated_monthly=100, funding_unallocated_monthly=0, operating_savings_monthly=100), {})
    assert "Plan funding pool</td><td class='num'>$100.00" in value
    assert "Currency unconfirmed" not in value


def test_legacy_loan_snapshot_keeps_original_assignment_fields():
    value = _goal_tracking(snapshot(currency="USD", allocated_monthly=90, unallocated_monthly=10), {})
    assert "Assigned loan cash relief</td><td class='num'>$90.00" in value
    assert "Still to assign</td><td class='num'>$10.00" in value
    assert "Plan funding pool" not in value


def test_default_kept_cash_has_no_instruction_to_allocate_it():
    value = _goal_tracking(snapshot(currency="USD", funding_capacity_monthly=3600,
        funding_retained_monthly=3600, funding_reinvested_monthly=0,
        funding_allocated_monthly=0, funding_unallocated_monthly=3600, operating_savings_monthly=100), {})
    assert "Kept as cash</td><td class='num'>$3,600.00" in value
    assert "Used in the plan</td><td class='num'>$0.00" in value
    assert "Still to assign" not in value and "Assigned plan funding" not in value
    assert "reinvestment is optional" in value
