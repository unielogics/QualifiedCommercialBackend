"""The factual file shell must not await unrelated analysis and package work."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.routers import application_profiles as profile_routes
from app.routers import application_terms as terms_routes
from app.routers import dealer_ai_intake as core
from app.routers.dealer_ai_intake_routes import admin, broker
from app.services import (
    application_terms,
    financial_cockpit,
    financial_publication,
    financing_projection,
    intake_source,
)
from app.tests.test_atomic_client_terms_plan import (
    env as terms_fixture,  # noqa: F401 - shared transactional fixture
)
from app.tests.test_atomic_client_terms_plan import payload


@pytest.fixture
def response_context(monkeypatch):
    profile = SimpleNamespace(id=uuid4(), communication_locale="en")
    bucket = SimpleNamespace(name="Synthetic file", notes=[], requested_documents=[])
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), bucket=bucket, latest_review=SimpleNamespace(),
        bucket_upload_link=None, variant="business_v1", preferred_language="en", intake_state={})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: profile)))
    result = {"key_metrics": {"dscr": 9, "estimated_dscr": 9, "annual_ebitda": 999999},
        "summary": "Historical AI narrative", "booking_recommended": True}
    for name, value in {
        "_is_main_street_equipment_intake": False, "_is_main_street_mca_intake": False,
        "_call_booked": False, "_active_files": [], "_lead_result": result,
        "_latest_result_for_intake": result, "_years_in_business": 4,
        "_client_safe_intake_state": {}, "_client_credit_check": {},
        "_message_for_widget": "Ready", "_format_review_update": "Current result",
        "_widget_for_type": {},
        "upload_link_visible_summary": {"summary": "Reviewed summary"},
        "_artifact_pdf_metadata": {}, "_artifact_read": {"id": "artifact"},
    }.items():
        monkeypatch.setattr(core, name, Mock(return_value=value))
    for name, value in {
        "_management_artifacts": [SimpleNamespace(artifact_type="executive_summary")],
        "_management_email_sends": [], "_client_message_reads": [], "actions_for_messages": [],
        "_request_uploaded_file_reads": [],
    }.items():
        monkeypatch.setattr(core, name, AsyncMock(return_value=value))
    monkeypatch.setattr(core.locked_file_requests, "current_public_request_documents", AsyncMock(return_value=[]))
    monkeypatch.setattr(core.inline_images, "hydrate", AsyncMock(return_value={}))
    monkeypatch.setattr(core.business_profile, "apply_read", Mock())
    monkeypatch.setattr(core.mca_refinance_policy, "is_mca_refinance", Mock(return_value=False))
    monkeypatch.setattr(financial_publication, "review_is_financially_stale", Mock(return_value=False))
    monkeypatch.setattr(intake_source, "source_for_intake", Mock(return_value={}))
    monkeypatch.setattr(core.DealerIntakeRead, "model_validate", Mock(side_effect=lambda _: SimpleNamespace()))
    monkeypatch.setattr(core.BucketAIReviewRead, "model_validate", Mock(side_effect=lambda _: SimpleNamespace()))
    monkeypatch.setattr(core, "DealerIntakeResponse", Mock(side_effect=lambda **values: values))
    readiness = AsyncMock(return_value=SimpleNamespace())
    cockpit = AsyncMock(return_value={})
    fingerprint = AsyncMock(return_value="current-fingerprint")
    monkeypatch.setattr(core.capital_readiness_service, "advisory_current_snapshot", readiness)
    monkeypatch.setattr(core.capital_readiness_service, "read_snapshot", Mock(return_value={}))
    monkeypatch.setattr(financial_cockpit, "load_cockpit", cockpit)
    monkeypatch.setattr(core, "_current_package_input_fingerprint", fingerprint)
    monkeypatch.setattr(core, "_decorate_widget", AsyncMock(return_value={}))
    return db, intake, profile, result, readiness, cockpit, fingerprint


@pytest.mark.asyncio
async def test_lightweight_operator_open_skips_all_three_derived_workloads(response_context):
    db, intake, profile, historical, readiness, cockpit, fingerprint = response_context
    before = deepcopy(historical)
    response = await core._response(db, intake, token=None, messages=[], include_management=True,
        admin_thread=True, include_derived=False)
    readiness.assert_not_awaited()
    cockpit.assert_not_awaited()
    fingerprint.assert_not_awaited()
    core._decorate_widget.assert_not_awaited()
    assert response["application_profile_id"] == profile.id
    assert response["derived_pending"] is True
    assert response["capital_readiness"] is None
    assert response["latest_review"] is None
    assert response["ai_summary"] is None
    assert response["intake"].result_snapshot["key_metrics"]["dscr"] is None
    assert response["intake"].result_snapshot["key_metrics"]["annual_ebitda"] is None
    assert "load separately" in response["assistant_message"]
    assert core._artifact_read.call_args.kwargs["current_input_fingerprint"] is None
    assert historical == before, "The stored historical review must not be rewritten"


@pytest.mark.asyncio
async def test_default_full_operator_response_still_validates_current_financials_and_packages(response_context):
    db, intake, profile, _, readiness, cockpit, fingerprint = response_context
    response = await core._response(db, intake, token=None, messages=[], include_management=True, admin_thread=True)
    readiness.assert_awaited_once_with(db, profile)
    cockpit.assert_awaited_once_with(db, profile)
    fingerprint.assert_awaited_once()
    assert response["derived_pending"] is False
    assert response["latest_review"] is not None
    assert core._artifact_read.call_args.kwargs["current_input_fingerprint"] == "current-fingerprint"


@pytest.mark.asyncio
async def test_public_room_cannot_switch_to_operator_shell_or_receive_profile_id(response_context):
    db, intake, profile, _, readiness, cockpit, fingerprint = response_context
    intake.latest_review = None
    response = await core._response(db, intake, token=None, messages=[], include_derived=False)
    readiness.assert_awaited_once_with(db, profile)
    cockpit.assert_not_awaited()
    fingerprint.assert_not_awaited()
    assert response["derived_pending"] is False
    assert response["application_profile_id"] is None


@pytest.mark.asyncio
async def test_admin_lightweight_route_preserves_access_and_seen_write(monkeypatch):
    intake = SimpleNamespace(id=uuid4())
    user, db = SimpleNamespace(), SimpleNamespace()
    require = Mock()
    load, seen, respond = AsyncMock(return_value=intake), AsyncMock(), AsyncMock(return_value="shell")
    monkeypatch.setattr(core, "_require_super_admin", require)
    monkeypatch.setattr(core, "_load_admin_dealer_lead", load)
    monkeypatch.setattr(core, "_mark_lead_seen", seen)
    monkeypatch.setattr(core, "_response", respond)
    assert await admin.get_dealer_ai_lead(intake.id, user, db, include_derived=False) == "shell"
    require.assert_called_once_with(user)
    load.assert_awaited_once_with(db, intake.id)
    seen.assert_awaited_once_with(db, user, intake.id)
    assert respond.call_args.kwargs["include_derived"] is False


@pytest.mark.asyncio
async def test_broker_lightweight_route_retains_ownership_and_partner_gate(monkeypatch):
    intake = SimpleNamespace(id=uuid4())
    user, db = SimpleNamespace(), SimpleNamespace()
    require, load, respond = AsyncMock(), AsyncMock(return_value=intake), AsyncMock(return_value="shell")
    monkeypatch.setattr(core, "_require_dealer_partner", require)
    monkeypatch.setattr(core, "_load_broker_dealer_lead", load)
    monkeypatch.setattr(core, "_response", respond)
    assert await broker.get_broker_dealer_lead(intake.id, user, db, include_derived=False) == "shell"
    require.assert_awaited_once_with(user, db)
    load.assert_awaited_once_with(db, user, intake.id)
    assert respond.call_args.kwargs["include_derived"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("include_current_plan", [True, False])
async def test_saved_terms_read_uses_immutable_calculation_and_only_refreshes_current_plan(request, monkeypatch, include_current_plan):
    env = request.getfixturevalue("terms_fixture")
    row = await application_terms.save_terms(env.db, env.profile, env.actor, payload())
    env.session.commit()
    forbidden = AsyncMock(side_effect=AssertionError("Saved calculations must not be recomputed during a read"))
    live_plan = AsyncMock(return_value={"active": True, "modifications": ["Current reviewed plan"]})
    monkeypatch.setattr(application_terms, "_dscr_context", forbidden)
    monkeypatch.setattr(application_terms, "_plan_block", live_plan)
    response = await application_terms.read_terms(env.db, env.profile, row, include_current_plan=include_current_plan)
    forbidden.assert_not_awaited()
    if include_current_plan:
        live_plan.assert_awaited_once_with(env.db, env.profile)
    else:
        live_plan.assert_not_awaited()
    assert response.calculation.dscr_after == float(row.dscr_after)
    assert response.calculation.cash_flow_value == float(row.cash_flow_value)
    expected_plan = {"active": True, "modifications": ["Current reviewed plan"]} if include_current_plan else None
    assert response.plan == expected_plan


@pytest.mark.asyncio
@pytest.mark.parametrize("include_financing", [True, False])
async def test_lifecycle_header_can_skip_duplicate_financial_projection(monkeypatch, include_financing):
    profile = SimpleNamespace(id=uuid4())
    user, db = SimpleNamespace(), SimpleNamespace()
    access, loaded = Mock(), AsyncMock(return_value=profile)
    fee_review = AsyncMock(return_value={"review_required": False})
    financial = AsyncMock(return_value={"with_plan_dscr": 1.2})
    render = Mock(side_effect=lambda _profile, **values: values)
    monkeypatch.setattr(profile_routes, "_require_underwriting_actor", access)
    monkeypatch.setattr(profile_routes.profiles, "load_profile", loaded)
    monkeypatch.setattr(profile_routes.funding_programs, "qc_fee_cap_review_for_profile", fee_review)
    monkeypatch.setattr(financing_projection, "read", financial)
    monkeypatch.setattr(profile_routes, "_underwriting_read", render)
    response = await profile_routes.get_application_underwriting(profile.id, user, db, include_financing=include_financing)
    access.assert_called_once_with(user)
    loaded.assert_awaited_once_with(db, profile.id, user)
    fee_review.assert_awaited_once_with(db, profile)
    if include_financing:
        financial.assert_awaited_once_with(db, profile)
        assert response["financing_projection"] == {"with_plan_dscr": 1.2}
    else:
        financial.assert_not_awaited()
        assert response["financing_projection"] is None


@pytest.mark.asyncio
async def test_terms_route_forwards_current_plan_opt_out_after_access_checks(monkeypatch):
    profile = SimpleNamespace(id=uuid4())
    user, db = SimpleNamespace(), SimpleNamespace()
    access, loaded = Mock(), AsyncMock(return_value=profile)
    read = AsyncMock(return_value="saved terms")
    monkeypatch.setattr(terms_routes, "_require_terms_actor", access)
    monkeypatch.setattr(terms_routes.profiles, "load_profile", loaded)
    monkeypatch.setattr(application_terms, "read_terms", read)
    assert await terms_routes.get_client_terms(profile.id, user, db, include_current_plan=False) == "saved terms"
    access.assert_called_once_with(user)
    loaded.assert_awaited_once_with(db, profile.id, user)
    read.assert_awaited_once_with(db, profile, include_current_plan=False)


def test_artifact_history_builds_one_storage_client_per_response_without_caching_urls(monkeypatch):
    now = datetime.now(UTC)
    artifacts = [SimpleNamespace(id=uuid4(), intake_id=uuid4(), artifact_type="executive_summary",
        title=f"Synthetic version {index}", body_text=None, body_json={}, s3_key=f"synthetic/{index}.pdf",
        created_by_user_id=None, created_at=now, updated_at=now) for index in range(10)]
    storage = SimpleNamespace(generate_presigned_url=Mock(return_value="https://example.test/fresh-presigned"))
    create = Mock(return_value=storage)
    monkeypatch.setattr(core, "_s3_client", create)
    monkeypatch.setattr(core, "_bucket_storage_config", Mock(return_value=("synthetic-bucket", "", None)))
    response = core._artifact_reads(artifacts)
    assert len(response) == 10
    create.assert_called_once_with()
    assert storage.generate_presigned_url.call_count == 20
    # Another request must create its own client and fresh 15-minute URLs.
    core._artifact_reads(artifacts)
    assert create.call_count == 2
    assert storage.generate_presigned_url.call_count == 40


def test_artifact_history_without_stored_documents_never_initializes_storage(monkeypatch):
    now = datetime.now(UTC)
    artifact = SimpleNamespace(id=uuid4(), intake_id=uuid4(), artifact_type="executive_summary",
        title="Unstored draft", body_text=None, body_json={}, s3_key=None,
        created_by_user_id=None, created_at=now, updated_at=now)
    create = Mock(side_effect=AssertionError("No storage object to sign"))
    monkeypatch.setattr(core, "_s3_client", create)
    result = core._artifact_reads([artifact])
    assert result[0].download_url is None and result[0].preview_url is None
    create.assert_not_called()


def test_storage_initialization_failure_is_not_retried_for_every_artifact_link(monkeypatch):
    now = datetime.now(UTC)
    artifact = SimpleNamespace(id=uuid4(), intake_id=uuid4(), artifact_type="executive_summary",
        title="Stored version", body_text=None, body_json={}, s3_key="synthetic/report.pdf",
        created_by_user_id=None, created_at=now, updated_at=now)
    create = Mock(side_effect=RuntimeError("Synthetic storage unavailable"))
    monkeypatch.setattr(core, "_s3_client", create)
    monkeypatch.setattr(core, "_bucket_storage_config", Mock(return_value=("synthetic-bucket", "", None)))
    result = core._artifact_reads([artifact, artifact])
    assert all(row.download_url is None and row.preview_url is None for row in result)
    create.assert_called_once_with()
