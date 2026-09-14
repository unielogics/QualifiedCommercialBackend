from __future__ import annotations

import base64
import inspect
import json
import struct
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from openpyxl import Workbook
from pypdf import PdfReader, PdfWriter

from app.routers import buckets as buckets_router
from app.routers import dealer_ai_intake as intake_router
from app.services import bucket_ai
from app.services.bucket_ai import (
    MAX_SPREADSHEET_TEXT_CHARS,
    _append_review_file_content,
    _extract_csv_text,
    _extract_pdf_text,
    _extract_xlsx_text,
    _is_csv_file,
    _is_legacy_xls_file,
    _is_xlsx_file,
    _limit_structured_text,
    _media_type,
    _password_skip_still_applies,
    _pdf_review_metadata,
)


def _file(name: str):
    return SimpleNamespace(id=uuid4(), file_name=name)


def _encrypted_pdf(user_password: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password=user_password, owner_password="owner-secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_extract_xlsx_includes_rows_formulas_and_sheet_warnings():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "May FS"
    sheet.append(["Account", "Amount"])
    sheet.append(["Gross sales", 1250000])
    sheet.append(["COGS", 850000])
    sheet["B4"] = "=B2-B3"

    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    hidden.append(["Should not", "appear"])

    raw = BytesIO()
    workbook.save(raw)

    extracted, skip = _extract_xlsx_text(_file("financials.xlsx"), raw.getvalue())

    assert skip is None
    payload = json.loads(extracted)
    assert payload["type"] == "xlsx_workbook"
    assert payload["sheet_names"] == ["May FS", "Hidden"]
    assert payload["visible_sheets_included"] == ["May FS"]
    assert payload["sheets"][0]["rows"][0] == ["Account", "Amount"]
    assert payload["sheets"][0]["rows"][1] == ["Gross sales", "1250000"]
    assert payload["sheets"][0]["formulas"][0]["cell"] == "B4"
    assert payload["sheets"][0]["formulas"][0]["formula"] == "=B2-B3"


def test_extract_csv_returns_structured_rows_and_warnings():
    raw = b"Name,Amount,Notes\nRent,4500,Monthly\nTaxes,6000,Annual\n"

    extracted, skip = _extract_csv_text(_file("rent_roll.csv"), raw)

    assert skip is None
    payload = json.loads(extracted)
    assert payload["type"] == "csv_table"
    assert payload["rows"] == [
        ["Name", "Amount", "Notes"],
        ["Rent", "4500", "Monthly"],
        ["Taxes", "6000", "Annual"],
    ]
    assert payload["warnings"] == []


def test_empty_csv_returns_parse_skip_reason():
    extracted, skip = _extract_csv_text(_file("empty.csv"), b"")

    assert extracted is None
    assert skip[0] == "csv_parse_failed"


def test_spreadsheet_file_type_detection_and_budget_limit():
    assert _is_xlsx_file(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "financials.bin",
    )
    assert _is_xlsx_file("application/octet-stream", "financials.xlsx")
    assert _is_legacy_xls_file("financials.xls")
    assert _is_csv_file("text/csv", "upload.bin")
    assert _is_csv_file("application/octet-stream", "rent_roll.csv")

    limited, truncated = _limit_structured_text("x" * (MAX_SPREADSHEET_TEXT_CHARS + 1), 10)
    assert limited == "x" * 10
    assert truncated is True


def test_existing_pdf_and_image_media_detection_unchanged():
    assert _media_type("application/pdf", "tax-return.bin") == "application/pdf"
    assert _media_type("application/octet-stream", "license.png") == "image/png"
    assert _media_type("image/jpeg", "photo.bin") == "image/jpeg"


def test_pdf_open_password_is_reported_as_password_protected():
    raw = _encrypted_pdf("client-secret")

    pages, metadata_skip = _pdf_review_metadata(raw)
    extracted, extraction_skip = _extract_pdf_text(_file("locked.pdf"), raw)

    assert pages is None
    assert metadata_skip and metadata_skip[0] == "password_protected"
    assert extracted is None
    assert extraction_skip and extraction_skip[0] == "password_protected"


def test_permission_encrypted_pdf_that_opens_without_password_is_not_locked():
    raw = _encrypted_pdf("")

    pages, metadata_skip = _pdf_review_metadata(raw)
    extracted, extraction_skip = _extract_pdf_text(_file("permissions.pdf"), raw)

    assert pages == 1
    assert metadata_skip is None
    assert extracted is None
    assert extraction_skip and extraction_skip[0] == "pdf_text_unavailable"


def test_permission_encrypted_pdf_is_unencrypted_before_model_attachment():
    raw = _encrypted_pdf("")
    content: list[dict] = []

    added, pages, _chars = _append_review_file_content(
        content=content,
        skipped=[],
        blocked_files=[],
        file=_file("permissions.pdf"),
        raw=raw,
        content_type="application/pdf",
        attached_pdf_pages=0,
        spreadsheet_text_chars=0,
    )

    document = next(item for item in content if item["type"] == "document")
    attached = base64.b64decode(document["source"]["data"])
    attached_reader = PdfReader(BytesIO(attached), strict=False)
    assert added is True
    assert pages == 1
    assert attached.startswith(b"%PDF")
    assert attached_reader.is_encrypted is False


def test_legacy_password_skip_is_revalidated_without_global_cache_bump():
    cached = SimpleNamespace(status="skipped", skip_reason="password_protected")

    assert _password_skip_still_applies(
        cached,
        raw=_encrypted_pdf("client-secret"),
        content_type="application/pdf",
        file_name="locked.pdf",
    )
    assert not _password_skip_still_applies(
        cached,
        raw=_encrypted_pdf(""),
        content_type="application/pdf",
        file_name="permissions-only.pdf",
    )


@pytest.mark.asyncio
async def test_readable_legacy_password_skip_clears_stale_lock_metadata():
    raw = _encrypted_pdf("")
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="permissions-only.pdf",
        content_type="application/pdf",
        content_hash=None,
        size_bytes=len(raw),
    )
    row = SimpleNamespace(
        status="skipped",
        skip_reason="password_protected",
        skip_detail="Legacy lock detail",
        error="Legacy error",
    )
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text=json.dumps(
                    {
                        "classification": "bank_statement",
                        "confidence": "high",
                        "summary": "Readable bank statement",
                        "supports": [],
                        "baseline_categories_supported": [],
                        "red_flags": [],
                        "limitations": [],
                        "key_facts": {},
                        "profile_facts": {},
                    }
                ),
            )
        ],
        stop_reason="end_turn",
        model="test-model",
        usage=None,
    )
    db = SimpleNamespace(flush=AsyncMock())

    with (
        patch.object(bucket_ai, "_lock_analysis_scope", AsyncMock()),
        patch.object(bucket_ai, "reconcile_uploaded_file", AsyncMock()),
        patch.object(
            bucket_ai,
            "_fetch_file",
            return_value=(raw, "application/pdf"),
        ),
        patch.object(
            bucket_ai, "_cached_file_analysis", AsyncMock(return_value=row)
        ),
        patch.object(
            bucket_ai, "_get_or_create_analysis_row", AsyncMock(return_value=row)
        ),
        patch.object(bucket_ai, "model_heavy", return_value="test-model"),
        patch.object(bucket_ai, "get_client", return_value=object()),
        patch.object(
            bucket_ai, "tracked_messages_create", AsyncMock(return_value=response)
        ),
        patch.object(
            bucket_ai, "_reconcile_analysis_consumers", AsyncMock()
        ) as reconcile,
        patch.object(
            bucket_ai.merchant_processing,
            "is_offer_document",
            return_value=False,
        ),
    ):
        result = await bucket_ai.analyze_bucket_file(db, file)

    assert result is row
    assert row.status == "completed"
    assert row.skip_reason is None
    assert row.skip_detail is None
    assert row.error is None
    reconcile.assert_awaited_once_with(db, file=file, analysis=row)


