from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.dealer_os import router
from app.dealer_os.services import recurrence, workflow_readiness


def test_workflow_completion_and_navigation_are_separate() -> None:
    gated = workflow_readiness.build_workflow(
        workflow_ungated=False,
        step_1_blockers=[],
        step_2_blockers=["Complete bank evidence."],
        step_3_blockers=["Confirm financials."],
        step_4_blockers=["Execute package."],
    )
    assert gated["step_1"]["complete"] is True
    assert gated["step_2"]["available"] is True
    assert gated["step_2"]["complete"] is False
    assert gated["step_3"]["available"] is False

    ungated = workflow_readiness.build_workflow(
        workflow_ungated=True,
        step_1_blockers=[],
        step_2_blockers=["Complete bank evidence."],
        step_3_blockers=["Confirm financials."],
        step_4_blockers=["Execute package."],
    )
    assert ungated["step_3"]["available"] is True
    assert ungated["step_4"]["available"] is True
    assert ungated["step_2"]["complete"] is False
    assert ungated["step_4"]["complete"] is False


def test_debt_confirmation_hash_changes_only_when_source_changes() -> None:
    row = SimpleNamespace(
        id="debt-1",
        lender="Main Street Bank",
        category="loan",
        balance=100_000,
        payment_amount=2_500,
        payment_frequency="monthly",
        monthly_payment=2_500,
        rate=8.5,
        factor_rate=None,
        maturity_on=None,
        payoff_amount=98_000,
        collateral="Business assets",
        count_in_dscr=True,
        status="active",
    )
    first = workflow_readiness.debt_source_hash([row])
    second = workflow_readiness.debt_source_hash([row])
    row.monthly_payment = 2_750
    changed = workflow_readiness.debt_source_hash([row])
    assert first == second
    assert changed != first


def test_bank_exception_accepts_three_to_five_current_months_only() -> None:
    current_five = recurrence.compute_freshness([], date.today(), window=5)["expected_months"]
    current_three = recurrence.compute_freshness([], date.today(), window=3)["expected_months"]
    assert router._bank_exception_window(current_five) == (5, [])
    assert router._bank_exception_window(current_three) == (3, [])

    too_few = current_three[-2:]
    accepted, missing = router._bank_exception_window(too_few)
    assert accepted is None
    assert missing

    noncontiguous = [current_five[0], current_five[1], current_five[3], current_five[4]]
    accepted, missing = router._bank_exception_window(noncontiguous)
    assert accepted is None
    assert current_five[2] in missing


def test_credit_quality_uses_safe_tier_and_range() -> None:
    expected = {
        850: ("Excellent", "760–850"),
        760: ("Excellent", "760–850"),
        759: ("Good", "720–759"),
        720: ("Good", "720–759"),
        719: ("Average", "700–719"),
        700: ("Average", "700–719"),
        699: ("Below average", "680–699"),
        680: ("Below average", "680–699"),
        679: ("Bad", "660–679"),
        660: ("Bad", "660–679"),
        659: ("Not fundable", "300–659"),
        300: ("Not fundable", "300–659"),
    }
    for score, (tier, score_range) in expected.items():
        assert router._credit_tier(score) == tier
        assert router._credit_score_band(score) == score_range
    assert router._credit_tier(None) is None
    assert router._credit_score_band(None) is None
    assert router._credit_tier(299) is None
    assert router._credit_score_band(851) is None
