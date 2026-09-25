from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.application_profile import ApplicationProfile
from app.models.operator_file import BucketIntakeLink, BucketIntakeLinkFile
from app.routers.operator_files import (
    _collapse_logical_rows,
    _forecast_values,
    _funding_stage,
    _pipeline_status_for_row,
    _reconcile_link_files,
    _rollup,
    _variant_vertical,
    _working_stage,
)
from app.schemas.operator_file import BucketIntakeLinkRead, BucketIntakeLinkRequest, UnifiedFileRow


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
        working_stage=_working_stage(
            vertical, "applicant_intake" if vertical != "real_estate" else "lead"
        ),
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


def test_rollup_counts_pipeline_lifecycle_when_present():
    row = _row(vertical="dealer", origin="ai_intake", stage="Applicant intake")
    row.pipeline_status = "in_underwriting"

    rollup = _rollup([row])

    assert rollup.by_stage == {"in_underwriting": 1}


def _economics_row(
    *,
    status: str,
    requested: float | None = None,
    approved: float | None = None,
    funded: float | None = None,
    points: float | None = None,
    profile_id=None,
) -> UnifiedFileRow:
    row = _row(vertical="dealer", origin="ai_intake", stage="Applicant intake")
    row.profile_id = profile_id or uuid4()
    row.pipeline_status = status  # type: ignore[assignment]
    row.requested_amount = requested
    row.approved_amount = approved
    row.funded_amount = funded
    row.forecast_fee_points = points
    basis, amount, earnings = _forecast_values(
        status_value=status,
        requested_amount=requested,
        approved_amount=approved,
        funded_amount=funded,
        fee_points=points,
    )
    row.forecast_amount_basis = basis  # type: ignore[assignment]
    row.forecast_amount = amount
    row.forecast_earnings = earnings
    return row


def test_pipeline_economics_use_lifecycle_amount_basis_and_one_point_is_one_percent():
    rows = [
        _economics_row(status="collecting_docs", requested=100_000, points=2),
        _economics_row(status="in_underwriting", requested=200_000, points=1.5),
        _economics_row(status="approved", approved=300_000, points=1),
        _economics_row(status="closed_won", funded=400_000, points=0.5),
    ]

    economics = _rollup(rows).pipeline_economics

    assert economics["requested"].count == 1
    assert economics["requested"].value == 100_000
    assert economics["requested"].forecast_earnings == 2_000
    assert economics["underwriting"].value == 200_000
    assert economics["underwriting"].forecast_earnings == 3_000
    assert economics["approved"].value == 300_000
    assert economics["approved"].forecast_earnings == 3_000
    assert economics["funded"].value == 400_000
    assert economics["funded"].forecast_earnings == 2_000


def test_pipeline_economics_exclude_null_points_from_earnings_and_report_coverage():
    rows = [
        _economics_row(status="submitted", requested=100_000, points=2),
        _economics_row(status="collecting_docs", requested=50_000),
        _economics_row(status="denied", requested=900_000, points=10),
    ]

    economics = _rollup(rows).pipeline_economics

    assert economics["requested"].count == 2
    assert economics["requested"].value == 150_000
    assert economics["requested"].forecasted_count == 1
    assert economics["requested"].forecast_coverage_pct == 50
    assert economics["requested"].forecast_earnings == 2_000
    assert sum(bucket.count for bucket in economics.values()) == 2


def test_pipeline_economics_deduplicate_rows_by_application_profile():
    profile_id = uuid4()
    primary = _economics_row(
        status="approved", approved=125_000, points=2, profile_id=profile_id
    )
    duplicate = _economics_row(
        status="approved", approved=125_000, points=2, profile_id=profile_id
    )

    approved = _rollup([primary, duplicate]).pipeline_economics["approved"]

    assert approved.count == 1
    assert approved.value == 125_000
    assert approved.forecast_earnings == 2_500


