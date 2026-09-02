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
    # Generic e-sign extension — a requested document can require the client
    # to sign rather than upload a file (the signed certificate becomes the
    # satisfying file). signature_kind="credit_authorization" drives the
    # identity-fields form; "custom" is any other admin-requested signed form.
    requires_signature: bool = False
    signature_kind: str | None = Field(default=None, max_length=32)
    template_file_id: UUID | None = None
    signature_document_text: str | None = None


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
    requires_signature: bool
    signature_kind: str | None
    template_file_id: UUID | None
    signature_document_text: str | None
    template_download_url: str | None = None


class BucketCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    bucket_type: str | None = Field(default=None, max_length=80)
    client_name: str | None = Field(default=None, max_length=180)
    purpose: str | None = Field(default=None, max_length=255)
    description: str | None = None


class BucketUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=180)
    bucket_type: str | None = Field(default=None, max_length=80)
    client_name: str | None = Field(default=None, max_length=180)
    purpose: str | None = Field(default=None, max_length=255)
    description: str | None = None
    status: str | None = Field(default=None, max_length=40)


class BucketLinkedFileRead(BaseModel):
    id: UUID
    kind: str
    label: str
    reference: str | None = None
    email: str | None = None
    phone: str | None = None
    route: str
    surface: str


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
    file_names: list[str] = Field(default_factory=list)
    linked_files: list[BucketLinkedFileRead] = Field(default_factory=list)


class BucketDetail(BucketRead):
    ai_context: dict | None = None
    requested_documents: list[BucketRequestedDocumentRead] = []
    files: list[BucketFileRead] = []
    upload_links: list[BucketUploadLinkRead] = []
    shares: list[BucketShareRead] = []
    vendor_access: list[BucketVendorAccessRead] = []
    public_shares: list[BucketPublicShareRead] = []
    notes: list[BucketNoteRead] = []
    activity: list[BucketActivityRead] = []


class BucketUploadLinkCreate(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=180)
    recipient_email: EmailStr | None = None
    expires_at: datetime | None = None
    allow_notes: bool = True
    allow_multiple_sessions: bool = True
    can_use_ai_chat: bool = True
    can_view_ai_tasks: bool = True
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
    can_use_ai_chat: bool
    can_view_ai_tasks: bool
    status: str
    completed_at: datetime | None
    created_at: datetime
    upload_url: str | None = None
    passcode: str | None = None


class BucketUploadLinkPasscodeResetRead(BaseModel):
    upload_link: BucketUploadLinkRead
    passcode: str


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


class BucketRequestUploadedFileRead(ORMModel):
    id: UUID
    bucket_id: UUID
    requested_document_id: UUID | None
    parent_zip_file_id: UUID | None = None
    zip_entry_path: str | None = None
    extraction_status: str | None = None
    extraction_reason: str | None = None
    file_name: str
    content_type: str
    size_bytes: int
    uploaded_by_name: str | None
    uploaded_by_email: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class BucketRequestAccessRead(BaseModel):
    bucket: BucketRequestBucketRead
    recipient_name: str
    recipient_email: str | None
    allow_notes: bool
    can_use_ai_chat: bool = True
    can_view_ai_tasks: bool = True
    requested_documents: list[BucketRequestedDocumentRead]
    files: list[BucketRequestUploadedFileRead] = []
    ai_summary: dict | None = None


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
    parent_zip_file_id: UUID | None = None
    zip_entry_path: str | None = None
    extraction_status: str | None = None
    extraction_reason: str | None = None
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
    vendor_access_id: UUID | None = None
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
    can_use_ai_chat: bool = True
    can_view_ai_summary: bool = True
    can_view_ai_tasks: bool = True
    can_propose_tasks: bool = True
    expires_at: datetime | None = None

    @field_validator("recipient_email", mode="before")
    @classmethod
    def empty_email_to_none(cls, value: object) -> object:
        return None if value == "" else value


class BucketShareEmailRequest(BaseModel):
    """Email an existing share's access (link + one-time passcode) from the acting
    admin's connected Gmail. The passcode is supplied by the caller (known only at
    create/regenerate time — the server stores only its hash), so the composer echoes
    it back here to embed in the body."""
    to_emails: list[EmailStr] = Field(min_length=1, max_length=25)
    cc_emails: list[EmailStr] = Field(default_factory=list, max_length=25)
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=12000)


