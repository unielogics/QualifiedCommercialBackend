from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.models.application_profile import (
    ApplicationOwner,
    ApplicationPlaidItem,
    ApplicationRequirementEvidence,
)
from app.models.bucket import BucketRequestedDocument
from app.routers.application_profiles import (
    _application_bank_state,
    _can_review_manual_bank_evidence,
    _require_profile_bank_client,
    get_application_banks,
    get_application_evidence,
    get_application_evidence_file_url,
)
from app.routers.communications import _intake_allowed_channels
from app.schemas.application_profile import (
    ApplicationBankEvidenceFileRead,
    ApplicationRequirementAIReview,
    ApplicationRequirementBatchReminder,
    ApplicationRequirementPatch,
    BusinessBankEvidence,
    ClientEvidenceBankingSummary,
    ClientEvidenceRequirementRead,
    FileOwnerPatch,
    SupportingDocumentGroupRead,
)
from app.services.application_profiles import (
    ManualStatementEvidence,
    _client_requirement_coverage,
    _statement_months_from_analysis,
    application_evidence_summary,
    capture_extracted_profile_facts,
    evidence_preview_endpoint,
    manual_statement_evidence,
)
from app.services.underwriting_intelligence import calculate_dscr


def test_owner_credit_threshold_is_inclusive_and_requires_personal_contacts() -> None:
    below = ApplicationOwner(first_name="Alex", last_name="Rivera", ownership_pct=19.99)
    required = ApplicationOwner(first_name="Blair", last_name="Chen", ownership_pct=20.00)

    assert below.credit_required is False
    assert required.credit_required is True
    assert required.credit_contact_complete is False

    required.email = "blair@example.com"
    required.phone = "+12025550123"
    assert required.credit_contact_complete is True


def test_owner_patch_allows_omitted_names_but_rejects_clearing_them() -> None:
    assert FileOwnerPatch(email="owner@example.com").first_name is None

    with pytest.raises(ValidationError):
        FileOwnerPatch(first_name=None)
    with pytest.raises(ValidationError):
        FileOwnerPatch(last_name="   ")


def test_requirement_patch_accepts_multiple_evidence_files() -> None:
    file_ids = [uuid4(), uuid4()]

    payload = ApplicationRequirementPatch(
        action="link_evidence",
        evidence_file_ids=file_ids,
        confirmed=True,
    )

    assert set(payload.evidence_file_ids) == set(file_ids)
    with pytest.raises(ValidationError):
        ApplicationRequirementPatch(action="unlink_evidence", confirmed=True)


def test_batch_reminder_deduplicates_requirements_and_requires_a_selection() -> None:
    payload = ApplicationRequirementBatchReminder(
        requirement_keys=["tax_returns", "bank_statements", "tax_returns"]
    )

    assert payload.requirement_keys == ["tax_returns", "bank_statements"]
    with pytest.raises(ValidationError):
        ApplicationRequirementBatchReminder(requirement_keys=[])


def test_ai_requirement_review_allows_all_or_selected_requirements() -> None:
    all_received = ApplicationRequirementAIReview(confirmed=True)
    selected = ApplicationRequirementAIReview(
        requirement_keys=["tax_returns", "tax_returns"],
        confirmed=True,
    )

    assert all_received.requirement_keys == []
    assert selected.requirement_keys == ["tax_returns"]


def test_model_metadata_contains_partial_uniqueness_contracts() -> None:
    owner_indexes = {index.name for index in ApplicationOwner.__table__.indexes}
    bank_indexes = {index.name for index in ApplicationPlaidItem.__table__.indexes}
    requirement_evidence_indexes = {
        index.name for index in ApplicationRequirementEvidence.__table__.indexes
    }
    requested_document_indexes = {
        index.name for index in BucketRequestedDocument.__table__.indexes
    }

    assert "uq_application_owners_primary" in owner_indexes
    assert "uq_application_owners_email" in owner_indexes
    assert "uq_application_plaid_items_primary" in bank_indexes
    assert "uq_application_requirement_evidence_active" in requirement_evidence_indexes
    assert (
        "uq_bucket_requested_documents_supporting_group"
        in requested_document_indexes
    )


