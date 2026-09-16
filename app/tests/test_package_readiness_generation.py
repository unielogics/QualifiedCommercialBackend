import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.public_underwriting_intake import PublicUnderwritingIntakeArtifact
from app.routers import buckets, dealer_ai_intake
from app.routers.dealer_ai_intake import (
    PackageReadinessBucketFileRead,
    PublicUnderwritingArtifactRead,
)
from app.schemas.application_profile import ApplicationProgramsPatch, ProgramFitCandidate
from app.services import application_profiles, application_programs
from app.services import public_underwriting_packet_pdf as packet_pdf
from app.services.email.ses_client import SesSendResult


def test_executive_summary_pdf_is_a_compact_chart_first_underwriter_brief() -> None:
    captured: dict[str, str] = {}

    def _capture(html: str) -> bytes:
        captured["html"] = html
        return b"%PDF-summary"

    financials = {
        "selected_programs": [
            {
                "program_key": "business_heloc",
                "name": "Business-purpose HELOC",
                "playbook_version": 4,
            }
        ],
        "bank_months": [
            {
                "label": "Jul 2026",
                "deposits": 180_000,
                "withdrawals": 150_000,
                "ending_balance": 42_000,
                "nsf": 0,
            },
            {
                "label": "Aug 2026",
                "deposits": 210_000,
                "withdrawals": 168_000,
                "ending_balance": 56_000,
                "nsf": 1,
            },
        ],
        "package_readiness": {
            "requirements": [
                {
                    "requirement_key": "current_appraisal",
                    "label": "Current appraisal",
                    "required_level": "required",
                    "status": "requested",
                    "source_program_keys": ["business_heloc"],
                }
            ],
            "programs": [
                {
                    "program_key": "business_heloc",
                    "program_name": "Business-purpose HELOC",
                    "completion_percent": 75,
                    "complete": False,
                    "blocking_requirement_labels": ["Current appraisal"],
                }
            ],
        },
    }
    intake = SimpleNamespace(
        full_name="Alex Rivera",
        business_name="Northstar LLC",
        requested_loan_amount=500_000,
        loan_purpose="Working capital",
    )
    with (
        patch.dict(sys.modules, {"weasyprint": None}),
        patch.object(packet_pdf, "_render_html_pymupdf", side_effect=_capture),
        patch.object(packet_pdf, "_apply_watermark", side_effect=lambda value: value),
    ):
        rendered = packet_pdf.render_executive_summary_pdf(
            intake=intake,
            result={
                "probability_status": "Ready for lender review",
                "one_next_step": "Confirm debt schedule.",
                "strengths": ["Positive monthly cash flow"],
                "risks": ["One recent overdraft"],
            },
            executive_summary={
                "executive_summary": "Stable deposit activity supports lender review.",
                "recommended_approach": "Structure a revolving facility.",
                "suggested_application_types": ["Business line of credit"],
            },
            financials=financials,
        )

    assert rendered == b"%PDF-summary"
    html = captured["html"]
    assert "Executive Credit Brief" in html
    assert "Month-to-month bank activity" in html
    assert "2 months" in html and "Avg deposits" in html and "Latest balance" in html
    assert "data:image/png;base64" in html
    assert "Business-purpose HELOC (criteria v4)" in html
    assert "Current appraisal" in html
    assert "Requested; awaiting evidence" in html
    assert "Structure a revolving facility" not in html
    assert "Documents reviewed" not in html


