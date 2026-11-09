from copy import deepcopy

from app.services.financial_calculation_policy import (
    authority_idempotency_key,
    authority_matches,
    financial_authority,
)
from app.tests.test_financial_metric_review import build, correction, pnl


def test_authority_is_stable_across_render_time_and_hypothetical_plans():
    first = build()
    original = deepcopy(first)
    authority = financial_authority(first)
    again = deepcopy(first)
    again["generated_at"] = "2099-01-01T00:00:00Z"
    again["plan"] = {"hypothetical_new_loan": 9999999}
    assert financial_authority(again) == authority
    assert first == original


def test_factual_correction_and_new_analysis_invalidate_the_same_request_key():
    original = build()
    before = financial_authority(original)
    corrected = financial_authority(build(state=correction(original)))
    replacement = pnl()
    replacement["analysis_id"] = "new-analysis"
    after_analysis = financial_authority(build([replacement]))
    assert before["signature"] != corrected["signature"] != after_analysis["signature"]
    assert authority_idempotency_key("same-event", before) != authority_idempotency_key("same-event", corrected)
    assert authority_idempotency_key("same-event", before) == authority_idempotency_key("same-event", before)
    assert not authority_matches(before, corrected)


def test_published_debt_changes_the_signature_and_unknowns_never_match_old_policy():
    before = financial_authority(build())
    after = financial_authority(build(accepted_schedule={"version_id": "new-published-version", "body": {
        "debts": [{"id": "loan", "lender": "Lender", "balance": 10000, "monthly_payment": 500}]}}))
    assert before["signature"] != after["signature"]
    assert not authority_matches({"signature": after["signature"], "policy_version": "old"}, after)
    assert not authority_matches(None, after)
    assert authority_matches(after, after)


def test_business_identity_preserves_word_boundaries_and_conjunctions():
    from app.services.financial_tax_earnings import normalize_business_identity

    assert normalize_business_identity("Example Partners, L.L.C.") == "example partners"
    assert normalize_business_identity("AB C LLC") != normalize_business_identity("A BC LLC")
    assert normalize_business_identity("A and B LLC") != normalize_business_identity("A B LLC")


def test_shared_payment_helper_keeps_an_unknown_rate_distinct_from_zero():
    from app.services.financial_plan import loan_payment

    assert loan_payment(12000, None, 12) is None
    assert loan_payment(12000, 0, 12) == 1000


def test_ebitda_only_source_selection_invalidates_authority_even_with_same_dscr():
    from app.services.financial_earnings_sources import change
    from app.tests.test_financial_earnings_sources import sources
    from app.tests.test_financial_metric_review import ACTOR

    original = build(sources())
    selection = original["ebitda"]["source_selection"]
    state, _ = change(None, selection, mode="manual", selected_file_id="tax-2024", excluded_file_ids=[], reason="Review alternative period",
                      expected_revision=0, source_stamp=selection["source_stamp"], idempotency_key="source-selection", actor=ACTOR)
    current = build(sources(), state=state)
    assert current["dscr"]["headline"]["value"] == original["dscr"]["headline"]["value"]
    assert financial_authority(current)["signature"] != financial_authority(original)["signature"]
