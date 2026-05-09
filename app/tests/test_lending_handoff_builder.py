"""Pure-Python tests for the Lending Handoff Packet builder + selector.

Locks down:

  - The packet derives client + buyer profile facts correctly.
  - missing_lending_items reflects what the realtor profile already
    covers (e.g. financing_needed=False removes the rent_or_income
    requirement).
  - recommended_lending_path picks DSCR for SFR and commercial_purchase
    for multifamily / commercial.
  - The first lending question is conversational + memory-aware
    (mentions known facts), not a form dump.
  - The selector picks LENDING_AI_SYSTEM_PROMPT when phase=lending.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from app.enums import Role
from app.routers.ai import (
    LENDING_AI_SYSTEM_PROMPT,
    REALTOR_SYSTEM_PROMPT,
    OPERATOR_SYSTEM_PROMPT,
    CLIENT_SYSTEM_PROMPT,
    _system_prompt_for,
)
from app.services.ai.handoff_builder import (
    _LENDING_REQUIRED_FIELDS,
    _compute_missing_lending_items,
    _extract_facts,
    _first_lending_question,
    _recommend_path,
    _build_handoff_summary,
)


@dataclass
class FakeClient:
    id: object = uuid.uuid4()
    name: str = "Marcus Holloway"
    email: str | None = "marcus@holloway.cap"
    phone: str | None = "555-0100"
    address: str | None = None
    fico: int | None = 720
    tier: str = "standard"
    client_type: str | None = "buyer"
    realtor_profile: dict | None = None


@dataclass
class FakeUser:
    role: Role


@dataclass
class FakeThread:
    loan_id: object | None = None
    client_id: object | None = None
    phase: str | None = None


# ── Selector tests ─────────────────────────────────────────────────


def test_selector_lending_phase_picks_lending_ai() -> None:
    t = FakeThread(client_id=uuid.uuid4(), phase="lending")
    assert _system_prompt_for(FakeUser(Role.BROKER), t) == LENDING_AI_SYSTEM_PROMPT


def test_selector_realtor_phase_picks_realtor_ai() -> None:
    t = FakeThread(client_id=uuid.uuid4(), phase="realtor")
    assert _system_prompt_for(FakeUser(Role.BROKER), t) == REALTOR_SYSTEM_PROMPT


def test_selector_lending_phase_for_borrower_still_concierge() -> None:
    """Borrowers always see CLIENT_SYSTEM_PROMPT — the phase doesn't
    leak the operator framing into a borrower's view."""
    t = FakeThread(client_id=uuid.uuid4(), phase="lending")
    assert _system_prompt_for(FakeUser(Role.CLIENT), t) == CLIENT_SYSTEM_PROMPT


def test_selector_phase_overrides_legacy_loan_id_check() -> None:
    """Loan-scoped + lending-phase still picks Lending AI (phase wins)."""
    t = FakeThread(loan_id=uuid.uuid4(), phase="lending")
    assert _system_prompt_for(FakeUser(Role.BROKER), t) == LENDING_AI_SYSTEM_PROMPT


def test_selector_loan_no_phase_picks_bank_ai() -> None:
    """Pre-0031 threads with NULL phase + loan_id set still route to
    the Bank AI — backwards compat with existing loan-scoped threads."""
    t = FakeThread(loan_id=uuid.uuid4(), phase=None)
    assert _system_prompt_for(FakeUser(Role.BROKER), t) == OPERATOR_SYSTEM_PROMPT


# ── Packet builder helpers ─────────────────────────────────────────


def test_extract_facts_pulls_buyer_profile() -> None:
    bp = {
        "target_property_type": "multifamily",
        "target_location": "North Jersey",
        "target_budget": 900000,
        "purchase_timeline": "30_60",
        "financing_needed": True,
    }
    client = FakeClient(client_type="buyer")
    facts = _extract_facts(client, bp, {}, {})
    keys = {f["field"] for f in facts}
    assert "client_name" in keys
    assert "target_property_type" in keys
    assert "target_budget" in keys
    assert "financing_needed" in keys


def test_extract_facts_handles_known_facts_list() -> None:
    profile = {
        "known_facts": [
            {"field": "lender_pref", "value": "Chase", "source": "agent"},
            {"field": "decision_maker", "value": "spouse", "source": "agent"},
        ]
    }
    client = FakeClient()
    facts = _extract_facts(client, {}, {}, profile)
    fields = {f["field"] for f in facts}
    assert "lender_pref" in fields
    assert "decision_maker" in fields


def test_missing_lending_items_drops_property_address_when_target_location_set() -> None:
    bp = {"target_location": "Brooklyn"}
    client = FakeClient()
    missing = _compute_missing_lending_items(client, bp)
    assert "property_address" not in missing


def test_missing_lending_items_drops_rent_when_cash_buyer() -> None:
    """Cash buyers (financing_needed=False) don't need rent/income —
    the lending AI shouldn't ask about it."""
    bp = {"financing_needed": False}
    client = FakeClient()
    missing = _compute_missing_lending_items(client, bp)
    assert "rent_or_income_details" not in missing


def test_missing_lending_items_keeps_required_fields_by_default() -> None:
    """A buyer with nothing captured beyond the basics should still
    have all the lending-required fields in the missing list."""
    client = FakeClient()
    missing = _compute_missing_lending_items(client, {})
    # All canonical required fields should be present when nothing's filled.
    for f in _LENDING_REQUIRED_FIELDS:
        assert f in missing


def test_recommend_path_seller_marks_seller_listing() -> None:
    rec = _recommend_path("seller", {}, {})
    assert rec["path"] == "seller_listing"


def test_recommend_path_buyer_multifamily_picks_commercial() -> None:
    bp = {"target_property_type": "multifamily"}
    rec = _recommend_path("buyer", bp, {})
    assert rec["path"] == "prequalification"
    assert rec["loan_type_guess"] == "commercial_purchase"


def test_recommend_path_buyer_sfr_picks_dscr() -> None:
    bp = {"target_property_type": "single_family"}
    rec = _recommend_path("buyer", bp, {})
    assert rec["loan_type_guess"] == "DSCR"


def test_recommend_path_urgency_high_for_asap() -> None:
    bp = {"target_property_type": "single_family", "purchase_timeline": "asap"}
    rec = _recommend_path("buyer", bp, {})
    assert rec["urgency"] == "high"


def test_first_lending_question_is_memory_aware_for_buyer() -> None:
    bp = {"target_property_type": "multifamily", "target_location": "Brooklyn"}
    q = _first_lending_question(["borrower_entity_type"], bp, "buyer")
    # Should not be a "tell me everything" form dump — single focused
    # question on the next-best-leverage gap.
    assert "entity" in q.lower()
    # Acknowledges the realtor-side context.
    assert "realtor" in q.lower() or "lead context" in q.lower()


def test_first_lending_question_is_seller_specific() -> None:
    q = _first_lending_question([], {}, "seller")
    # Seller flow doesn't ask about borrower entity.
    assert "entity" not in q.lower() or "1031" in q.lower()


def test_handoff_summary_includes_known_facts() -> None:
    client = FakeClient()
    bp = {"target_property_type": "multifamily", "target_location": "North Jersey", "target_budget": 900000}
    summary = _build_handoff_summary(
        client,
        client_type="buyer",
        buyer_profile=bp,
        seller_profile={},
        intent_summary="Looking for mixed-use commercial.",
    )
    assert "Marcus" in summary
    assert "multifamily" in summary
    assert "North Jersey" in summary
    assert "900,000" in summary or "900000" in summary


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