def test_supporting_document_group_is_optional_and_multi_file() -> None:
    group = SupportingDocumentGroupRead(
        id=uuid4(),
        bucket_id=uuid4(),
        name="Supporting / Other",
    )

    assert group.required is False
    assert group.allow_multiple_files is True
    assert group.file_count == 0


def test_business_bank_evidence_uses_provider_neutral_sources() -> None:
    state = BusinessBankEvidence(
        source="uploaded_statements",
        accepted_statement_months=["2026-01", "2026-02"],
        required_statement_months=6,
    )

    assert state.connected_institutions == 0
    assert state.statement_coverage_complete is False
    with pytest.raises(ValidationError):
        BusinessBankEvidence(source="manual_upload")


def test_client_evidence_summary_excludes_staff_program_details() -> None:
    summary = ClientEvidenceBankingSummary(
        requirements=[
            ClientEvidenceRequirementRead(
                requirement_key="business_bank_statements_6_months",
                label="Last 6 months business bank statements",
                required_level="required",
                status="verified",
                complete=True,
                evidence_count=6,
                accepted_evidence_count=6,
                coverage={"months": 6, "required_months": 6, "complete": True},
            )
        ],
        required_count=1,
        completed_required_count=1,
        bank_evidence=BusinessBankEvidence(
            source="uploaded_statements",
            accepted_statement_months=[
                "2026-01",
                "2026-02",
                "2026-03",
                "2026-04",
                "2026-05",
                "2026-06",
            ],
            statement_coverage_complete=True,
            banking_access_complete=True,
        ),
    )

    payload = summary.model_dump()
    assert payload["missing_required_count"] == 0
    assert payload["bank_evidence"]["statement_coverage_complete"] is True
    assert "programs" not in payload
    assert "candidates" not in payload
    assert "source_program_keys" not in payload["requirements"][0]


def test_client_requirement_coverage_strips_classifier_metadata() -> None:
    coverage = _client_requirement_coverage(
        {
            "matched_files": 2,
            "expected_classifications": ["business_tax_return"],
            "classifications": ["business_tax_return"],
            "years": ["2024", "2025"],
            "current": 2,
            "required": 2,
            "unit": "years",
            "complete": True,
        }
    )

    assert coverage == {
        "years": ["2024", "2025"],
        "current": 2,
        "required": 2,
        "unit": "years",
        "complete": True,
    }


@pytest.mark.parametrize(
    ("role", "channels"),
    [
        (Role.SUPER_ADMIN, {"underwriter_ai", "client", "partner", "internal"}),
        (Role.LOAN_EXEC, {"underwriter_ai", "client", "partner", "internal"}),
        (Role.DEALER_PARTNER, {"partner"}),
        (Role.BROKER, {"client"}),
        (Role.REGIONAL_MANAGER, {"client"}),
        (Role.CLIENT, {"client"}),
        (Role.VENDOR, set()),
    ],
)
def test_intake_channels_remain_role_confined(role: Role, channels: set[str]) -> None:
    assert _intake_allowed_channels(SimpleNamespace(role=role)) == channels


def test_application_bank_actions_are_client_owned() -> None:
    application = SimpleNamespace(dealer_id=None)
    dealer = SimpleNamespace(dealer_id="dealer-id")

    _require_profile_bank_client(dealer, SimpleNamespace(role=Role.DEALER))
    _require_profile_bank_client(application, SimpleNamespace(role=Role.CLIENT))

    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP):
        with pytest.raises(HTTPException) as application_error:
            _require_profile_bank_client(application, SimpleNamespace(role=role))
        assert application_error.value.status_code == 403

        with pytest.raises(HTTPException) as dealer_error:
            _require_profile_bank_client(dealer, SimpleNamespace(role=role))
        assert dealer_error.value.status_code == 403


