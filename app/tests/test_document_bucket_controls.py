from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dealer_os.router import _can_delete_documents, _document_month_keys
from app.dealer_os.services.buckets_link import audit_bucket_name
from app.enums import Role
from app.models.bucket import Bucket
from app.routers.buckets import _attach_bucket_file_links, _newest_unique_active_files


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_ScalarRows":
        return self

    def all(self) -> list[object]:
        return self.rows


def test_application_bucket_name_matches_application_name() -> None:
    dealer = SimpleNamespace(
        name="Qualified Commercial",
        legal_name="Qualified Commercial LLC",
        city="Garfield",
        state="NJ",
    )

    assert audit_bucket_name(dealer) == "Qualified Commercial"


def test_document_delete_policy_locks_non_admin_after_submission() -> None:
    assert _can_delete_documents(
        Role.FIELD_REP,
        application_submitted=False,
        package_evidence_exists=False,
    )
    assert not _can_delete_documents(
        Role.FIELD_REP,
        application_submitted=True,
        package_evidence_exists=False,
    )
    assert not _can_delete_documents(
        Role.LOAN_EXEC,
        application_submitted=False,
        package_evidence_exists=True,
    )
    assert _can_delete_documents(
        Role.SUPER_ADMIN,
        application_submitted=True,
        package_evidence_exists=True,
    )


def test_document_month_keys_ignore_malformed_values() -> None:
    document = SimpleNamespace(
        extracted={
            "months": [
                {"month": "2026-06"},
                {"month": "2026-13"},
                {"month": "June 2026"},
                None,
            ]
        },
        doc_meta={"pl_months": [{"month": "2026-07"}]},
    )

    assert {value.isoformat() for value in _document_month_keys(document)} == {"2026-06-01"}
    assert {value.isoformat() for value in _document_month_keys(document, "pl_months")} == {
        "2026-07-01"
    }


@pytest.mark.anyio
async def test_bucket_name_repair_refreshes_database_managed_timestamp() -> None:
    bucket_id = uuid4()
    bucket = Bucket(
        id=bucket_id,
        name="Legacy name",
        client_name="Legacy name",
        name_sync_mode="linked",
    )
    dealer = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        name="Current application name",
        legal_name="Current application LLC",
        case_ref="QC-2026-00123",
        email="owner@example.com",
        phone="+15555550123",
        updated_at=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _ScalarRows([dealer]),
                _ScalarRows([]),
                _ScalarRows([]),
            ]
        ),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )

    changed = await _attach_bucket_file_links(db, [bucket])

    assert changed is True
    assert bucket.name == "Current application name"
    assert bucket.client_name == "Current application name"
    db.flush.assert_awaited_once()
    db.refresh.assert_awaited_once_with(bucket, attribute_names=["updated_at"])


@pytest.mark.anyio
async def test_custom_bucket_name_is_not_repaired_from_linked_file() -> None:
    bucket_id = uuid4()
    bucket = Bucket(
        id=bucket_id,
        name="Underwriting workspace",
        client_name="Underwriting workspace",
        name_sync_mode="custom",
    )
    dealer = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        name="Current application name",
        legal_name="Current application LLC",
        case_ref="QC-2026-00124",
        email="owner@example.com",
        phone="+15555550123",
        updated_at=datetime.now(UTC),
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _ScalarRows([dealer]),
                _ScalarRows([]),
                _ScalarRows([]),
            ]
        ),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )

    changed = await _attach_bucket_file_links(db, [bucket])

    assert changed is False
    assert bucket.name == "Underwriting workspace"
    assert bucket.linked_name == "Current application name"
    db.flush.assert_not_awaited()


def test_bucket_file_deduplication_keeps_newest_active_content_hash() -> None:
    timestamp = datetime.now(UTC)
    newest = SimpleNamespace(
        id=uuid4(),
        created_at=timestamp,
        deleted_at=None,
        content_hash="ABC123",
    )
    older_duplicate = SimpleNamespace(
        id=uuid4(),
        created_at=timestamp - timedelta(days=1),
        deleted_at=None,
        content_hash="abc123",
    )
    same_name_distinct_content = SimpleNamespace(
        id=uuid4(),
        created_at=timestamp - timedelta(days=2),
        deleted_at=None,
        content_hash="different",
    )
    deleted = SimpleNamespace(
        id=uuid4(),
        created_at=timestamp + timedelta(days=1),
        deleted_at=timestamp,
        content_hash="deleted",
    )

    rows = _newest_unique_active_files(
        [older_duplicate, deleted, same_name_distinct_content, newest]
    )

    assert rows == [newest, same_name_distinct_content]
