from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.crm_router import _apply_field_desk_profile_update
from app.dealer_os.crm_schemas import FieldDeskProfileUpdate
from app.dealer_os.models import DealerFieldDeskProfile
from app.dealer_os.router import _contact_card_message, _vcard_escape


def _profile(*, user_id, headshot_s3_key: str | None) -> DealerFieldDeskProfile:
    return DealerFieldDeskProfile(
        user_id=user_id,
        preferred_locale="en",
        card_visible=True,
        headshot_s3_key=headshot_s3_key,
    )


def test_profile_update_treats_blank_optional_email_as_none() -> None:
    payload = FieldDeskProfileUpdate(display_email="   ")

    assert payload.display_email is None


def test_profile_update_accepts_unchanged_legacy_headshot_key() -> None:
    owner_id = uuid4()
    legacy_key = "booking/headshots/legacy-photo.jpg"
    profile = _profile(user_id=owner_id, headshot_s3_key=legacy_key)

    _apply_field_desk_profile_update(
        profile,
        FieldDeskProfileUpdate(display_name="Agent Name", headshot_s3_key=legacy_key),
        owner_id=owner_id,
    )

    assert profile.display_name == "Agent Name"
    assert profile.headshot_s3_key == legacy_key


def test_profile_update_rejects_a_new_headshot_outside_owner_prefix() -> None:
    owner_id = uuid4()
    profile = _profile(user_id=owner_id, headshot_s3_key=None)

    with pytest.raises(HTTPException) as exc:
        _apply_field_desk_profile_update(
            profile,
            FieldDeskProfileUpdate(headshot_s3_key="dealer-os/profiles/someone-else/photo.jpg"),
            owner_id=owner_id,
        )

    assert exc.value.status_code == 422


def test_contact_card_message_prefers_personal_note() -> None:
    assert (
        _contact_card_message("Email body", {"personal_note": "  Let us discuss your file.  "})
        == "Let us discuss your file."
    )


def test_contact_card_message_removes_delivery_links() -> None:
    body = """Hi Client,

I help businesses compare financing programs.

Contact card: https://rep.example/card/token
Book a time: https://rep.example/book/agent
Open an application: https://rep.example/?new=1
Email: agent@example.com
Qualified Commercial"""

    message = _contact_card_message(body, {})

    assert message == "I help businesses compare financing programs."
    assert "https://" not in message


def test_vcard_escape_protects_delimiters_and_newlines() -> None:
    assert _vcard_escape("Last, First; Team\nDesk") == "Last\\, First\\; Team\\nDesk"
