from uuid import uuid4

from app.services.google.calendar_sync import (
    _google_pull_external_ref,
    client_rsvp_status,
)


def test_google_pull_external_ref_is_fixed_length_and_namespaced() -> None:
    user_id = uuid4()
    first = _google_pull_external_ref(user_id, "recurring-event-with-a-long-shared-prefix-0001")
    second = _google_pull_external_ref(user_id, "recurring-event-with-a-long-shared-prefix-0002")

    assert len(first) == 64
    assert first.startswith(f"{user_id.hex}:")
    assert first != second


def test_google_pull_external_ref_changes_by_user() -> None:
    event_id = "shared-google-event"

    assert _google_pull_external_ref(uuid4(), event_id) != _google_pull_external_ref(
        uuid4(), event_id
    )


def test_client_rsvp_requires_exact_client_attendee() -> None:
    event = {
        "attendees": [
            {"email": "rep@qualifiedcommercial.com", "responseStatus": "accepted"},
            {"email": "client@example.com", "responseStatus": "needsAction"},
        ]
    }

    assert client_rsvp_status(event, "CLIENT@example.com") == "needs_action"
    assert client_rsvp_status(event, "missing@example.com") == "unknown"


def test_client_rsvp_maps_google_response_states() -> None:
    for google_status, expected in (
        ("accepted", "accepted"),
        ("tentative", "tentative"),
        ("declined", "declined"),
        ("needsAction", "needs_action"),
        ("unexpected", "unknown"),
    ):
        event = {
            "attendees": [
                {"email": "client@example.com", "responseStatus": google_status}
            ]
        }

        assert client_rsvp_status(event, "client@example.com") == expected


def test_client_rsvp_without_client_email_is_unknown() -> None:
    event = {
        "attendees": [
            {"email": "client@example.com", "responseStatus": "accepted"}
        ]
    }

    assert client_rsvp_status(event, None) == "unknown"
