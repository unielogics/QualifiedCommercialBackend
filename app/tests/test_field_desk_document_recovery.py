from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.models import DealerCashEvent, DealerDocument, DealerFinancialPeriod
from app.dealer_os.services import buckets_link, document_recovery
from app.enums import Role


def test_normalized_fact_snapshot_round_trips_exact_types() -> None:
    recorded_at = datetime(2026, 9, 19, 14, 30, tzinfo=UTC)
    event = DealerCashEvent(
        id=uuid4(),
        dealer_id=uuid4(),
        account_id=uuid4(),
        period=date(2026, 9, 1),
        occurred_on=date(2026, 9, 18),
        description="Deposit",
        amount=Decimal("1234.56"),
        category="revenue",
        flags={"reviewed": True},
        source="upload",
        document_id=uuid4(),
        created_at=recorded_at,
        updated_at=recorded_at,
    )

    snapshot = document_recovery.row_snapshot(event)
    restored = document_recovery.row_from_snapshot(DealerCashEvent, snapshot)

    assert restored.id == event.id
    assert restored.document_id == event.document_id
    assert restored.period == date(2026, 9, 1)
    assert restored.occurred_on == date(2026, 9, 18)
    assert restored.amount == Decimal("1234.56")
    assert restored.flags == {"reviewed": True}
    assert restored.created_at == recorded_at
    assert restored.updated_at == recorded_at


def test_recovery_archive_is_namespaced_without_erasing_classifier_metadata() -> None:
    document = DealerDocument(
        id=uuid4(),
        dealer_id=uuid4(),
        filename="statement.pdf",
        content_type="application/pdf",
        size_bytes=120,
        kind="statement",
        status="extracted",
        doc_meta={"classifier": {"kind": "bank_statement"}},
    )
    archive = {
        "version": document_recovery.ARCHIVE_VERSION,
        "state": "deleted",
        "root_document_id": str(document.id),
    }

    document_recovery.set_archive(document, archive)

    assert document_recovery.archive_for(document) == archive
    assert document.doc_meta["classifier"] == {"kind": "bank_statement"}
    document_recovery.clear_archive(document)
    assert document_recovery.archive_for(document) is None
    assert document.doc_meta == {"classifier": {"kind": "bank_statement"}}


def test_period_snapshot_detects_later_underwriting_change() -> None:
    period = DealerFinancialPeriod(
        id=uuid4(),
        dealer_id=uuid4(),
        account_id=None,
        period=date(2026, 9, 1),
        deposits=Decimal("5000.00"),
        withdrawals=Decimal("3000.00"),
        nsf_count=0,
        source="upload",
        reconciled=False,
    )
    post_delete = document_recovery.row_snapshot(period)
    assert document_recovery.snapshots_match(period, post_delete)

    period.deposits = Decimal("7200.00")

    assert not document_recovery.snapshots_match(period, post_delete)


@pytest.mark.asyncio
async def test_field_desk_bucket_restore_only_accepts_coordinated_retention(monkeypatch) -> None:
    user = SimpleNamespace(
        id=uuid4(),
        name="QC Admin",
        email="admin@example.com",
        role=Role.SUPER_ADMIN,
    )
    bucket_file = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="statement.pdf",
        deleted_at=datetime.now(UTC),
        deleted_by_user_id=uuid4(),
        delete_storage_status="retained_for_audit",
        requested_document_id=None,
    )
    db = SimpleNamespace(add=lambda _row: None, flush=AsyncMock())
    verify = AsyncMock()
    monkeypatch.setattr(
        buckets_link.bucket_storage_integrity,
        "verify_bucket_file_objects",
        verify,
    )

    await buckets_link.restore_mirrored_file(
        db, bucket_file, user, detail="Restored with Field Desk document"
    )

    assert bucket_file.deleted_at is None
    assert bucket_file.deleted_by_user_id is None
    assert bucket_file.delete_storage_status is None
    verify.assert_awaited_once_with([bucket_file])
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_field_desk_bucket_restore_rejects_generic_soft_delete() -> None:
    bucket_file = SimpleNamespace(
        deleted_at=datetime.now(UTC),
        delete_storage_status="retained_soft_delete",
    )
    with pytest.raises(HTTPException) as exc:
        await buckets_link.restore_mirrored_file(
            SimpleNamespace(),
            bucket_file,
            SimpleNamespace(),
            detail="not coordinated",
        )
    assert exc.value.status_code == 409

