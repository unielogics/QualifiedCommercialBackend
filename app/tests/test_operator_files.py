from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.routers.operator_files import (
    _funding_stage,
    _rollup,
    _variant_vertical,
    _working_stage,
)
from app.schemas.operator_file import BucketIntakeLinkRequest, UnifiedFileRow


def _row(*, vertical: str, origin: str, stage: str, health_tone: str = "mut") -> UnifiedFileRow:
    source_id = uuid4()
    return UnifiedFileRow(
        id=f"intake:{source_id}",
        source_kind="intake",
        source_id=source_id,
        ref="QC-I-TEST",
        title="Test file",
        vertical=vertical,  # type: ignore[arg-type]
        vertical_label="Test",
        origin=origin,  # type: ignore[arg-type]
        origin_label="Test",
        source_label="Test",
        working_stage=_working_stage(vertical, "applicant_intake" if vertical != "real_estate" else "lead"),
        funding_stage=_funding_stage(stage) if stage in {"prequalified", "funded"} else None,
        normalized_stage=stage,
        health="On track",
        health_tone=health_tone,  # type: ignore[arg-type]
        updated_at=datetime.now(UTC),
    )


def test_variant_verticals_match_unified_operator_taxonomy():
    assert _variant_vertical("real_estate_dscr_v1") == "real_estate"
    assert _variant_vertical("main_street_v1") == "main_street"
    assert _variant_vertical("dealer_gatekeeper_v1") == "dealer"
    assert _variant_vertical("mca_refi_v1") == "mca"
    assert _variant_vertical("unknown") == "real_estate"


def test_stage_shapes_keep_working_and_funding_ladders_separate():
    working = _working_stage("real_estate", "ready_for_lending")
    funding = _funding_stage("processing")

    assert working.family == "working"
    assert working.index == 4
    assert working.total == 4
    assert funding is not None
    assert funding.family == "funding"
    assert funding.index == 4
    assert funding.total == 6


def test_rollup_counts_vertical_origin_attention_and_promoted():
    rows = [
        _row(vertical="real_estate", origin="agent", stage="Lead", health_tone="warn"),
        _row(vertical="dealer", origin="dealer", stage="funded", health_tone="ok"),
    ]
    rollup = _rollup(rows)

    assert rollup.total == 2
    assert rollup.by_vertical == {"real_estate": 1, "dealer": 1}
    assert rollup.by_origin == {"agent": 1, "dealer": 1}
    assert rollup.needs_attention == 1
    assert rollup.promoted == 1
    assert rollup.real_estate == 1
    assert rollup.dealer == 1
    assert rollup.working == 1


def test_row_computed_aliases_match_desktop_contract():
    row = _row(vertical="main_street", origin="ai_intake", stage="Applicant intake")

    assert row.label == "Test file"
    assert row.stage.family == "working"
    assert row.stage.label == "Applicant intake"
    assert row.coverage == "none"
    assert row.created_at == row.updated_at


def test_bucket_intake_link_request_defaults_file_list():
    req = BucketIntakeLinkRequest(bucket_id=uuid4(), intake_id=uuid4())
    assert req.file_ids == []
    assert req.relationship == "primary"
