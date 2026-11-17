"""A real-estate prequalification waits only on questions that fit the deal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.routers.dealer_ai_intake as core
from app.services import business_purpose


def _intake(transaction: str, **details) -> SimpleNamespace:
    return SimpleNamespace(
        id="intake-1",
        variant=core.FUNDING_VARIANT,
        loan_purpose=transaction,
        bucket=None,
        intake_state={
            "funding_review_basics": {
                "target_property_address": "3844 Sudbury Rd, Charlotte NC 28205",
                "transaction_type": transaction,
                "requested_amount": 300000,
                "estimated_value_or_purchase_price": 550000,
            },
            "funding_review_details": details,
        },
    )


@pytest.fixture
def evidence_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_credit_pull_state", lambda intake: {"fico": 720})
    monkeypatch.setattr(core, "_missing_required_docs", lambda bucket: [])


def test_a_home_equity_line_never_waits_on_a_down_payment(evidence_complete: None) -> None:
    intake = _intake(
        "Home equity (HELOC)",
        occupancy="primary_residence",
        business_purpose=True,
        prior_property_ownership=True,
        is_commercial_property=False,
    )
    assert core._re_prequal_missing(intake) == []
    assert core._re_prequal_ready(intake)


def test_a_purchase_still_asks_for_the_down_payment(evidence_complete: None) -> None:
    intake = _intake(
        "Purchase",
        occupancy="investment",
        business_purpose=True,
        prior_property_ownership=False,
        is_commercial_property=False,
    )
    assert core._re_prequal_missing(intake) == ["down_payment_amount"]
    assert not core._re_prequal_ready(intake)
    intake.intake_state["funding_review_details"]["down_payment_amount"] = 125000.0
    assert core._re_prequal_ready(intake)


def test_occupancy_and_purpose_are_asked_on_every_file(evidence_complete: None) -> None:
    intake = _intake("Refinance", prior_property_ownership=True, is_commercial_property=False)
    assert core._re_prequal_missing(intake) == ["occupancy", "business_purpose"]
    assert not core._re_prequal_ready(intake)


def test_an_open_personal_use_review_stops_the_prequalification(evidence_complete: None) -> None:
    intake = _intake(
        "Home equity (HELOC)",
        occupancy="primary_residence",
        business_purpose=False,
        prior_property_ownership=True,
        is_commercial_property=False,
    )
    intake.intake_state[business_purpose.REVIEW_KEY] = {"status": business_purpose.HELD}
    assert not core._re_prequal_ready(intake)
    intake.intake_state[business_purpose.REVIEW_KEY] = {"status": business_purpose.NEEDS_REVIEW}
    assert not core._re_prequal_ready(intake)
    intake.intake_state[business_purpose.REVIEW_KEY] = {"status": business_purpose.CLEARED}
    intake.intake_state["funding_review_details"]["business_purpose"] = True
    assert core._re_prequal_ready(intake)


def test_the_chat_rules_ask_purpose_first_and_never_a_down_payment_on_a_refinance() -> None:
    from app.services import bucket_ai

    rules = bucket_ai.RE_CHAT_RULES
    assert "business-purpose loans only" in rules
    assert "Never ask for a down payment on a refinance" in rules
    assert "personal_use_review" in rules
    assert "never put who lives there" in rules
