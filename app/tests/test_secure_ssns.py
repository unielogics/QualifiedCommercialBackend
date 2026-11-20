from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.models.bucket import BucketUploadLink
from app.models.secure_ssn import SecureSsn, SecureSsnAccess
from app.routers import buckets
from app.routers import secure_ssns as routes
from app.services import client_forms, form_link_codes, secure_ssns
from app.tests._sqlite_schema import sqlite_session, sqlite_template
from app.tests.test_re_application import _heloc_file


class EncryptedStorageDouble:
    def __init__(self):
        self.objects = {}
        self.encryption = "aws:kms"

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"ServerSideEncryption": self.encryption, "SSEKMSKeyId": "test-only-kms",
                "Body": BytesIO(self.objects[Bucket, Key])}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, ServerSideEncryption, SSEKMSKeyId):
        assert Key.startswith("private-identifiers/keys/v1/")
        assert IfNoneMatch == "*" and ServerSideEncryption == "aws:kms" and SSEKMSKeyId == "test-only-kms"
        assert (Bucket, Key) not in self.objects and len(Body) == 32
        self.objects[Bucket, Key] = Body


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "secure_ssns")


@pytest.fixture
async def fixture(template, tmp_path, monkeypatch):
    async with sqlite_session(template, tmp_path) as db:
        settings = get_settings()
        monkeypatch.setattr(settings, "clerk_secret_key", "test-only-clerk")
        monkeypatch.setattr(settings, "s3_bucket", "private-test")
        monkeypatch.setattr(settings, "buckets_kms_key_id", "test-only-kms")
        kms = EncryptedStorageDouble()
        monkeypatch.setattr(secure_ssns, "_s3_client", lambda: kms)
        intake, profile = await _heloc_file(db)
        link = BucketUploadLink(bucket_id=profile.primary_bucket_id, recipient_name="Jordan Sample",
                                recipient_email=intake.email, token="synthetic-room", passcode_hash=buckets._hash_passcode("000019"),
                                passcode_set_by_client_at=datetime.now(UTC))
        db.add(link)
        await db.commit()
        user = SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4())
        app = FastAPI()
        app.include_router(routes.router)
        async def test_db():
            try:
                yield db
            except Exception:
                await db.rollback()
                raise
        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_current_user] = lambda: user
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as client:
            yield SimpleNamespace(db=db, client=client, profile=profile, intake=intake, link=link, user=user, kms=kms)


def batch():
    # Impossible area numbers, never real SSNs.
    return {"authorized": True, "entries": [
        {"id": str(uuid4()), "person_name": "Jordan Sample", "ssn": "000-00-0011"},
        {"id": str(uuid4()), "person_name": "Alex Sample", "ssn": "000000022"},
    ]}


PIN = {"X-Room-Pin": "000019"}
ROOM = "/secure-ssns/room/synthetic-room"


async def test_room_receipts_storage_retry_and_audited_staff_reveal(fixture, monkeypatch):
    f = fixture
    data = batch()
    pid = f.profile.id
    result = await f.client.post(ROOM, json=data, headers=PIN)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "private, no-store"
    rows = result.json()
    assert [r["last_four"] for r in rows] == ["0011", "0022"]
    assert all(set(row) == {"id", "person_name", "last_four"} for row in rows)
    stored = (await f.db.scalars(select(SecureSsn.ciphertext))).all()
    assert len(stored) == 2 and all(len(value) > 9 for value in stored)
    assert all(b"0000000" not in value for value in stored)
    assert (await f.client.post(ROOM, json=data, headers=PIN)).status_code == 409
    assert len((await f.client.post(ROOM, json={}, headers=PIN)).json()) == 2
    row_id = rows[0]["id"]
    path = f"/secure-ssns/files/{pid}/{row_id}/reveal"
    f.user.role = Role.CLIENT
    assert (await f.client.post(path)).status_code == 403
    f.user.role = Role.LOAN_EXEC
    assert (await f.client.post(f"/secure-ssns/files/{uuid4()}/{row_id}/reveal")).status_code == 404
    assert await f.db.scalar(select(func.count()).select_from(SecureSsnAccess)) == 0
    r = await f.client.post(path)
    assert r.headers["cache-control"] == "private, no-store"
    assert r.json() == {"ssn": "000000011"}
    event = await f.db.scalar(select(SecureSsnAccess))
    assert (event.profile_id, str(event.entry_id), event.user_id) == (pid, row_id, f.user.id)
    monkeypatch.setattr(get_settings(), "clerk_secret_key", "")
    assert (await f.client.post(path)).status_code == 503


async def test_invalid_or_partial_submissions_do_not_disclose_or_persist(fixture, monkeypatch):
    f = fixture
    data = batch()
    assert (await f.client.post(ROOM, json=data)).status_code == 422
    assert (await f.client.post(ROOM, headers={"X-Room-Pin": "000011"}, json=data)).status_code == 403
    assert (await f.client.post(ROOM, headers=PIN, json={**data, "authorized": False})).status_code == 422
    for invalid in [{"entries": "000000011"}, {**data, "unexpected": "000000011"}]:
        r = await f.client.post(ROOM, json=invalid, headers=PIN)
        assert r.status_code == 422 and "000000011" not in r.text
    f.kms.encryption = "AES256"
    assert (await f.client.post(ROOM, json=data, headers=PIN)).status_code == 503
    assert await f.db.scalar(select(func.count()).select_from(SecureSsn)) == 0
    monkeypatch.setattr(get_settings(), "buckets_kms_key_id", "")
    assert (await f.client.post(ROOM, json=data, headers=PIN)).status_code == 503
    assert len(f.kms.objects) == 1


async def test_account_portal_is_limited_to_its_own_intake(fixture):
    f = fixture
    intake_id = f.intake.id
    f.user.role = Role.CLIENT
    f.user.client = SimpleNamespace(id=uuid4())
    f.intake.client_id = f.user.client.id
    f.intake.bucket_upload_link_id = f.link.id
    await f.db.commit()
    path = f"/secure-ssns/account/{intake_id}"
    assert (await f.client.post(path, json=batch())).status_code == 200
    f.user.client = SimpleNamespace(id=uuid4())
    assert (await f.client.post(path, json={})).status_code == 404


async def test_room_identity_and_form_link_authority(fixture):
    f = fixture
    link, token = await client_forms.standing_link(f.db, f.profile)
    await f.db.commit()
    url = f"/secure-ssns/application/{token}"
    handoff, _ = form_link_codes.issue_session(link)
    code = form_link_codes.new_code(link)
    verified, _ = form_link_codes.verify_code(link, code)
    assert (await f.client.post(url, json={}, headers={"X-Form-Session": handoff})).status_code == 403
    assert (await f.client.post(url, json={}, headers={"X-Form-Session": verified})).status_code == 200
    assert (await f.client.post(url, json={})).status_code == 401
    # An assistant's separate invitation with a valid PIN confers upload access only.
    room = await f.db.scalar(select(BucketUploadLink).where(BucketUploadLink.token == "synthetic-room"))
    room.recipient_email = "assistant@example.com"
    await f.db.commit()
    assert (await f.client.post(ROOM, headers=PIN, json=batch())).status_code == 403
