"""Booking message templates: the placeholder contract, AI drafting and test sends.

The host authors every client-facing booking message, so three things have to
hold: what they may type is a published contract rather than three drifting
copies in the UI, a drafted template never carries the compliance lines the
transport appends for itself, and a model outage returns usable wording instead
of an error.
"""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.dealer_os.services import precall
from app.routers.me import BOOKING_TEMPLATE_KINDS, _draft_fallback
from app.services import message_render


def _booking(**over):
    row = SimpleNamespace(
        precall_messages={}, confirmation_messages={}, precall_video_url=None,
        precall_videos=[], reminder_email_messages={}, reminder_sms_messages={},
        timezone="America/New_York",
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


# --- the placeholder contract -------------------------------------------------


def test_video_is_a_published_placeholder():
    assert "{video}" in message_render.PLACEHOLDERS
    assert "{video}" not in message_render.PIN_ONLY


def test_pin_stays_restricted_to_the_two_messages_that_deliver_it():
    assert message_render.PIN_ONLY == frozenset({"{pin}"})
    assert message_render.disallowed_placeholders("call {name} on {pin}") == ["{pin}"]
    assert message_render.disallowed_placeholders("your PIN is {pin}", allow_pin=True) == []


def test_every_template_kind_names_a_real_channel():
    for kind, spec in BOOKING_TEMPLATE_KINDS.items():
        assert spec["channel"] in {"email", "sms"}, kind
        assert spec["label"] and spec["goal"], kind


# --- the video ---------------------------------------------------------------


def test_the_firm_video_is_live_without_a_per_host_backfill():
    values = _template_values(_booking())
    assert values["{video}"] == precall.DEFAULT_PRECALL_VIDEO_URL


def test_a_host_can_point_at_their_own_video():
    values = _template_values(_booking(precall_video_url="https://youtu.be/other"))
    assert values["{video}"] == "https://youtu.be/other"


def test_the_default_precall_block_carries_the_video_and_the_room():
    block = precall.DEFAULT_MESSAGES["precall_block"]
    assert "{video}" in block
    assert "{room_link}" in block
    # The PIN travels on its own channel and must not be in the block.
    assert "{pin}" not in block


def _template_values(booking):
    """The placeholder table, with the parts this test does not exercise stubbed."""
    from datetime import datetime

    notice = SimpleNamespace(invitee_name="Jordan Reyes", join_url="")
    event = SimpleNamespace(starts_at=datetime(2026, 9, 9, 14, 0, tzinfo=UTC))
    host = SimpleNamespace(name="Jonathan Franco")
    return precall.template_values(
        notice=notice, event=event, booking=booking, host=host, dealer=None,
        room_link="https://app.example.com/room", ready=None,
    )


# --- the template the host authored is the one that goes out ------------------


def test_the_hosts_confirmation_sms_is_read_from_where_the_ui_stores_it():
    # The UI writes this under "sms"; this module asks for "confirmation_sms".
    # Before the alias the host's own text was silently ignored.
    booking = _booking(confirmation_messages={"sms": "Booked. See you {time}."})
    assert precall.message_text(booking, "confirmation_sms") == "Booked. See you {time}."


def test_a_blank_override_still_falls_back_to_the_default():
    booking = _booking(confirmation_messages={"sms": "   "})
    assert precall.message_text(booking, "confirmation_sms") == precall.DEFAULT_MESSAGES["confirmation_sms"]


def test_precall_messages_win_over_confirmation_messages():
    booking = _booking(
        precall_messages={"confirmation_sms": "from precall"},
        confirmation_messages={"sms": "from confirmations"},
    )
    assert precall.message_text(booking, "confirmation_sms") == "from precall"


# --- drafting -----------------------------------------------------------------


def test_every_kind_has_usable_wording_when_the_model_is_down():
    for kind, spec in BOOKING_TEMPLATE_KINDS.items():
        subject, body = _draft_fallback(kind, _booking())
        assert body.strip(), kind
        if spec["channel"] == "sms":
            assert subject is None, kind


@pytest.mark.asyncio
async def test_a_drafted_sms_never_carries_the_opt_out_line_the_sender_appends():
    from app.routers.me import BookingTemplateDraftRequest, draft_booking_template

    resp = SimpleNamespace(content=[SimpleNamespace(text='{"body": "Your call is {time}. Reply STOP to opt out."}')])
    with patch("app.services.ai.usage.tracked_messages_create", new=AsyncMock(return_value=resp)), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await draft_booking_template(
            BookingTemplateDraftRequest(kind="reminder_sms"), SimpleNamespace(id=uuid4(), name="Jonathan"), None
        )

    assert message_render.STOP_NOTICE not in out.body
    assert out.channel == "sms"
    assert out.fallback is False


@pytest.mark.asyncio
async def test_a_drafted_template_never_leaks_the_pin_placeholder():
    from app.routers.me import BookingTemplateDraftRequest, draft_booking_template

    resp = SimpleNamespace(content=[SimpleNamespace(text='{"subject": "Your PIN {pin}", "body": "Use {pin} to sign in."}')])
    with patch("app.services.ai.usage.tracked_messages_create", new=AsyncMock(return_value=resp)), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await draft_booking_template(
            BookingTemplateDraftRequest(kind="reminder_email"), SimpleNamespace(id=uuid4(), name="Jonathan"), None
        )

    assert "{pin}" not in out.body
    assert "{pin}" not in (out.subject or "")


@pytest.mark.asyncio
async def test_a_model_outage_returns_wording_rather_than_an_error():
    from app.routers.me import BookingTemplateDraftRequest, draft_booking_template

    with patch("app.services.ai.usage.tracked_messages_create", new=AsyncMock(side_effect=RuntimeError("bedrock down"))), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await draft_booking_template(
            BookingTemplateDraftRequest(kind="precall_block"), SimpleNamespace(id=uuid4(), name="Jonathan"), None
        )

    assert out.fallback is True
    assert out.body.strip()


@pytest.mark.asyncio
async def test_an_unknown_template_kind_is_refused():
    from fastapi import HTTPException

    from app.routers.me import BookingTemplateDraftRequest, draft_booking_template

    with pytest.raises(HTTPException) as err:
        await draft_booking_template(
            BookingTemplateDraftRequest(kind="not_a_message"), SimpleNamespace(id=uuid4(), name="J"), None
        )
    assert err.value.status_code == 422


# --- test sends ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_test_email_is_rendered_marked_and_sent_to_the_operator():
    from app.routers.me import BookingTestSendRequest, test_send_booking_message

    db = SimpleNamespace(add=lambda _row: None, commit=AsyncMock())
    sent = {}

    def _send(*, to_email, subject, body_text, body_html=None):
        sent.update(to=to_email, subject=subject, body=body_text)
        return SimpleNamespace(ok=True, detail="")

    user = SimpleNamespace(id=uuid4(), name="Jonathan Franco", email="jf@example.com")
    with patch("app.services.email.ses_client.send_email", _send), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await test_send_booking_message(
            BookingTestSendRequest(channel="email", subject="Your call {time}", body="Hi {first}, watch {video}"),
            SimpleNamespace(), user, db,
        )

    assert out.ok and out.to == "jf@example.com"
    assert sent["subject"].startswith("[Test] ")
    # Placeholders resolved, and the operator can see it is a test.
    assert "Jordan" in sent["body"] and "{first}" not in sent["body"]
    assert precall.DEFAULT_PRECALL_VIDEO_URL in sent["body"]
    assert "test of a booking message" in sent["body"]


@pytest.mark.asyncio
async def test_a_test_text_says_why_when_texting_is_unavailable_instead_of_failing_silently():
    from app.routers.me import BookingTestSendRequest, test_send_booking_message

    db = SimpleNamespace(add=lambda _row: None, commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), name="Jonathan", email="jf@example.com")
    with patch("app.services.sms.sms_available", return_value=False), \
         patch("app.services.sms.unavailable_reason", return_value="the relay is offline"), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await test_send_booking_message(
            BookingTestSendRequest(channel="sms", body="Call is {time}", to="+19735550148"),
            SimpleNamespace(), user, db,
        )

    assert out.ok is False
    assert "relay is offline" in out.detail
    # They still see what the client would have received, opt-out line included.
    assert message_render.STOP_NOTICE in out.rendered


