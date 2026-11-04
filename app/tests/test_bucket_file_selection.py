"""Multi-file selection in a room: download the checked files as one zip, or
move them to Removed files in one request."""

from __future__ import annotations

import inspect
import io
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.bucket import PROTECTED_RETENTION_CLASSES
from app.routers import buckets
from app.schemas.bucket import BucketFileSelection
from app.services import (
    ach_fee_workflow,
    application_profiles,
    bucket_file_archive,
    capital_readiness,
    retention_holds,
)

BUCKET_ID = uuid4()


def _file(**over):
    row = {
        "id": uuid4(),
        "bucket_id": BUCKET_ID,
        "file_name": "October statement.pdf",
        "s3_key": "uploads/october.pdf",
        "s3_version_id": None,
        "status": "uploaded",
        "deleted_at": None,
        "retention_class": "evidence",
        "content_hash": None,
        "size_bytes": 12,
        "created_at": datetime(2026, 11, 4, 15, 30, tzinfo=UTC),
        "requested_document_id": None,
        "shares": [object()],
        "public_shares": [object()],
        "vendor_access": [],
        "delete_storage_status": None,
        "deleted_by_user_id": None,
        "source_kind": None,
        "source_detail": None,
    }
    row.update(over)
    return SimpleNamespace(**row)


def _db(rows):
    result = Mock()
    result.scalars.return_value.all.return_value = rows
    return SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock(), commit=AsyncMock())


def _user():
    return SimpleNamespace(id=uuid4())


# --- archive layout -----------------------------------------------------------


def test_entry_names_are_flat_and_safe() -> None:
    assert bucket_file_archive.entry_name("../../etc/passwd", fallback="x") == "passwd"
    assert bucket_file_archive.entry_name("C:\\Users\\me\\..hidden.pdf", fallback="x") == "hidden.pdf"
    assert bucket_file_archive.entry_name("bad\x00name\x7f.pdf", fallback="x") == "badname.pdf"
    assert bucket_file_archive.entry_name("   ", fallback="file-1234") == "file-1234"


def test_repeated_names_get_a_counter_before_the_extension() -> None:
    names = bucket_file_archive.unique_entry_names(["a.pdf", "a.pdf", "A.PDF", "notes", "notes"])

    assert names == ["a.pdf", "a (2).pdf", "A (3).PDF", "notes", "notes (2)"]


def test_archive_file_name_is_the_room_name_plus_files() -> None:
    assert (
        bucket_file_archive.archive_file_name("Little Star Therapy Services Inc")
        == "Little Star Therapy Services Inc files.zip"
    )
    assert bucket_file_archive.archive_file_name("Café ☕ / Nov") == "Caf Nov files.zip"
    assert bucket_file_archive.archive_file_name(None) == "room files.zip"
    assert bucket_file_archive.archive_file_name(".. ..") == "room files.zip"


def test_build_archive_packs_each_file_under_its_name_with_its_upload_date() -> None:
    first = _file(file_name="Statement.pdf", size_bytes=5)
    second = _file(file_name="Statement.pdf", size_bytes=3)
    payload = {first.id: b"first", second.id: b"two"}

    result = bucket_file_archive.build_archive([first, second], lambda row: payload[row.id])

    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.namelist() == ["Statement.pdf", "Statement (2).pdf"]
        assert archive.read("Statement (2).pdf") == b"two"
        info = archive.getinfo("Statement.pdf")
        assert info.date_time == (2026, 11, 4, 15, 30, 0)
        assert info.external_attr >> 16 == 0o644
    assert result.entry_names == ("Statement.pdf", "Statement (2).pdf")
    assert (result.files_packaged, result.bytes_total) == (2, 8)


def test_build_archive_refuses_an_oversized_selection_before_reading(monkeypatch) -> None:
    monkeypatch.setattr(bucket_file_archive, "ARCHIVE_MAX_BYTES", 10)
    reader = Mock()

    with pytest.raises(bucket_file_archive.ArchiveTooLargeError, match="fewer files"):
        bucket_file_archive.build_archive([_file(size_bytes=8), _file(size_bytes=8)], reader)
    reader.assert_not_called()


def test_build_archive_refuses_when_the_bytes_outgrow_the_declared_sizes(monkeypatch) -> None:
    monkeypatch.setattr(bucket_file_archive, "ARCHIVE_MAX_BYTES", 10)

    with pytest.raises(bucket_file_archive.ArchiveTooLargeError):
        bucket_file_archive.build_archive([_file(size_bytes=1)], lambda row: b"x" * 11)


def test_build_archive_caps_the_file_count(monkeypatch) -> None:
    monkeypatch.setattr(bucket_file_archive, "ARCHIVE_MAX_FILES", 1)

    with pytest.raises(bucket_file_archive.ArchiveTooLargeError, match="at most 1 files"):
        bucket_file_archive.build_archive([_file(), _file()], lambda row: b"")


