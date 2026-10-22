from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as intake
from app.services import foreclosure_rescue, intake_lifecycle


def test_admin_ai_intake_list_supports_explicit_archived_discovery() -> None:
    signature = inspect.signature(intake.list_dealer_ai_leads)
    assert signature.parameters["archived_filter"].default == "active"
    assert "archived" in str(signature.parameters["archived_filter"].annotation)
    assert "archived_at" in intake.DealerAILeadRow.model_fields
    assert "workspace_state" in intake.DealerAILeadRow.model_fields
    assert "underwriting_status" in intake.DealerAILeadRow.model_fields


def test_partner_ai_intake_list_defaults_to_active_and_supports_history() -> None:
    signature = inspect.signature(intake.list_broker_dealer_leads)
    assert signature.parameters["archived_filter"].default == "active"
    source = inspect.getsource(intake.list_broker_dealer_leads)
    assert "_closed_intake_lifecycle_clause" in source
    assert "_intake_status_filter_clause" in source


def test_closed_lifecycle_is_distinct_from_manual_archive() -> None:
    common = {
        "bucket_archived_at": None,
        "intake_status": "reviewed",
        "outcome_status": "submitted",
    }
    assert intake_lifecycle.intake_workspace_state(**common) == ("active", None)
    assert intake_lifecycle.intake_workspace_state(
        **common, underwriting_status="closed_won"
    ) == ("active", None)
    assert intake_lifecycle.intake_workspace_state(
        **common, underwriting_status="denied"
    ) == ("closed", "denied")
    assert intake_lifecycle.intake_workspace_state(
        **common, foreclosure_status="withdrawn_expired"
    ) == ("closed", "withdrawn_expired")
    archived_at = datetime.now(UTC)
    assert intake_lifecycle.intake_workspace_state(
        **{
            **common,
            "bucket_archived_at": archived_at,
            "underwriting_status": "denied",
        }
    ) == ("archived", "manual_archive")


def test_review_completed_does_not_claim_funding() -> None:
    assert intake_lifecycle.intake_workspace_state(
        bucket_archived_at=None,
        intake_status="completed",
        outcome_status="closed",
    ) == ("active", None)
    assert intake_lifecycle.intake_workspace_state(
        bucket_archived_at=None,
        intake_status="completed",
        outcome_status="closed",
        underwriting_status="in_underwriting",
    ) == ("active", None)


def test_legacy_terminal_status_still_closes_unless_funding_is_authoritative() -> None:
    assert intake_lifecycle.intake_workspace_state(
        bucket_archived_at=None,
        intake_status="denied",
        outcome_status="denied",
        underwriting_status="in_underwriting",
    ) == ("closed", "denied")


def test_foreclosure_completed_is_reserved_for_funding() -> None:
    assert foreclosure_rescue.INTAKE_STATUS_MAP["funded"] == "completed"
    assert foreclosure_rescue.INTAKE_STATUS_MAP["closing_docs_pending"] == "approved"
    assert foreclosure_rescue.INTAKE_STATUS_MAP["clear_to_close"] == "approved"
    assert foreclosure_rescue.INTAKE_STATUS_MAP["declined"] == "denied"
    assert foreclosure_rescue.INTAKE_STATUS_MAP["withdrawn_expired"] == "closed_lost"


def test_canonical_funding_wins_over_stale_legacy_denial() -> None:
    assert intake_lifecycle.intake_workspace_state(
        bucket_archived_at=None,
        intake_status="denied",
        outcome_status="denied",
        underwriting_status="closed_won",
    ) == ("active", None)
    assert intake_lifecycle.intake_workspace_state(
        bucket_archived_at=None,
        intake_status="denied",
        outcome_status="denied",
        foreclosure_status="funded",
    ) == ("active", None)


def test_terminal_underwriting_sync_preserves_rows_and_evidence() -> None:
    row = SimpleNamespace(status="reviewed", outcome_status="submitted")

    intake_lifecycle.sync_intake_from_underwriting(row, "denied")

    assert row.status == "denied"
    assert row.outcome_status == "denied"
    # The lifecycle helper deliberately has no bucket/file deletion surface.
    source = inspect.getsource(intake_lifecycle.sync_intake_from_underwriting)
    assert "archived_at" not in source
    assert "db.delete" not in source
    assert "_delete_s3_object" not in source


def test_archive_filter_clause_includes_legacy_profile_statuses() -> None:
    query = str(
        select(PublicUnderwritingIntake.id).where(
            intake._closed_intake_lifecycle_clause()
        )
    )

    assert "application_profiles" in query
    assert "underwriting_status" in query
    assert "foreclosure_rescue_status" in query


@pytest.mark.parametrize(
    "status_filter, expected",
    [
        ("disqualified", "denied"),
        ("lost", "closed_lost"),
        ("withdrawn", "withdrawn_expired"),
        ("funded", "closed_won"),
        ("completed", "closed_won"),
    ],
)
def test_status_filter_aliases_use_canonical_lifecycle(
    status_filter: str, expected: str
) -> None:
    compiled = select(PublicUnderwritingIntake.id).where(
        intake._intake_status_filter_clause(status_filter)
    ).compile()
    assert expected in repr(compiled.params)


def test_admin_ai_intake_restore_route_is_registered() -> None:
    contract = {
        (route.path, method)
        for route in intake.admin_router.routes
        for method in getattr(route, "methods", set())
    }
    assert any(path.endswith("/{intake_id}/restore") and method == "POST" for path, method in contract)


def test_ai_intake_search_includes_visible_bucket_identity() -> None:
    query = str(intake._intake_search_clause("101 Motors"))
    assert "buckets.name" in query
    assert "buckets.client_name" in query


def test_archive_confirmation_name_is_required_and_normalized() -> None:
    with pytest.raises(ValidationError):
        intake.ConfirmLeadDeletionRequest.model_validate({})
    with pytest.raises(ValidationError):
        intake.ConfirmLeadDeletionRequest.model_validate({"confirm_name": "   "})

    payload = intake.ConfirmLeadDeletionRequest.model_validate(
        {"confirm_name": "  101   Motors  "}
    )
    assert payload.confirm_name == "101 Motors"


def test_legacy_delete_route_archives_without_destroying_storage() -> None:
    source = inspect.getsource(intake.admin_confirm_lead_deletion)
    assert "Confirmation name does not match" in source
    assert 'event_type="intake_archived"' in source
    assert 'intake.bucket.status = "archived"' in source
    assert "delete_guard" not in source
    assert "_delete_s3_object" not in source
    assert "db.delete" not in source
