"""The PIN that opens a client's room.

Six digits guard document upload, bank connection, credit authorization and
contract signing, so the rules about what may become one matter more than the
size of the number suggests.

The denylist used to guard only the client's self-chosen replacement. Staff
opening a file could set 111111 or 123456 — the codes a guesser tries first —
on the same credential. These pin the rule at both ends.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.dealer_os.services import client_room


def _dealer():
    return SimpleNamespace(id="d1", name="Delgado Auto Group", email="owner@example.com")


async def _initialize(passcode: str):
    db = SimpleNamespace(add=lambda _r: None, flush=AsyncMock())
    with patch.object(client_room.buckets_link, "ensure_bucket",
                      new=AsyncMock(return_value=SimpleNamespace(id="b1"))), \
         patch.object(client_room, "active_link", new=AsyncMock(return_value=None)):
        return await client_room.initialize_room(db, _dealer(), passcode)


# --- what may become a PIN ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_pin_opens_the_room():
    room = await _initialize("104293")
    assert room.passcode == "104293"
    assert room.link.passcode_hash.startswith("pbkdf2_sha256$")
    # Recoverable, so a rep can read it back instead of only rotating.
    assert room.link.encrypted_passcode and room.link.passcode_encryption_provider


@pytest.mark.asyncio
async def test_the_codes_a_guesser_tries_first_are_refused_from_staff_too():
    for trivial in ("111111", "000000", "123456", "654321", "121212"):
        with pytest.raises(ValueError, match="too easy to guess"):
            await _initialize(trivial)


@pytest.mark.asyncio
async def test_anything_that_is_not_six_digits_is_refused():
    for bad in ("12345", "1234567", "abcdef", "12 345", "", "١٢٣٤٥٦"):
        with pytest.raises(ValueError):
            await _initialize(bad)


def test_staff_and_client_are_held_to_one_rule():
    """The two entry points must not disagree about what is acceptable."""
    for trivial in sorted(client_room._TRIVIAL_PASSCODES):
        assert client_room.passcode_problem(trivial) is not None
    import inspect

    assert "_TRIVIAL_PASSCODES" in inspect.getsource(client_room.initialize_room)


# --- what the client may choose later ------------------------------------------


def test_a_client_cannot_reuse_the_code_they_were_sent():
    assert client_room.passcode_problem("104293", "104293") is not None
    assert client_room.passcode_problem("104293", "884120") is None


def test_a_generated_code_is_always_six_digits_in_range():
    for _ in range(200):
        code = client_room._generate_passcode()
        assert len(code) == 6 and code.isdigit()
        assert 100000 <= int(code) <= 999999


def test_a_decrypted_value_that_is_not_a_pin_is_refused_rather_than_shown():
    """read_passcode is the staff-facing reveal, so it validates what came back
    out of the ciphertext instead of trusting it."""
    link = SimpleNamespace(id="l1", encrypted_passcode="x", passcode_encryption_provider="fernet")
    with patch.object(client_room, "_decrypt_fernet", return_value="not-a-pin"):
        assert client_room.read_passcode(link) is None
    with patch.object(client_room, "_decrypt_fernet", return_value="104293"):
        assert client_room.read_passcode(link) == "104293"


def test_an_undecryptable_copy_reports_nothing_rather_than_raising():
    link = SimpleNamespace(id="l1", encrypted_passcode="corrupt", passcode_encryption_provider="fernet")
    with patch.object(client_room, "_decrypt_fernet", side_effect=ValueError("bad key")):
        assert client_room.read_passcode(link) is None


def test_a_bad_pin_is_a_422_not_a_503():
    """It used to fall into the generic handler and come back as "The secure
    client room could not be created. Try creating the file again." — an
    outage message for a typo, and trying again would fail identically."""
    import inspect

    from app.dealer_os import router

    source = inspect.getsource(router.create_dealer)
    value_at = source.index("except ValueError")
    generic_at = source.index("except Exception")
    assert value_at < generic_at, "the generic handler would swallow it first"
    assert "HTTP_422_UNPROCESSABLE_ENTITY" in source
