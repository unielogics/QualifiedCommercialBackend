"""Pipeline batch-summary response shape (Phase 6).

The frontend usePipelineClientSummary hook reads these field names
verbatim. A backend rename would silently break the pipeline badges.
"""

from __future__ import annotations

import uuid

from app.routers.pipeline import PipelineClientSummary


def test_pipeline_client_summary_full_payload():
    s = PipelineClientSummary(
        client_id=uuid.uuid4(),
        ai_state="deployed",
        current_blocker="Waiting on proof of funds",
        next_follow_up_at=None,
        human_needed=False,
        missing_items_count=2,
        handoff_status="none",
        funding_status=None,
        ready_for_lending_eligible=True,
        deals_count=1,
        loans_count=0,
        open_tasks_count=3,
        last_activity_at=None,
    )
    payload = s.model_dump()
    expected = {
        "client_id",
        "ai_state",
        "current_blocker",
        "next_follow_up_at",
        "human_needed",
        "missing_items_count",
        "handoff_status",
        "funding_status",
        "ready_for_lending_eligible",
        "deals_count",
        "loans_count",
        "open_tasks_count",
        "last_activity_at",
    }
    assert set(payload.keys()) == expected


def test_pipeline_client_summary_ai_state_literals():
    """ai_state must be one of the 5 known literals. The frontend
    WorkspaceAiState type expects this exact set."""
    valid = ["deployed", "paused", "draft_first", "human_only", "idle"]
    for v in valid:
        PipelineClientSummary(
            client_id=uuid.uuid4(),
            ai_state=v,  # type: ignore[arg-type]
            handoff_status="none",
        )


def test_pipeline_client_summary_handoff_status_literals():
    valid = ["none", "requested", "packet_built", "promoted"]
    for v in valid:
        PipelineClientSummary(
            client_id=uuid.uuid4(),
            ai_state="idle",
            handoff_status=v,  # type: ignore[arg-type]
        )


def test_human_needed_defaults_false():
    s = PipelineClientSummary(
        client_id=uuid.uuid4(),
        ai_state="idle",
        handoff_status="none",
    )
    assert s.human_needed is False
    assert s.ready_for_lending_eligible is False
    assert s.missing_items_count == 0
    assert s.deals_count == 0
    assert s.loans_count == 0
    assert s.open_tasks_count == 0
