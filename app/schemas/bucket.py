from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel


class BucketTemplateRead(ORMModel):
    id: UUID
    name: str
    category: str | None
    description: str | None
    required: bool
    allow_multiple_files: bool
    is_active: bool


class BucketRequestedDocumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = None
    required: bool = True
    allow_multiple_files: bool = False
    is_custom: bool = False
    save_to_library: bool = False


class BucketRequestedDocumentRead(ORMModel):
    id: UUID
    bucket_id: UUID
    template_id: UUID | None
    name: str
    category: str | None
    description: str | None
    required: bool
    allow_multiple_files: bool
    status: str
    is_custom: bool


class BucketCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    bucket_type: str | None = Field(default=None, max_length=80)
    client_name: str | None = Field(default=None, max_length=180)
    purpose: str | None = Field(default=None, max_length=255)
    description: str | None = None


class BucketRead(ORMModel):
    id: UUID
    name: str
    bucket_type: str | None
    client_name: str | None
    purpose: str | None
    description: str | None
    status: str
    created_by_id: UUID | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    file_count: int = 0
    uploaded_file_count: int = 0


class BucketDetail(BucketRead):
    requested_documents: list[BucketRequestedDocumentRead] = []
    files: list[BucketFileRead] = []
    shares: list[BucketShareRead] = []
    notes: list[BucketNoteRead] = []
    activity: list[BucketActivityRead] = []


class BucketUploadLinkCreate(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=180)
    recipient_email: EmailStr | None = None
    expires_at: datetime | None = None
    allow_notes: bool = True
    allow_multiple_sessions: bool = True
    passcode: str | None = Field(default=None, max_length=80)

    @field_validator("recipient_email", mode="before")
    @classmethod
    def empty_email_to_none(cls, value: object) -> object:
        return None if value == "" else value


class BucketUploadLinkRead(ORMModel):
    id: UUID
    bucket_id: UUID
    token: str
    recipient_name: str
    recipient_email: str | None
    expires_at: datetime | None
    allow_notes: bool
    allow_multiple_sessions: bool
    status: str
    completed_at: datetime | None
    created_at: datetime
    upload_url: str | None = None
    passcode: str | None = None


class BucketRequestBucketRead(BaseModel):
    name: str
    client_name: str | None = None
    purpose: str | None = None


class BucketRequestInfoRead(BaseModel):
    bucket: BucketRequestBucketRead
    recipient_name: str
    recipient_email: str | None
    requires_passcode: bool
    status: str


class BucketRequestAccessRequest(BaseModel):
    passcode: str = Field(min_length=1, max_length=80)


class BucketRequestAccessRead(BaseModel):
    bucket: BucketRequestBucketRead
    recipient_name: str
    recipient_email: str | None
    allow_notes: bool
    requested_documents: list[BucketRequestedDocumentRead]


class BucketFileUploadInit(BaseModel):
    requested_document_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    uploader_name: str = Field(min_length=1, max_length=180)
    uploader_email: EmailStr | None = None
    note: str | None = None
    passcode: str | None = None

    @field_validator("uploader_email", mode="before")
    @classmethod
    def empty_email_to_none(cls, value: object) -> object:
        return None if value == "" else value


class BucketFileUploadInitResponse(BaseModel):
    file_id: UUID
    upload_url: str
    s3_key: str
    required_headers: dict[str, str]


class BucketUploadComplete(BaseModel):
    file_id: UUID
    note: str | None = None


class BucketFileRead(ORMModel):
    id: UUID
    bucket_id: UUID
    requested_document_id: UUID | None
    file_name: str
    s3_key: str
    content_type: str
    size_bytes: int
    uploaded_by_name: str | None
    uploaded_by_email: str | None
    status: str
    deleted_at: datetime | None = None
    deleted_by_user_id: UUID | None = None
    delete_storage_status: str | None = None
    created_at: datetime
    updated_at: datetime


class BucketFileUrl(BaseModel):
    url: str
    expires_in: int


class BucketFileAnnotationCreate(BaseModel):
    page_number: int = Field(default=1, ge=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    comment: str = Field(min_length=1)
    passcode: str | None = Field(default=None, max_length=80)


class BucketFileAnnotationRead(ORMModel):
    id: UUID
    bucket_id: UUID
    file_id: UUID
    share_id: UUID | None
    page_number: int
    x: float
    y: float
    width: float
    height: float
    comment: str
    author_name: str
    author_role: str
    created_at: datetime
    updated_at: datetime


class BucketFileReviewRead(BaseModel):
    file: BucketFileRead
    preview_url: str | None
    annotations: list[BucketFileAnnotationRead]


class BucketShareCreate(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=180)
    recipient_email: EmailStr | None = None
    file_ids: list[UUID] = Field(min_length=1)
    passcode: str | None = Field(default=None, max_length=80)
    can_preview: bool = True
    can_download: bool = False
    can_add_notes: bool = True
    can_upload: bool = False
    can_see_internal_notes: bool = False
    expires_at: datetime | None = None

    @field_validator("recipient_email", mode="before")
    @classmethod
    def empty_email_to_none(cls, value: object) -> object:
        return None if value == "" else value


class BucketShareRead(ORMModel):
    id: UUID
    bucket_id: UUID
    token: str
    recipient_name: str
    recipient_email: str | None
    can_preview: bool
    can_download: bool
    can_add_notes: bool
    can_upload: bool
    can_see_internal_notes: bool
    status: str
    expires_at: datetime | None
    last_accessed_at: datetime | None
    view_count: int
    download_count: int
    created_at: datetime
    files: list[BucketFileRead] = []
    share_url: str | None = None
    passcode: str | None = None


class BucketSharePatch(BaseModel):
    can_download: bool | None = None
    can_add_notes: bool | None = None
    can_upload: bool | None = None
    expires_at: datetime | None = None
    file_ids: list[UUID] | None = None
    status: str | None = None


class BucketSharePasscodeResetRead(BaseModel):
    share: BucketShareRead
    passcode: str


class BucketShareAccessRequest(BaseModel):
    passcode: str


class BucketShareInfoRead(BaseModel):
    bucket: BucketRead
    recipient_name: str
    recipient_email: str | None
    can_download: bool
    can_add_notes: bool


class BucketShareFileRead(BucketFileRead):
    preview_url: str | None = None
    download_url: str | None = None


class BucketShareAccessRead(BaseModel):
    bucket: BucketRead
    share: BucketShareRead
    files: list[BucketShareFileRead]
    notes: list[BucketNoteRead]


class BucketNoteCreate(BaseModel):
    content: str = Field(min_length=1)
    visibility: str = "admin"


class BucketSharedNoteCreate(BaseModel):
    passcode: str
    content: str = Field(min_length=1)


class BucketSharedDownloadCreate(BaseModel):
    passcode: str


class BucketNoteRead(ORMModel):
    id: UUID
    bucket_id: UUID
    share_id: UUID | None
    author_name: str
    author_role: str
    visibility: str
    content: str
    created_at: datetime
    updated_at: datetime


class BucketActivityRead(ORMModel):
    id: UUID
    bucket_id: UUID
    actor_user_id: UUID | None
    actor_name: str | None
    actor_email: str | None
    actor_role: str | None
    action: str
    target_type: str | None
    target_id: str | None
    detail: str | None
    ip_address: str | None
    user_agent: str | None
    created_at: datetime


class BucketActivityPage(BaseModel):
    items: list[BucketActivityRead]
    total: int
    limit: int
    offset: int