def test_readiness_snapshot_hides_program_waived_missing_requirement() -> None:
    selection_id = uuid4()
    readiness = SimpleNamespace(
        selection_mode="manual",
        selections=[
            SimpleNamespace(
                program_key="business_heloc",
                program_name="Business-purpose HELOC",
                playbook_id=uuid4(),
                playbook_version=2,
                source="operator",
                needs_scope_review=False,
            )
        ],
        requirements=[
            SimpleNamespace(
                requirement_key="appraisal",
                label="Current appraisal",
                category="collateral",
                required_level="required",
                status="missing",
                evidence_count=0,
                verified_evidence_count=0,
                coverage_complete=False,
                verified_coverage_complete=False,
                verification_required=False,
                source_program_keys=["business_heloc"],
                source_policy_keys=[],
                program_overrides={"business_heloc": "waived"},
                state_reason=None,
            )
        ],
        programs=[
            SimpleNamespace(
                selection_id=selection_id,
                program_key="business_heloc",
                program_name="Business-purpose HELOC",
                complete=True,
                completion_percent=100,
                required_count=1,
                satisfied_count=1,
                blocking_requirement_keys=[],
            )
        ],
        can_advance=True,
        automatic_stage_status="advanced",
    )

    snapshot = dealer_ai_intake._normalized_package_readiness(readiness)

    assert snapshot["requirements"][0]["effectively_open"] is False
    assert snapshot["automatic_stage_status"] == "in_underwriting"
    assert packet_pdf._open_readiness_rows(
        {"package_readiness": snapshot}
    ) == []


def test_package_metadata_is_reassigned_and_outputs_are_identifiable() -> None:
    artifact = PublicUnderwritingIntakeArtifact(
        intake_id=uuid4(),
        artifact_type="executive_summary",
        title="Executive summary",
        body_json={"title": "Existing summary"},
    )
    metadata = {
        "version": 2,
        "status": "current",
        "bucket_file_id": str(uuid4()),
    }

    dealer_ai_intake._set_artifact_package_metadata(artifact, metadata)

    assert artifact.body_json == {
        "title": "Existing summary",
        "_package": metadata,
    }
    assert dealer_ai_intake._artifact_package_metadata(artifact) == metadata
    assert dealer_ai_intake._is_package_readiness_output(
        SimpleNamespace(
            source_kind="generated",
            source_detail="package_readiness:executive_summary:v2",
        )
    )
    assert not dealer_ai_intake._is_package_readiness_output(
        SimpleNamespace(source_kind="internal_upload", source_detail="package_readiness:lender_packet:v2")
    )


@pytest.mark.asyncio
async def test_bucket_publication_versions_snapshot_and_supersedes_prior_rows() -> None:
    now = datetime.now(UTC)
    old_file = SimpleNamespace(
        id=uuid4(),
        status="uploaded",
        deleted_at=None,
        deleted_by_user_id=None,
        delete_storage_status=None,
    )
    prior = PublicUnderwritingIntakeArtifact(
        id=uuid4(),
        intake_id=uuid4(),
        artifact_type="executive_summary",
        title="Prior summary",
        body_json={"_package": {"version": 3, "status": "current"}},
    )
    older = PublicUnderwritingIntakeArtifact(
        id=uuid4(),
        intake_id=prior.intake_id,
        artifact_type="executive_summary",
        title="Older summary",
        body_json={
            "_package": {
                "version": 2,
                "status": "superseded",
                "superseded_by_artifact_id": str(prior.id),
                "superseded_at": "2026-09-15T12:00:00+00:00",
            }
        },
    )
    legacy = PublicUnderwritingIntakeArtifact(
        id=uuid4(),
        intake_id=prior.intake_id,
        artifact_type="executive_summary",
        title="Standalone legacy summary",
        body_json={"title": "Legacy summary without package metadata"},
    )
    current = PublicUnderwritingIntakeArtifact(
        id=uuid4(),
        intake_id=prior.intake_id,
        artifact_type="executive_summary",
        title="Current summary",
        body_json={"_pdf": {"size_bytes": 8192, "sha256": "a" * 64}},
        s3_key="bucket/artifacts/current.pdf",
    )
    added: list[object] = []

    def _add(row: object) -> None:
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        if getattr(row, "created_at", None) is None:
            row.created_at = now
        added.append(row)

    def _rows(values: list[object]) -> SimpleNamespace:
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[_rows([old_file]), _rows([older, prior, legacy])]
        ),
        add=_add,
        flush=AsyncMock(),
    )
    intake = SimpleNamespace(
        id=prior.intake_id,
        bucket_id=uuid4(),
        business_name="Northstar LLC",
        full_name="Alex Rivera",
    )
    user = SimpleNamespace(id=uuid4(), name="Underwriter", email="uw@example.com")
    generation_id = uuid4()

    bucket_file, superseded_ids = await dealer_ai_intake._publish_package_artifact_to_bucket(
        db,
        intake=intake,
        artifact=current,
        previous_artifact=legacy,
        generation_id=generation_id,
        generated_at=now,
        user=user,
    )

    assert superseded_ids == [old_file.id]
    assert old_file.status == "superseded"
    assert old_file.deleted_at == now
    assert old_file.delete_storage_status == "retained_superseded"
    assert bucket_file.file_name == "Northstar LLC - Executive Summary.pdf"
    assert bucket_file.content_hash == "a" * 64
    assert bucket_file.source_detail == "package_readiness:executive_summary:v5"
    assert current.body_json["_package"]["version"] == 5
    assert current.body_json["_package"]["bucket_file_id"] == str(bucket_file.id)
    assert prior.body_json["_package"]["status"] == "superseded"
    assert prior.body_json["_package"]["superseded_by_artifact_id"] == str(legacy.id)
    assert legacy.body_json["_package"]["status"] == "superseded"
    assert legacy.body_json["_package"]["version"] == 4
    assert legacy.body_json["_package"]["superseded_by_artifact_id"] == str(current.id)
    assert older.body_json["_package"]["superseded_by_artifact_id"] == str(prior.id)
    assert older.body_json["_package"]["superseded_at"] == "2026-09-15T12:00:00+00:00"
    versions = [
        older.body_json["_package"]["version"],
        prior.body_json["_package"]["version"],
        legacy.body_json["_package"]["version"],
        current.body_json["_package"]["version"],
    ]
    assert len(versions) == len(set(versions))