@pytest.mark.asyncio
async def test_a_test_text_needs_a_number():
    from fastapi import HTTPException

    from app.routers.me import BookingTestSendRequest, test_send_booking_message

    db = SimpleNamespace(add=lambda _row: None, commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), name="Jonathan", email="jf@example.com")
    with patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        with pytest.raises(HTTPException) as err:
            await test_send_booking_message(
                BookingTestSendRequest(channel="sms", body="hello"), SimpleNamespace(), user, db
            )
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_a_test_text_goes_through_the_guarded_sender_with_its_own_context():
    from app.routers.me import BookingTestSendRequest, test_send_booking_message

    db = SimpleNamespace(add=lambda _row: None, commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), name="Jonathan", email="jf@example.com")
    checked = AsyncMock(return_value=SimpleNamespace(ok=True, detail=""))
    with patch("app.services.sms.sms_available", return_value=True), \
         patch("app.services.sms.send_sms_checked", checked), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await test_send_booking_message(
            BookingTestSendRequest(channel="sms", body="Call is {time}", to="+19735550148"),
            SimpleNamespace(), user, db,
        )

    assert out.ok
    kwargs = checked.await_args.kwargs
    # A test is the operator's own message, so no client consent kind is claimed,
    # but it stays distinguishable in the ledger.
    assert kwargs["require_consent_kind"] is None
    assert kwargs["context"] == "booking_template_test"
    assert kwargs["body"].startswith("[Test] ")


