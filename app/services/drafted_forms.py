"""Filing a form we generated as though the borrower had uploaded it.

A form typed on screen satisfies a checklist item the same way a real upload
does: the PDF becomes an ordinary `BucketFile` linked by `requested_document_id`,
which alone flips the requested document to `uploaded`, and a `BucketFileAnalysis`
is written straight from the structured input rather than asking a model to read
numbers back out of a picture we just drew. Everything downstream reads
`classification` and `key_facts` and never asks where they came from.

That behaviour already existed inside the public intake router. It lives here so
the staff side can file a statement the same way without a second copy of the
rules — the two must agree about what satisfies a slot, and the surest way to
keep them agreeing is for there to be one of them.

Deliberately no audit log and no commit. The actor differs (a borrower on a
public link, a staff member filling one in on their behalf) and so does the
transaction the write belongs to, so both stay with the caller.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION


async def store_form_pdf(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    upload_link_id: UUID | None,
    requested_document: BucketRequestedDocument,
    pdf_bytes: bytes,
    file_label: str,
    classification: str,
    key_facts: dict[str, Any],
    actor_name: str,
    actor_email: str,
    summary: str | None = None,
) -> BucketFile:
    """Store the PDF against the slot and record what it says.

    Flushes so the caller has the file's id; does not commit.
    """
    from app.routers.buckets import _bucket_storage_config
    from app.routers.dealer_ai_intake import _put_bucket_object

    _, prefix, _kms = _bucket_storage_config()
    file_id = uuid4()
    s3_key = f"{prefix}/drafted-forms/{bucket_id}/{file_id}.pdf"
    _put_bucket_object(s3_key, "application/pdf", pdf_bytes)

    result_file = BucketFile(
        id=file_id,
        bucket_id=bucket_id,
        requested_document_id=requested_document.id,
        upload_link_id=upload_link_id,
        file_name=f"{file_label}.pdf"[:255],
        s3_key=s3_key,
        content_type="application/pdf",
        size_bytes=len(pdf_bytes),
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        status="uploaded",
    )
    db.add(result_file)
    requested_document.status = "uploaded"
    await db.flush()

    db.add(
        BucketFileAnalysis(
            bucket_file_id=result_file.id,
            bucket_id=bucket_id,
            content_hash=hashlib.sha256(pdf_bytes).hexdigest(),
            analysis_version=CURRENT_FILE_ANALYSIS_VERSION,
            provider="drafted_form",
            status="completed",
            classification=classification,
            confidence="high",
            summary=summary or f"{file_label} submitted via the on-screen drafting form.",
            analysis={"key_facts": key_facts},
            analyzed_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return result_file