@pytest.mark.asyncio
async def test_combined_generation_builds_and_publishes_both_pdfs_once() -> None:
    now = datetime.now(UTC)
    intake = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        bucket=SimpleNamespace(files=[], requested_documents=[]),
        latest_review_id=None,
        variant="main_street_business_v1",
        full_name="Alex Rivera",
        business_name="Northstar LLC",
        loan_purpose="Working capital",
        requested_loan_amount=250_000,
        estimated_credit_score=720,
        intake_state={},
        result_snapshot={},
    )
    user = SimpleNamespace(
        id=uuid4(),
        role=Role.LOAN_EXEC,
        name="Loan executive",
        email="loanexec@example.com",
    )

    def _artifact(kind: str) -> PublicUnderwritingIntakeArtifact:
        row = PublicUnderwritingIntakeArtifact(
            id=uuid4(),
            intake_id=intake.id,
            artifact_type=kind,
            title=kind.replace("_", " ").title(),
            body_json={"_pdf": {"size_bytes": 1024, "sha256": "b" * 64}},
            s3_key=f"artifacts/{kind}.pdf",
            created_by_user_id=user.id,
        )
        row.created_at = now
        row.updated_at = now
        return row

    summary = _artifact("executive_summary")
    packet = _artifact("lender_packet")
    summary_file = SimpleNamespace(id=uuid4())
    packet_file = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    def _artifact_read(
        row: PublicUnderwritingIntakeArtifact,
        *,
        current_input_fingerprint: str | None = None,
        active_bucket_file_ids: set | None = None,
    ) -> PublicUnderwritingArtifactRead:
        return PublicUnderwritingArtifactRead(
            id=row.id,
            intake_id=row.intake_id,
            artifact_type=row.artifact_type,
            title=row.title,
            body_json=row.body_json,
            s3_key=row.s3_key,
            created_by_user_id=row.created_by_user_id,
            created_at=now,
            updated_at=now,
        )

    def _file_read(row: SimpleNamespace) -> PackageReadinessBucketFileRead:
        return PackageReadinessBucketFileRead(
            id=row.id,
            bucket_id=intake.bucket_id,
            file_name=f"{row.id}.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            source_kind="generated",
            source_detail="package_readiness:test:v1",
            status="uploaded",
            created_at=now,
        )

    create_packet = AsyncMock(return_value=packet)
    publish = AsyncMock(
        side_effect=[(summary_file, [uuid4()]), (packet_file, [uuid4()])]
    )
    with (
        patch.object(
            dealer_ai_intake,
            "_load_admin_dealer_lead",
            AsyncMock(return_value=intake),
        ),
        patch.object(
            dealer_ai_intake,
            "_latest_artifact",
            AsyncMock(side_effect=[None, None]),
        ),
        patch.object(
            dealer_ai_intake,
            "_create_executive_summary_artifact",
            AsyncMock(return_value=summary),
        ) as create_summary,
        patch.object(
            dealer_ai_intake,
            "_lead_management_context",
            AsyncMock(return_value={"snapshot": True}),
        ),
        patch.object(
            dealer_ai_intake,
            "_collect_packet_financials",
            AsyncMock(return_value={"bank_months": []}),
        ),
        patch.object(
            dealer_ai_intake.profiles_service,
            "provision_profile_for_intake",
            AsyncMock(return_value=SimpleNamespace(id=uuid4())),
        ),
        patch.object(
            application_programs,
            "get_program_readiness",
            AsyncMock(
                return_value=SimpleNamespace(
                    selection_mode="manual",
                    selections=[],
                    programs=[],
                    requirements=[],
                    can_advance=False,
                    automatic_stage_status="not_ready",
                )
            ),
        ),
        patch.object(
            dealer_ai_intake,
            "_create_lender_packet_artifact",
            create_packet,
        ),
        patch.object(
            dealer_ai_intake,
            "_publish_package_artifact_to_bucket",
            publish,
        ),
        patch.object(dealer_ai_intake, "_log", AsyncMock()),
        patch.object(
            dealer_ai_intake,
            "_management_artifacts",
            AsyncMock(return_value=[packet, summary]),
        ),
        patch.object(dealer_ai_intake, "_artifact_read", side_effect=_artifact_read),
        patch.object(
            dealer_ai_intake,
            "_package_bucket_file_read",
            side_effect=_file_read,
        ),
    ):
        result = await dealer_ai_intake.generate_package_readiness_documents(
            intake.id,
            user,
            db,
        )

    create_summary.assert_awaited_once()
    create_packet.assert_awaited_once()
    assert create_summary.await_args.kwargs["financials"]["bank_months"] == []
    assert create_packet.await_args.kwargs["financials"]["bank_months"] == []
    assert create_summary.await_args.kwargs["financials"]["selected_programs"] == []
    assert create_summary.await_args.kwargs["context_snapshot"]["snapshot"] is True
    assert create_summary.await_args.kwargs["context_snapshot"]["package_readiness"] == {
        "selection_mode": "manual",
        "selected_programs": [],
        "programs": [],
        "requirements": [],
        "can_advance": False,
        "automatic_stage_status": "not_ready",
    }
    assert create_packet.await_args.kwargs["files_snapshot"] == []
    assert (
        create_summary.await_args.kwargs["source_snapshot_metadata"]
        == create_packet.await_args.kwargs["source_snapshot_metadata"]
    )
    assert publish.await_count == 2
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    assert result.executive_summary.id == summary.id
    assert result.lender_packet.id == packet.id
    assert [item.id for item in result.bucket_files] == [summary_file.id, packet_file.id]
    assert len(result.artifact_history) == 2


