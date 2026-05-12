"""plan_builder deal_id threading — signature + shape tests (Phase 3).

The Phase 3 plan extended rebuild/preview/_compute/_load_statuses/
_find_existing_plan/_upsert_plan to accept deal_id. These tests
lock down the signatures so callers (deal_secretary, handoff,
routers/clients workspace endpoint) don't drift if the helpers
get refactored later.
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import fields as dataclass_fields

from app.services.ai.plan_builder import (
    PlanSnapshot,
    _compute,
    _find_existing_plan,
    _load_statuses,
    _upsert_plan,
    preview,
    rebuild,
)


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters.keys())


def test_rebuild_accepts_deal_id():
    assert "deal_id" in _params(rebuild)
    # And the existing scope params survive.
    assert "loan_id" in _params(rebuild)
    assert "client_id" in _params(rebuild)


def test_preview_accepts_deal_id():
    assert "deal_id" in _params(preview)
    assert "loan_id" in _params(preview)
    assert "overrides" in _params(preview)


def test_compute_accepts_deal_id():
    assert "deal_id" in _params(_compute)


def test_load_statuses_accepts_deal_id():
    assert "deal_id" in _params(_load_statuses)


def test_find_existing_plan_accepts_deal_id():
    assert "deal_id" in _params(_find_existing_plan)


def test_plan_snapshot_has_deal_id_field():
    """PlanSnapshot is the plain-data view callers serialize. deal_id
    must be a first-class field so the workspace + AI-preview surfaces
    don't lose scope context."""
    names = {f.name for f in dataclass_fields(PlanSnapshot)}
    assert "deal_id" in names
    assert "loan_id" in names
    assert "client_id" in names


def test_plan_snapshot_constructs_with_deal_id():
    """Verify the dataclass actually accepts a deal_id keyword."""
    from datetime import datetime, timezone

    snap = PlanSnapshot(
        client_id=uuid.uuid4(),
        loan_id=None,
        deal_id=uuid.uuid4(),
        agent_id=None,
        current_phase="realtor",
        active_playbook_versions=[],
        custom_instructions=None,
        required_items=[],
        waived_items=[],
        ai_suggested_items=[],
        next_best_question=None,
        next_best_action=None,
        readiness_score=0,
        computed_at=datetime.now(timezone.utc),
    )
    assert snap.deal_id is not None
    assert snap.loan_id is None
