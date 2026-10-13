from __future__ import annotations

from types import SimpleNamespace

from app.routers.auth import SignupAttribution, _persist_signup_attribution


def test_signup_attribution_accepts_canonical_equipment_intent_and_optional_purpose():
    payload = SignupAttribution(
        source="Equipment Page",
        page="/equipment-financing",
        funding_intent="equipment",
        purpose="  CNC mill purchase  ",
    )
    assert payload.funding_intent == "equipment"
    assert payload.purpose == "CNC mill purchase"


def test_signup_attribution_persists_equipment_intent_without_breaking_existing_data():
    client = SimpleNamespace(
        lead_intake={"existing": "preserved"},
        source_channel=None,
        lead_source=None,
    )
    request = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"},
        client=None,
    )

    _persist_signup_attribution(
        client,
        SignupAttribution(
            source="equipment_page",
            page="/equipment-financing",
            funding_intent="equipment",
        ),
        request,
    )

    assert client.lead_intake["existing"] == "preserved"
    assert client.lead_intake["signup_attribution"]["funding_intent"] == "equipment"
    assert client.lead_intake["signup_attribution"]["page"] == "/equipment-financing"
    assert client.lead_source == "equipment_page"

