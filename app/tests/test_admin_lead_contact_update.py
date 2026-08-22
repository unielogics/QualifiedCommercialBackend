from pydantic import ValidationError

from app.routers.dealer_ai_intake import AdminLeadContactUpdate, admin_router


def test_admin_lead_contact_update_accepts_partial_entity_details() -> None:
    payload = AdminLeadContactUpdate(
        business_name="Northstar Holdings LLC",
        phone="",
        requested_loan_amount=750_000,
        estimated_credit_score=735,
    )

    assert payload.business_name == "Northstar Holdings LLC"
    assert payload.phone is None
    assert payload.requested_loan_amount == 750_000
    assert payload.model_dump(exclude_unset=True) == {
        "business_name": "Northstar Holdings LLC",
        "phone": None,
        "requested_loan_amount": 750_000,
        "estimated_credit_score": 735,
    }


def test_admin_lead_contact_update_rejects_invalid_email() -> None:
    try:
        AdminLeadContactUpdate(email="not-an-email")
    except ValidationError:
        return
    raise AssertionError("invalid email should fail validation")


def test_admin_lead_contact_update_route_is_registered() -> None:
    patch_routes = {
        route.path
        for route in admin_router.routes
        if "PATCH" in getattr(route, "methods", set())
    }
    assert "/admin/ai-underwriter-leads/{intake_id}/contact" in patch_routes