def test_pipeline_economics_exclude_profileless_bucket_workspaces():
    bucket = _row(vertical="dealer", origin="console", stage="Applicant intake")
    bucket.source_kind = "bucket"
    bucket.pipeline_status = "collecting_docs"
    bucket.requested_amount = 500_000
    bucket.forecast_amount = 500_000
    bucket.forecast_fee_points = 2
    bucket.forecast_earnings = 10_000

    requested = _rollup([bucket]).pipeline_economics["requested"]

    assert requested.count == 0
    assert requested.value == 0
    assert requested.forecast_earnings == 0


def test_pipeline_status_prefers_profile_underwriting_lifecycle():
    row = _row(vertical="dealer", origin="ai_intake", stage="Applicant intake")
    profile = ApplicationProfile(underwriting_status="approved")

    assert _pipeline_status_for_row(row, profile) == "approved"


def test_pipeline_status_maps_promoted_loan_stage():
    row = _row(vertical="main_street", origin="console", stage="funded")

    assert _pipeline_status_for_row(row, None) == "closed_won"


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


def test_explicit_dealer_handoff_collapses_to_one_logical_file():
    intake = _row(vertical="dealer", origin="ai_intake", stage="Prequalified")
    intake.loan_id = uuid4()
    intake.funding_stage = _funding_stage("prequalified")
    dealer_id = uuid4()
    dealer = UnifiedFileRow(
        id=f"dealer:{dealer_id}",
        source_kind="dealer",
        source_id=dealer_id,
        ref="QC-R-TEST",
        title="Same Business",
        dealer_id=dealer_id,
        intake_id=intake.source_id,
        vertical="dealer",
        vertical_label="Dealer",
        origin="rep",
        origin_label="Rep desk",
        source_label="Rep desk",
        working_stage=_working_stage("dealer", "verification"),
        normalized_stage="Verification",
        health="On track",
        updated_at=datetime.now(UTC),
    )

    collapsed = _collapse_logical_rows([intake, dealer])

    assert len(collapsed) == 1
    assert collapsed[0].source_kind == "dealer"
    assert collapsed[0].loan_id == intake.loan_id
    assert collapsed[0].funding_stage is not None


def test_matching_names_without_lineage_never_collapse():
    intake = _row(vertical="dealer", origin="ai_intake", stage="Applicant intake")
    dealer_id = uuid4()
    dealer = UnifiedFileRow(
        id=f"dealer:{dealer_id}",
        source_kind="dealer",
        source_id=dealer_id,
        ref="QC-R-SEPARATE",
        title=intake.title,
        dealer_id=dealer_id,
        vertical="dealer",
        vertical_label="Dealer",
        origin="rep",
        origin_label="Rep desk",
        source_label="Rep desk",
        working_stage=_working_stage("dealer", "applicant_intake"),
        normalized_stage="Applicant intake",
        health="On track",
        updated_at=datetime.now(UTC),
    )

    assert len(_collapse_logical_rows([intake, dealer])) == 2


def test_selected_file_reconciliation_is_reversible_without_copying_files():
    first, second = uuid4(), uuid4()
    actor = uuid4()
    link = BucketIntakeLink(
        bucket_id=uuid4(),
        intake_id=uuid4(),
        relationship="supporting",
        files=[BucketIntakeLinkFile(bucket_file_id=first, selected_by_user_id=actor)],
    )

    _reconcile_link_files(link, [second], actor)

    by_file = {item.bucket_file_id: item for item in link.files}
    assert by_file[first].removed_at is not None
    assert by_file[second].removed_at is None
    assert by_file[second].bucket_file_id == second

    _reconcile_link_files(link, [first], actor)

    assert by_file[first].removed_at is None
    assert by_file[second].removed_at is not None


def test_link_read_contract_carries_id_and_selected_references():
    now = datetime.now(UTC)
    link_id, bucket_id, intake_id, file_id = uuid4(), uuid4(), uuid4(), uuid4()
    row = BucketIntakeLinkRead(
        link_id=link_id,
        bucket_id=bucket_id,
        intake_id=intake_id,
        relationship="supporting",
        linked_file_ids=[file_id],
        status="active",
        created_at=now,
        updated_at=now,
    )

    assert row.link_id == link_id
    assert row.linked_file_ids == [file_id]
