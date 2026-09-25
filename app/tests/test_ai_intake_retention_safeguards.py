from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.routers import dealer_ai_intake as intake


def test_admin_ai_intake_list_supports_explicit_archived_discovery() -> None:
    signature = inspect.signature(intake.list_dealer_ai_leads)
    assert signature.parameters["archived_filter"].default == "active"
    assert "archived" in str(signature.parameters["archived_filter"].annotation)
    assert "archived_at" in intake.DealerAILeadRow.model_fields


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


def test_hard_delete_confirmation_name_is_required_and_normalized() -> None:
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
    assert "_delete_s3_object" not in source
    assert "db.delete" not in source
