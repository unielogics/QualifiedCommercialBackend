from __future__ import annotations

import pytest

from app.services import production_arrangement as arrangement
from app.services import production_term_structure as terms


def _calculate(**overrides):
    payload = {
        "funding_party_kind": "Lender",
        "facility_type": "Term loan",
        "approved_amount": 500_000,
        "min_activation_amount": 1,
        "rate_pct": 9,
        "term_months": 24,
        "monthly_debt_service": None,
        "debt_service_is_level_payment": False,
    }
    payload.update(overrides)
    return terms.calculate(payload)


def test_legacy_level_payment_keeps_the_existing_monthly_formula() -> None:
    result = _calculate(
        approved_amount=120_000,
        rate_pct=12,
        term_months=12,
        debt_service_is_level_payment=True,
    )

    expected = arrangement.level_payment(120_000, 12, 12)
    assert result["repayment_structure"] == "fully_amortizing"
    assert result["periodic_payment"] == pytest.approx(expected, abs=0.01)
    assert result["monthly_equivalent_payment"] == pytest.approx(expected, abs=0.01)
    assert result["annual_debt_service"] == pytest.approx(expected * 12, abs=0.12)
    assert result["balloon_amount"] == 0


@pytest.mark.parametrize(
    ("rate_structure", "payment_frequency", "expected"),
    (
        ("fixed", "monthly", True),
        ("variable", "monthly", False),
        ("fixed", "weekly", False),
    ),
)
def test_level_payment_compatibility_requires_fixed_rate_and_monthly_cadence(
    rate_structure: str,
    payment_frequency: str,
    expected: bool,
) -> None:
    result = _calculate(
        repayment_structure="fully_amortizing",
        rate_structure=rate_structure,
        payment_frequency=payment_frequency,
        rate_index="Prime" if rate_structure == "variable" else None,
        rate_index_rate_pct=7 if rate_structure == "variable" else None,
        rate_margin_pct=2 if rate_structure == "variable" else None,
        rate_as_of="2026-09-16" if rate_structure == "variable" else None,
    )

    assert terms.is_level_payment(result, term_months=24) is expected


def test_interest_only_excludes_maturity_balloon_from_annual_dscr_service() -> None:
    result = _calculate(repayment_structure="interest_only")

    assert result["periodic_payment"] == 3_750
    assert result["monthly_equivalent_payment"] == 3_750
    assert result["annual_debt_service"] == 45_000
    assert result["balloon_amount"] == 500_000
    assert result["total_repayment"] == 590_000
    assert result["financing_cost"] == 90_000


def test_io_then_amortizing_has_distinct_first_and_step_up_payments() -> None:
    result = _calculate(
        rate_pct=12,
        term_months=36,
        repayment_structure="interest_only_then_amortizing",
        interest_only_months=12,
        amortization_months=24,
    )

    assert result["periodic_payment"] == 5_000
    assert result["post_io_payment"] == pytest.approx(23_536.74, abs=0.01)
    assert result["monthly_equivalent_payment"] == 5_000
    assert result["annual_debt_service"] == 60_000
    assert result["monthly_program_coverage_amount"] == result["post_io_payment"]
    assert result["balloon_amount"] == 0


def test_io_then_amortizing_rejects_payoff_shorter_than_the_remaining_phase() -> None:
    result = _calculate(
        term_months=36,
        repayment_structure="interest_only_then_amortizing",
        interest_only_months=12,
        amortization_months=18,
    )

    assert "cannot be shorter than the remaining term" in " ".join(terms.validation_errors(result))


def test_io_then_amortizing_lender_override_applies_to_io_phase_only() -> None:
    result = _calculate(
        rate_pct=12,
        term_months=36,
        repayment_structure="interest_only_then_amortizing",
        interest_only_months=12,
        amortization_months=24,
        lender_payment_override=True,
        periodic_payment=5_500,
    )

    assert result["periodic_payment"] == 5_500
    assert result["post_io_payment"] == pytest.approx(23_536.74, abs=0.01)
    assert result["annual_debt_service"] == 66_000
    assert result["monthly_program_coverage_amount"] == result["post_io_payment"]


def test_revolving_variable_rate_uses_draw_balance_and_current_index_snapshot() -> None:
    result = _calculate(
        facility_type="Revolving line of credit",
        facility_kind="revolving_loc",
        repayment_structure="revolving_interest_only",
        initial_draw_amount=100_000,
        payment_basis_amount=100_000,
        rate_structure="variable",
        rate_index="Prime",
        rate_index_rate_pct=8.5,
        rate_margin_pct=2,
        rate_floor_pct=9,
        rate_cap_pct=11,
        rate_as_of="2026-09-16",
    )

    assert result["effective_rate_pct"] == 10.5
    assert result["periodic_payment"] == 875
    assert result["monthly_equivalent_payment"] == 875
    assert result["annual_debt_service"] == 10_500
    assert result["balloon_amount"] == 100_000
    assert "Prime 8.50% + 2.00%" in terms.rate_label(result)
    assert "2026-09-16" in terms.rate_label(result)


def test_variable_rate_label_uses_custom_index_description_and_formats_negative_margin() -> None:
    result = _calculate(
        rate_structure="variable",
        rate_index="Other",
        custom_rate_description="SOFR, reset monthly",
        rate_index_rate_pct=8.5,
        rate_margin_pct=-1,
        rate_as_of="2026-09-16",
    )

    assert terms.rate_label(result).startswith(
        "7.50% current (SOFR, reset monthly 8.50% - 1.00%) as of 2026-09-16"
    )
    assert "+ -" not in terms.rate_label(result)