@pytest.mark.asyncio
async def test_cached_analysis_replays_application_mapping_without_a_model_call():
    file = SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), file_name="statement.pdf", content_hash=None
    )
    cached = SimpleNamespace(status="completed")
    db = SimpleNamespace()

    with (
        patch.object(bucket_ai, "reconcile_uploaded_file", AsyncMock()),
        patch.object(bucket_ai, "_fetch_file", return_value=(b"pdf", "application/pdf")),
        patch.object(bucket_ai, "_cached_file_analysis", AsyncMock(return_value=cached)),
        patch.object(bucket_ai, "_lock_analysis_scope", AsyncMock()) as lock_scope,
        patch.object(bucket_ai, "_reconcile_analysis_consumers", AsyncMock()) as reconcile_consumers,
        patch.object(bucket_ai.merchant_processing, "is_offer_document", return_value=False),
        patch.object(bucket_ai, "tracked_messages_create", AsyncMock()) as model_call,
    ):
        result = await bucket_ai.analyze_bucket_file(db, file)

    assert result is cached
    lock_scope.assert_awaited_once_with(db, file)
    reconcile_consumers.assert_awaited_once_with(db, file=file, analysis=cached)
    model_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_zip_parent_analysis_is_terminal_without_fetching_storage():
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="batch.zip",
        content_type="application/zip",
        content_hash=None,
    )
    row = SimpleNamespace(
        status="pending",
        skip_reason=None,
        skip_detail=None,
        error="old error",
        analyzed_at=None,
    )
    db = SimpleNamespace(flush=AsyncMock())

    with (
        patch.object(bucket_ai, "_lock_analysis_scope", AsyncMock()),
        patch.object(bucket_ai, "reconcile_uploaded_file", AsyncMock()),
        patch.object(bucket_ai, "_get_or_create_analysis_row", AsyncMock(return_value=row)) as upsert,
        patch.object(bucket_ai, "_reconcile_analysis_consumers", AsyncMock()) as reconcile,
        patch.object(bucket_ai, "_fetch_file") as fetch,
    ):
        result = await bucket_ai.analyze_bucket_file(db, file)

    assert result is row
    assert row.status == "skipped"
    assert row.skip_reason == "zip_parent_archive"
    assert row.error is None
    upsert.assert_awaited_once_with(db, file, f"zip-parent:{file.id}")
    reconcile.assert_awaited_once_with(db, file=file, analysis=row)
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_shared_bucket_completion_expands_zip_and_queues_its_children():
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        upload_link_id=uuid4(),
        file_name="evidence.zip",
        content_type="application/zip",
    )
    child = SimpleNamespace(id=uuid4())
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [child]))
    db = SimpleNamespace(flush=AsyncMock(), execute=AsyncMock(return_value=rows))

    with (
        patch("app.routers.dealer_ai_intake._extract_zip_bucket_files", AsyncMock()) as extract,
        patch("app.services.bucket_evidence.reconcile_uploaded_file", AsyncMock()) as reconcile,
        patch("app.services.bucket_ai.enqueue_file_analysis", AsyncMock()) as enqueue,
    ):
        await buckets_router._reconcile_completed_bucket_file(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Desk",
            actor_email="desk@example.com",
        )

    extract.assert_awaited_once()
    assert [call.args[1] for call in reconcile.await_args_list] == [parent, child]
    assert [call.args[1] for call in enqueue.await_args_list] == [parent, child]


