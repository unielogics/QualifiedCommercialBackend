from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from app.enums import DocStatus
from app.schemas.common import ORMModel


class DocumentRead(ORMModel):
    id: UUID
    loan_id: UUID
    name: str
    category: str | None
    s3_key: str | None
    status: DocStatus
    requested_on: date | None
    received_on: date | None
    verified_at: datetime | None
    verified_by: str | None


class DocumentRequest(BaseModel):
    loan_id: UUID
    name: str
    category: str | None = None
    due_in_days: int = 7


class DocumentUploadInit(BaseModel):
    loan_id: UUID
    name: str
    content_type: str = "application/pdf"


class DocumentUploadInitResponse(BaseModel):
    document_id: UUID
    upload_url: str | None  # presigned S3 PUT URL; None when S3 not configured
    s3_key: str
