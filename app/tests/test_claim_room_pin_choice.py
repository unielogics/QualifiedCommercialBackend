"""Owned diagnostic claims choose a room PIN once, including safe retries."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.services import client_room
from app.routers.dealer_ai_intake_routes import client

PIN = "729518"
INVITE_PIN = "104293"
CHOSEN_AT = datetime(2026, 10, 1, tzinfo=UTC)


def room(*, chosen=False, **changes):
    row = SimpleNamespace(
        id=uuid4(), status="active", expires_at=None, completed_at=None,
        allow_multiple_sessions=True,
        passcode_hash=client_room._hash_passcode(PIN if chosen else INVITE_PIN),
        passcode_set_by_client_at=CHOSEN_AT if chosen else None,
        encrypted_passcode="existing-ciphertext", passcode_encryption_provider="test",
    )
    row.__dict__.update(changes)
    return row


def context(row, *, locked=None):
    intake = SimpleNamespace(bucket_upload_link_id=row.id if row else None)
    current = row if locked is None else locked
    db = SimpleNamespace(
        get=AsyncMock(return_value=row),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: current)),
        flush=AsyncMock(), commit=AsyncMock(),
    )
    return intake, db


async def test_retry_with_the_configured_pin_preserves_hash_ciphertext_and_timestamp():
    row = room(chosen=True)
    before = dict(vars(row))
    intake, db = context(row)
    with patch.object(client_room, "set_passcode", AsyncMock()) as store:
        result = await client._claim_room_pin(db, intake, PIN)
    assert result is row
    assert vars(row) == before
    store.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert "FOR UPDATE" in str(db.execute.await_args.args[0])


async def test_a_different_choice_cannot_replace_an_existing_client_pin():
    row = room(chosen=True)
    before = dict(vars(row))
    intake, db = context(row)
    with patch.object(client_room, "set_passcode", AsyncMock()) as store:
        with pytest.raises(HTTPException) as error:
            await client._claim_room_pin(db, intake, "638274")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "room_pin_already_configured"
    assert vars(row) == before
    store.assert_not_awaited()


async def test_first_choice_stamps_the_owned_generated_room_without_creating_another_file():
    row = room()
    intake, db = context(row)

    def encrypt_and_hash(target, pin):
        # Exercise the real timestamp/flush path without external encryption configuration.
        target.passcode_hash = client_room._hash_passcode(pin)
        target.encrypted_passcode = "new-ciphertext"

    with patch.object(client_room, "_store_passcode", side_effect=encrypt_and_hash) as store:
        result = await client._claim_room_pin(db, intake, PIN)
    assert result is row
    assert client_room.verify_passcode(PIN, row.passcode_hash)
    assert row.passcode_set_by_client_at is not None
    assert row.encrypted_passcode == "new-ciphertext"
    store.assert_called_once_with(row, PIN)
    db.flush.assert_awaited()
    db.commit.assert_not_awaited()  # Claim creation owns the surrounding transaction.
    assert PIN not in repr(vars(row))


@pytest.mark.parametrize("row", [
    None,
    room(status="revoked"),
    room(expires_at=datetime.now(UTC) - timedelta(days=1)),
    room(completed_at=CHOSEN_AT, allow_multiple_sessions=False),
])
async def test_a_missing_or_unusable_room_never_looks_like_successful_setup(row):
    intake, db = context(row)
    with patch.object(client_room, "set_passcode", AsyncMock()) as store:
        with pytest.raises(HTTPException) as error:
            await client._claim_room_pin(db, intake, PIN)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "room_access_unavailable"
    store.assert_not_awaited()


async def test_the_fresh_locked_row_wins_over_the_original_generated_room_snapshot():
    initial = room()
    concurrent = room(chosen=True)
    concurrent.id = initial.id
    intake, db = context(initial, locked=concurrent)
    with patch.object(client_room, "set_passcode", AsyncMock()) as store:
        with pytest.raises(HTTPException) as error:
            await client._claim_room_pin(db, intake, "638274")
    assert error.value.detail["code"] == "room_pin_already_configured"
    store.assert_not_awaited()


async def test_legacy_claim_without_a_pin_only_reads_setup_state():
    row = room()
    before = dict(vars(row))
    intake, db = context(row)
    with patch.object(client_room, "set_passcode", AsyncMock()) as store:
        assert await client._claim_room_pin(db, intake, None) is row
    assert vars(row) == before
    store.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("pin", ["123456", "111111", "121212", "12345", "abcdef"])
async def test_claim_write_boundary_refuses_weak_or_invalid_first_choices(pin):
    row = room()
    before = dict(vars(row))
    intake, db = context(row)
    with patch.object(client_room, "set_passcode", AsyncMock()) as store:
        with pytest.raises(HTTPException) as error:
            await client._claim_room_pin(db, intake, pin)
    assert error.value.status_code == 422
    assert vars(row) == before
    store.assert_not_awaited()