class BucketShareEmailResponse(BaseModel):
    ok: bool
    sent: int
    detail: str | None = None


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
    can_use_ai_chat: bool
    can_view_ai_summary: bool
    can_view_ai_tasks: bool
    can_propose_tasks: bool
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
    can_use_ai_chat: bool | None = None
    can_view_ai_summary: bool | None = None
    can_view_ai_tasks: bool | None = None
    can_propose_tasks: bool | None = None
    expires_at: datetime | None = None
    file_ids: list[UUID] | None = None
    status: str | None = None


class BucketSharePasscodeResetRead(BaseModel):
    share: BucketShareRead
    passcode: str


class BucketVendorRead(ORMModel):
    id: UUID
    email: EmailStr | str
    name: str
    role: str
    created_at: datetime | None = None


class BucketVendorAccessCreate(BaseModel):
    vendor_user_id: UUID | None = None
    vendor_name: str | None = Field(default=None, max_length=180)
    vendor_email: EmailStr | None = None
    file_scope: str = Field(default="all_active", pattern="^(all_active|selected)$")
    file_ids: list[UUID] = []
    can_preview: bool = True
    can_download: bool = False
    can_add_notes: bool = True
    can_see_internal_notes: bool = False
    can_use_ai_chat: bool = True
    can_view_ai_summary: bool = True
    can_view_ai_tasks: bool = True
    can_propose_tasks: bool = True
    expires_at: datetime | None = None

    @field_validator("vendor_email", mode="before")
    @classmethod
    def empty_vendor_email_to_none(cls, value: object) -> object:
        return None if value == "" else value


class BucketVendorAccessPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|revoked)$")
    expires_at: datetime | None = None
    file_scope: str | None = Field(default=None, pattern="^(all_active|selected)$")
    file_ids: list[UUID] | None = None
    can_preview: bool | None = None
    can_download: bool | None = None
    can_add_notes: bool | None = None
    can_see_internal_notes: bool | None = None
    can_use_ai_chat: bool | None = None
    can_view_ai_summary: bool | None = None
    can_view_ai_tasks: bool | None = None
    can_propose_tasks: bool | None = None


class BucketVendorAccessRead(ORMModel):
    id: UUID
    bucket_id: UUID
    vendor_user_id: UUID
    vendor_name: str | None = None
    vendor_email: str | None = None
    status: str
    expires_at: datetime | None
    file_scope: str
    can_preview: bool
    can_download: bool
    can_add_notes: bool
    can_see_internal_notes: bool
    can_use_ai_chat: bool
    can_view_ai_summary: bool
    can_view_ai_tasks: bool
    can_propose_tasks: bool
    last_accessed_at: datetime | None
    view_count: int
    download_count: int
    created_at: datetime
    updated_at: datetime
    files: list[BucketFileRead] = []


class BucketVendorBucketRead(BucketRead):
    vendor_access: BucketVendorAccessRead


class BucketVendorAccessReadResponse(BaseModel):
    bucket: BucketRead
    vendor_access: BucketVendorAccessRead
    files: list[BucketShareFileRead]
    notes: list[BucketNoteRead]
    ai_summary: dict | None = None
    ai_tasks: list[BucketAIActionItemRead] = []


class BucketShareAccessRequest(BaseModel):
    passcode: str


class BucketShareInfoRead(BaseModel):
    bucket: BucketRead
    recipient_name: str
    recipient_email: str | None
    can_download: bool
    can_add_notes: bool
    can_use_ai_chat: bool = True
    can_view_ai_summary: bool = True
    can_view_ai_tasks: bool = True
    can_propose_tasks: bool = True


class BucketShareFileRead(BucketFileRead):
    # Internal storage key must never reach external share/vendor recipients;
    # they download via the server-signed preview_url/download_url only.
    s3_key: str = Field(exclude=True)
    preview_url: str | None = None
    download_url: str | None = None


class BucketShareAccessRead(BaseModel):
    bucket: BucketRead
    share: BucketShareRead
    files: list[BucketShareFileRead]
    notes: list[BucketNoteRead]
    ai_summary: dict | None = None
    ai_tasks: list[BucketAIActionItemRead] = []