@pytest.mark.asyncio
async def test_extracted_facts_stay_with_source_bucket_while_links_are_discovered() -> None:
    bucket_id = uuid4()
    primary = SimpleNamespace(id=uuid4(), primary_bucket_id=bucket_id)
    linked = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())

    def rows(values):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    missing = SimpleNamespace(scalar_one_or_none=lambda: None)
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                rows([uuid4()]),
                rows([primary, linked, primary]),
                rows([]),
                missing,
            ]
        ),
        add=added.append,
        flush=AsyncMock(),
    )
    file = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, statement_period=None)
    analysis = SimpleNamespace(
        id=uuid4(),
        classification="tax_return",
        analysis={
            "profile_facts": {
                "legal_entity_name": {"value": "Grace Auto Sales and Service, Inc."}
            },
            "key_facts": {},
        },
    )

    profile_ids = await capture_extracted_profile_facts(db, file=file, analysis=analysis)

    assert profile_ids == [primary.id]
    assert [fact.profile_id for fact in added] == [primary.id]
    assert db.execute.await_count == 4
    db.flush.assert_awaited_once()


def test_manual_statement_coverage_uses_every_explicit_month() -> None:
    assert _statement_months_from_analysis(
        {
            "key_facts": {
                "statement_period": "2026-01-01 to 2026-01-31",
                "months": [
                    {"month": "2026-02"},
                    {"statement_period": "2026/03/01 through 2026/03/31"},
                    {"period": "not stated"},
                ],
            }
        }
    ) == {"2026-01", "2026-02", "2026-03"}


def test_shared_evidence_summary_preserves_ai_decision_states() -> None:
    summary = application_evidence_summary(
        ManualStatementEvidence(
            months=["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"],
            file_count=9,
            accepted_file_count=6,
            pending_analysis_count=1,
            needs_more_file_count=1,
            rejected_file_count=0,
            failed_analysis_count=1,
        )
    )

    assert summary.bank_statement_coverage_complete is True
    assert summary.bank_statement_processing_count == 1
    assert summary.bank_statement_needs_more_count == 1
    assert summary.bank_statement_failed_count == 1


