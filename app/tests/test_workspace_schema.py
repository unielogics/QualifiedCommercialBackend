"""Workspace + Deal + AgentTask schema shape tests (Phase 2/3/7).

These tests lock down the public response shapes the frontend
WorkspaceData type depends on. If a backend field renames, the
frontend type drift will be caught here before it ships.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.schemas.agent_task import AgentTaskCreate, AgentTaskOut, PromoteToAiResponse
from app.schemas.deal import DealCreate, DealOut, DealUpdate, MarkReadyRequest, MarkReadyResponse
from app.schemas.workspace import (
    FundingFileSummary,
    WorkspaceAISummary,
    WorkspaceDocumentsSummary,
    WorkspaceOut,
    WorkspacePermissions,
    WorkspaceSelectedContext,
    WorkspaceTabCounts,
)


# ── Deal schemas ──────────────────────────────────────────────────


def test_deal_create_minimum():
    d = DealCreate(deal_type="buyer", title="Buy westside")
    assert d.side is None  # router auto-derives from deal_type
    assert d.property_id is None


def test_deal_update_excludes_promoted_status():
    """DealUpdate.status literal must not include 'promoted' — that
    transition is set only by promote_deal_to_loan."""
    # 'open' is valid
    DealUpdate(status="open")
    # 'promoted' is rejected
    with pytest.raises(Exception):
        DealUpdate(status="promoted")  # type: ignore[arg-type]


def test_mark_ready_response_carries_lineage():
    r = MarkReadyResponse(
        loan_id=uuid.uuid4(),
        deal_id=uuid.uuid4(),
        handoff_packet_id=uuid.uuid4(),
        prequal_request_id=uuid.uuid4(),
        lending_thread_id=uuid.uuid4(),
        handoff_summary="ok",
        missing_lending_items=["proof_of_funds"],
    )
    payload = r.model_dump()
    # Frontend reads these keys verbatim — see useMarkDealReadyForLending.
    for k in (
        "loan_id",
        "deal_id",
        "handoff_packet_id",
        "prequal_request_id",
        "lending_thread_id",
        "handoff_summary",
        "missing_lending_items",
    ):
        assert k in payload


# ── Workspace aggregate ────────────────────────────────────────────


def test_workspace_permissions_seven_flags():
    """The frontend RolePermissions type expects exactly these seven
    booleans. Adding one without updating the frontend will cause a
    silent runtime miss."""
    p = WorkspacePermissions(
        can_mark_ready_for_lending=True,
        can_edit_underwriting=False,
        can_create_deals=True,
        can_create_funding_files=False,
        can_assign_ai=True,
        can_edit_client_fields=True,
        can_view_funding_tab=False,
    )
    fields = set(WorkspacePermissions.model_fields.keys())
    _ = p  # the instance exercise above is the smoke-test
    assert fields == {
        "can_mark_ready_for_lending",
        "can_edit_underwriting",
        "can_create_deals",
        "can_create_funding_files",
        "can_assign_ai",
        "can_edit_client_fields",
        "can_view_funding_tab",
    }


def test_workspace_ai_summary_fields():
    s = WorkspaceAISummary(
        state="deployed",
        outstanding_followups=3,
        current_blocker=None,
        next_follow_up_at=None,
        next_best_question=None,
        readiness_score=82,
    )
    payload = s.model_dump()
    # state literal — accepts the 5 known values.
    assert payload["state"] == "deployed"
    assert payload["outstanding_followups"] == 3


def test_workspace_tab_counts_allows_none_for_unwired_tabs():
    """tab_counts.tasks was None in Phase 2 (no AgentTask) and gained
    a real integer in Phase 7. The schema must accept both shapes so
    rolling deploys don't fail the validator."""
    c1 = WorkspaceTabCounts(deals=2, funding=1, tasks=None, ai_follow_up=5, documents=12)
    c2 = WorkspaceTabCounts(deals=2, funding=1, tasks=4, ai_follow_up=5, documents=12)
    assert c1.tasks is None
    assert c2.tasks == 4


def test_workspace_selected_context_recommended_tab():
    ctx = WorkspaceSelectedContext(
        tab=None,
        deal_id=None,
        funding_file_id=None,
        loan_id=None,
        recommended_tab="funding",
    )
    assert ctx.recommended_tab == "funding"


def test_funding_file_summary_includes_handoff_fields():
    """The FundingPanel renders source_deal_id + handoff_summary +
    funding_file_kind. They must exist on the schema regardless of
    whether 0048 has been applied to the DB yet (NULL is OK)."""
    f = FundingFileSummary(
        id=uuid.uuid4(),
        deal_id="L-1234",
        client_id=uuid.uuid4(),
        side="buyer",
        stage="prequalified",
        address="123 Main St",
        amount=350000,
        funding_file_kind=None,
        source_deal_id=None,
        handoff_summary=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    payload = f.model_dump()
    for k in ("source_deal_id", "handoff_summary", "funding_file_kind"):
        assert k in payload


# ── AgentTask schemas ──────────────────────────────────────────────


def test_agent_task_create_defaults():
    t = AgentTaskCreate(title="Open house Sat 2pm")
    assert t.category == "other"
    assert t.visibility == "team_visible"
    assert t.owner_type == "human"
    assert t.priority == "medium"


def test_agent_task_out_carries_ai_assignment_id():
    """ai_assignment_id is the contract handle between AgentTask and
    DealSecretaryPicker — promote-to-ai stamps it, unassign clears it."""
    t = AgentTaskOut(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        deal_id=None,
        loan_id=None,
        title="Send pre-approval letter",
        description=None,
        category="document_collection",
        visibility="funding_visible",
        owner_type="ai",
        assigned_user_id=None,
        ai_assignment_id=uuid.uuid4(),
        due_at=None,
        reminder_at=None,
        status="open",
        priority="medium",
        notes=None,
        created_by=None,
        completed_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert "ai_assignment_id" in AgentTaskOut.model_fields
    _ = t  # instance exercise


def test_promote_to_ai_response_contract():
    """The promote response carries the synthetic requirement_key so
    callers (frontend hooks) can refetch /ai-follow-up and verify the
    new row appears in the AI column."""
    task = AgentTaskOut(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        deal_id=None,
        loan_id=None,
        title="x",
        description=None,
        category="other",
        visibility="team_visible",
        owner_type="ai",
        assigned_user_id=None,
        ai_assignment_id=uuid.uuid4(),
        due_at=None,
        reminder_at=None,
        status="open",
        priority="medium",
        notes=None,
        created_by=None,
        completed_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    r = PromoteToAiResponse(
        task=task,
        assignment_id=uuid.uuid4(),
        requirement_key=f"agent_task:{task.id}",
    )
    assert r.requirement_key.startswith("agent_task:")