# --- the video library --------------------------------------------------------


def test_a_message_points_at_a_video_by_key_not_by_url():
    # Re-recording a video changes its URL; the key is what templates hold, so
    # the pointer survives.
    booking = _booking(precall_videos=[
        {"key": "bank", "label": "Connecting your bank", "url": "https://videos.example.com/bank-v1"},
        {"key": "credit", "label": "The soft credit check", "url": "https://videos.example.com/credit"},
    ])
    values = precall.video_values(booking)
    assert values["{video_bank}"] == "https://videos.example.com/bank-v1"
    assert values["{video_credit}"] == "https://videos.example.com/credit"

    booking.precall_videos[0]["url"] = "https://videos.example.com/bank-v2"
    assert precall.video_values(booking)["{video_bank}"] == "https://videos.example.com/bank-v2"


def test_the_first_video_is_the_one_plain_video_renders():
    booking = _booking(precall_videos=[
        {"key": "intro", "label": "Intro", "url": "https://videos.example.com/intro"},
        {"key": "bank", "label": "Bank", "url": "https://videos.example.com/bank"},
    ])
    assert precall.video_values(booking)["{video}"] == "https://videos.example.com/intro"


def test_a_host_who_never_set_a_video_still_gets_the_firm_one():
    assert precall.video_values(_booking())["{video}"] == precall.DEFAULT_PRECALL_VIDEO_URL


def test_the_single_video_set_before_the_library_existed_still_renders():
    booking = _booking(precall_video_url="https://videos.example.com/legacy")
    assert precall.video_values(booking)["{video}"] == "https://videos.example.com/legacy"


def test_an_entry_with_no_url_is_ignored_rather_than_rendering_blank():
    booking = _booking(precall_videos=[
        {"key": "empty", "label": "Not recorded yet", "url": "  "},
        {"key": "real", "label": "Real", "url": "https://videos.example.com/real"},
    ])
    values = precall.video_values(booking)
    assert "{video_empty}" not in values
    assert values["{video}"] == "https://videos.example.com/real"


def test_the_settings_ui_is_offered_every_video_as_an_insertable_token():
    booking = _booking(precall_videos=[{"key": "bank", "label": "Connecting your bank", "url": "https://v/bank"}])
    tokens = [v["token"] for v in precall.video_placeholders(booking)]
    assert tokens == ["{video}", "{video_bank}"]


def test_two_videos_cannot_share_a_key():
    from pydantic import ValidationError

    from app.schemas.booking_settings import UserBookingSettingsBase

    with pytest.raises(ValidationError) as err:
        UserBookingSettingsBase(precall_videos=[
            {"key": "a", "label": "One", "url": "https://v/1"},
            {"key": "a", "label": "Two", "url": "https://v/2"},
        ])
    assert "own key" in str(err.value)