@pytest.mark.asyncio
async def test_manual_statement_rows_include_unassigned_failed_and_operator_linked_files() -> None:
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=None)
    now = datetime.now(UTC)
    completed_id, failed_id, linked_id, processing_id = uuid4(), uuid4(), uuid4(), uuid4()

    def evidence_file(file_id, name, created_at):
        return SimpleNamespace(
            id=file_id,
            bucket_id=profile.primary_bucket_id,
            file_name=name,
            content_type="application/pdf",
            size_bytes=1024,
            created_at=created_at,
            statement_period=None,
        )

    completed_file = evidence_file(completed_id, "Operating Statement 2026-05.pdf", now)
    completed_file.content_hash = "completed-current"
    failed_file = evidence_file(failed_id, "Bank Statement 2026-06.pdf", now - timedelta(minutes=1))
    linked_file = evidence_file(linked_id, "miscellaneous.pdf", now - timedelta(minutes=2))
    processing_file = evidence_file(processing_id, "opaque.pdf", now - timedelta(minutes=3))
    older_completed = SimpleNamespace(
        id=uuid4(),
        status="completed",
        skip_reason=None,
        skip_detail=None,
        classification="bank_statement",
        confidence="high",
        summary="Older completed analysis",
        analysis={"key_facts": {"statement_period": "2026-05"}},
        error=None,
    )
    newest_pending = SimpleNamespace(
        id=uuid4(),
        status="pending",
        skip_reason=None,
        skip_detail=None,
        classification=None,
        confidence=None,
        summary=None,
        analysis=None,
        error=None,
        content_hash="completed-current",
    )
    stale_locked = SimpleNamespace(
        id=uuid4(),
        status="skipped",
        skip_reason="password_protected",
        skip_detail="Historical password lock",
        classification="unreadable",
        confidence=None,
        summary=None,
        analysis=None,
        error=None,
        content_hash="completed-old",
    )
    failed_analysis = SimpleNamespace(
        id=uuid4(),
        status="failed",
        skip_reason=None,
        skip_detail=None,
        classification=None,
        confidence=None,
        summary=None,
        analysis=None,
        error="PDF extraction failed",
    )
    wrong_document_analysis = SimpleNamespace(
        id=uuid4(),
        status="completed",
        skip_reason=None,
        skip_detail=None,
        classification="purchase_contract",
        confidence="high",
        summary="Purchase agreement",
        analysis={"key_facts": {}},
        error=None,
    )
    processing_analysis = SimpleNamespace(
        id=uuid4(),
        status="completed",
        skip_reason=None,
        skip_detail=None,
        classification="purchase_contract",
        confidence="high",
        summary="Completed extraction awaiting a fresh assignment decision",
        analysis={"key_facts": {}},
        error=None,
    )
    link = SimpleNamespace(
        id=uuid4(),
        file_id=linked_id,
        source="operator",
        verified_at=None,
        verified_by_user_id=None,
    )
    processing_link = SimpleNamespace(
        id=uuid4(),
        file_id=processing_id,
        source="operator",
        verified_at=None,
        verified_by_user_id=None,
    )
    linked_read = SimpleNamespace(
        file_id=linked_id,
        source="operator",
        verified=False,
        verified_at=None,
        ai_decision="rejected",
        ai_reason_code="wrong_document",
        ai_explanation="This is a purchase agreement, not a bank statement.",
        ai_confidence="high",
        decision_actor="ai",
        analysis_id=wrong_document_analysis.id,
        coverage_contribution={},
    )
    processing_read = SimpleNamespace(
        file_id=processing_id,
        source="operator",
        verified=False,
        verified_at=None,
        ai_decision="processing",
        ai_reason_code="analysis_pending",
        ai_explanation="A fresh decision is pending.",
        ai_confidence=None,
        decision_actor="system",
        analysis_id=processing_analysis.id,
        coverage_contribution={},
    )
    readiness = SimpleNamespace(
        requirements=[
            SimpleNamespace(
                requirement_key="business_bank_statements_6_months",
                evidence_files=[linked_read, processing_read],
            )
        ]
    )
    requirement_state = SimpleNamespace(id=uuid4())

    def rows(values):
        return SimpleNamespace(all=lambda: values)

    def scalar_rows(values):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                rows(
                    [
                        (completed_file, None, stale_locked),
                        (completed_file, None, newest_pending),
                        (completed_file, None, older_completed),
                        (failed_file, None, failed_analysis),
                        (linked_file, None, wrong_document_analysis),
                        (processing_file, None, processing_analysis),
                    ]
                ),
                SimpleNamespace(scalar_one_or_none=lambda: requirement_state),
                scalar_rows([link, processing_link]),
            ]
        )
    )

    with (
        patch(
            "app.services.application_programs.get_program_readiness",
            AsyncMock(return_value=readiness),
        ),
        patch(
            "app.services.application_profiles._profile_evidence_file_ids",
            AsyncMock(return_value={completed_id, failed_id, linked_id, processing_id}),
        ),
        patch(
            "app.services.application_profiles.locked_file_requests.request_states_for_files",
            AsyncMock(return_value={}),
        ),
    ):
        result = await manual_statement_evidence(db, profile)

    files = {item.file_id: item for item in result.files}
    assert set(files) == {completed_id, failed_id, linked_id, processing_id}
    assert files[completed_id].analysis_status == "pending"
    assert files[completed_id].is_password_protected is False
    assert files[completed_id].analysis_detail is None
    assert files[completed_id].ai_decision is None
    assert files[failed_id].analysis_status == "failed"
    assert files[failed_id].analysis_detail == "PDF extraction failed"
    assert files[linked_id].linked_to_requirement is True
    assert files[linked_id].source == "operator"
    assert files[linked_id].ai_decision == "rejected"
    assert files[linked_id].ai_reason_code == "wrong_document"
    assert files[processing_id].ai_decision == "processing"
    assert result.file_count == 2
    assert result.pending_analysis_count == 1
    assert result.rejected_file_count == 0
    assert result.evidence_processing_count == 1

    analysis_query = str(db.execute.await_args_list[0].args[0])
    assert "bucket_file_analyses.created_at DESC" in analysis_query


def test_manual_bank_evidence_review_roles_fail_closed() -> None:
    allowed = {
        Role.SUPER_ADMIN,
        Role.REGIONAL_MANAGER,
        Role.BROKER,
        Role.LOAN_EXEC,
        Role.DEALER_PARTNER,
        Role.PROFESSIONAL_REFERRAL_PARTNER,
        Role.FIELD_REP,
    }
    for role in Role:
        assert _can_review_manual_bank_evidence(SimpleNamespace(role=role)) is (role in allowed)


