"""Every access PIN the system issues is six digits from one generator.

The rooms, upload links, file shares, production-package and worksheet links
all open with a PIN. The rule for a new one is six ASCII digits, leading zeros
kept, never a code a guesser tries first. These tests pin three things:

* the generator itself (shape, the whole 000000-999999 range, no easy codes);
* that every issuer goes through it instead of minting its own;
* that only NEW credentials are held to the rule. A share or link issued
  earlier with a longer code must still open with that code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app
from app.dealer_os.services import application_precall, client_room, prospects
from app.routers import buckets
from app.schemas.bucket import (
    NEW_ACCESS_PIN_MESSAGE,
    BucketRequestAccessRequest,
    BucketRequestPasscodeChange,
    BucketShareAccessRequest,
    BucketShareCreate,
    BucketUploadLinkCreate,
)
from app.services import worksheet_links

SIX_DIGITS = re.compile(r"[0-9]{6}")
# Not the shapes of anyone's real code: an old eight-character code, a typed
# phrase, and digits that are not ASCII.
NOT_A_NEW_PIN = ("AB12CD34", "12345", "1234567", "12 345", "12345a", "١٢٣٤٥٦", "Welcome-123")


def _scripted_randbelow(*values: int):
    """Stand-in for secrets.randbelow that hands back the given draws in order."""
    draws = iter(values)
    calls: list[int] = []

    def randbelow(upper: int) -> int:
        calls.append(upper)
        return next(draws)

    return randbelow, calls


# --- the generator --------------------------------------------------------------


def test_a_generated_pin_is_always_six_ascii_digits_and_never_an_easy_code():
    for _ in range(500):
        code = client_room._generate_passcode()
        assert isinstance(code, str)
        assert SIX_DIGITS.fullmatch(code), code
        assert client_room.passcode_problem(code) is None, code


def test_the_whole_range_is_drawn_and_leading_zeros_are_kept():
    randbelow, calls = _scripted_randbelow(42)
    with patch.object(client_room.secrets, "randbelow", side_effect=randbelow):
        assert client_room._generate_passcode() == "000042"
    assert calls == [1_000_000], "draw from 000000-999999, not 100000-999999"


def test_an_easy_code_is_drawn_again_rather_than_issued():
    # 0 -> 000000 and 123456 are on the guess-first list; 7 -> 000007 is not.
    randbelow, calls = _scripted_randbelow(0, 123456, 7)
    with patch.object(client_room.secrets, "randbelow", side_effect=randbelow):
        assert client_room._generate_passcode() == "000007"
    assert len(calls) == 3


# --- every issuer goes through it ---------------------------------------------------


def _forced(value: int):
    return patch.object(client_room.secrets, "randbelow", return_value=value)


def test_share_and_upload_link_pins_come_from_the_one_generator():
    with _forced(4321):
        assert buckets._generate_passcode() == "004321"


def test_the_prospect_room_pin_helper_comes_from_the_one_generator():
    with _forced(4321):
        assert prospects.random_room_pin() == "004321"


@pytest.mark.asyncio
async def test_rotating_an_ai_intake_room_pin_issues_a_six_digit_pin():
    link = SimpleNamespace(
        token="room-token",
        passcode_hash=None,
        encrypted_passcode="old",
        passcode_encryption_provider="fernet",
        passcode_set_by_client_at=object(),
        expires_at=object(),
    )
    db = SimpleNamespace(flush=AsyncMock())
    with _forced(4321), patch.object(application_precall, "_room_link", new=AsyncMock(return_value=link)):
        room = await application_precall.rotate_passcode(db, SimpleNamespace(id=uuid4()))
    assert room.passcode == "004321"
    assert buckets._verify_passcode("004321", link.passcode_hash)


@pytest.mark.asyncio
async def test_a_worksheet_link_pin_comes_from_the_one_generator():
    link = SimpleNamespace(pin_hash=None, pin_set_at=None, pin_attempts=3, pin_locked_until=object())
    with _forced(4321):
        code = await worksheet_links.set_pin(link)
    assert code == "004321"
    assert client_room.verify_passcode("004321", link.pin_hash)


@pytest.mark.asyncio
async def test_a_worksheet_pin_of_the_wrong_shape_says_so():
    link = SimpleNamespace(pin_hash=None, pin_set_at=None, pin_attempts=0, pin_locked_until=None)
    with pytest.raises(HTTPException) as wrong_shape:
        await worksheet_links.set_pin(link, "12ab")
    assert wrong_shape.value.detail == "Choose a 6-digit PIN."
    with pytest.raises(HTTPException) as easy:
        await worksheet_links.set_pin(link, "111111")
    assert "too easy to guess" in easy.value.detail


def _pin_mints(tree: ast.AST) -> set[tuple[int, str]]:
    """(line, name) for each randbelow() result bound to, or returned from,
    something named like a PIN or passcode."""

    def draws(node: ast.AST) -> bool:
        return any(
            isinstance(call, ast.Call)
            and (getattr(call.func, "attr", None) or getattr(call.func, "id", None)) == "randbelow"
            for call in ast.walk(node)
        )

    found: set[tuple[int, str]] = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Return) and node.value is not None and draws(node.value):
                found.add((node.lineno, func.name))
            elif isinstance(node, ast.Assign) and draws(node.value):
                for target in node.targets:
                    for named in ast.walk(target):
                        name = getattr(named, "id", None) or getattr(named, "attr", None)
                        if name:
                            found.add((node.lineno, name))
    return {(line, name) for line, name in found if re.search(r"pin|passcode", name, re.IGNORECASE)}


def test_no_module_mints_a_pin_of_its_own():
    """A second generator is how a flow ends up issuing codes nothing accepts.

    One-time sign-in codes (emailed, short-lived) and loan reference numbers
    also use randbelow and are not PINs, so they are not named like one.
    """
    root = Path(app.__file__).resolve().parent
    canonical = root / "dealer_os" / "services" / "client_room.py"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or path == canonical:
            continue
        for line, name in sorted(_pin_mints(ast.parse(path.read_text(encoding="utf-8")))):
            offenders.append(f"{path.relative_to(root)}:{line} ({name})")
    assert offenders == [], f"use client_room._generate_passcode(): {offenders}"


# --- the rule applies to new credentials ----------------------------------------------


@pytest.mark.parametrize("model", ["share", "upload_link"])
def test_a_new_share_or_upload_link_takes_six_digits_or_generates_one(model: str):
    def make(passcode):
        if model == "share":
            return BucketShareCreate(recipient_name="Avery Example", file_ids=[uuid4()], passcode=passcode)
        return BucketUploadLinkCreate(recipient_name="Avery Example", passcode=passcode)

    assert make("012345").passcode == "012345"
    assert make(" 482913 ").passcode == "482913"
    # Blank or absent: the route generates one.
    assert make("").passcode is None
    assert make("   ").passcode is None
    assert make(None).passcode is None
    for invalid in NOT_A_NEW_PIN:
        with pytest.raises(ValidationError) as refused:
            make(invalid)
        assert NEW_ACCESS_PIN_MESSAGE in str(refused.value), invalid


def test_a_new_share_route_uses_the_pin_it_was_given_or_the_generator():
    import inspect

    source = inspect.getsource(buckets.create_share)
    assert "payload.passcode or _generate_passcode()" in source


def test_a_share_issued_with_an_older_longer_code_still_opens():
    """Shares made before the rule kept whatever code the desk typed or the old
    panel generated. The access route verifies the stored hash, and its request
    body takes any code, so those viewers are not locked out."""
    older = "AB12CD34"
    assert BucketShareAccessRequest(passcode=older).passcode == older
    assert buckets._verify_passcode(older, buckets._hash_passcode(older))


def test_a_room_link_issued_with_an_older_code_still_opens_and_can_move_to_a_pin():
    older = "AB12CD34"
    assert BucketRequestAccessRequest(passcode=older).passcode == older
    change = BucketRequestPasscodeChange(passcode=older, new_passcode="482913")
    assert client_room.passcode_problem(change.new_passcode, change.passcode) is None
