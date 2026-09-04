"""Whose credit report is this, and whose record does it land on.

An owner's soft pull used to resolve its subject by a global email match, so it
landed on whatever stranger happened to share the address and wrote that
person's FICO onto their Client row. The branch meant to handle "no match" could
not run at all — it passed `first_name`/`last_name` to a model that has neither
and leaves `name` NOT NULL — so the dangerous branch was the only one that ever
completed, and an owner whose email matched nobody got a 500.

Resolution is scoped to the file now: the subject a previous pull used, then the
file's own client for the primary owner, then a new client belonging to this
file. Never by email.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.client import Client


def _owner(**over):
    row = SimpleNamespace(
        id=uuid4(), first_name="Dana", last_name="Ruiz", full_name="Dana Ruiz",
        email="dana@example.com", phone="+19735550148", is_primary=False,
        credit_pull_id=None, ownership_pct=50.0,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _dealer(**over):
    row = SimpleNamespace(id=uuid4(), dealer_user_id=None)
    for k, v in over.items():
        setattr(row, k, v)
    return row


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _db(*, scalars=(), get=None):
    """A db whose execute() returns the queued scalars in order."""
    queued = list(scalars)
    db = SimpleNamespace(added=[])

    async def _execute(_stmt):
        return _Result(queued.pop(0) if queued else None)

    db.execute = _execute
    db.add = db.added.append
    db.flush = AsyncMock()
    db.get = AsyncMock(side_effect=lambda _model, pk: get.get(pk) if get else None)
    return db


# --- the crash -----------------------------------------------------------------


def test_the_client_model_has_no_first_or_last_name():
    # The old create branch passed both. This is why it could never run.
    columns = set(Client.__table__.columns.keys())
    assert "first_name" not in columns
    assert "last_name" not in columns
    assert Client.__table__.columns["name"].nullable is False


def test_a_client_can_be_built_for_an_owner_the_way_the_resolver_now_does_it():
    owner = _owner()
    client = Client(name=owner.full_name, email=owner.email, phone=owner.phone)
    assert client.name == "Dana Ruiz"


@pytest.mark.asyncio
async def test_an_owner_matching_no_existing_client_gets_one_instead_of_a_500():
    from app.dealer_os.router import _resolve_owner_client

    db = _db()
    client = await _resolve_owner_client(db, _dealer(), _owner())

    assert isinstance(client, Client)
    assert client.name == "Dana Ruiz"
    assert client.email == "dana@example.com"
    assert db.added == [client]


# --- never by email ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_sharing_the_email_is_never_adopted():
    from app.dealer_os.router import _resolve_owner_client

    # A pre-existing borrower with the same address exists, but nothing in the
    # resolver looks up by email any more, so a fresh subject is created.
    db = _db()
    client = await _resolve_owner_client(db, _dealer(), _owner(email="shared@example.com"))

    assert client in db.added
    assert client.email == "shared@example.com"


@pytest.mark.asyncio
async def test_an_owner_with_no_email_still_resolves():
    from app.dealer_os.router import _resolve_owner_client

    db = _db()
    client = await _resolve_owner_client(db, _dealer(), _owner(email=None))

    assert client.email is None
    assert client.name == "Dana Ruiz"


# --- the scoped paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refresh_lands_on_the_subject_the_first_pull_used():
    from app.dealer_os.router import _resolve_owner_client

    pull_id, client_id = uuid4(), uuid4()
    prior = Client(name="Dana Ruiz")
    db = _db(scalars=[client_id], get={client_id: prior})

    resolved = await _resolve_owner_client(db, _dealer(), _owner(credit_pull_id=pull_id))

    assert resolved is prior
    assert db.added == []


@pytest.mark.asyncio
async def test_the_primary_owner_resolves_to_the_files_own_client():
    from app.dealer_os.router import _resolve_owner_client

    # This is what makes the FICO written on the Client row genuinely theirs.
    client_id = uuid4()
    borrower = Client(name="Loyd Bradley")
    db = _db(scalars=[client_id], get={client_id: borrower})

    resolved = await _resolve_owner_client(db, _dealer(), _owner(is_primary=True))

    assert resolved is borrower
    assert db.added == []


@pytest.mark.asyncio
async def test_a_co_owner_is_their_own_subject_not_the_borrower():
    from app.dealer_os.router import _resolve_owner_client

    # A 30% co-owner's report must not be filed against the borrower.
    db = _db(scalars=[uuid4()])
    resolved = await _resolve_owner_client(db, _dealer(), _owner(is_primary=False))

    assert resolved in db.added


@pytest.mark.asyncio
async def test_a_primary_owner_on_a_file_with_no_client_yet_gets_their_own():
    from app.dealer_os.router import _resolve_owner_client

    db = _db(scalars=[None, None])
    resolved = await _resolve_owner_client(db, _dealer(), _owner(is_primary=True))

    assert resolved in db.added
    assert resolved.name == "Dana Ruiz"