class BucketPublicShareCreate(BaseModel):
    """No-login, no-passcode share link. Preview/download only — no notes,
    AI chat, or task capabilities, unlike BucketShare."""
    recipient_name: str | None = Field(default=None, max_length=180)
    file_ids: list[UUID] = Field(min_length=1)
    can_preview: bool = True
    can_download: bool = True
    expires_at: datetime | None = None


class BucketPublicShareRead(ORMModel):
    id: UUID
    bucket_id: UUID
    token: str
    recipient_name: str | None
    can_preview: bool
    can_download: bool
    status: str
    expires_at: datetime | None
    last_accessed_at: datetime | None
    view_count: int
    download_count: int
    created_at: datetime
    files: list[BucketFileRead] = []
    share_url: str | None = None


class BucketPublicShareFileRead(BucketFileRead):
    s3_key: str = Field(exclude=True)
    preview_url: str | None = None
    download_url: str | None = None


class BucketPublicShareAccessRead(BaseModel):
    bucket: BucketRead
    share: BucketPublicShareRead
    files: list[BucketPublicShareFileRead]


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
    vendor_access_id: UUID | None = None
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


class BucketAIContextPatch(BaseModel):
    deal_type: str | None = Field(default=None, max_length=160)
    documentation_level: str | None = Field(default=None, max_length=120)
    collateral_type: str | None = Field(default=None, max_length=160)
    loan_purpose: str | None = Field(default=None, max_length=180)
    underwriting_focus: str | None = Field(default=None, max_length=1200)
    custom_instructions: str | None = Field(default=None, max_length=2500)


class BucketAIReviewCreate(BaseModel):
    context: BucketAIContextPatch | None = None


class BucketAIReviewRead(ORMModel):
    id: UUID
    bucket_id: UUID
    requested_by_user_id: UUID | None
    status: str
    context_snapshot: dict | None
    file_ids: list | None
    provider: str | None
    model: str | None
    result: dict | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BucketAIChatMessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    passcode: str | None = Field(default=None, max_length=80)


class BucketAIMessageRead(ORMModel):
    id: UUID
    bucket_id: UUID
    upload_link_id: UUID | None
    share_id: UUID | None
    vendor_access_id: UUID | None = None
    user_id: UUID | None
    audience: str
    role: str
    author_name: str | None
    content: str
    proposed_context_patch: dict | None
    metadata_json: dict | None
    provider: str | None
    model: str | None
    created_at: datetime


class BucketAIChatResponse(BaseModel):
    messages: list[BucketAIMessageRead]
    proposed_action_items: list[BucketAIActionItemRead] = []


class BucketAIActionItemRead(ORMModel):
    id: UUID
    bucket_id: UUID
    source_message_id: UUID | None
    upload_link_id: UUID | None
    share_id: UUID | None
    vendor_access_id: UUID | None = None
    file_id: UUID | None
    requested_document_id: UUID | None
    status: str
    route: str
    title: str
    instructions: str
    rationale: str | None
    created_by: str
    created_by_user_id: UUID | None
    approved_by_user_id: UUID | None
    approved_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BucketAIActionItemCreate(BaseModel):
    status: str = Field(default="approved", pattern="^(proposed|approved|rejected|completed)$")
    route: str = Field(default="admin", pattern="^(admin|uploader|share|vendor)$")
    upload_link_id: UUID | None = None
    share_id: UUID | None = None
    vendor_access_id: UUID | None = None
    file_id: UUID | None = None
    requested_document_id: UUID | None = None
    title: str = Field(min_length=1, max_length=220)
    instructions: str = Field(min_length=1)
    rationale: str | None = None


class BucketAIActionItemPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(proposed|approved|rejected|completed)$")
    route: str | None = Field(default=None, pattern="^(admin|uploader|share|vendor)$")
    upload_link_id: UUID | None = None
    share_id: UUID | None = None
    vendor_access_id: UUID | None = None
    file_id: UUID | None = None
    requested_document_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=220)
    instructions: str | None = Field(default=None, min_length=1)
    rationale: str | None = None


class BucketAISummaryRead(BaseModel):
    summary: dict | None = None