@pytest.mark.asyncio
async def test_zip_children_inherit_parent_bucket_and_actual_upload_link():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("statements/August.pdf", b"%PDF-1.7")
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        upload_link_id=uuid4(),
        file_name="batch.zip",
        content_type="application/zip",
        s3_key="incoming/batch.zip",
        extraction_status=None,
        uploaded_by_user_id=None,
        source_kind="client_upload",
    )
    added = []
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    no_duplicate = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[locked, no_duplicate, no_duplicate]), add=added.append
    )

    with (
        patch.object(intake_router, "_read_bucket_object", return_value=archive.getvalue()),
        patch.object(intake_router, "_put_bucket_object") as put_object,
        patch.object(intake_router, "_bucket_storage_config", return_value=("bucket", "prefix", "kms")),
        patch.object(intake_router, "_log", AsyncMock()),
    ):
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Grace",
            actor_email="grace@example.com",
        )

    assert parent.extraction_status == "extracted"
    assert len(added) == 1
    child = added[0]
    assert child.bucket_id == parent.bucket_id
    assert child.upload_link_id == parent.upload_link_id
    assert child.parent_zip_file_id == parent.id
    assert child.zip_entry_path == "statements/August.pdf"
    assert child.source_kind == "zip_extract"
    assert child.content_hash
    put_object.assert_called_once()


