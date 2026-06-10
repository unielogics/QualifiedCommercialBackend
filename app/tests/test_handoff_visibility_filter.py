"""Handoff visibility filter — pure-logic tests (Phase 4).

The filter enforces what flows from the deal-stage baseline into the
funding handoff packet:

  - 'funding_visible' / no visibility marker → always keep
  - 'team_visible' → keep only when handoff_includes_team_notes=True
  - 'agent_private' → exclude
  - 'client_visible' → exclude
  - instructions_visibility = 'internal' → exclude

This is load-bearing for the agent-private safety guarantee. The
existing handoff_builder doesn't filter on these axes itself; the
filter lives in services/handoff and is exercised by
promote_deal_to_loan when composing baseline_profile_snapshot.
"""

from __future__ import annotations

import uuid

from app.models.agent_task import AgentTask
from app.services.handoff import _agent_task_handoff_item, _filter_visibility


def test_funding_visible_always_kept():
    items = [
        {"requirement_key": "k1", "visibility": "funding_visible"},
        {"requirement_key": "k2"},  # default visibility
    ]
    kept, excluded = _filter_visibility(items, handoff_includes_team_notes=False)
    assert len(kept) == 2
    assert excluded == 0


def test_agent_private_always_excluded():
    items = [
        {"requirement_key": "k1", "visibility": "agent_private"},
        {"requirement_key": "k2", "visibility": "funding_visible"},
    ]
    kept, excluded = _filter_visibility(items, handoff_includes_team_notes=True)
    assert [k["requirement_key"] for k in kept] == ["k2"]
    assert excluded == 1


def test_client_visible_always_excluded():
    items = [
        {"requirement_key": "k1", "visibility": "client_visible"},
        {"requirement_key": "k2", "visibility": "funding_visible"},
    ]
    kept, excluded = _filter_visibility(items, handoff_includes_team_notes=True)
    assert [k["requirement_key"] for k in kept] == ["k2"]
    assert excluded == 1


def test_team_visible_gated_by_firm_policy():
    items = [
        {"requirement_key": "k1", "visibility": "team_visible"},
        {"requirement_key": "k2", "visibility": "funding_visible"},
    ]
    # Firm policy off — team notes excluded.
    kept_off, exc_off = _filter_visibility(items, handoff_includes_team_notes=False)
    assert [k["requirement_key"] for k in kept_off] == ["k2"]
    assert exc_off == 1

    # Firm policy on — team notes included.
    kept_on, exc_on = _filter_visibility(items, handoff_includes_team_notes=True)
    assert {k["requirement_key"] for k in kept_on} == {"k1", "k2"}
    assert exc_on == 0


def test_internal_instructions_excluded_regardless():
    items = [
        {
            "requirement_key": "k1",
            "visibility": "funding_visible",
            "instructions_visibility": "internal",
        },
        {
            "requirement_key": "k2",
            "visibility": "funding_visible",
            "instructions_visibility": "agent",
        },
    ]
    kept, excluded = _filter_visibility(items, handoff_includes_team_notes=True)
    assert [k["requirement_key"] for k in kept] == ["k2"]
    assert excluded == 1


def test_excluded_count_tallies_correctly():
    items = [
        {"requirement_key": "a", "visibility": "agent_private"},
        {"requirement_key": "b", "visibility": "client_visible"},
        {"requirement_key": "c", "visibility": "team_visible"},
        {"requirement_key": "d", "visibility": "funding_visible"},
        {"requirement_key": "e", "instructions_visibility": "internal"},
    ]
    kept, excluded = _filter_visibility(items, handoff_includes_team_notes=False)
    assert [k["requirement_key"] for k in kept] == ["d"]
    # a, b, c, e all excluded.
    assert excluded == 4


def test_empty_input():
    assert _filter_visibility(None) == ([], 0)
    assert _filter_visibility([]) == ([], 0)


def test_non_dict_entries_ignored():
    items = [
        "stringy",  # not a dict — silently dropped
        {"requirement_key": "k1", "visibility": "funding_visible"},
        None,
    ]
    kept, excluded = _filter_visibility(items)  # type: ignore[arg-type]
    assert [k["requirement_key"] for k in kept] == ["k1"]
    # Non-dict entries don't bump the excluded count — they're invalid input.
    assert excluded == 0


def test_agent_task_handoff_item_shape():
    task_id = uuid.uuid4()
    task = AgentTask(
        id=task_id,
        client_id=uuid.uuid4(),
        deal_id=uuid.uuid4(),
        title="Confirm access instructions",
        description="Gate code and lockbox note",
        category="funding_prep",
        visibility="funding_visible",
        owner_type="human",
        status="open",
        priority="high",
        notes="Listing agent confirmed by text.",
    )

    item = _agent_task_handoff_item(task)

    assert item["id"] == str(task_id)
    assert item["requirement_key"] == f"agent_task:{task_id}"
    assert item["label"] == "Confirm access instructions"
    assert item["source"] == "agent_task"
    assert item["visibility"] == "funding_visible"