# --- routes -------------------------------------------------------------------


def test_selection_takes_one_to_two_hundred_ids() -> None:
    with pytest.raises(ValidationError):
        BucketFileSelection(file_ids=[])
    with pytest.raises(ValidationError):
        BucketFileSelection(file_ids=[uuid4() for _ in range(201)])
    assert len(BucketFileSelection(file_ids=[uuid4()]).file_ids) == 1


def test_selection_routes_are_registered() -> None:
    contract = {
        (route.path, method)
        for route in buckets.router.routes
        for method in getattr(route, "methods", set())
    }

    assert any(
        path.endswith("/admin/{bucket_id}/selected-files/archive") and method == "POST"
        for path, method in contract
    )
    assert any(
        path.endswith("/admin/{bucket_id}/selected-files/remove") and method == "POST"
        for path, method in contract
    )


def test_single_delete_shares_the_soft_delete_path_with_the_selection() -> None:
    single = inspect.getsource(buckets.delete_admin_file)
    bulk = inspect.getsource(buckets.remove_admin_files)

    assert "_soft_delete_file(" in single and "_soft_delete_file(" in bulk
    assert "is_effective_file_soft_deletion_prohibited" in single
    assert "is_effective_file_soft_deletion_prohibited" in bulk
    assert "advisory_recompute_profiles" in bulk


@pytest.fixture
def archive_storage(monkeypatch):
    objects: dict[str, bytes] = {}
    client = Mock()
    client.get_object.side_effect = lambda **params: {"Body": io.BytesIO(objects[params["Key"]])}
    monkeypatch.setattr(buckets, "_s3_client", lambda: client)
    monkeypatch.setattr(buckets, "_bucket_storage_config", lambda: ("documents", "uploads", "kms"))
    monkeypatch.setattr(
        buckets,
        "_load_bucket_or_404",
        AsyncMock(return_value=SimpleNamespace(name="Little Star Therapy Services Inc")),
    )
    monkeypatch.setattr(buckets, "_log", AsyncMock())
    return objects


async def test_archive_route_returns_one_zip_and_audits_each_file(archive_storage) -> None:
    first = _file(file_name="Statement.pdf", s3_key="uploads/one.pdf", size_bytes=3)
    second = _file(file_name="Statement.pdf", s3_key="uploads/two.pdf", s3_version_id="v2", size_bytes=3)
    archive_storage.update({"uploads/one.pdf": b"one", "uploads/two.pdf": b"two"})
    db = _db([second, first])

    response = await buckets.archive_admin_files(
        BUCKET_ID,
        BucketFileSelection(file_ids=[first.id, second.id]),
        request=Mock(),
        user=_user(),
        db=db,
    )

    assert response.media_type == "application/zip"
    assert "Little Star Therapy Services Inc files.zip" in response.headers["content-disposition"]
    assert response.headers["x-files-packaged"] == "2"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.namelist() == ["Statement.pdf", "Statement (2).pdf"]
        assert archive.read("Statement (2).pdf") == b"two"
    audit = buckets._log.await_args_list
    assert [call.args[2] for call in audit] == ["file_archive_downloaded", "file_archive_downloaded"]
    assert {call.kwargs["target_id"] for call in audit} == {str(first.id), str(second.id)}
    db.commit.assert_awaited_once()
    reads = [call.kwargs for call in buckets._s3_client().get_object.call_args_list]
    assert [params for params in reads if params.get("VersionId")] == [
        {"Bucket": "documents", "Key": "uploads/two.pdf", "VersionId": "v2"}
    ]


async def test_archive_route_refuses_when_a_checked_file_is_gone(archive_storage) -> None:
    present = _file(s3_key="uploads/one.pdf")
    removed = _file(deleted_at=datetime(2026, 11, 1, tzinfo=UTC))
    archive_storage["uploads/one.pdf"] = b"one"

    with pytest.raises(HTTPException) as refusal:
        await buckets.archive_admin_files(
            BUCKET_ID,
            BucketFileSelection(file_ids=[present.id, removed.id, uuid4()]),
            request=Mock(),
            user=_user(),
            db=_db([present, removed]),
        )

    assert refusal.value.status_code == 404
    assert "2 of the selected files are no longer in this room" in refusal.value.detail
    buckets._log.assert_not_awaited()


async def test_archive_route_words_an_oversized_selection(archive_storage, monkeypatch) -> None:
    monkeypatch.setattr(bucket_file_archive, "ARCHIVE_MAX_BYTES", 10)
    big = _file(size_bytes=11)

    with pytest.raises(HTTPException) as refusal:
        await buckets.archive_admin_files(
            BUCKET_ID, BucketFileSelection(file_ids=[big.id]), request=Mock(), user=_user(), db=_db([big])
        )

    assert refusal.value.status_code == 400
    assert "Download fewer files at a time" in refusal.value.detail