def test_zip_object_read_is_bounded_by_the_archive_limit():
    body = MagicMock()
    client = MagicMock()
    client.get_object.return_value = {
        "Body": body,
        "ContentLength": intake_router.ZIP_MAX_ARCHIVE_BYTES + 1,
    }

    with (
        patch.object(intake_router, "_s3_client", return_value=client),
        patch.object(intake_router, "_bucket_storage_config", return_value=("bucket", "prefix", "kms")),
        pytest.raises(intake_router._ZipArchiveTooLarge),
    ):
        intake_router._read_bucket_object(
            "incoming/oversized.zip", max_bytes=intake_router.ZIP_MAX_ARCHIVE_BYTES
        )

    body.read.assert_not_called()


@pytest.mark.asyncio
async def test_zip_fetch_failure_remains_retryable():
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="batch.zip",
        content_type="application/zip",
        s3_key="incoming/batch.zip",
        extraction_status=None,
    )
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    db = SimpleNamespace(execute=AsyncMock(return_value=locked))

    with patch.object(intake_router, "_read_bucket_object", return_value=None) as read_object:
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Grace",
            actor_email="grace@example.com",
        )

    assert parent.extraction_status == "retryable"
    assert "zip_fetch_failed" in parent.extraction_reason
    assert read_object.call_count == 3


@pytest.mark.asyncio
async def test_zip_fetch_failure_becomes_terminal_after_bounded_runs():
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="missing.zip",
        content_type="application/zip",
        s3_key="incoming/missing.zip",
        extraction_status=None,
        extraction_reason=None,
    )
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    db = SimpleNamespace(execute=AsyncMock(return_value=locked))

    with patch.object(intake_router, "_read_bucket_object", return_value=None):
        for _ in range(intake_router.ZIP_FETCH_MAX_RUNS):
            parent.extraction_status = "retryable"
            await intake_router._extract_zip_bucket_files(
                db,
                parent,
                SimpleNamespace(),
                actor_name="Grace",
                actor_email="grace@example.com",
            )

    assert parent.extraction_status == "skipped"
    assert f'"runs": {intake_router.ZIP_FETCH_MAX_RUNS}' in parent.extraction_reason


