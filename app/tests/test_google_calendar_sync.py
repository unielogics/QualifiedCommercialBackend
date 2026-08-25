from uuid import uuid4

from app.services.google.calendar_sync import _google_pull_external_ref


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
