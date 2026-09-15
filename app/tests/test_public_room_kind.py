from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.routers.buckets import _request_room_kind
from app.schemas.bucket import BucketRequestAccessRead


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (SimpleNamespace(dealer_id=None), "application"),
        (SimpleNamespace(dealer_id=uuid4()), "dealer"),
    ],
)
async def test_profile_resolves_room_kind_without_legacy_probe(
    profile: SimpleNamespace,
    expected: str,
) -> None:
    db = SimpleNamespace(execute=AsyncMock())

    assert await _request_room_kind(db, uuid4(), profile) == expected
    db.execute.assert_not_awaited()


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object | None:
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dealer_id", "expected"),
    [(uuid4(), "dealer"), (None, "basic")],
)
async def test_legacy_bucket_room_kind_uses_dealer_lookup(
    dealer_id: object | None,
    expected: str,
) -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult(dealer_id)))

    assert await _request_room_kind(db, uuid4(), None) == expected
    db.execute.assert_awaited_once()


def test_public_access_contract_carries_resolved_room_kind() -> None:
    payload = {
        "room_kind": "application",
        "bucket": {"name": "Example LLC"},
        "recipient_name": "Client",
        "recipient_email": None,
        "allow_notes": True,
        "requested_documents": [],
    }

    assert BucketRequestAccessRead.model_validate(payload).room_kind == "application"
