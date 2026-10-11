"""Entity labels are review signals, not permission to discard or mix numbers."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services import application_programs as programs
from app.services.document_qualification_review import SYSTEM, check_id, validate_result


def inputs(**analysis_changes):
    file = SimpleNamespace(
        id=uuid4(), file_name="Financial statement 2025.pdf", content_hash="current",
        statement_period=None,
    )
    analysis = SimpleNamespace(**{
        "id": uuid4(), "status": "completed", "content_hash": "current",
        "confidence": "high", "classification": "current_p_and_l",
        "analysis_version": 1, "analyzed_at": datetime(2026, 10, 11, tzinfo=UTC),
        "analysis": {
            "key_facts": {"business_name": "Different legal name LLC", "revenue": 100000,
                          "gross_profit": 18000, "net_income": 5000},
        },
        **analysis_changes,
    })
    requirement = SimpleNamespace(
        requirement_key="current_p_and_l", label="Current P&L", category="financials",
        verification_required=False, completion_mode="ai_can_complete", review_checks=[],
    )
    return {"requirement": requirement, "file": file, "analysis": analysis,
            "expected_entity": "Marketing display name", "duplicate_content": False}


def test_usable_numbers_accepted_with_explicit_nonblocking_name_warning():
    args = inputs()
    decision = programs._automatic_evidence_decision(**args)
    assert decision[:2] == ("accepted", "entity_mismatch_warning")
    assert "Usable evidence is retained" in decision[2]
    assert "staff should confirm" in decision[2]
    assert "Do not combine" in decision[2]
    warning = programs._entity_identity_warning(args["analysis"], args["expected_entity"])
    assert warning["blocks_document_acceptance"] is False
    assert warning["association_review_required"] is True
    assert warning["observed_entity"] == "Different legal name LLC"
    # No name substitution or cross-file pooling rewrites the extracted figures.
    assert args["analysis"].analysis["key_facts"]["gross_profit"] == 18000


def test_unidentified_name_does_not_discard_usable_numbers():
    args = inputs(analysis={"key_facts": {"revenue": 100000, "net_income": 3000}})
    assert programs._automatic_evidence_decision(**args)[:2] == (
        "accepted", "entity_unconfirmed_warning",
    )


@pytest.mark.parametrize(("changes", "result"), [
    ({"content_hash": "old"}, ("processing", "analysis_stale")),
    ({"confidence": "medium"}, ("needs_more", "low_confidence")),
    ({"classification": "insurance"}, ("rejected", "wrong_document")),
    ({"classification": "unreadable"}, ("rejected", "unreadable")),
    ({"analysis": {"red_flags": ["Visible document tampering"]}},
     ("rejected", "integrity_or_readability")),
    ({"analysis": {"limitations": ["Missing pages"]}}, ("needs_more", "incomplete")),
])
def test_name_warning_does_not_disable_integrity_or_evidence_quality(changes, result):
    assert programs._automatic_evidence_decision(**inputs(**changes))[:2] == result


@pytest.mark.parametrize(("assessment", "status"), [
    ({"status": "pass"}, "accepted"),
    ({"status": "blocked"}, "rejected"),
    ({"status": "unknown"}, "needs_more"),
])
def test_published_numerical_checks_stay_authoritative(assessment, status):
    args = inputs()
    check = {"key": "net_income_nonnegative", "label": "Positive income",
             "instructions": "Validate the printed income", "severity": "block"}
    args["requirement"].review_checks = [check]
    decision = programs._automatic_evidence_decision(**args, check_assessment=assessment)
    assert decision[0] == status
    if status == "accepted":
        assert decision[1] == "entity_mismatch_warning"
        assert "every published documentary check" in decision[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("staff_rejection", [False, True])
async def test_warning_persisted_and_old_ai_rejection_recomputed_not_staff_override(monkeypatch, staff_rejection):
    args = inputs()
    link = SimpleNamespace(id=uuid4(), file_id=args["file"].id, verified_at=None,
                           verified_by_user_id=None, reason=None,
                           provenance={"source": "automatic", "source_file": "retained"})
    previous = SimpleNamespace(id=uuid4(), actor_kind="staff" if staff_rejection else "ai",
        decision="rejected", reason_code="wrong_entity", explanation="Prior identity result",
        content_hash="current", analysis_version=1, policy_version=1,
        idempotency_key="legacy-v3", confidence="high")
    monkeypatch.setattr(programs, "_latest_evidence_decisions", AsyncMock(return_value={link.id: previous}))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
                         add=Mock(), flush=AsyncMock())
    result = await programs._reconcile_evidence_decisions(
        db, requirement=args["requirement"], links=[link], inventory={args["file"].id: args["file"]},
        analyses={args["file"].id: args["analysis"]}, expected_entity=args["expected_entity"],
        corroborated_entities=set(), criteria_version=1,
    )
    assert link.provenance["source_file"] == "retained"
    assert link.provenance["entity_identity_warning"]["blocks_document_acceptance"] is False
    if staff_rejection:
        assert result[link.id] is previous
        assert link.verified_at is None
        db.add.assert_not_called()
    else:
        current = result[link.id]
        assert current.decision == "accepted"
        assert current.reason_code == "entity_mismatch_warning"
        assert current.supersedes_decision_id == previous.id
        assert link.verified_at is not None
        db.add.assert_called_once()


def test_named_source_warning_does_not_authorize_borrowing_another_files_numbers():
    check = {"key": "net_income_not_declining", "label": "Income trend",
             "instructions": "Compare both complete years", "severity": "block"}
    pages = {("one", 1): "2024 Net income: 10,000", ("two", 1): "2025 Net income: 20,000"}
    assessment = validate_result({"checks": [{
        "id": check_id(check), "status": "pass", "confidence": "high", "reason": "Increasing",
        "citations": [{"file_id": file_id, "page": page, "quote": text}
                      for (file_id, page), text in pages.items()],
        "observations": [
            {"period": "2024", "measure": "net_income", "value": 10000, "file_id": "one", "page": 1},
            # Correct number, deliberately bound to the other source file.
            {"period": "2025", "measure": "net_income", "value": 20000, "file_id": "one", "page": 1},
        ],
    }]}, [check], pages)
    assert assessment["status"] == "unknown"
    assert "source name differing" in SYSTEM.lower()
    assert "do not pool numbers across entities" in SYSTEM
