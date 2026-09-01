from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os.router import (
    _appointment_google_color,
    _appointment_workflow_google_color,
    _can_manage_appointment_crm,
    _rep_calendar_capabilities,
    _require_appointment_crm,
    router,
)
from app.dealer_os.schemas import (
    RepAppointmentCancel,
    RepAppointmentCrmPatch,
    RepAppointmentDeliveryRetry,
    RepAppointmentOutcomePatch,
    RepAppointmentPatch,
    RepAppointmentStartApplication,
)
from app.models.user import Role
from app.routers.dealer_ai_intake import _require_governance_admin, _require_intake_operator


def test_converted_outcome_requires_exactly_one_destination() -> None:
    with pytest.raises(ValidationError, match="Choose a conversion destination"):
        RepAppointmentOutcomePatch(outcome="converted")

    with pytest.raises(ValidationError, match="Choose an AI intake type"):
        RepAppointmentOutcomePatch(outcome="converted", conversion_target="ai_intake")

    with pytest.raises(ValidationError, match="six-digit secure room PIN"):
        RepAppointmentOutcomePatch(
            outcome="converted",
            conversion_target="ai_intake",
            ai_variant="real_estate",
        )

    payload = RepAppointmentOutcomePatch(
        outcome="converted",
        conversion_target="ai_intake",
        ai_variant="real_estate",
        secure_room_pin="104293",
    )
    assert payload.ai_variant == "real_estate"


@pytest.mark.parametrize("variant", ["dealer", "real_estate", "main_street", "mca_refinance"])
def test_new_application_supports_every_intake_variant(variant: str) -> None:
    payload = RepAppointmentStartApplication(
        variant=variant,
        secure_room_pin="104293",
    )
    assert payload.variant == variant


def test_new_application_requires_pin_but_explicit_reuse_does_not() -> None:
    with pytest.raises(ValidationError, match="six-digit secure room PIN"):
        RepAppointmentStartApplication(variant="dealer")

    existing_id = uuid4()
    payload = RepAppointmentStartApplication(
        variant="dealer",
        existing_intake_id=existing_id,
    )
    assert payload.existing_intake_id == existing_id
    assert payload.secure_room_pin is None


def test_crm_follow_up_and_terminal_changes_are_reviewed() -> None:
    with pytest.raises(ValidationError, match="follow-up date"):
        RepAppointmentCrmPatch(status="follow_up")
    with pytest.raises(ValidationError, match="confirmed reason"):
        RepAppointmentCrmPatch(status="not_qualified", reason="No fit")
    with pytest.raises(ValidationError, match="confirmed reason"):
        RepAppointmentCrmPatch(status="cancelled", confirm_terminal=True)

    terminal = RepAppointmentCrmPatch(
        status="not_qualified",
        reason="Outside current credit policy",
        confirm_terminal=True,
    )
    assert terminal.status == "not_qualified"


def test_cancellation_requires_an_audit_reason() -> None:
    with pytest.raises(ValidationError):
        RepAppointmentCancel()
    with pytest.raises(ValidationError):
        RepAppointmentCancel(reason="")
    assert RepAppointmentCancel(reason="Client asked to reschedule next quarter").reason


def test_crm_management_excludes_field_representatives() -> None:
    assert _can_manage_appointment_crm(SimpleNamespace(role=Role.SUPER_ADMIN))
    assert _can_manage_appointment_crm(SimpleNamespace(role=Role.LOAN_EXEC))
    assert not _can_manage_appointment_crm(SimpleNamespace(role=Role.FIELD_REP))
    assert not _can_manage_appointment_crm(SimpleNamespace(role=Role.CLIENT))


def test_field_desk_calendar_capabilities_come_from_backend_permissions() -> None:
    underwriter = _rep_calendar_capabilities(SimpleNamespace(role=Role.LOAN_EXEC))
    assert underwriter.can_manage_all
    assert underwriter.can_manage_appointment_crm
    assert underwriter.can_apply_outcomes
    assert not underwriter.can_manage_outcome_catalog

    field_rep = _rep_calendar_capabilities(SimpleNamespace(role=Role.FIELD_REP))
    assert not field_rep.can_manage_all
    assert not field_rep.can_manage_appointment_crm
    assert not field_rep.can_apply_outcomes
    assert not field_rep.can_manage_outcome_catalog


def test_field_representative_cannot_apply_shared_outcome_by_direct_api_call() -> None:
    with pytest.raises(HTTPException) as denied:
        _require_appointment_crm(SimpleNamespace(role=Role.FIELD_REP))
    assert denied.value.status_code == 403


def test_delivery_retry_accepts_quiet_google_color_sync() -> None:
    assert RepAppointmentDeliveryRetry(action="google_sync").action == "google_sync"


def test_ai_intake_operator_and_governance_boundaries_are_distinct() -> None:
    _require_intake_operator(SimpleNamespace(role=Role.SUPER_ADMIN))
    _require_intake_operator(SimpleNamespace(role=Role.LOAN_EXEC))
    with pytest.raises(HTTPException) as field_rep_denied:
        _require_intake_operator(SimpleNamespace(role=Role.FIELD_REP))
    assert field_rep_denied.value.status_code == 403

    _require_governance_admin(SimpleNamespace(role=Role.SUPER_ADMIN))
    with pytest.raises(HTTPException) as underwriter_denied:
        _require_governance_admin(SimpleNamespace(role=Role.LOAN_EXEC))
    assert underwriter_denied.value.status_code == 403


def test_calendar_crm_workspace_routes_are_registered() -> None:
    routes = {(route.path, method) for route in router.routes for method in (route.methods or set())}
    assert ("/dealer-os/appointments/{appointment_id}/workspace", "GET") in routes
    assert ("/dealer-os/appointments/{appointment_id}/crm", "PATCH") in routes
    assert ("/dealer-os/appointments/{appointment_id}/notes", "POST") in routes
    assert ("/dealer-os/appointments/{appointment_id}/start-application", "POST") in routes
    assert ("/dealer-os/appointments/{appointment_id}/delivery/retry", "POST") in routes


def test_reschedule_outcome_reopen_is_explicit() -> None:
    assert RepAppointmentPatch().reopen_outcome is False
    assert RepAppointmentPatch(reopen_outcome=True).reopen_outcome is True


def test_google_outcome_colors_are_stable() -> None:
    assert _appointment_google_color("not_converted") == "11"
    assert _appointment_google_color("did_not_show") == "5"
    assert _appointment_google_color("converted") == "10"
    assert _appointment_google_color(None) is None
    assert _appointment_workflow_google_color("blue") == "9"
    assert _appointment_workflow_google_color("green") == "10"
    assert _appointment_workflow_google_color("amber") == "5"
    assert _appointment_workflow_google_color("red") == "11"
    assert _appointment_workflow_google_color("violet") == "3"
    assert _appointment_workflow_google_color("gray") == "8"
