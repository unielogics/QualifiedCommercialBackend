"""Walking an AI-intake client into their own secure room.

The intake and the room have always been the same bucket and the same link, but
the client had no way across: the room URL was returned on every intake response
and rendered nowhere, and for a self-serve intake the room PIN was generated and
thrown away, so nobody could enter. Even with a PIN the room showed empty
banking and agreements tabs, because those are keyed on an application profile
that only a staff member could create.

This is the one tap that closes all three, and the rules it has to keep: recover
the code rather than rotate it, never hand back a code the client chose
themselves, and provision the profile that makes the room worth opening.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest


def _link(**over):
    row = SimpleNamespace(
        id=uuid4(), token="room-token", passcode_set_by_client_at=None,
        passcode_hash=None, encrypted_passcode=None, passcode_encryption_provider=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _intake(link=None):
    return SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), full_name="Loyd Bradley", email="loyd@example.com",
        bucket_upload_link=link if link is not None else _link(),
    )


def _db():
    return SimpleNamespace(commit=AsyncMock(), flush=AsyncMock(), add=lambda _row: None)


async def _handoff(intake, *, db=None, read=None, provision=None):
    from app.routers.dealer_ai_intake import _ROOM_HANDOFF_LAST_BY_TOKEN, _secure_room_handoff

    _ROOM_HANDOFF_LAST_BY_TOKEN.clear()
    with patch("app.routers.dealer_ai_intake.profiles_service.provision_profile_for_intake",
               new=provision or AsyncMock()), \
         patch("app.routers.dealer_ai_intake.client_room.read_passcode", return_value=read), \
         patch("app.routers.dealer_ai_intake.client_room.room_url",
               side_effect=lambda t: f"https://app.example.com/buckets/request/{t}"), \
         patch("app.routers.dealer_ai_intake._log", new=AsyncMock()):
        return await _secure_room_handoff(db or _db(), intake, SimpleNamespace(), tab="todo")


# --- the code -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_existing_room_code_is_recovered_not_rotated():
    # Rotating would break a link staff emailed with the code read out
    # separately, and lock out a client who bookmarked the room.
    out = await _handoff(_intake(), read="104293")

    assert out.room_code == "104293"
    assert out.code_source == "recovered"
    assert out.room_url.endswith("/buckets/request/room-token")


@pytest.mark.asyncio
async def test_the_same_code_comes_back_on_a_second_tap():
    intake = _intake()
    first = await _handoff(intake, read="104293")
    second = await _handoff(intake, read="104293")

    assert first.room_code == second.room_code


@pytest.mark.asyncio
async def test_a_room_whose_code_was_discarded_gets_one_minted_and_stored():
    # Every self-serve intake before the single-writer fix is in this state.
    link = _link()
    stored: dict[str, str] = {}
    from app.routers.dealer_ai_intake import _ROOM_HANDOFF_LAST_BY_TOKEN, _secure_room_handoff

    _ROOM_HANDOFF_LAST_BY_TOKEN.clear()
    with patch("app.routers.dealer_ai_intake.profiles_service.provision_profile_for_intake", new=AsyncMock()), \
         patch("app.routers.dealer_ai_intake.client_room.read_passcode", return_value=None), \
         patch("app.routers.dealer_ai_intake.client_room._store_passcode",
               side_effect=lambda _l, code: stored.update(code=code)), \
         patch("app.routers.dealer_ai_intake.client_room.room_url", side_effect=lambda t: t), \
         patch("app.routers.dealer_ai_intake._log", new=AsyncMock()):
        out = await _secure_room_handoff(_db(), _intake(link), SimpleNamespace(), tab="todo")

    assert out.code_source == "minted"
    assert out.room_code == stored["code"]
    assert len(out.room_code) == 6 and out.room_code.isdigit()


@pytest.mark.asyncio
async def test_a_code_the_client_chose_is_never_handed_back():
    # Theirs to remember. Returning it would re-expose a secret they set, and
    # they may well have reused it elsewhere.
    from datetime import UTC, datetime

    out = await _handoff(_intake(_link(passcode_set_by_client_at=datetime.now(UTC))), read="104293")

    assert out.room_code is None
    assert out.code_source == "client_chosen"
    assert out.room_url  # they can still reach the room and type their own code


# --- the room is worth opening -------------------------------------------------


@pytest.mark.asyncio
async def test_the_profile_the_room_needs_is_provisioned_on_the_way_through():
    # Without it, _public_application_room 404s and the client lands on empty
    # banking and agreements tabs.
    provision = AsyncMock()
    intake = _intake()
    await _handoff(intake, read="104293", provision=provision)

    provision.assert_awaited_once()
    assert provision.await_args.args[1] is intake


@pytest.mark.asyncio
async def test_a_file_with_no_room_says_so_rather_than_half_working():
    from fastapi import HTTPException

    intake = _intake()
    intake.bucket_upload_link = None
    with pytest.raises(HTTPException) as err:
        await _handoff(intake)
    assert err.value.status_code == 409


# --- not a loop ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_tapping_twice_in_a_row_is_throttled():
    from fastapi import HTTPException

    from app.routers.dealer_ai_intake import _ROOM_HANDOFF_LAST_BY_TOKEN, _secure_room_handoff

    intake = _intake()
    _ROOM_HANDOFF_LAST_BY_TOKEN.clear()
    with patch("app.routers.dealer_ai_intake.profiles_service.provision_profile_for_intake", new=AsyncMock()), \
         patch("app.routers.dealer_ai_intake.client_room.read_passcode", return_value="104293"), \
         patch("app.routers.dealer_ai_intake.client_room.room_url", side_effect=lambda t: t), \
         patch("app.routers.dealer_ai_intake._log", new=AsyncMock()):
        await _secure_room_handoff(_db(), intake, SimpleNamespace(), tab="todo")
        with pytest.raises(HTTPException) as err:
            await _secure_room_handoff(_db(), intake, SimpleNamespace(), tab="todo")
    assert err.value.status_code == 429


# --- the routes ----------------------------------------------------------------


def test_every_intake_room_can_be_opened():
    # Asserted against the routers rather than app.main: nothing in this suite
    # imports the app, and a sibling test stubbing a service breaks it if you do.
    from app.routers.dealer_ai_intake import funding_router, mca_router, router

    for name, api in (("dealer", router), ("funding", funding_router), ("mca", mca_router)):
        paths = {r.path for r in api.routes}
        assert any(p.endswith("/{token}/secure-room") for p in paths), name


def test_the_handoff_is_a_post_so_a_code_never_lands_in_a_url_someone_shares():
    from app.routers.dealer_ai_intake import funding_router, mca_router, router

    for api in (router, funding_router, mca_router):
        for route in api.routes:
            if route.path.endswith("/{token}/secure-room"):
                assert route.methods == {"POST"}


# ---------------------------------------------------------------------------
# The same rule, one screen over: the buckets admin panel.
# ---------------------------------------------------------------------------


def _upload_link_row(**over):
    from datetime import UTC, datetime

    row = SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), token="room-token",
        recipient_name="John Grace", recipient_email="john@example.com",
        expires_at=None, allow_notes=True, allow_multiple_sessions=True,
        can_use_ai_chat=True, can_view_ai_tasks=True, status="active",
        completed_at=None, created_at=datetime.now(UTC),
        passcode_hash="x", encrypted_passcode="enc", passcode_encryption_provider="fernet",
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def test_invite_panel_reads_a_recoverable_code_instead_of_hiding_it():
    """Staff were told to regenerate to see a code the system could still read.

    Regenerating changes the client's PIN, so the advice broke an invite that
    had already been sent in order to display it.
    """
    from app.routers import buckets

    with patch.object(buckets.client_room, "read_passcode", return_value="954577"), \
         patch.object(buckets, "_public_url", side_effect=lambda p: f"https://app.example.com{p}"):
        out = buckets._upload_link_read(_upload_link_row())

    assert out.passcode == "954577"


def test_invite_panel_stays_silent_when_the_code_really_is_gone():
    """Links minted before the recoverable copy existed have no code to show."""
    from app.routers import buckets

    with patch.object(buckets.client_room, "read_passcode", return_value=None), \
         patch.object(buckets, "_public_url", side_effect=lambda p: f"https://app.example.com{p}"):
        out = buckets._upload_link_read(_upload_link_row(encrypted_passcode=None))

    assert out.passcode is None


def test_a_freshly_minted_code_wins_over_the_stored_one():
    """Create and regenerate pass the plaintext they just generated."""
    from app.routers import buckets

    with patch.object(buckets.client_room, "read_passcode", return_value="000000") as stored, \
         patch.object(buckets, "_public_url", side_effect=lambda p: f"https://app.example.com{p}"):
        out = buckets._upload_link_read(_upload_link_row(), passcode="123456")

    assert out.passcode == "123456"
    stored.assert_not_called()


# ---------------------------------------------------------------------------
# Which link is "the room" — one rule, one place.
# ---------------------------------------------------------------------------


def test_every_screen_resolves_the_room_through_one_function():
    """The newest-active-link query must not grow copies again.

    It had four: the room service, the application-profile room, the
    bank-evidence panel and the credit-invite PIN gate. Four places that had to
    agree about which door a client was sent to, with nothing making them.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    canonical = root / "dealer_os" / "services" / "client_room.py"

    # A query that orders upload links by recency is the tie-break rule itself.
    pattern = re.compile(r"BucketUploadLink\.created_at\.desc\(\)")
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path != canonical
        and "tests" not in path.parts
        and pattern.search(path.read_text())
    ]

    assert offenders == [], (
        "These modules re-derive which upload link is the room. Call "
        "client_room.active_link instead: " + ", ".join(offenders)
    )
