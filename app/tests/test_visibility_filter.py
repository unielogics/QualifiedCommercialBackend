"""Visibility filter tests — pure-Python.

Locks down:
  - Borrower-facing render filters out internal_only / agent_visible
    / bank_visible facts.
  - Agent render keeps shared / agent_visible / bank_visible.
  - Underwriter render sees everything.
  - Custom instructions are stripped from the borrower view.
  - Default visibility (None) is treated as agent-internal.
"""

from __future__ import annotations

import pytest

from app.services.ai.visibility_filter import (
    filter_facts,
    filter_handoff_packet,
    filter_plan,
)


_FACTS = [
    {"field": "client_name", "value": "Marcus", "visibility": "shared"},
    {"field": "credit_score", "value": 720, "visibility": "internal_only"},
    {"field": "agent_notes", "value": "high-risk profile", "visibility": "agent_visible"},
    {"field": "purchase_price", "value": 875000, "visibility": "borrower_visible"},
    {"field": "ltv", "value": 0.7, "visibility": "bank_visible"},
    {"field": "untagged", "value": "unknown"},  # no visibility — default agent-internal
]


def test_borrower_only_sees_shared_or_borrower_visible() -> None:
    out = filter_facts(_FACTS, "borrower")
    fields = {f["field"] for f in out}
    assert "client_name" in fields  # shared
    assert "purchase_price" in fields  # borrower_visible
    assert "credit_score" not in fields  # internal_only
    assert "agent_notes" not in fields  # agent_visible
    assert "ltv" not in fields  # bank_visible
    assert "untagged" not in fields  # default agent-internal


def test_agent_sees_everything_except_internal_only() -> None:
    out = filter_facts(_FACTS, "agent")
    fields = {f["field"] for f in out}
    assert "agent_notes" in fields
    assert "ltv" in fields
    assert "credit_score" not in fields  # internal_only stays internal


def test_underwriter_sees_everything() -> None:
    out = filter_facts(_FACTS, "underwriter")
    assert len(out) == 6
    assert "untagged" in {fact["field"] for fact in out}


def test_underwriter_includes_untagged() -> None:
    """Untagged defaults to agent-internal; underwriter is allowed."""
    out = filter_facts(_FACTS, "underwriter")
    fields = {f["field"] for f in out}
    assert "untagged" in fields
    assert "credit_score" in fields  # internal_only


def test_filter_plan_strips_custom_instructions_for_borrower() -> None:
    plan = {
        "required_items": [],
        "waived_items": [],
        "custom_instructions": "Don't ask about liquidity",
    }
    out = filter_plan(plan, "borrower")
    assert out["custom_instructions"] is None


def test_filter_plan_keeps_custom_instructions_for_agent() -> None:
    plan = {
        "required_items": [],
        "waived_items": [],
        "custom_instructions": "Don't ask about liquidity",
    }
    out = filter_plan(plan, "agent")
    assert out["custom_instructions"] == "Don't ask about liquidity"


def test_filter_plan_filters_required_items() -> None:
    plan = {
        "required_items": [
            {"requirement_key": "purchase_price", "label": "Price", "visibility": ["borrower_visible"]},
            {"requirement_key": "agent_notes", "label": "Notes", "visibility": ["agent_visible"]},
        ],
        "waived_items": [],
        "custom_instructions": None,
    }
    out = filter_plan(plan, "borrower")
    keys = {i["requirement_key"] for i in out["required_items"]}
    assert "purchase_price" in keys
    assert "agent_notes" not in keys


def test_filter_handoff_packet_strips_agent_notes_from_borrower() -> None:
    packet = {
        "extracted_facts": [{"field": "name", "value": "M", "visibility": "shared"}],
        "realtor_summary": {"intent": "buying", "agent_notes": "high-risk"},
    }
    out = filter_handoff_packet(packet, "borrower")
    assert "agent_notes" not in (out["realtor_summary"] or {})
    # Agent retains it.
    out2 = filter_handoff_packet(packet, "agent")
    assert "agent_notes" in (out2["realtor_summary"] or {})


def test_filter_facts_handles_list_visibility() -> None:
    """A fact tagged with multiple visibility labels passes if ANY
    label is in the audience's allowed set."""
    facts = [
        {"field": "x", "visibility": ["agent_visible", "client_visible"]},
    ]
    out = filter_facts(facts, "borrower")
    assert len(out) == 1  # client_visible is in borrower's allowed set


def test_filter_facts_drops_non_dict_entries() -> None:
    """Defensive: a malformed entry shouldn't crash the filter."""
    out = filter_facts([{"field": "x", "visibility": "shared"}, "not_a_dict", None], "agent")
    assert len(out) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
