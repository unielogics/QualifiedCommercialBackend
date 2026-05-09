"""Schema-level tests for the Realtor Client Intelligence Profile.

Pure-Python — no DB. Guards the helpers in
`app/services/ai/realtor_profile.py` so future edits don't silently
change finance_ready / missing_facts / readiness_score behavior.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.ai.realtor_profile import (
    apply_profile_patch,
    compute_finance_ready,
    compute_listing_ready,
    compute_missing_facts,
    compute_readiness_score,
    empty_profile,
)


CID = str(uuid.uuid4())
AID = str(uuid.uuid4())


def _full_buyer_profile() -> dict:
    """Returns a buyer profile with every required finance_ready field
    populated. Used as the 'happy path' fixture for the tests below."""
    p = empty_profile(CID, AID)
    p = apply_profile_patch(p, {"client_type": "buyer"}, client_id=CID, agent_id=AID)
    p = apply_profile_patch(p, {"buyer_profile": {
        "target_property_type": "multifamily",
        "target_location": "Brooklyn",
        "target_budget": 900000,
        "purchase_timeline": "30_60",
        "financing_needed": True,
    }}, client_id=CID, agent_id=AID)
    return p


def test_empty_profile_finance_ready_false() -> None:
    p = empty_profile(CID, AID)
    assert compute_finance_ready(p) is False
    assert compute_listing_ready(p) is False
    assert compute_readiness_score(p) == 0
    assert compute_missing_facts(p) == ["client_type"]


def test_buyer_with_all_required_fields_finance_ready() -> None:
    p = _full_buyer_profile()
    assert compute_finance_ready(p) is True
    assert compute_readiness_score(p) > 0
    # Buyer agreement still missing — appears in missing_facts.
    missing = compute_missing_facts(p)
    assert "buyer.buyer_agreement_status" in missing


def test_buyer_missing_budget_blocks_finance_ready() -> None:
    p = _full_buyer_profile()
    p["buyer_profile"]["target_budget"] = None
    p["buyer_profile"]["target_budget_range"] = None
    assert compute_finance_ready(p) is False


def test_buyer_with_range_satisfies_budget() -> None:
    p = _full_buyer_profile()
    p["buyer_profile"]["target_budget"] = None
    p["buyer_profile"]["target_budget_range"] = {"low": 800000, "high": 1000000}
    assert compute_finance_ready(p) is True


def test_buyer_cash_buyer_blocks_finance_ready() -> None:
    """financing_needed=False (cash buyer) explicitly blocks the bridge
    to the bank AI — they don't need financing."""
    p = _full_buyer_profile()
    p["buyer_profile"]["financing_needed"] = False
    assert compute_finance_ready(p) is False


def test_seller_listing_ready_requires_signed_agreement_plus_cma_plus_photos() -> None:
    p = empty_profile(CID, AID)
    p = apply_profile_patch(p, {"client_type": "seller"}, client_id=CID, agent_id=AID)
    p = apply_profile_patch(p, {"seller_profile": {
        "property_address": "123 Main",
        "desired_list_price": 500000,
    }}, client_id=CID, agent_id=AID)
    assert compute_listing_ready(p) is False  # agreement / cma / photos missing
    p = apply_profile_patch(p, {"seller_profile": {
        "listing_agreement_status": "signed",
        "cma_status": "in_progress",
        "photos_status": "scheduled",
    }}, client_id=CID, agent_id=AID)
    assert compute_listing_ready(p) is True


def test_apply_profile_patch_dedups_known_facts_by_field() -> None:
    p = empty_profile(CID, AID)
    p = apply_profile_patch(
        p, {"known_fact": {"field": "lender_pref", "value": "Chase", "source": "agent"}},
        client_id=CID, agent_id=AID,
    )
    p = apply_profile_patch(
        p, {"known_fact": {"field": "lender_pref", "value": "Wells Fargo", "source": "agent"}},
        client_id=CID, agent_id=AID,
    )
    facts = p["known_facts"]
    assert len(facts) == 1
    assert facts[0]["value"] == "Wells Fargo"


def test_readiness_score_increases_as_fields_fill() -> None:
    p = empty_profile(CID, AID)
    p = apply_profile_patch(p, {"client_type": "buyer"}, client_id=CID, agent_id=AID)
    score_after_type = p["readiness_score"]
    p = apply_profile_patch(
        p, {"buyer_profile": {"target_property_type": "multifamily"}},
        client_id=CID, agent_id=AID,
    )
    score_after_one = p["readiness_score"]
    assert score_after_one > score_after_type


def test_missing_facts_lists_unfilled_buyer_fields() -> None:
    p = empty_profile(CID, AID)
    p = apply_profile_patch(p, {"client_type": "buyer"}, client_id=CID, agent_id=AID)
    missing = compute_missing_facts(p)
    assert "buyer.target_property_type" in missing
    assert "buyer.target_location" in missing
    assert "buyer.target_budget" in missing
    assert "buyer.purchase_timeline" in missing
    assert "buyer.financing_needed" in missing


def test_apply_patch_initializes_buyer_profile_on_type_set() -> None:
    """Setting client_type=buyer should auto-init buyer_profile so
    subsequent buyer_profile patches don't have to handle None."""
    p = empty_profile(CID, AID)
    p = apply_profile_patch(p, {"client_type": "buyer"}, client_id=CID, agent_id=AID)
    assert p["buyer_profile"] is not None
    assert p["seller_profile"] is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