@pytest.mark.asyncio
async def test_delivery_requires_one_fresh_package_generation() -> None:
    generation_id = uuid4()
    fingerprint = "f" * 64
    bucket_id = uuid4()
    bucket_file_ids = {
        "executive_summary": uuid4(),
        "lender_packet": uuid4(),
    }

    def _artifact(kind: str, *, value: str = fingerprint) -> PublicUnderwritingIntakeArtifact:
        return PublicUnderwritingIntakeArtifact(
            id=uuid4(),
            intake_id=uuid4(),
            artifact_type=kind,
            title=kind,
            body_json={
                "_package": {
                    "status": "current",
                    "generation_id": str(generation_id),
                    "bucket_file_id": str(bucket_file_ids[kind]),
                },
                "_pdf": {"package_input_sha256": value},
            },
            s3_key=f"artifacts/{kind}.pdf",
        )

    intake = SimpleNamespace(id=uuid4(), bucket_id=bucket_id)
    summary = _artifact("executive_summary")
    packet = _artifact("lender_packet")
    summary.intake_id = intake.id
    packet.intake_id = intake.id
    bucket_files = [
        SimpleNamespace(
            id=bucket_file_ids[artifact.artifact_type],
            s3_key=artifact.s3_key,
        )
        for artifact in (summary, packet)
    ]
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: bucket_files)
            )
        )
    )
    with (
        patch.object(
            dealer_ai_intake,
            "_latest_artifact",
            AsyncMock(side_effect=[summary, packet]),
        ),
        patch.object(
            dealer_ai_intake,
            "_current_package_input_fingerprint",
            AsyncMock(return_value=fingerprint),
        ),
    ):
        current_summary, current_packet = (
            await dealer_ai_intake._fresh_package_artifact_pair(db, intake)
        )

    assert current_summary is summary
    assert current_packet is packet

    with (
        patch.object(
            dealer_ai_intake,
            "_latest_artifact",
            AsyncMock(side_effect=[summary, packet]),
        ),
        patch.object(
            dealer_ai_intake,
            "_current_package_input_fingerprint",
            AsyncMock(return_value="0" * 64),
        ),
        pytest.raises(HTTPException) as caught,
    ):
        await dealer_ai_intake._fresh_package_artifact_pair(db, intake)

    assert getattr(caught.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_out_of_scope_published_program_remains_available_for_manual_override() -> None:
    program_id = uuid4()
    playbook_id = uuid4()
    catalog = SimpleNamespace(
        id=program_id,
        program_key="business_heloc",
        name="Business-purpose HELOC",
        public_slug="business-heloc",
    )
    scope = SimpleNamespace(
        vertical="real_estate",
        intake_variants=[],
        intent_keys=[],
        industry_keys=[],
        naics_prefixes=[],
        required_fact_keys=[],
    )
    playbook = SimpleNamespace(
        id=playbook_id,
        funding_program_id=program_id,
        owner_type="funding",
        version=3,
        published_at=None,
        created_at=None,
        rules={"fit": {"field": "requested_amount", "operator": "gte", "value": 1}},
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [playbook])
            )
        )
    )
    profile = SimpleNamespace(id=uuid4(), vertical="main_street")

    with (
        patch.object(
            application_programs,
            "profile_fit_context",
            AsyncMock(
                return_value={
                    "vertical": "main_street",
                    "intent_kind": "lending",
                    "requested_amount": 100_000,
                }
            ),
        ),
        patch.object(application_programs.program_catalog, "catalog_rows", AsyncMock(return_value=[catalog])),
        patch.object(
            application_programs.program_catalog,
            "scopes_by_program",
            AsyncMock(return_value={program_id: [scope]}),
        ),
        patch.object(application_programs, "validate_rules"),
        patch.object(
            application_programs,
            "evaluate_rules",
            return_value=SimpleNamespace(matched=True, confidence=0.96, reasons=["Amount fits"]),
        ),
    ):
        candidates = await application_programs.published_candidates(db, profile)

    assert len(candidates) == 1
    assert candidates[0].program_key == "business_heloc"
    assert candidates[0].recommendation_status == "not_eligible"
    assert candidates[0].eligible is False
    assert candidates[0].playbook_id == playbook_id
    assert candidates[0].reasons == ["Product is not offered for this vertical"]