@pytest.mark.asyncio
async def test_bank_state_statement_file_details_are_opt_in() -> None:
    profile = SimpleNamespace(
        id=uuid4(),
        dealer_id=None,
        bank_verification_override_at=None,
        bank_verification_override_reason=None,
    )
    policy = SimpleNamespace(
        selected_products=["assets"],
        available_products=["assets", "statements"],
        assets_enabled=True,
        statements_enabled=False,
    )
    owner = SimpleNamespace(
        plaid_policy_updated_at=None,
        plaid_policy_updated_by_user_id=None,
    )
    detail = ApplicationBankEvidenceFileRead(
        file_id=uuid4(),
        file_name="statement.pdf",
        bucket_id=uuid4(),
        content_type="application/pdf",
        size_bytes=123,
        created_at=datetime.now(UTC),
    )
    evidence = ManualStatementEvidence(
        months=[],
        file_count=1,
        accepted_file_count=0,
        pending_analysis_count=1,
        needs_more_file_count=0,
        rejected_file_count=0,
        failed_analysis_count=0,
        evidence_processing_count=1,
        files=(detail,),
    )
    db = SimpleNamespace()

    with (
        patch(
            "app.routers.application_profiles.plaid_policy.for_profile",
            AsyncMock(return_value=(policy, owner)),
        ),
        patch(
            "app.routers.application_profiles.dealer_bank_consent.disclosure",
            return_value={"version": "v1", "text": "Disclosure"},
        ),
        patch(
            "app.routers.application_profiles._application_consent_row",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.routers.application_profiles._application_consent_granted",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.routers.application_profiles.profiles.manual_statement_evidence",
            AsyncMock(return_value=evidence),
        ),
        patch(
            "app.routers.application_profiles.profiles.bank_rows",
            AsyncMock(return_value=[]),
        ),
        patch("app.routers.application_profiles.plaid_client.enabled", return_value=True),
        patch("app.routers.application_profiles.plaid_client.environment", return_value="sandbox"),
        patch(
            "app.routers.application_profiles.plaid_lifecycle.owner_asset_reports",
            AsyncMock(return_value=[]),
        ),
    ):
        public_safe = await _application_bank_state(db, profile)
        staff = await _application_bank_state(db, profile, include_statement_files=True)

    assert public_safe.manual_statement_files == []
    assert public_safe.evidence_processing_count == 0
    assert [item.file_id for item in staff.manual_statement_files] == [detail.file_id]
    assert staff.evidence_processing_count == 1


@pytest.mark.asyncio
async def test_authenticated_bank_route_requests_details_only_for_review_roles() -> None:
    profile_id = uuid4()
    profile = SimpleNamespace(id=profile_id)
    db = SimpleNamespace()
    for role, expected in [(Role.LOAN_EXEC, True), (Role.CLIENT, False)]:
        bank_state = AsyncMock(return_value=SimpleNamespace())
        with (
            patch(
                "app.routers.application_profiles.profiles.load_profile",
                AsyncMock(return_value=profile),
            ),
            patch("app.routers.application_profiles._application_bank_state", bank_state),
        ):
            await get_application_banks(profile_id, SimpleNamespace(role=role), db)
        bank_state.assert_awaited_once_with(
            db,
            profile,
            include_statement_files=expected,
        )


def test_shared_dscr_engine_requires_deterministic_inputs() -> None:
    assert calculate_dscr(240_000, 200_000) == 1.2
    assert calculate_dscr(None, 200_000) is None
    assert calculate_dscr(240_000, 0) is None