def test_a_video_key_stays_typeable_inside_a_placeholder():
    from pydantic import ValidationError

    from app.schemas.booking_settings import UserBookingSettingsBase

    for key in ("Bank Video", "bank-video", "bank video", "_bank", "bank_"):
        with pytest.raises(ValidationError):
            UserBookingSettingsBase(precall_videos=[{"key": key, "label": "x", "url": "https://v/1"}])
    ok = UserBookingSettingsBase(precall_videos=[{"key": "bank_2", "label": "x", "url": "https://v/1"}])
    assert ok.precall_videos[0].key == "bank_2"


def test_a_video_link_has_to_be_a_link():
    from pydantic import ValidationError

    from app.schemas.booking_settings import UserBookingSettingsBase

    with pytest.raises(ValidationError):
        UserBookingSettingsBase(precall_videos=[{"key": "x", "label": "x", "url": "javascript:alert(1)"}])


# --- drafting the whole sequence ---------------------------------------------


@pytest.mark.asyncio
async def test_the_sequence_drafter_fills_the_blanks_and_leaves_written_copy_alone():
    from app.routers.me import BookingSequenceDraftRequest, draft_booking_sequence

    # The host has written the confirmation email; everything else is default.
    booking = _booking(confirmation_messages={"email_body": "My own wording."})
    resp = SimpleNamespace(content=[SimpleNamespace(text='{"subject": "S", "body": "B"}')])
    with patch("app.services.ai.usage.tracked_messages_create", new=AsyncMock(return_value=resp)), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=booking)):
        out = await draft_booking_sequence(
            BookingSequenceDraftRequest(), SimpleNamespace(id=uuid4(), name="Jonathan"), None
        )

    assert "confirmation_email" in out.skipped
    drafted = {d.kind for d in out.drafts}
    assert "confirmation_email" not in drafted
    assert drafted == set(BOOKING_TEMPLATE_KINDS) - {"confirmation_email"}


@pytest.mark.asyncio
async def test_the_sequence_drafter_will_overwrite_when_asked():
    from app.routers.me import BookingSequenceDraftRequest, draft_booking_sequence

    booking = _booking(confirmation_messages={"email_body": "My own wording."})
    resp = SimpleNamespace(content=[SimpleNamespace(text='{"subject": "S", "body": "B"}')])
    with patch("app.services.ai.usage.tracked_messages_create", new=AsyncMock(return_value=resp)), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=booking)):
        out = await draft_booking_sequence(
            BookingSequenceDraftRequest(overwrite_authored=True),
            SimpleNamespace(id=uuid4(), name="Jonathan"), None,
        )

    assert out.skipped == []
    assert len(out.drafts) == len(BOOKING_TEMPLATE_KINDS)


@pytest.mark.asyncio
async def test_the_sequence_drafter_can_be_pointed_at_named_messages():
    from app.routers.me import BookingSequenceDraftRequest, draft_booking_sequence

    resp = SimpleNamespace(content=[SimpleNamespace(text='{"body": "B"}')])
    with patch("app.services.ai.usage.tracked_messages_create", new=AsyncMock(return_value=resp)), \
         patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        out = await draft_booking_sequence(
            BookingSequenceDraftRequest(kinds=["reminder_sms", "precall_block"]),
            SimpleNamespace(id=uuid4(), name="Jonathan"), None,
        )

    assert [d.kind for d in out.drafts] == ["reminder_sms", "precall_block"]


@pytest.mark.asyncio
async def test_the_sequence_drafter_refuses_a_message_that_does_not_exist():
    from fastapi import HTTPException

    from app.routers.me import BookingSequenceDraftRequest, draft_booking_sequence

    with patch("app.routers.me._get_or_create_booking_settings", new=AsyncMock(return_value=_booking())):
        with pytest.raises(HTTPException) as err:
            await draft_booking_sequence(
                BookingSequenceDraftRequest(kinds=["not_a_message"]),
                SimpleNamespace(id=uuid4(), name="Jonathan"), None,
            )
    assert err.value.status_code == 422