@pytest.mark.asyncio
async def test_manual_program_override_requires_reason_and_pins_published_version() -> None:
    candidate = ProgramFitCandidate(
        program_key="business_heloc",
        program_name="Business-purpose HELOC",
        catalog_id=uuid4(),
        public_slug="business-heloc",
        playbook_id=uuid4(),
        playbook_version=4,
        eligible=False,
        recommendation_status="not_eligible",
        reasons=["Product is outside the current file scope"],
    )
    profile = SimpleNamespace(
        id=uuid4(),
        program_selection_mode="auto",
        program_selection_locked_at=None,
        program_selection_locked_by_user_id=None,
    )
    user = SimpleNamespace(id=uuid4())
    added: list[object] = []
    db = SimpleNamespace(add=added.append, flush=AsyncMock())

    with (
        patch.object(application_programs, "active_selections", AsyncMock(return_value=[])),
        patch.object(
            application_programs,
            "published_candidates",
            AsyncMock(return_value=[candidate]),
        ),
    ):
        with pytest.raises(Exception) as exc:
            await application_programs.set_programs(
                db,
                profile,
                ApplicationProgramsPatch(
                    program_keys=[candidate.program_key],
                    confirmed=True,
                ),
                user,
            )
        assert "reviewed reason" in str(exc.value.detail)

        await application_programs.set_programs(
            db,
            profile,
            ApplicationProgramsPatch(
                program_keys=[candidate.program_key],
                confirmed=True,
                reason="Underwriter approved this exception for manual review.",
            ),
            user,
        )

    assert profile.program_selection_mode == "manual"
    assert len(added) == 1
    selection = added[0]
    assert selection.program_key == candidate.program_key
    assert selection.playbook_id == candidate.playbook_id
    assert selection.playbook_version == candidate.playbook_version
    assert selection.needs_scope_review is True


