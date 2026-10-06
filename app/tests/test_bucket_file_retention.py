from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.bucket import PROTECTED_RETENTION_CLASSES, BucketFile
from app.routers import buckets


def test_bucket_file_delete_retains_storage_for_recovery() -> None:
    source = inspect.getsource(buckets.delete_admin_file)
    assert 'file.delete_storage_status = "retained_soft_delete"' in source
    assert "_delete_s3_object" not in source
    assert "selectinload(BucketFile.public_shares)" in source
    assert "file.public_shares.clear()" in source


def test_bucket_archive_does_not_delete_or_reject_protected_files() -> None:
    source = inspect.getsource(buckets.delete_bucket)

    assert "bucket.archived_at" in source
    assert "is_deletion_prohibited" not in source
    assert "_delete_s3_object" not in source


def test_deleted_file_list_and_restore_routes_are_registered() -> None:
    contract = {
        (route.path, method)
        for route in buckets.router.routes
        for method in getattr(route, "methods", set())
    }
    assert any(path.endswith("/admin/{bucket_id}/deleted-files") and method == "GET" for path, method in contract)
    assert any(path.endswith("/admin/{bucket_id}/files/{file_id}/restore") and method == "POST" for path, method in contract)


def test_restore_rejects_rows_whose_storage_was_already_removed() -> None:
    source = inspect.getsource(buckets.restore_admin_file)
    assert "retained_soft_delete" in source
    assert '"delete_failed"' not in source
    assert "predates recovery retention" in source


def test_bucket_file_legal_hold_always_blocks_deletion() -> None:
    row = BucketFile(legal_hold=True)

    assert row.is_deletion_prohibited(
        at=datetime(2035, 1, 1, tzinfo=UTC)
    )


def test_bucket_file_future_protection_blocks_deletion() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    row = BucketFile(legal_hold=False, protected_until=now + timedelta(days=730))

    assert row.is_deletion_prohibited(at=now)


def test_bucket_file_expired_protection_allows_deletion() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    row = BucketFile(legal_hold=False, protected_until=now - timedelta(seconds=1))

    assert not row.is_deletion_prohibited(at=now)


def test_bucket_file_treats_naive_legacy_timestamp_as_utc() -> None:
    row = BucketFile(
        legal_hold=False,
        protected_until=datetime(2026, 10, 7, 12, 0),
    )

    assert row.is_deletion_prohibited(
        at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    )


@pytest.mark.parametrize("retention_class", sorted(PROTECTED_RETENTION_CLASSES))
def test_protected_retention_class_without_expiry_fails_closed(
    retention_class: str,
) -> None:
    row = BucketFile(
        legal_hold=False,
        retention_class=retention_class,
        protected_until=None,
    )

    assert row.is_deletion_prohibited(
        at=datetime(2035, 1, 1, tzinfo=UTC)
    )


def test_legacy_unclassified_file_without_expiry_remains_deletable() -> None:
    row = BucketFile(
        legal_hold=False,
        retention_class=None,
        protected_until=None,
    )

    assert not row.is_deletion_prohibited(
        at=datetime(2035, 1, 1, tzinfo=UTC)
    )


def test_ach_retention_migration_recovers_legacy_certificate_rows() -> None:
    migration = Path("alembic/versions/0234_ach_proof_retention.py").read_text(
        encoding="utf-8"
    )

    assert "s3_version_id" in migration
    assert "uq_bucket_files_protected_source_ref" in migration
    assert "m.certificate_s3_key" in migration
    assert "payment_transfer_events" in migration
    assert "m.certificate_bucket_file_id IS NULL" in migration