def test_balloon_amortization_keeps_balloon_separate_from_annual_service() -> None:
    result = _calculate(
        repayment_structure="balloon",
        term_months=60,
        amortization_months=360,
    )

    assert result["periodic_payment"] == pytest.approx(4_023.11, abs=0.01)
    assert result["annual_debt_service"] == pytest.approx(result["periodic_payment"] * 12, abs=0.12)
    assert result["balloon_amount"] == pytest.approx(479_400.68, abs=0.02)
    assert result["total_repayment"] == pytest.approx(result["periodic_payment"] * 60 + result["balloon_amount"], abs=0.6)


def test_explicit_balloon_is_the_target_balance_for_server_payment_math() -> None:
    result = _calculate(
        repayment_structure="balloon",
        term_months=60,
        amortization_months=360,
        balloon_amount=400_000,
    )

    assert result["balloon_amount"] == 400_000
    assert result["periodic_payment"] == pytest.approx(5_075.84, abs=0.01)
    assert result["annual_debt_service"] == pytest.approx(result["periodic_payment"] * 12, abs=0.12)


def test_cadence_contract_matches_frontend_and_normalizes_custom_payments() -> None:
    assert terms.CADENCE_PERIODS_PER_YEAR == {
        "daily": 252.0,
        "weekly": 51.96,
        "biweekly": 25.98,
        "monthly": 12.0,
    }
    result = _calculate(
        repayment_structure="custom",
        payment_frequency="weekly",
        periodic_payment=500,
        lender_payment_override=True,
        custom_payment_description="Weekly seasonal payment agreed by the lender.",
    )
    assert result["payment_count"] == 104
    assert result["monthly_equivalent_payment"] == 2_165
    assert result["annual_debt_service"] == 25_980


def test_expiration_is_presentation_metadata_until_the_offer_is_issued() -> None:
    result = _calculate(expiration_days=7)

    assert result["expiration_days"] == 7
    assert result["expires_on"] is None


def test_switching_structures_discards_stale_io_amortization_and_balloon_fields() -> None:
    result = _calculate(
        repayment_structure="fully_amortizing",
        extra={
            "interest_only_months": 12,
            "amortization_months": 360,
            "balloon_amount": 450_000,
        },
    )

    assert result["interest_only_months"] == 0
    assert result["amortization_months"] == 24
    assert result["balloon_amount"] == 0

    pure_io = _calculate(
        repayment_structure="interest_only",
        extra={"interest_only_months": 6, "amortization_months": 360},
    )
    assert pure_io["interest_only_months"] == 24
    assert pure_io["amortization_months"] is None


def test_payment_summary_object_and_unknown_extra_metadata_round_trip() -> None:
    snapshot = {
        "periodic_payment": 3_750,
        "lines": ["browser display line"],
        "assumptions": ["browser display assumption"],
    }
    extra = {
        "payment_summary": snapshot,
        "custom_rate_description": "Prime plus lender spread",
        "future_metadata": {"source": "desk"},
    }
    result = _calculate(extra=extra, repayment_structure="interest_only")
    stored = terms.stored_extra(extra, result)

    assert stored["payment_summary"] == snapshot
    assert isinstance(stored["payment_summary"], dict)
    assert stored["future_metadata"] == {"source": "desk"}
    assert "browser display line" not in terms.payment_summary_text(result)


def test_post_io_monthly_equivalent_and_disclosed_apr_round_trip_through_extra() -> None:
    result = _calculate(
        repayment_structure="interest_only_then_amortizing",
        interest_only_months=12,
        amortization_months=12,
        apr_pct=11.75,
    )
    stored = terms.stored_extra({}, result)

    assert stored["post_io_monthly_equivalent"] == result["post_io_payment"]
    assert stored["apr_pct"] == 11.75


def test_debt_service_treatment_requires_refinance_remainder_and_clears_stale_additive_value() -> None:
    missing = _calculate(debt_service_treatment="refinance")
    assert "enter 0 for a full payoff" in " ".join(terms.validation_errors(missing))

    full_payoff = _calculate(debt_service_treatment="refinance", retained_annual_debt_service=0)
    assert full_payoff["retained_annual_debt_service"] == 0
    assert "full payoff" not in " ".join(terms.validation_errors(full_payoff))

    additive = _calculate(debt_service_treatment="additive", retained_annual_debt_service=45_000)
    assert additive["retained_annual_debt_service"] is None


def test_structured_validation_catches_variable_rate_and_zero_draw_failures() -> None:
    missing_index = _calculate(
        facility_kind="revolving_loc",
        repayment_structure="revolving_interest_only",
        initial_draw_amount=100_000,
        payment_basis_amount=100_000,
        rate_structure="variable",
    )
    joined = " ".join(terms.validation_errors(missing_index))
    assert "rate index" in joined
    assert "current index rate and margin" in joined
    assert "rate-as-of date" in joined

    zero_draw = _calculate(
        facility_kind="revolving_loc",
        repayment_structure="revolving_interest_only",
        initial_draw_amount=0,
        payment_basis_amount=0,
    )
    joined = " ".join(terms.validation_errors(zero_draw))
    assert "zero-payment term sheet" in joined
