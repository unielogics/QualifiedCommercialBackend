from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import buckets
from app.schemas.bucket import BucketSharedDownloadCreate


def _file(
    *,
    status: str = "uploaded",
    deleted: bool = False,
    storage_status: str | None = None,
    package: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        file_name="Executive Summary.pdf",
        s3_key=f"artifacts/{uuid4()}.pdf",
        status=status,
        deleted_at=datetime.now(UTC) if deleted else None,
        delete_storage_status=storage_status,
        source_kind="generated" if package else "client_room",
        source_detail=(
            "package_readiness:executive_summary:v1" if package else ""
        ),
    )


def test_selected_access_retains_superseded_package_but_not_manual_deletion() -> None:
    active = _file(package=False)
    superseded = _file(
        status="superseded",
        deleted=True,
        storage_status="retained_superseded",
    )
    manually_deleted = _file(
        deleted=True,
        storage_status="retained_package_artifact",
    )
    share = SimpleNamespace(files=[active, superseded, manually_deleted])
    access = SimpleNamespace(
        file_scope="selected",
        files=[active, superseded, manually_deleted],
        bucket=SimpleNamespace(files=[active, superseded, manually_deleted]),
    )

    assert buckets._file_belongs_to_share(share, superseded.id) is superseded
    assert buckets._file_belongs_to_share(share, manually_deleted.id) is None
    assert buckets._file_belongs_to_public_share(share, superseded.id) is superseded
    assert buckets._vendor_access_files(access) == [active, superseded]
    assert buckets._file_belongs_to_vendor_access(access, superseded.id) is superseded


def test_all_active_vendor_access_never_includes_superseded_package() -> None:
    active = _file(package=False)
    superseded = _file(
        status="superseded",
        deleted=True,
        storage_status="retained_superseded",
    )
    access = SimpleNamespace(
        file_scope="all_active",
        files=[superseded],
        bucket=SimpleNamespace(files=[active, superseded]),
    )

    assert buckets._vendor_access_files(access) == [active]
    assert buckets._file_belongs_to_vendor_access(access, superseded.id) is None


@pytest.mark.asyncio
async def test_passcode_share_can_download_selected_superseded_package() -> None:
    retained = _file(
        status="superseded",
        deleted=True,
        storage_status="retained_superseded",
    )
    share = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        recipient_name="Lender",
        recipient_email="lender@example.com",
        passcode_hash="stored-hash",
        can_download=True,
        download_count=0,
        files=[retained],
    )
    db = SimpleNamespace(commit=AsyncMock())

    with (
        patch.object(buckets, "_load_share_or_404", AsyncMock(return_value=share)),
        patch.object(buckets, "_verify_passcode", return_value=True),
        patch.object(buckets, "_download_url", return_value="https://example.test/file"),
        patch.object(buckets, "_log", AsyncMock()),
    ):
        result = await buckets.shared_file_download(
            "share-token",
            retained.id,
            BucketSharedDownloadCreate(passcode="123456"),
            SimpleNamespace(),
            db,
        )

    assert result.url == "https://example.test/file"
    assert share.download_count == 1
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_vendor_download_all_active_rejects_superseded_package() -> None:
    retained = _file(
        status="superseded",
        deleted=True,
        storage_status="retained_superseded",
    )
    access = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_scope="all_active",
        bucket=SimpleNamespace(files=[retained]),
        files=[retained],
        can_download=True,
        download_count=0,
    )
    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(name="Vendor", email="vendor@example.com")

    with (
        patch.object(
            buckets,
            "_load_vendor_access_or_404",
            AsyncMock(return_value=access),
        ),
        patch.object(buckets, "_log", AsyncMock()),
        pytest.raises(HTTPException) as caught,
    ):
        await buckets.vendor_file_download(
            access.bucket_id,
            retained.id,
            SimpleNamespace(),
            user,
            db,
        )

    assert caught.value.status_code == 404
    assert access.download_count == 0
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_vendor_download_selected_allows_superseded_package() -> None:
    retained = _file(
        status="superseded",
        deleted=True,
        storage_status="retained_superseded",
    )
    access = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_scope="selected",
        bucket=SimpleNamespace(files=[]),
        files=[retained],
        can_download=True,
        download_count=0,
    )
    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(name="Vendor", email="vendor@example.com")

    with (
        patch.object(
            buckets,
            "_load_vendor_access_or_404",
            AsyncMock(return_value=access),
        ),
        patch.object(buckets, "_download_url", return_value="https://example.test/file"),
        patch.object(buckets, "_log", AsyncMock()),
    ):
        result = await buckets.vendor_file_download(
            access.bucket_id,
            retained.id,
            SimpleNamespace(),
            user,
            db,
        )

    assert result.url == "https://example.test/file"
    assert access.download_count == 1
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_access_loaders_apply_selected_and_active_relationship_filters() -> None:
    share = SimpleNamespace(status="active", expires_at=None)
    public_share = SimpleNamespace(status="active", expires_at=None)
    vendor_access = SimpleNamespace(
        status="active",
        expires_at=None,
        bucket=SimpleNamespace(archived_at=None),
    )
    user = SimpleNamespace(id=uuid4())

    share_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: share)
        )
    )
    with patch.object(
        buckets,
        "_selected_access_file_clause",
        wraps=buckets._selected_access_file_clause,
    ) as selected_clause:
        assert await buckets._load_share_or_404(share_db, "share-token") is share
    selected_clause.assert_called_once_with()

    public_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: public_share)
        )
    )
    with patch.object(
        buckets,
        "_selected_access_file_clause",
        wraps=buckets._selected_access_file_clause,
    ) as selected_clause:
        assert (
            await buckets._load_public_share_or_404(public_db, "public-token")
            is public_share
        )
    selected_clause.assert_called_once_with()

    vendor_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: vendor_access)
        )
    )
    with (
        patch.object(
            buckets,
            "_selected_access_file_clause",
            wraps=buckets._selected_access_file_clause,
        ) as selected_clause,
        patch.object(
            buckets,
            "_active_access_file_clause",
            wraps=buckets._active_access_file_clause,
        ) as active_clause,
    ):
        assert (
            await buckets._load_vendor_access_or_404(
                vendor_db, uuid4(), user
            )
            is vendor_access
        )
    selected_clause.assert_called_once_with()
    assert active_clause.call_count >= 2

    list_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        )
    )
    with (
        patch.object(
            buckets,
            "_selected_access_file_clause",
            wraps=buckets._selected_access_file_clause,
        ) as selected_clause,
        patch.object(
            buckets,
            "_active_access_file_clause",
            wraps=buckets._active_access_file_clause,
        ) as active_clause,
    ):
        assert await buckets.list_vendor_buckets(user, list_db) == []
    selected_clause.assert_called_once_with()
    assert active_clause.call_count >= 2
