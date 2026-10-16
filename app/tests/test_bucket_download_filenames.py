from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from app.routers import buckets
from app.schemas.bucket import BucketSharedDownloadCreate


@pytest.fixture
def storage(monkeypatch):
    client = Mock()
    client.generate_presigned_url.side_effect = (
        lambda operation, *, Params, ExpiresIn:
        "https://storage.example.test/object?" + urlencode(Params)
    )
    monkeypatch.setattr(buckets, "_s3_client", lambda: client)
    monkeypatch.setattr(
        buckets, "_bucket_storage_config", lambda: ("documents", "uploads", "kms")
    )
    return client


def _disposition(url: str) -> str:
    return parse_qs(urlsplit(url).query)["ResponseContentDisposition"][0]


def _filename(url: str) -> str:
    return unquote(_disposition(url).split("filename*=UTF-8''", 1)[1])


@pytest.mark.parametrize(
    "name",
    [
        "October Bank Statement (final).pdf",
        'Client "final" résumé 2026.pdf',
        "财务报表.xlsx",
        "Sales #1; 100% + fees.csv",
    ],
)
def test_download_uses_original_filename_instead_of_storage_key(storage, name):
    url = buckets._download_url(
        "uploads/uuid-October_Bank_Statement.pdf",
        file_name=name,
        disposition="attachment",
        version_id="original-version",
    )

    assert _filename(url) == name
    assert _disposition(url).startswith('attachment; filename="')
    assert _disposition(url).isascii()
    params = storage.generate_presigned_url.call_args.kwargs["Params"]
    assert params["Key"] == "uploads/uuid-October_Bank_Statement.pdf"
    assert params["VersionId"] == "original-version"
    assert storage.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 900


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("../../Statement.pdf", "Statement.pdf"),
        (r"C:\fakepath\Statement.pdf", "Statement.pdf"),
        ("Statement\r\nInjected:\x00.pdf", "StatementInjected:.pdf"),
        ("", "document"),
    ],
)
def test_download_name_cannot_contain_paths_or_header_controls(storage, supplied, expected):
    url = buckets._download_url("uploads/object", file_name=supplied, disposition="attachment")

    assert _filename(url) == expected
    assert "\r" not in _disposition(url)
    assert "\n" not in _disposition(url)
    assert "\x00" not in _disposition(url)


def test_s3_signed_url_round_trips_unicode_filename(storage, monkeypatch):
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="test",
        aws_secret_access_key="test", config=Config(signature_version="s3v4"),
    )
    monkeypatch.setattr(buckets, "_s3_client", lambda: client)
    url = buckets._download_url(
        "uploads/internal-uuid.pdf", file_name='Résumé "final" #1.pdf',
        disposition="attachment",
    )

    query = parse_qs(urlsplit(url).query)
    header = query["response-content-disposition"][0]
    assert unquote(header.split("filename*=UTF-8''", 1)[1]) == 'Résumé "final" #1.pdf'
    assert 'filename="Rsum \\"final\\" #1.pdf"' in header
    assert "X-Amz-Signature" in query


@pytest.mark.parametrize(
    ("content_type", "expected_disposition"),
    [("application/pdf", "inline"), ("image/svg+xml", "attachment")],
)
def test_preview_preserves_filename_and_existing_content_type_protection(
    storage, content_type, expected_disposition
):
    url = buckets._download_url(
        "uploads/random-id.pdf",
        file_name="Original upload.pdf",
        content_type=content_type,
    )

    assert _filename(url) == "Original upload.pdf"
    assert _disposition(url).startswith(expected_disposition + ";")
    params = storage.generate_presigned_url.call_args.kwargs["Params"]
    if expected_disposition == "attachment":
        assert params["ResponseContentType"] == "application/octet-stream"
    else:
        assert "ResponseContentType" not in params


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["admin", "vendor", "public-share", "passcode-share"])
async def test_bucket_download_routes_keep_uploaded_filename(storage, route):
    file = SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), file_name="Client Résumé (October).pdf",
        s3_key="uploads/internal-uuid-Client_Resume.pdf", s3_version_id="v1",
        content_type="application/pdf", status="uploaded", deleted_at=None,
        retention_class=None,
    )
    access = SimpleNamespace(
        id=uuid4(), bucket_id=file.bucket_id, files=[file], can_download=True,
        download_count=0, recipient_name="Recipient", recipient_email="recipient@example.test",
        passcode_hash="hash", file_scope="selected",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=file), commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), name="Operator", email="operator@example.test")
    request = SimpleNamespace()
    with (
        patch.object(buckets, "_log", AsyncMock()),
        patch.object(buckets, "_load_vendor_access_or_404", AsyncMock(return_value=access)),
        patch.object(buckets, "_load_public_share_or_404", AsyncMock(return_value=access)),
        patch.object(buckets, "_load_share_or_404", AsyncMock(return_value=access)),
        patch.object(buckets, "_verify_passcode", return_value=True),
    ):
        if route == "admin":
            result = await buckets.admin_file_url(
                file.bucket_id, file.id, request, download=True, user=user, db=db
            )
        elif route == "vendor":
            result = await buckets.vendor_file_download(file.bucket_id, file.id, request, user, db)
        elif route == "public-share":
            result = await buckets.public_share_file_download("token", file.id, request, db)
        else:
            result = await buckets.shared_file_download(
                "token", file.id, BucketSharedDownloadCreate(passcode="123456"), request, db
            )

    assert _filename(result.url) == file.file_name
    assert _disposition(result.url).startswith("attachment;")
    db.commit.assert_awaited_once()