@pytest.mark.asyncio
async def test_retained_program_selection_keeps_its_pinned_playbook_version() -> None:
    original_playbook_id = uuid4()
    selection = SimpleNamespace(
        program_key="business_heloc",
        playbook_id=original_playbook_id,
        playbook_version=2,
        removed_at=None,
        removed_by_user_id=None,
    )
    candidate = ProgramFitCandidate(
        program_key="business_heloc",
        program_name="Business-purpose HELOC",
        catalog_id=uuid4(),
        public_slug="business-heloc",
        playbook_id=uuid4(),
        playbook_version=5,
        eligible=True,
        recommendation_status="recommended",
    )
    profile = SimpleNamespace(
        id=uuid4(),
        program_selection_mode="manual",
        program_selection_locked_at=None,
        program_selection_locked_by_user_id=None,
    )
    db = SimpleNamespace(add=lambda _row: None, flush=AsyncMock())
    user = SimpleNamespace(id=uuid4())
    with (
        patch.object(application_programs, "active_selections", AsyncMock(return_value=[selection])),
        patch.object(application_programs, "published_candidates", AsyncMock(return_value=[candidate])),
    ):
        await application_programs.set_programs(
            db,
            profile,
            ApplicationProgramsPatch(
                program_keys=[candidate.program_key],
                confirmed=True,
                reason="Keep the previously reviewed program selection.",
            ),
            user,
        )

    assert selection.playbook_id == original_playbook_id
    assert selection.playbook_version == 2


@pytest.mark.asyncio
async def test_deleting_package_bucket_row_retains_immutable_artifact_bytes() -> None:
    bucket_id = uuid4()
    file_id = uuid4()
    file = SimpleNamespace(
        id=file_id,
        bucket_id=bucket_id,
        status="uploaded",
        deleted_at=None,
        deleted_by_user_id=None,
        delete_storage_status=None,
        s3_key="artifacts/executive-summary.pdf",
        source_kind="generated",
        source_detail="package_readiness:executive_summary:v1",
        file_name="Executive Summary.pdf",
        requested_document_id=None,
        shares=[],
        vendor_access=[],
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: file)
        ),
        commit=AsyncMock(),
    )
    with (
        patch.object(buckets, "_load_bucket_or_404", AsyncMock()),
        patch.object(buckets, "_delete_s3_object") as delete_s3,
        patch.object(
            buckets,
            "_recalculate_requested_document_status",
            AsyncMock(),
        ),
        patch.object(buckets, "_log", AsyncMock()),
    ):
        await buckets.delete_admin_file(
            bucket_id,
            file_id,
            SimpleNamespace(),
            SimpleNamespace(id=uuid4()),
            db,
        )

    delete_s3.assert_not_called()
    assert file.deleted_at is not None
    assert file.delete_storage_status == "retained_package_artifact"
    db.commit.assert_awaited_once()


def test_foreclosure_intake_uses_real_estate_program_vertical() -> None:
    intake = SimpleNamespace(variant="commercial_foreclosure_bailout_v1")
    assert application_profiles._vertical_for_intake(intake) == "real_estate"


