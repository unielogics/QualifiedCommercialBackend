from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from app.models.ai_cadence_rule import AICadenceRule
from app.models.ai_task_assignment import AITaskAssignment
from app.services.ai.cadence_engine import (
    _assignment_attempts_exhausted,
    _assignment_due_for_run,
    _assignment_template_context,
    _compose_assignment_message,
)


def _assignment(**overrides) -> AITaskAssignment:
    values = {
        "id": uuid.uuid4(),
        "client_requirement_status_id": uuid.uuid4(),
        "instructions": "Internal note that must not leak",
        "instructions_visibility": "internal",
        "channels": ["portal"],
        "cadence": {"hours_between_attempts": 24, "max_attempts": 2},
        "approval_mode": "draft_first",
        "complete_file_by": date(2026, 5, 20),
        "link_url": "https://app.example/upload",
        "link_label": "Upload portal",
        "objective_text": "Collect the borrower PFS.",
        "completion_criteria": "Signed PFS is uploaded and readable.",
        "completion_mode": "requires_human_verify",
        "attempts_made": 1,
        "next_run_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return AITaskAssignment(**values)


def test_assignment_run_window_and_attempts_are_enforced() -> None:
    now = datetime.now(timezone.utc)
    assert _assignment_due_for_run(_assignment(next_run_at=now - timedelta(minutes=1)), now)
    assert not _assignment_due_for_run(_assignment(next_run_at=now + timedelta(hours=1)), now)
    assert not _assignment_attempts_exhausted(_assignment(attempts_made=1))
    assert _assignment_attempts_exhausted(_assignment(attempts_made=2))


def test_assignment_context_shapes_borrower_draft_without_internal_notes() -> None:
    assignment = _assignment()
    rule = AICadenceRule(
        id=uuid.uuid4(),
        trigger_event="requirement_missing",
        action_type="draft_message",
        approval_required=True,
        message_template="Hi {{client_name}}, please send {{requirement_label}}.",
        visibility="borrower",
        is_active=True,
    )
    target = {
        "client_name": "Marcus",
        "requirement_key": "borrower_pfs",
        "context": {
            "requirement_label": "Borrower PFS",
            **_assignment_template_context(assignment),
        },
    }

    message = _compose_assignment_message(rule, target, assignment=assignment)

    assert "Borrower PFS" in message
    assert "Collect the borrower PFS" in message
    assert "Signed PFS is uploaded" in message
    assert "2026-05-20" in message
    assert "Upload portal: https://app.example/upload" in message
    assert "Internal note that must not leak" not in message
