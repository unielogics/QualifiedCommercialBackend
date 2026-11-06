"""A reviewed replacement changes one source projection, retaining its proof."""
from __future__ import annotations

import copy
import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.dealer_os.models import (
    DealerAddback,
    DealerCashEvent,
    DealerDocument,
    DealerFinancialPeriod,
    DealerTaxFiling,
)
from app.dealer_os.services.document_version_projection import (
    ARCHIVE_KEY,
    current_dealer_document_source,
    identity_conflict,
    retire_previous_projection,
)


class Rows:
    def __init__(self, values=()):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


def test_dealer_numeric_sources_keep_manual_rows_and_exclude_superseded_documents():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE dos_documents (id TEXT PRIMARY KEY, is_current_document_version BOOLEAN, document_version_status TEXT, status TEXT);
            CREATE TABLE dos_cash_events (id TEXT PRIMARY KEY, document_id TEXT);
        """)
        old, latest, old_event, new_event, manual = [uuid4().hex for _ in range(5)]
        db.executemany("INSERT INTO dos_documents VALUES (?,?,?,?)", [(old, False, "superseded", "extracted"), (latest, True, "current", "extracted")])
        db.executemany("INSERT INTO dos_cash_events VALUES (?,?)", [(old_event, old), (new_event, latest), (manual, None)])
        statement = str(select(DealerCashEvent.id).where(current_dealer_document_source(DealerCashEvent.document_id)).compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        assert {row[0] for row in db.execute(statement).fetchall()} == {new_event, manual}
        db.execute("UPDATE dos_documents SET status='deleted' WHERE id=?", (latest,))
        assert {row[0] for row in db.execute(statement).fetchall()} == {manual}


def test_dealer_identity_uses_printed_account_and_period_but_payoff_date_may_advance():
    old = SimpleNamespace(doc_meta={"_document_identity_source": {"account": {"mask": "1234"}, "months": ["2026-06"]}}, extracted={}, detected_kind="bank_statement", account_id=uuid4())
    new = SimpleNamespace(doc_meta={"_document_identity_source": {"account": {"mask": "1234"}, "months": ["2026-07"]}}, extracted={}, detected_kind="bank_statement", account_id=old.account_id)
    assert "reporting period" in identity_conflict(new, old)
    old.detected_kind = new.detected_kind = "loan_agreement"
    old.doc_meta = {"_document_identity_source": {"loan_number": "loan-1", "payoff_date": "2026-10-01"}}
    new.doc_meta = {"_document_identity_source": {"loan_number": "loan-1", "payoff_date": "2026-11-06"}}
    assert identity_conflict(new, old) is None


@pytest.mark.asyncio
async def test_projection_retirement_is_family_scoped_lossless_and_preserves_manual_addbacks():
    dealer, family, old_id, new_id = [uuid4() for _ in range(4)]
    period = date(2026, 6, 1)
    original = {"months": ["2026-06"], "transactions_count": 1, "notes": ["Original interpretation"]}
    old = DealerDocument(id=old_id, dealer_id=dealer, document_family_id=family, doc_meta={"other": "retained"}, extracted=copy.deepcopy(original), account_id=None)
    new = DealerDocument(id=new_id, dealer_id=dealer, document_family_id=family, supersedes_document_id=old_id)
    event = DealerCashEvent(id=uuid4(), dealer_id=dealer, document_id=old_id, account_id=None, period=period, amount=Decimal("-100"), description="Old source line")
    filing = DealerTaxFiling(id=uuid4(), dealer_id=dealer, document_id=old_id, year=2025, revenue_reported=Decimal("100000"))
    candidate = DealerAddback(id=uuid4(), dealer_id=dealer, document_id=old_id, title="AI proposal", status="candidate", monthly_amount=Decimal("100"))
    manual = DealerAddback(id=uuid4(), dealer_id=dealer, document_id=old_id, title="Reviewed officer compensation", status="verified", monthly_amount=Decimal("900"))
    financial = DealerFinancialPeriod(id=uuid4(), dealer_id=dealer, account_id=None, period=period, source="manual", ending_balance=Decimal("5000"), deposits=Decimal("1000"), withdrawals=Decimal("100"))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Rows([old]), Rows([event]), Rows([filing]), Rows([candidate]), Rows([financial]), Rows(), Rows(), Rows(), Rows([])]), flush=AsyncMock())
    with patch("app.dealer_os.services.document_version_projection.rebuild_periods", AsyncMock()) as rebuild:
        await retire_previous_projection(db, new)
    assert old.extracted == original and old.doc_meta["other"] == "retained"
    archive = old.doc_meta[ARCHIVE_KEY]
    assert archive["cash_events"][0]["amount"] == "-100"
    assert archive["tax_filings"][0]["document_id"] == str(old_id)
    assert archive["unverified_addbacks"][0]["id"] == str(candidate.id)
    assert financial.ending_balance == 5000 and financial.source == "manual"
    assert manual.status == "verified" and manual.monthly_amount == 900
    delete_addbacks = str(db.execute.await_args_list[7].args[0])
    assert "document_id IN" in delete_addbacks and "status !=" in delete_addbacks
    rebuild.assert_awaited_once_with(db, dealer, {period}, account_id=None)


@pytest.mark.asyncio
async def test_initial_dealer_upload_does_not_retire_any_other_source():
    db = SimpleNamespace(execute=AsyncMock())
    await retire_previous_projection(db, SimpleNamespace(supersedes_document_id=None))
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bucket_pull_carries_explicit_lineage_and_quarantines_replacement():
    from app.dealer_os.services.bucket_ingest import prepare_ingested_document
    from app.models.bucket import BucketFile

    dealer, bucket, bucket_family, dealer_family = [uuid4() for _ in range(4)]
    previous = DealerDocument(id=uuid4(), dealer_id=dealer, document_family_id=dealer_family, document_version_number=2)
    failed = DealerDocument(id=uuid4(), dealer_id=dealer, document_family_id=dealer_family, document_version_number=3)
    source = BucketFile(id=uuid4(), bucket_id=bucket, document_family_id=bucket_family, document_version_number=5, supersedes_file_id=uuid4(), content_hash="new-bytes")
    doc = DealerDocument(dealer_id=dealer, status="uploaded")
    first = SimpleNamespace(scalar_one_or_none=lambda: previous)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[first, Rows([previous, failed])]))
    await prepare_ingested_document(db, doc, source)
    assert doc.document_family_id == dealer_family and doc.document_family_id != bucket_family
    assert doc.document_version_number == 4 and doc.supersedes_document_id == previous.id
    assert doc.status == "pending_review" and not doc.is_current_document_version
    assert doc.document_version_status == "pending" and doc.content_hash == "new-bytes"
    statement = str(db.execute.await_args_list[0].args[0])
    assert "bucket_files.document_family_id" in statement and "bucket_files.bucket_id" in statement
    assert "dos_documents.dealer_id" in statement and "document_version_number <" in statement


@pytest.mark.asyncio
async def test_independent_bucket_files_do_not_guess_dealer_families():
    from app.dealer_os.services.bucket_ingest import prepare_ingested_document
    from app.models.bucket import BucketFile

    source = BucketFile(id=uuid4(), document_family_id=uuid4(), document_version_number=1, supersedes_file_id=None, content_hash="original")
    doc = DealerDocument(dealer_id=uuid4(), status="uploaded", filename="Same Lender LLC.pdf")
    db = SimpleNamespace(execute=AsyncMock())
    await prepare_ingested_document(db, doc, source)
    assert doc.document_family_id == doc.id and doc.document_family_id != source.document_family_id
    assert doc.document_version_number == 1 and doc.supersedes_document_id is None
    assert doc.document_version_status == "current" and doc.is_current_document_version
    db.execute.assert_not_called()