async def test_archive_route_packages_protected_artifacts_from_the_private_store(
    archive_storage, monkeypatch
) -> None:
    protected = _file(
        file_name="ACH authorization.pdf",
        retention_class=sorted(PROTECTED_RETENTION_CLASSES)[0],
        content_hash="abc",
        size_bytes=9,
    )
    monkeypatch.setattr(ach_fee_workflow, "verified_protected_bytes", AsyncMock(return_value=b"protected"))

    response = await buckets.archive_admin_files(
        BUCKET_ID,
        BucketFileSelection(file_ids=[protected.id]),
        request=Mock(),
        user=_user(),
        db=_db([protected]),
    )

    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.read("ACH authorization.pdf") == b"protected"
    buckets._s3_client().get_object.assert_not_called()


async def test_archive_route_refuses_a_protected_artifact_that_fails_integrity(
    archive_storage, monkeypatch
) -> None:
    protected = _file(
        file_name="ACH authorization.pdf",
        retention_class=sorted(PROTECTED_RETENTION_CLASSES)[0],
        content_hash="abc",
    )
    monkeypatch.setattr(ach_fee_workflow, "verified_protected_bytes", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as refusal:
        await buckets.archive_admin_files(
            BUCKET_ID,
            BucketFileSelection(file_ids=[protected.id]),
            request=Mock(),
            user=_user(),
            db=_db([protected]),
        )

    assert refusal.value.status_code == 409
    assert "ACH authorization.pdf failed its integrity check" in refusal.value.detail


def test_verified_protected_bytes_keeps_the_signed_link_rules() -> None:
    source = inspect.getsource(ach_fee_workflow.verified_protected_bytes)

    assert "retention_class not in RETENTION_CLASSES" in source
    assert "expected_sha256=file.content_hash" in source


@pytest.fixture
def removal_services(monkeypatch):
    profile = SimpleNamespace(id=uuid4())
    recompute = AsyncMock()
    monkeypatch.setattr(buckets, "_load_bucket_or_404", AsyncMock(return_value=SimpleNamespace(name="Room")))
    monkeypatch.setattr(buckets, "_log", AsyncMock())
    monkeypatch.setattr(buckets, "_recalculate_requested_document_status", AsyncMock())
    monkeypatch.setattr(application_profiles, "affected_profiles_for_file", AsyncMock(return_value=[profile]))
    monkeypatch.setattr(capital_readiness, "advisory_recompute_profiles", recompute)
    return SimpleNamespace(profile=profile, recompute=recompute)


async def test_remove_route_moves_each_removable_file_and_reports_the_rest(
    removal_services, monkeypatch
) -> None:
    kept = _file(file_name="Signed ACH.pdf")
    first = _file(file_name="Statement.pdf")
    second = _file(file_name="Tax return.pdf")
    already = _file(deleted_at=datetime(2026, 11, 1, tzinfo=UTC))
    monkeypatch.setattr(
        retention_holds,
        "is_effective_file_soft_deletion_prohibited",
        AsyncMock(side_effect=lambda db, file: file is kept),
    )
    db = _db([kept, first, second, already])
    gone = uuid4()

    result = await buckets.remove_admin_files(
        BUCKET_ID,
        BucketFileSelection(file_ids=[first.id, kept.id, second.id, already.id, gone]),
        request=Mock(),
        user=_user(),
        db=db,
    )

    assert result.removed == [first.id, second.id]
    assert [(item.file_id, item.reason) for item in result.blocked] == [
        (kept.id, "Protected by retention or legal hold")
    ]
    assert result.missing == [already.id, gone]
    assert first.delete_storage_status == "retained_soft_delete"
    assert first.deleted_at == second.deleted_at
    assert first.public_shares == [] and first.shares == []
    assert kept.deleted_at is None and kept.public_shares
    removal_services.recompute.assert_awaited_once()
    assert removal_services.recompute.await_args.args[1] == [removal_services.profile]
    assert removal_services.recompute.await_args.kwargs["event_key"].startswith(f"files-removed:{BUCKET_ID}:")
    assert [call.kwargs["target_id"] for call in buckets._log.await_args_list] == [str(first.id), str(second.id)]
    db.commit.assert_awaited_once()


async def test_remove_route_leaves_everything_when_every_file_is_protected(
    removal_services, monkeypatch
) -> None:
    kept = _file()
    monkeypatch.setattr(retention_holds, "is_effective_file_soft_deletion_prohibited", AsyncMock(return_value=True))

    result = await buckets.remove_admin_files(
        BUCKET_ID, BucketFileSelection(file_ids=[kept.id]), request=Mock(), user=_user(), db=_db([kept])
    )

    assert result.removed == []
    assert [item.file_id for item in result.blocked] == [kept.id]
    removal_services.recompute.assert_not_awaited()
    buckets._log.assert_not_awaited()
