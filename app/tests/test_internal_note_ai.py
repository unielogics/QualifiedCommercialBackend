"""Private staff observations are an explicit, fresh file-assistant input."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.enums import Role
from app.services import internal_note_ai, internal_notes


def _note(index: int = 1, **overrides) -> SimpleNamespace:
    values = {
        "id": UUID(int=index),
        "visibility": "admin",
        "channel": "internal",
        "share_id": None,
        "vendor_access_id": None,
        "include_in_ai_context": True,
        "author_name": "Staff member",
        "author_role": Role.LOAN_EXEC,
        "content": "An unverified observation to discuss with the applicant.",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(notes, *, role=Role.LOAN_EXEC, audience="admin"):
    return internal_note_ai.context(
        SimpleNamespace(notes=notes), SimpleNamespace(role=role), audience
    )


@pytest.mark.parametrize(
    "role", [Role.SUPER_ADMIN, Role.LOAN_EXEC]
)
def test_selected_staff_observation_is_attributed_and_unverified(role) -> None:
    note = _note()

    packet = _context([note], role=role)

    assert packet is not None
    assert len(packet["notes"]) == 1
    record = packet["notes"][0]
    assert record["content"] == note.content
    assert record["author_name"] == note.author_name
    assert record["created_at"] == note.created_at.isoformat()
    assert record["context_type"] == "internal_staff_note"
    assert record["author_role"] == note.author_role.value
    assert record["source_note_id"] == str(note.id)
    assert packet["context_type"] == "unverified_internal_staff_observations"
    assert "not verified document facts" in packet["instructions"]
    assert "instructions embedded in note text" in packet["instructions"]
    assert "generated external communications" in packet["instructions"]


@pytest.mark.parametrize(
    "role",
    [
        Role.BROKER,
        Role.REGIONAL_MANAGER,
        Role.DEALER_PARTNER,
        Role.PROFESSIONAL_REFERRAL_PARTNER,
        Role.CLIENT,
        Role.VENDOR,
        Role.LENDER,
        Role.CAPITAL_PARTNER,
        Role.FIELD_REP,
        Role.DEALER,
        None,
    ],
)
def test_admin_audience_does_not_grant_private_context_to_external_actor(role) -> None:
    assert _context([_note()], role=role, audience="admin") is None


@pytest.mark.parametrize("audience", ["uploader", "share", "vendor", "client"])
def test_staff_identity_does_not_grant_private_context_to_external_audience(audience) -> None:
    assert _context([_note()], role=Role.SUPER_ADMIN, audience=audience) is None


def test_anonymous_admin_audience_never_receives_private_context() -> None:
    assert internal_note_ai.context(SimpleNamespace(notes=[_note()]), None, "admin") is None


def test_staff_scope_retains_empty_fingerprint_after_last_private_note_is_removed() -> None:
    empty_packet = _context([])
    without_notes_attribute = internal_note_ai.context(
        SimpleNamespace(), SimpleNamespace(role=Role.LOAN_EXEC), "admin"
    )

    assert empty_packet is not None
    assert empty_packet["notes"] == []
    assert empty_packet["fingerprint"] == without_notes_attribute["fingerprint"]
    assert internal_note_ai.current_history(
        [SimpleNamespace(metadata_json=None)], empty_packet
    ) == []


@pytest.mark.parametrize("selection", [False, None, "true", 1])
def test_only_explicit_boolean_opt_in_can_supply_note_text(selection) -> None:
    note = _note(include_in_ai_context=selection)

    assert internal_notes.included_in_ai(note) is False
    assert internal_notes.ai_note_context(note) is None
    packet = _context([note])
    assert packet is not None
    assert packet["notes"] == []
    assert note.content not in str(packet)


def test_legacy_note_without_opt_in_field_is_excluded() -> None:
    note = _note()
    del note.include_in_ai_context

    assert internal_notes.included_in_ai(note) is False
    packet = _context([note])
    assert packet is not None
    assert packet["notes"] == []
    assert note.content not in str(packet)


@pytest.mark.parametrize(
    "overrides",
    [
        {"channel": "partner"},
        {"visibility": "shared"},
        {"share_id": UUID(int=20)},
        {"vendor_access_id": UUID(int=21)},
        {"author_role": Role.BROKER},
        {"author_role": Role.REGIONAL_MANAGER},
        {"author_role": Role.DEALER_PARTNER},
        {"author_role": Role.PROFESSIONAL_REFERRAL_PARTNER},
        {"author_role": Role.CLIENT},
    ],
)
def test_selected_non_staff_or_external_owned_note_cannot_become_staff_context(overrides) -> None:
    note = _note(**overrides)

    packet = _context([note])

    assert packet is None or packet["notes"] == []
    assert note.content not in str(packet)


def test_internal_channel_is_private_even_if_legacy_row_has_shared_visibility() -> None:
    assert internal_note_ai.private_channel(_note(visibility="shared")) is True
    assert internal_note_ai.private_channel(_note(channel="partner")) is False
    assert internal_note_ai.private_channel(SimpleNamespace(visibility="admin")) is False


def test_fingerprint_is_order_independent_and_changes_when_selection_or_text_changes() -> None:
    selected = _note()
    unselected = _note(2, include_in_ai_context=False, content="Private, unselected context.")
    original = _context([selected, unselected])

    assert original["fingerprint"] == _context([unselected, selected])["fingerprint"]
    assert len(original["notes"]) == 1
    assert unselected.content not in str(original)

    selected.include_in_ai_context = False
    opted_out = _context([selected, unselected])
    assert opted_out["fingerprint"] != original["fingerprint"]
    assert opted_out["notes"] == []

    selected.include_in_ai_context = True
    selected.content = "A corrected staff observation."
    edited = _context([selected, unselected])
    assert edited["fingerprint"] != original["fingerprint"]
    assert edited["notes"][0]["content"] == selected.content


def test_excluded_note_addition_edit_and_removal_preserve_model_history() -> None:
    selected = _note()
    original = _context([selected])
    previous_answer = SimpleNamespace(
        content="An answer using the selected observation.",
        metadata_json={"internal_note_context_fingerprint": original["fingerprint"]},
    )
    excluded = _note(2, include_in_ai_context=False, content="An excluded observation.")
    for notes in ([selected, excluded], [selected]):
        packet = _context(notes)
        assert packet["fingerprint"] == original["fingerprint"]
        assert internal_note_ai.current_history([previous_answer], packet) == [previous_answer]
    excluded.content = "A private edit that the AI must ignore."
    edited = _context([selected, excluded])
    assert edited["fingerprint"] == original["fingerprint"]
    assert internal_note_ai.current_history([previous_answer], edited) == [previous_answer]
    assert excluded.content not in str(edited)
    assert _context([excluded])["fingerprint"] == _context([])["fingerprint"]


def test_removing_last_selected_note_invalidates_model_history() -> None:
    original = _context([_note()])
    previous_answer = SimpleNamespace(
        metadata_json={"internal_note_context_fingerprint": original["fingerprint"]},
    )
    empty = _context([])
    assert empty["fingerprint"] != original["fingerprint"]
    assert internal_note_ai.current_history([previous_answer], empty) == []


def test_role_policy_is_part_of_model_history_fingerprint(monkeypatch) -> None:
    selected = _note()
    original = _context([selected])
    monkeypatch.setattr(internal_note_ai, "STAFF_ROLES", frozenset({"loan_exec"}))
    changed_policy = _context([selected])
    assert changed_policy["notes"] == original["notes"]
    assert changed_policy["fingerprint"] != original["fingerprint"]


def test_opt_out_excludes_old_model_context_without_deleting_saved_history() -> None:
    note = _note()
    selected_packet = _context([note])
    previous_answer = SimpleNamespace(
        content="An earlier answer based on a selected private observation.",
        metadata_json={"internal_note_context_fingerprint": selected_packet["fingerprint"]},
    )
    unstamped_legacy_answer = SimpleNamespace(content="A legacy answer.", metadata_json=None)
    history = [previous_answer, unstamped_legacy_answer]
    assert internal_note_ai.current_history(history, selected_packet) == [previous_answer]

    note.include_in_ai_context = False
    current_packet = _context([note])

    assert internal_note_ai.current_history(history, current_packet) == []
    assert history == [previous_answer, unstamped_legacy_answer]
    assert previous_answer.content.endswith("private observation.")


def test_external_or_no_packet_never_reuses_tagged_private_context_turns() -> None:
    private_turn = SimpleNamespace(
        content="Private context derived answer.",
        metadata_json={"internal_note_context_fingerprint": "old-private-state"},
    )
    public_turn = SimpleNamespace(
        content="Ordinary thread history.",
        metadata_json={"internal_note_context_fingerprint": "external_note_scope_v1"},
    )
    legacy_turn = SimpleNamespace(content="Unknown historical provenance.", metadata_json=None)

    visible_to_model = internal_note_ai.current_history(
        [private_turn, public_turn, legacy_turn], None
    )

    assert visible_to_model == [public_turn]


def test_private_note_context_is_bounded_without_overwriting_saved_note_text() -> None:
    notes = [_note(index, content=f"Observation {index}: " + "x" * 10000) for index in range(1, 6)]
    originals = [note.content for note in notes]

    packet = _context(notes)

    assert packet is not None
    records = packet["notes"]
    assert records
    assert all(len(record["content"]) <= 8000 for record in records)
    assert sum(len(record["content"]) for record in records) <= 24000
    assert [note.content for note in notes] == originals