@pytest.mark.asyncio
async def test_zip_child_write_failure_is_persisted_for_scheduler_retry():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("statement.pdf", b"%PDF-1.7")
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        upload_link_id=None,
        file_name="transient.zip",
        content_type="application/zip",
        s3_key="incoming/transient.zip",
        extraction_status=None,
        extraction_reason=None,
        uploaded_by_user_id=None,
        source_kind="internal_upload",
    )
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    missing = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[locked, missing, missing]), add=MagicMock())

    with (
        patch.object(intake_router, "_read_bucket_object", return_value=archive.getvalue()),
        patch.object(intake_router, "_put_bucket_object", side_effect=OSError("temporary S3 error")),
        patch.object(intake_router, "_bucket_storage_config", return_value=("bucket", "prefix", "kms")),
    ):
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Desk",
            actor_email="desk@example.com",
        )

    assert parent.extraction_status == "retryable"
    assert "zip_extract_failed" in parent.extraction_reason
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_zip_retry_counts_an_already_committed_child_as_recovered():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("statement.pdf", b"%PDF-1.7")
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="partial.zip",
        content_type="application/zip",
        s3_key="incoming/partial.zip",
        extraction_status="retryable",
        extraction_reason='[{"reason":"zip_extract_failed","runs":4}]',
        source_kind="internal_upload",
    )
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    existing_child = SimpleNamespace(scalar_one_or_none=lambda: uuid4())
    db = SimpleNamespace(execute=AsyncMock(side_effect=[locked, existing_child]))

    with (
        patch.object(intake_router, "_read_bucket_object", return_value=archive.getvalue()),
        patch.object(intake_router, "_put_bucket_object") as put_object,
        patch.object(intake_router, "_bucket_storage_config", return_value=("bucket", "prefix", "kms")),
        patch.object(intake_router, "_log", AsyncMock()),
    ):
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Desk",
            actor_email="desk@example.com",
        )

    assert parent.extraction_status == "extracted"
    put_object.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_drain_retries_zip_without_an_analysis_placeholder():
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="legacy.zip",
        content_type="application/zip",
        status="uploaded",
        deleted_at=None,
        extraction_status="retryable",
        uploaded_by_name="Grace",
        uploaded_by_email="grace@example.com",
    )
    ids = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [file.id]))
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[ids, empty, empty]),
        get=AsyncMock(return_value=file),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with patch(
        "app.routers.buckets._reconcile_completed_bucket_file", AsyncMock()
    ) as reconcile:
        processed = await bucket_ai.drain_file_analyses(db, limit=1)

    assert processed == 0
    reconcile.assert_awaited_once_with(
        db,
        file,
        None,
        actor_name="Grace",
        actor_email="grace@example.com",
    )


@pytest.mark.asyncio
async def test_zip_entry_count_is_rejected_before_zipfile_materializes_entries():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(intake_router.ZIP_MAX_ENTRIES + 1):
            zipped.writestr(f"empty-{index}.pdf", b"")
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="too-many.zip",
        content_type="application/zip",
        s3_key="incoming/too-many.zip",
        extraction_status=None,
    )
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    db = SimpleNamespace(execute=AsyncMock(return_value=locked))

    with (
        patch.object(intake_router, "_read_bucket_object", return_value=archive.getvalue()),
        patch.object(intake_router, "_put_bucket_object") as put_object,
    ):
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Grace",
            actor_email="grace@example.com",
        )

    assert parent.extraction_status == "skipped"
    assert "zip_entry_limit" in parent.extraction_reason
    put_object.assert_not_called()


@pytest.mark.asyncio
async def test_zip_with_trailing_bytes_cannot_bypass_directory_prescan():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("statement.pdf", b"%PDF-1.7")
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="trailing-data.zip",
        content_type="application/zip",
        s3_key="incoming/trailing-data.zip",
        extraction_status=None,
    )
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    db = SimpleNamespace(execute=AsyncMock(return_value=locked))

    with (
        patch.object(intake_router, "_read_bucket_object", return_value=archive.getvalue() + b"junk"),
        patch.object(intake_router, "_put_bucket_object") as put_object,
    ):
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Grace",
            actor_email="grace@example.com",
        )

    assert parent.extraction_status == "skipped"
    assert "zip_parse_failed" in parent.extraction_reason
    put_object.assert_not_called()


def test_zip_prescan_never_falls_back_to_an_earlier_forged_end_record():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(intake_router.ZIP_MAX_ENTRIES + 1):
            zipped.writestr(f"empty-{index}.pdf", b"")
    trailing_archive = archive.getvalue() + b"trailing"
    forged = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        0,
        0,
        len(trailing_archive),
    )

    assert intake_router._zip_directory_metadata(forged + trailing_archive) is None