@pytest.mark.asyncio
async def test_package_inputs_and_lender_scope_requery_current_bucket_rows() -> None:
    now = datetime.now(UTC)
    stale_cached = SimpleNamespace(
        id=uuid4(),
        source_kind="client_upload",
        source_detail=None,
        content_hash="a" * 64,
        size_bytes=10,
        created_at=now,
    )
    current = SimpleNamespace(
        id=uuid4(),
        source_kind="client_upload",
        source_detail=None,
        content_hash="b" * 64,
        size_bytes=20,
        created_at=now,
    )
    merchant_source = SimpleNamespace(
        id=uuid4(),
        source_kind="internal_upload",
        source_detail="Merchant processing offer",
        content_hash="c" * 64,
        size_bytes=30,
        created_at=now,
    )
    intake = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        bucket=SimpleNamespace(files=[stale_cached], requested_documents=[]),
    )

    def _rows(values: list[object]) -> SimpleNamespace:
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_rows([current]), _rows([current, merchant_source])])
    )
    readiness = SimpleNamespace(
        selection_mode="auto",
        selections=[],
        programs=[],
        requirements=[],
        can_advance=False,
        automatic_stage_status="not_ready",
    )
    context = AsyncMock(return_value={"snapshot": True})
    financials = AsyncMock(return_value={"bank_months": []})
    with (
        patch.object(
            dealer_ai_intake.profiles_service,
            "provision_profile_for_intake",
            AsyncMock(return_value=SimpleNamespace(id=uuid4())),
        ),
        patch.object(
            application_programs,
            "get_program_readiness",
            AsyncMock(return_value=readiness),
        ),
        patch.object(dealer_ai_intake, "_lead_management_context", context),
        patch.object(dealer_ai_intake, "_collect_packet_financials", financials),
    ):
        source_files, *_rest = await dealer_ai_intake._package_generation_inputs(
            db, intake
        )
        lender_files = await dealer_ai_intake._lender_package_bucket_files(db, intake)

    assert source_files == [current]
    assert lender_files == [current]
    assert context.await_args.kwargs["files_snapshot"] == [current]
    assert financials.await_args.kwargs["active_file_ids"] == {current.id}


@pytest.mark.asyncio
async def test_prepare_vendor_access_eager_loads_existing_selected_files() -> None:
    intake = SimpleNamespace(bucket_id=uuid4())
    vendor = SimpleNamespace(id=uuid4())
    existing_file = SimpleNamespace(id=uuid4())
    current_file = SimpleNamespace(id=uuid4())
    access = SimpleNamespace(id=uuid4(), files=[existing_file])
    captured: dict[str, object] = {}

    async def _execute(statement: object) -> SimpleNamespace:
        captured["statement"] = statement
        return SimpleNamespace(scalar_one_or_none=lambda: access)

    db = SimpleNamespace(execute=_execute)
    payload = dealer_ai_intake.VendorEmailSendRequest(
        to_emails=["lender@example.com"],
        cc_emails=[],
        include_lender_packet=False,
        attach_lender_packet=False,
        subject="Northstar package",
        body="Please review this package.",
        bucket_access="login",
    )

    with (
        patch.object(
            dealer_ai_intake,
            "_vendor_user_from_payload",
            AsyncMock(return_value=vendor),
        ),
        patch.object(
            dealer_ai_intake,
            "_lender_package_bucket_files",
            AsyncMock(return_value=[current_file]),
        ),
        patch.object(dealer_ai_intake, "_log", AsyncMock()),
    ):
        result = await dealer_ai_intake._prepare_vendor_access(
            db, intake, "lender@example.com", payload
        )

    statement = captured["statement"]
    assert any(
        "BucketVendorAccess.files" in str(option.path)
        for option in statement._with_options
    )
    assert result is access
    assert access.files == [current_file]
    assert access.file_scope == "selected"


