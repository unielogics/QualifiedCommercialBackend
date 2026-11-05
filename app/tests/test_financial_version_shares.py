from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.bucket import BucketFile, BucketPublicShare
from app.routers.financial_workspace import FinancialVersionShareWrite
from app.services import financial_version_shares as shares


class Memory:
    def __init__(self, file):
        self.file, self.added = file, []

    async def get(self, model, key):
        return self.file if key == self.file.id else None

    def add(self, row):
        row.id = uuid4()
        self.added.append(row)

    async def flush(self):
        pass


def context():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    file = BucketFile(id=uuid4(), bucket_id=profile.primary_bucket_id, status="uploaded", deleted_at=None)
    family = SimpleNamespace(label="Business debt schedule", current_version_id=uuid4())
    version = SimpleNamespace(id=uuid4(), version_number=2, pdf_file_id=file.id, source_file_id=None)
    return Memory(file), profile, family, version, file


@pytest.mark.asyncio
async def test_share_is_pinned_to_one_verified_historical_pdf_with_expiry_and_no_write_capability():
    db, profile, family, version, file = context()
    with patch.object(shares.workspace, "version_for_profile", AsyncMock(return_value=(family, version))), \
         patch.object(shares.workspace, "version_pdf", AsyncMock(return_value=(b"verified", "v2.pdf"))) as verify, \
         patch("app.routers.buckets._public_url", side_effect=lambda path: "https://app.example.test" + path):
        result = await shares.create_share(db, profile, version.id)
    verify.assert_awaited_once_with(db, profile, version.id)
    share = db.added[0]
    assert isinstance(share, BucketPublicShare)
    assert share.files == [file] and share.can_preview and share.can_download
    assert result["version_id"] == str(version.id)
    assert result["file_id"] == str(file.id)
    assert "public-share" in result["url"]
    expiry = datetime.fromisoformat(result["expires_at"])
    assert datetime.now(UTC) + timedelta(days=6) < expiry < datetime.now(UTC) + timedelta(days=8)
    family.current_version_id = uuid4()
    assert share.files == [file]  # A later save never retargets the existing delivery.


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_bucket,deleted", [(True, False), (False, True)])
async def test_unavailable_or_cross_bucket_pdf_cannot_be_shared(wrong_bucket, deleted):
    db, profile, family, version, file = context()
    if wrong_bucket:
        file.bucket_id = uuid4()
    if deleted:
        file.deleted_at = datetime.now(UTC)
    with patch.object(shares.workspace, "version_for_profile", AsyncMock(return_value=(family, version))), \
         patch.object(shares.workspace, "version_pdf", AsyncMock(return_value=(b"verified", "v2.pdf"))):
        with pytest.raises(HTTPException) as error:
            await shares.create_share(db, profile, version.id)
    assert error.value.status_code == 404 and db.added == []


@pytest.mark.asyncio
async def test_changed_immutable_pdf_stops_share_creation_before_any_access_is_granted():
    db, profile, family, version, file = context()
    with patch.object(shares.workspace, "version_for_profile", AsyncMock(return_value=(family, version))), \
         patch.object(shares.workspace, "version_pdf", AsyncMock(side_effect=HTTPException(409, "PDF hash mismatch"))):
        with pytest.raises(HTTPException) as error:
            await shares.create_share(db, profile, version.id)
    assert error.value.status_code == 409 and db.added == []


@pytest.mark.parametrize("ttl", [0, -1, 31])
def test_financial_pdf_shares_require_a_bounded_expiration(ttl):
    with pytest.raises(ValidationError):
        FinancialVersionShareWrite(ttl_days=ttl)
