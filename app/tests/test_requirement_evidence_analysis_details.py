"""Document recognition and requirement-match confidence answer different questions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.application_profile import ApplicationRequirementEvidenceRead
from app.services.application_programs import _evidence_analysis_details


def evidence():
    file = SimpleNamespace(id=uuid4(), content_hash="current-content")
    analysis = SimpleNamespace(id=uuid4(), bucket_file_id=file.id, content_hash=file.content_hash,
                               analysis_version=6, status="completed", classification="floorplan_mca_inventory",
                               confidence="high")
    decision = SimpleNamespace(analysis_id=analysis.id, content_hash=file.content_hash, analysis_version=6,
                               decision="needs_more", reason_code="low_confidence", confidence="low")
    return file, analysis, decision


def test_high_confidence_contract_recognition_stays_separate_from_low_schedule_match():
    file, analysis, decision = evidence()
    result = _evidence_analysis_details(file, analysis, decision)
    assert result == {"detected_classification": "floorplan_mca_inventory", "analysis_confidence": "high"}
    assert decision.decision == "needs_more" and decision.confidence == "low"
    assert analysis.classification == "floorplan_mca_inventory"
    projected = ApplicationRequirementEvidenceRead(
        file_id=file.id, file_name="Financing contract.pdf", bucket_id=uuid4(),
        created_at=datetime.now(UTC), source="operator", ai_confidence=decision.confidence, **result,
    ).model_dump()
    assert projected["analysis_confidence"] == "high" and projected["ai_confidence"] == "low"
    assert projected["detected_classification"] == "floorplan_mca_inventory"


@pytest.mark.parametrize(("target", "field", "value"), [
    ("file", "content_hash", "replacement"),
    ("file", "content_hash", None),
    ("analysis", "bucket_file_id", uuid4()),
    ("analysis", "status", "processing"),
    ("decision", "analysis_id", uuid4()),
    ("decision", "content_hash", "old-content"),
    ("decision", "analysis_version", 5),
])
def test_stale_or_unrelated_analysis_metadata_is_not_presented_as_current(target, field, value):
    file, analysis, decision = evidence()
    setattr({"file": file, "analysis": analysis, "decision": decision}[target], field, value)
    assert _evidence_analysis_details(file, analysis, decision) == {
        "detected_classification": None, "analysis_confidence": None,
    }


def test_completed_current_recognition_is_available_before_a_requirement_decision():
    file, analysis, _ = evidence()
    assert _evidence_analysis_details(file, analysis)["analysis_confidence"] == "high"
    assert _evidence_analysis_details(file, None)["analysis_confidence"] is None


def test_legacy_decision_needs_matching_content_and_analysis_version_without_an_id():
    file, analysis, decision = evidence()
    decision.analysis_id = None
    assert _evidence_analysis_details(file, analysis, decision)["analysis_confidence"] == "high"
    decision.analysis_version = None
    assert _evidence_analysis_details(file, analysis, decision)["analysis_confidence"] is None