@pytest.mark.asyncio
async def test_vendor_send_commits_dispatch_and_provider_outcome_before_audit() -> None:
    now = datetime.now(UTC)
    intake = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        bucket=SimpleNamespace(files=[], requested_documents=[]),
    )
    summary = PublicUnderwritingIntakeArtifact(
        id=uuid4(),
        intake_id=intake.id,
        artifact_type="executive_summary",
        title="Executive Summary",
        body_json={},
        s3_key="artifacts/summary.pdf",
    )
    packet = PublicUnderwritingIntakeArtifact(
        id=uuid4(),
        intake_id=intake.id,
        artifact_type="lender_packet",
        title="Lender Package",
        body_json={},
        s3_key="artifacts/packet.pdf",
    )
    user = SimpleNamespace(
        id=uuid4(),
        role=Role.SUPER_ADMIN,
        name="Underwriter",
        email="underwriter@example.com",
    )
    payload = dealer_ai_intake.VendorEmailSendRequest(
        to_emails=["lender@example.com"],
        cc_emails=[],
        include_lender_packet=False,
        attach_lender_packet=False,
        attach_executive_summary=False,
        attach_package_zip=False,
        bucket_access="none",
        subject="Northstar package",
        body="Please review the attached underwriting package.",
    )
    added: list[object] = []

    def _add(row: object) -> None:
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        row.created_at = now
        row.updated_at = now
        added.append(row)

    def _rows(values: list[object]) -> SimpleNamespace:
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[SimpleNamespace(), _rows([]), SimpleNamespace()]
        ),
        add=_add,
        commit=AsyncMock(),
        rollback=AsyncMock(),
        refresh=AsyncMock(),
    )

    async def _send(*_args: object, **_kwargs: object) -> SesSendResult:
        assert db.commit.await_count == 1
        assert db.execute.await_count == 3
        assert len(added) == 1
        assert added[0].ses_status == "dispatching"
        return SesSendResult(True, "provider-123", "sent")

    async def _audit(*_args: object, **_kwargs: object) -> None:
        assert db.commit.await_count >= 2
        assert added[0].ses_message_ids == ["provider-123"]

    with (
        patch.object(
            dealer_ai_intake,
            "_load_admin_dealer_lead",
            AsyncMock(return_value=intake),
        ),
        patch.object(
            dealer_ai_intake,
            "_fresh_package_artifact_pair",
            AsyncMock(side_effect=[(summary, packet), (summary, packet)]),
        ) as fresh_pair,
        patch.object(dealer_ai_intake, "send_as_user", side_effect=_send) as send,
        patch.object(dealer_ai_intake, "_log", side_effect=_audit),
    ):
        response = await dealer_ai_intake.send_dealer_ai_vendor_email(
            intake.id, payload, user, db
        )

    assert fresh_pair.await_count == 2
    send.assert_awaited_once()
    assert db.commit.await_count == 4
    assert response.email_sends[0].ses_status == "sent"
    assert response.email_sends[0].ses_message_ids == ["provider-123"]


@pytest.mark.asyncio
async def test_vendor_send_blocks_retry_with_uncertain_dispatch() -> None:
    intake = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        bucket=SimpleNamespace(files=[], requested_documents=[]),
    )
    summary = SimpleNamespace(id=uuid4())
    packet = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    payload = dealer_ai_intake.VendorEmailSendRequest(
        to_emails=["lender@example.com"],
        cc_emails=[],
        include_lender_packet=False,
        attach_lender_packet=False,
        subject="Northstar package",
        body="Please review this package.",
        bucket_access="none",
    )
    outstanding = SimpleNamespace(
        to_emails=["lender@example.com"],
        cc_emails=[],
        body="Please review this package.\n\nSecure file access",
    )

    def _rows(values: list[object]) -> SimpleNamespace:
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[SimpleNamespace(), _rows([outstanding])])
    )
    with (
        patch.object(
            dealer_ai_intake,
            "_load_admin_dealer_lead",
            AsyncMock(return_value=intake),
        ),
        patch.object(
            dealer_ai_intake,
            "_fresh_package_artifact_pair",
            AsyncMock(return_value=(summary, packet)),
        ),
        patch.object(dealer_ai_intake, "send_as_user", AsyncMock()) as send,
        pytest.raises(HTTPException) as caught,
    ):
        await dealer_ai_intake.send_dealer_ai_vendor_email(
            intake.id, payload, user, db
        )

    assert caught.value.status_code == 409
    assert "already in progress" in str(caught.value.detail)
    send.assert_not_awaited()
