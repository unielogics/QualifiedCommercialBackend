"""Settings → company signature on file: the two routes over the
stored_signatures service (adopt the letterhead image as Qualified
Commercial's signature; read the live adoption)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import settings as settings_router
from app.services import stored_signatures as sigs

LETTERHEAD_KEY = "firm_settings/letterhead_signature.png"


def _user(role: Role = Role.SUPER_ADMIN, **kw) -> SimpleNamespace:
    return SimpleNamespace(**{"id": uuid.uuid4(), "name": "Denny Matos", "email": "denny@example.com", "role": role, **kw})


def _request(ip: str = "203.0.113.9") -> SimpleNamespace:
    return SimpleNamespace(headers={"x-forwarded-for": ip, "user-agent": "pytest/1.0"}, client=None)


def _settings_row(signature_key: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), singleton=True, data={"letterhead": {"signature_s3_key": signature_key}})


class _Db:
    """Just enough AsyncSession for the two routes: the settings singleton
    query answers `settings`, the live-signature query answers `live`,
    add() collects rows, flush() stamps ids the way the insert would."""

    def __init__(self, settings: SimpleNamespace, live=None):
        self.settings = settings
        self.live = live
        self.added: list = []
        self.flushes = 0
        self.execute = AsyncMock(side_effect=self._execute)
        self.refresh = AsyncMock()

    async def _execute(self, q):
        sql = str(q)
        if "app_settings" in sql:
            row = self.settings
        else:
            row = self.live if (self.live is not None and self.live.revoked_at is None) else None
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushes += 1
        for row in self.added:
            if getattr(row, "id", None) is None and hasattr(row, "subject_type"):
                row.id = uuid.uuid4()


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


async def test_read_is_empty_until_adopted_and_reports_the_letterhead_precondition():
    db = _Db(_settings_row(None))
    state = await settings_router.get_company_signature(_user(Role.LOAN_EXEC), db)
    assert state.signature is None
    assert state.letterhead_signature_present is False
    assert state.authorization_text == sigs.COMPANY_SIGNATURE_AUTHORIZATION_TEXT
    assert state.authorization_version == sigs.COMPANY_SIGNATURE_AUTHORIZATION_VERSION

    db = _Db(_settings_row(LETTERHEAD_KEY))
    state = await settings_router.get_company_signature(_user(Role.LOAN_EXEC), db)
    assert state.signature is None and state.letterhead_signature_present is True


async def test_read_presigns_the_live_adoption():
    now = datetime.now(UTC)
    live = SimpleNamespace(
        id=uuid.uuid4(), subject_type="qc", subject_id=None, typed_name="Denny Matos", title="Chief Executive Officer",
        source="letterhead", adopted_at=now, adopted_by_user_id=uuid.uuid4(),
        adoption_consent_version=sigs.COMPANY_SIGNATURE_AUTHORIZATION_VERSION, revoked_at=None,
        signature_s3_key=LETTERHEAD_KEY, signature_sha256="0" * 64,
    )
    db = _Db(_settings_row(LETTERHEAD_KEY), live=live)
    with patch.object(sigs, "presign_private_s3_object", return_value="https://s3/presigned") as presign:
        state = await settings_router.get_company_signature(_user(Role.LOAN_EXEC), db)
    presign.assert_called_once_with(LETTERHEAD_KEY, ttl_seconds=900)
    assert state.signature is not None
    assert state.signature.typed_name == "Denny Matos" and state.signature.title == "Chief Executive Officer"
    assert state.signature.source == "letterhead" and state.signature.preview_url == "https://s3/presigned"
    assert state.signature.adopted_at == now


@pytest.mark.parametrize("role", [Role.LOAN_EXEC, Role.BROKER, Role.FIELD_REP, Role.DEALER_PARTNER])
async def test_adopt_requires_super_admin(role: Role):
    body = settings_router.CompanySignatureAdoptBody(typed_name="Denny Matos", title="CEO")
    with pytest.raises(HTTPException) as exc:
        await settings_router.adopt_company_signature(body, _request(), _user(role), _Db(_settings_row(LETTERHEAD_KEY)))
    assert exc.value.status_code == 403


async def test_adopt_refuses_without_a_saved_letterhead_signature():
    body = settings_router.CompanySignatureAdoptBody(typed_name="Denny Matos", title="CEO")
    db = _Db(_settings_row(None))
    with patch.object(sigs.storage, "get_bytes") as get:
        with pytest.raises(HTTPException) as exc:
            await settings_router.adopt_company_signature(body, _request(), _user(), db)
    get.assert_not_called()
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "letterhead_signature_missing"
    assert db.added == []


async def test_adopt_hashes_the_letterhead_image_and_records_the_officer():
    admin = _user()
    png = _png_bytes()
    body = settings_router.CompanySignatureAdoptBody(typed_name="  Denny   Matos ", title="Chief Executive Officer")
    db = _Db(_settings_row(LETTERHEAD_KEY))
    with patch.object(sigs.storage, "get_bytes", return_value=png) as get, \
            patch.object(sigs, "presign_private_s3_object", return_value="https://s3/presigned"):
        state = await settings_router.adopt_company_signature(body, _request("198.51.100.4"), admin, db)
    get.assert_called_once_with(LETTERHEAD_KEY)

    rows = [r for r in db.added if getattr(r, "subject_type", None) == "qc"]
    assert len(rows) == 1
    row = rows[0]
    assert row.subject_id is None and row.source == "letterhead"
    assert row.signature_s3_key == LETTERHEAD_KEY
    assert row.signature_sha256 == hashlib.sha256(png).hexdigest()
    assert row.typed_name == "Denny Matos" and row.title == "Chief Executive Officer"
    assert row.adopted_by_user_id == admin.id and row.adopted_ip == "198.51.100.4"
    assert row.adoption_consent_version == sigs.COMPANY_SIGNATURE_AUTHORIZATION_VERSION
    assert row.revoked_at is None

    activities = [r for r in db.added if getattr(r, "kind", None) == "settings.company_signature_adopted"]
    assert len(activities) == 1
    assert activities[0].actor_id == admin.id
    assert activities[0].payload["signature_s3_key"] == LETTERHEAD_KEY
    assert activities[0].payload["signature_sha256"] == row.signature_sha256

    assert state.signature is not None and state.signature.id == row.id
    assert state.signature.typed_name == "Denny Matos" and state.signature.preview_url == "https://s3/presigned"
    assert state.letterhead_signature_present is True
    assert db.flushes >= 2, "the adoption flush and the activity flush"


async def test_readopting_retires_the_previous_company_signature():
    admin = _user()
    previous = SimpleNamespace(id=uuid.uuid4(), revoked_at=None, revoked_by_user_id=None)
    db = _Db(_settings_row(LETTERHEAD_KEY), live=previous)
    body = settings_router.CompanySignatureAdoptBody(typed_name="Denny Matos", title="CEO")
    with patch.object(sigs.storage, "get_bytes", return_value=_png_bytes()), \
            patch.object(sigs, "presign_private_s3_object", return_value=None):
        state = await settings_router.adopt_company_signature(body, _request(), admin, db)
    assert previous.revoked_at is not None and previous.revoked_by_user_id == admin.id
    assert state.signature is not None and state.signature.id != previous.id


async def test_adopt_surfaces_a_storage_outage():
    body = settings_router.CompanySignatureAdoptBody(typed_name="Denny Matos", title="CEO")
    db = _Db(_settings_row(LETTERHEAD_KEY))
    with patch.object(sigs.storage, "get_bytes", return_value=None):
        with pytest.raises(HTTPException) as exc:
            await settings_router.adopt_company_signature(body, _request(), _user(), db)
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "storage_unavailable"
    assert db.added == []
