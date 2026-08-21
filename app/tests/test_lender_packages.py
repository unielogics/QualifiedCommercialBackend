from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

# Stand in for PyJWT only when it is genuinely absent.
#
# This was an unconditional sys.modules.setdefault, which meant that whenever
# this module happened to be collected before a test needing the REAL jwt, that
# test silently received a stub carrying four functions. Whether it broke was
# decided by collection order, which is the worst kind of test failure: it looks
# like a bug in whatever ran second. PyJWT is a real dependency, so the stub is
# now a fallback rather than a fixture.
try:  # pragma: no cover - depends on what is installed
    import jwt  # noqa: F401
except ImportError:  # pragma: no cover
    jwt_stub = ModuleType("jwt")
    jwt_stub.algorithms = SimpleNamespace(
        RSAAlgorithm=SimpleNamespace(from_jwk=lambda *_args, **_kwargs: None)
    )
    jwt_stub.PyJWTError = Exception
    jwt_stub.get_unverified_header = lambda *_args, **_kwargs: {}
    jwt_stub.decode = lambda *_args, **_kwargs: {}
    sys.modules["jwt"] = jwt_stub

activity_log_stub = ModuleType("app.services.activity_log")

async def _mark_loan_dirty(*_args, **_kwargs):
    return None

activity_log_stub.mark_loan_dirty = _mark_loan_dirty
sys.modules.setdefault("app.services.activity_log", activity_log_stub)

lender_connect_stub = ModuleType("app.services.lender_connect")
lender_connect_stub.LenderConnectError = ValueError

async def _connect_lender(*_args, **_kwargs):
    return None

lender_connect_stub.connect_lender = _connect_lender
sys.modules.setdefault("app.services.lender_connect", lender_connect_stub)

from app.routers.lender_packages import (
    _assert_package_open,
    _effective_recipient_status,
    _term_value_fields,
)
from app.schemas.lender_package import LenderTermManualCreate


def test_effective_recipient_status_blocks_revoked_and_expired_packages():
    recipient = SimpleNamespace(status="sent")
    active = SimpleNamespace(status="active", revoked_at=None, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    expired = SimpleNamespace(status="active", revoked_at=None, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    revoked = SimpleNamespace(status="revoked", revoked_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=1))

    assert _effective_recipient_status(active, recipient) == "sent"
    assert _effective_recipient_status(expired, recipient) == "expired"
    assert _effective_recipient_status(revoked, recipient) == "revoked"


def test_assert_package_open_raises_for_closed_access():
    expired = SimpleNamespace(status="active", revoked_at=None, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    revoked = SimpleNamespace(status="revoked", revoked_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=1))

    with pytest.raises(HTTPException):
        _assert_package_open(expired)
    with pytest.raises(HTTPException):
        _assert_package_open(revoked)


def test_manual_terms_constructor_filters_metadata_fields():
    payload = LenderTermManualCreate(
        lender_id=uuid4(),
        package_recipient_id=uuid4(),
        source="email",
        status="received",
        approved_amount=1_250_000,
        final_rate=0.1125,
        notes="Sent by email",
    )

    fields = _term_value_fields(payload)

    assert fields == {
        "approved_amount": 1_250_000,
        "final_rate": 0.1125,
        "notes": "Sent by email",
    }