def test_classification_snapshot_survives_json_encoding() -> None:
    """Confirming a classification writes this dict into a JSONB column.

    The taxonomy entry ids are UUID columns, so leaving them as UUID objects
    made psycopg's json.dumps raise at flush time — surfacing as a 500 from an
    unrelated line further down the request, and only for files that actually
    had a taxonomy entry selected.
    """
    import json
    from uuid import uuid4

    from app.models.application_profile import ApplicationProfile
    from app.routers.application_profiles import _classification_dict

    profile = ApplicationProfile(
        vertical="main_street",
        funding_category="working_capital",
        entity_type="llc",
        industry="restaurant_food_service",
        subindustry="full_service",
        naics_code="722511",
        naics_label="Full-Service Restaurants",
        industry_entry_id=uuid4(),
        subindustry_entry_id=uuid4(),
        activity_entry_id=uuid4(),
    )

    snapshot = _classification_dict(profile)
    json.dumps({"analysis_status": "stale", "previous": snapshot, "current": snapshot})

    assert snapshot["industry_entry_id"] == str(profile.industry_entry_id)
    assert snapshot["naics_code"] == "722511"


def test_classification_snapshot_keeps_unset_entry_ids_null() -> None:
    from app.models.application_profile import ApplicationProfile
    from app.routers.application_profiles import _classification_dict

    snapshot = _classification_dict(ApplicationProfile(vertical="main_street"))

    assert snapshot["industry_entry_id"] is None
    assert snapshot["subindustry_entry_id"] is None
    assert snapshot["activity_entry_id"] is None


def test_evidence_preview_endpoint_is_profile_scoped_and_not_a_storage_url() -> None:
    profile_id = uuid4()
    file_id = uuid4()

    endpoint = evidence_preview_endpoint(profile_id, file_id)

    assert endpoint == (f"/api/v1/application-profiles/{profile_id}/evidence/files/{file_id}/url")
    assert "s3" not in endpoint.casefold()


@pytest.mark.asyncio
async def test_evidence_file_url_rechecks_inventory_and_signs_inline() -> None:
    profile_id = uuid4()
    file_id = uuid4()
    profile = SimpleNamespace(id=profile_id)
    file = SimpleNamespace(
        id=file_id,
        file_name="statement.pdf",
        s3_key="private/evidence/statement.pdf",
        content_type="application/pdf",
        status="uploaded",
        deleted_at=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=file), commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)])),
        ),
        patch(
            "app.routers.application_profiles.profiles.log_profile_action",
            AsyncMock(),
        ) as log_action,
        patch(
            "app.routers.buckets._download_url",
            return_value="https://storage.example/signed",
        ) as sign,
    ):
        result = await get_application_evidence_file_url(profile_id, file_id, user, db)

    assert result == {"url": "https://storage.example/signed", "expires_in": 900}
    sign.assert_called_once_with(
        file.s3_key,
        disposition="inline",
        content_type="application/pdf",
    )
    log_action.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_evidence_file_url_hides_files_outside_current_inventory() -> None:
    profile_id = uuid4()
    file_id = uuid4()
    db = SimpleNamespace(get=AsyncMock(), commit=AsyncMock())

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=SimpleNamespace(id=profile_id)),
        ),
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(return_value=SimpleNamespace(files=[])),
        ),
        pytest.raises(HTTPException) as error,
    ):
        await get_application_evidence_file_url(
            profile_id,
            file_id,
            SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC),
            db,
        )

    assert error.value.status_code == 404
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_evidence_file_url_denies_non_staff_before_inventory_lookup() -> None:
    profile_id = uuid4()
    file_id = uuid4()
    db = SimpleNamespace(get=AsyncMock(), commit=AsyncMock())

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(),
        ) as load_profile,
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(),
        ) as evidence_state,
        pytest.raises(HTTPException) as error,
    ):
        await get_application_evidence_file_url(
            profile_id,
            file_id,
            SimpleNamespace(id=uuid4(), role=Role.CLIENT),
            db,
        )

    assert error.value.status_code == 403
    load_profile.assert_not_awaited()
    evidence_state.assert_not_awaited()
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_evidence_inventory_omits_preview_resolvers_for_non_staff() -> None:
    profile_id = uuid4()
    file_id = uuid4()
    file = SimpleNamespace(id=file_id, preview_url="/should/not/leak")
    state = SimpleNamespace(files=[file])
    user = SimpleNamespace(id=uuid4(), role=Role.CLIENT)

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=SimpleNamespace(id=profile_id)),
        ),
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(return_value=state),
        ),
    ):
        result = await get_application_evidence(profile_id, user, SimpleNamespace())

    assert result.files[0].preview_url is None