def test_zip64_cannot_replace_forged_small_classic_directory_metadata():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(intake_router.ZIP_MAX_ENTRIES + 1):
            zipped.writestr(f"empty-{index}.pdf", b"")
    raw = archive.getvalue()
    offset = raw.rfind(b"PK\x05\x06")
    eocd = list(struct.unpack_from("<4s4H2LH", raw, offset))
    actual_count, actual_size, actual_offset = eocd[4], eocd[5], eocd[6]
    eocd[3] = eocd[4] = 1
    eocd[5] = 0
    classic = struct.pack("<4s4H2LH", *eocd)
    zip64_eocd = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        actual_count,
        actual_count,
        actual_size,
        actual_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, offset, 1)
    forged = raw[:offset] + zip64_eocd + locator + classic

    assert len(zipfile.ZipFile(BytesIO(forged)).infolist()) == intake_router.ZIP_MAX_ENTRIES + 1
    assert intake_router._zip_directory_metadata(forged) is None


@pytest.mark.asyncio
async def test_losing_review_worker_returns_without_doing_work():
    running = SimpleNamespace(status="running")
    no_claim = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=no_claim),
        rollback=AsyncMock(),
        get=AsyncMock(return_value=running),
    )

    result = await bucket_ai.run_bucket_ai_review(db, uuid4())

    assert result is running
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_worker_cannot_overwrite_a_completed_review_claim():
    completed = SimpleNamespace(status="completed")
    no_owned_claim = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=no_owned_claim),
        rollback=AsyncMock(),
        get=AsyncMock(return_value=completed),
        commit=AsyncMock(),
    )

    with patch.object(bucket_ai, "log_bucket_ai_activity", AsyncMock()) as activity:
        result = await bucket_ai._persist_review_failure(
            db,
            review_id=uuid4(),
            bucket_id=uuid4(),
            error=RuntimeError("late duplicate"),
            files_total=3,
            files_done=2,
            claimed_at=bucket_ai._now(),
        )

    assert result is completed
    assert db.rollback.await_count == 2
    db.commit.assert_not_awaited()
    activity.assert_not_awaited()


def test_review_claim_is_committed_before_file_or_model_work():
    source = inspect.getsource(bucket_ai.run_bucket_ai_review)

    assert ".with_for_update(skip_locked=True)" in source
    assert source.index("await db.commit()") < source.index("files = [")


@pytest.mark.asyncio
async def test_zip_child_keeps_full_lineage_but_bounds_display_filename():
    entry_path = "statements/" + ("a" * 280) + ".pdf"
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(entry_path, b"%PDF-1.7")
    parent = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        upload_link_id=uuid4(),
        file_name="long-name.zip",
        content_type="application/zip",
        s3_key="incoming/long-name.zip",
        extraction_status=None,
        uploaded_by_user_id=uuid4(),
        source_kind="bucket_admin",
    )
    added = []
    locked = SimpleNamespace(scalar_one_or_none=lambda: parent)
    no_duplicate = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[locked, no_duplicate, no_duplicate]), add=added.append
    )

    with (
        patch.object(intake_router, "_read_bucket_object", return_value=archive.getvalue()),
        patch.object(intake_router, "_put_bucket_object"),
        patch.object(intake_router, "_bucket_storage_config", return_value=("bucket", "prefix", "kms")),
        patch.object(intake_router, "_log", AsyncMock()),
    ):
        await intake_router._extract_zip_bucket_files(
            db,
            parent,
            SimpleNamespace(),
            actor_name="Desk",
            actor_email="desk@example.com",
        )

    assert len(added) == 1
    child = added[0]
    assert len(child.file_name) == 255
    assert child.zip_entry_path == entry_path
    assert child.uploaded_by_user_id == parent.uploaded_by_user_id
