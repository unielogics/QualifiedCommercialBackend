from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.bucket import BucketFile
from app.routers import buckets
from app.services import upload_validation


def test_unversioned_staging_put_replay_cannot_change_sealed_version_bytes():
    storage = {"staging/upload": b"payoff,70000"}

    def copy_object(**kwargs):
        storage[kwargs["Key"]] = bytes(storage[kwargs["CopySource"]["Key"]])
        return {}

    client = SimpleNamespace(copy_object=Mock(side_effect=copy_object))
    cfg = SimpleNamespace(s3_bucket="evidence", buckets_s3_prefix="docs", buckets_kms_key_id="kms")
    with patch.object(upload_validation, "get_settings", return_value=cfg), patch.object(upload_validation, "_s3_client", return_value=client):
        sealed, version = upload_validation._seal_s3_upload_sync("staging/upload", file_id=uuid4())
    storage["staging/upload"] = b"payoff,999999"
    assert sealed != "staging/upload" and "/immutable-documents/" in sealed
    assert storage[sealed] == b"payoff,70000" and version is None
    assert client.copy_object.call_args.kwargs["SSEKMSKeyId"] == "kms"


def test_validation_reads_the_exact_s3_version_inspected_by_head():
    client = SimpleNamespace(
        head_object=Mock(return_value={"ContentLength": 12, "VersionId": "snapshot-1"}),
        get_object=Mock(return_value={"Body": BytesIO(b"payoff,70000"), "ContentType": "text/csv"}),
    )
    with patch.object(upload_validation, "get_settings", return_value=SimpleNamespace(s3_bucket="evidence")), patch.object(upload_validation, "_s3_client", return_value=client):
        assert upload_validation._validate_s3_pdf_upload_sync(s3_key="sealed", file_name="payoff.csv") == "snapshot-1"
    assert client.get_object.call_args.kwargs["VersionId"] == "snapshot-1"


@pytest.mark.asyncio
async def test_seal_or_validation_failure_does_not_publish_a_different_binary_and_retry_is_safe():
    original_key = "staging/upload"
    file = BucketFile(id=uuid4(), bucket_id=uuid4(), document_family_id=uuid4(), s3_key=original_key,
                      file_name="payoff.pdf", content_type="application/pdf", size_bytes=100, status="uploading")
    seal = AsyncMock(return_value=("immutable/snapshot", "snapshot-1"))
    validate = AsyncMock(side_effect=upload_validation.UploadInspectionUnavailable("unavailable"))
    discard = AsyncMock(return_value=True)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    with patch.object(upload_validation, "seal_s3_upload", seal), patch.object(upload_validation, "validate_s3_pdf_upload", validate), patch.object(upload_validation, "discard_s3_upload", discard):
        with pytest.raises(HTTPException) as error:
            await buckets._validate_completed_bucket_file(db, file, None, action="test")
        assert error.value.status_code == 503
        assert file.s3_key == original_key and file.s3_version_id is None and file.status == "uploading"
        discard.assert_awaited_once_with("immutable/snapshot")
        validate.side_effect = None
        validate.return_value = "snapshot-1"
        await buckets._validate_completed_bucket_file(db, file, None, action="test")
    assert file.s3_key == "immutable/snapshot" and file.s3_version_id == "snapshot-1"
    assert file.document_version_status == "pending" and not file.is_current_document_version
