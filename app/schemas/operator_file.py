"""Unified operator file read models.

These schemas are the API contract for QCDashboard's Operator Console spine:
Deals, Loans, Buckets, AI intakes, and Dealer OS rep files are still stored in
their domain tables, but the dashboard reads them through one normalized shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field


UnifiedVertical = Literal["real_estate", "main_street", "dealer", "mca"]
UnifiedOrigin = Literal["console", "agent", "rep", "dealer", "ai_intake"]
UnifiedSourceKind = Literal["deal", "loan", "intake", "bucket", "dealer"]
UnifiedTone = Literal["ok", "warn", "bad", "mut", "acc", "gold", "pet"]


class UnifiedStage(BaseModel):
    key: str
    label: str
    index: int
    total: int
    family: Literal["working", "funding"]


class UnifiedDocumentProgress(BaseModel):
    docs_uploaded: int = 0
    docs_total: int = 0
    signatures_uploaded: int = 0
    signatures_total: int = 0
    bucket_progress_label: str = "No room"


class UnifiedFileRow(BaseModel):
    id: str
    source_kind: UnifiedSourceKind
    source_id: UUID
    ref: str
    title: str
    subtitle: str | None = None
    principal: str | None = None
    phone: str | None = None
    client_id: UUID | None = None
    client_name: str | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    intake_id: UUID | None = None
    bucket_id: UUID | None = None
    dealer_id: UUID | None = None
    vertical: UnifiedVertical
    vertical_label: str
    origin: UnifiedOrigin
    origin_label: str
    source_label: str
    amount: float | None = None
    amount_label: str | None = None
    working_stage: UnifiedStage | None = None
    funding_stage: UnifiedStage | None = None
    normalized_stage: str
    stage_tone: UnifiedTone = "mut"
    health: str
    health_tone: UnifiedTone = "mut"
    document_progress: UnifiedDocumentProgress = Field(default_factory=UnifiedDocumentProgress)
    program_tags: list[str] = Field(default_factory=list)
    owner_name: str | None = None
    rep_name: str | None = None
    dealer_name: str | None = None
    case_ref: str | None = None
    linked_bucket_ids: list[UUID] = Field(default_factory=list)
    linked_intake_ids: list[UUID] = Field(default_factory=list)
    source_deal_id: UUID | None = None
    promoted_loan_id: UUID | None = None
    updated_at: datetime

    @computed_field
    @property
    def label(self) -> str:
        return self.title

    @computed_field
    @property
    def business_name(self) -> str | None:
        if self.title and self.title != self.principal and self.source_kind in {"intake", "loan", "dealer"}:
            return self.title
        return None

    @computed_field
    @property
    def bucket_name(self) -> str | None:
        return self.title if self.source_kind == "bucket" else None

    @computed_field
    @property
    def source_url(self) -> str | None:
        return None

    @computed_field
    @property
    def created_at(self) -> datetime:
        return self.updated_at

    @computed_field
    @property
    def stage(self) -> UnifiedStage:
        if self.funding_stage is not None:
            return self.funding_stage
        if self.working_stage is not None:
            return self.working_stage
        key = self.normalized_stage.lower().replace(" ", "_")
        return UnifiedStage(key=key, label=self.normalized_stage, index=1, total=1, family="working")

    @computed_field
    @property
    def coverage(self) -> str:
        progress = self.document_progress
        total = progress.docs_total + progress.signatures_total
        uploaded = progress.docs_uploaded + progress.signatures_uploaded
        if total <= 0:
            return "none"
        if uploaded >= total:
            return "complete"
        if uploaded > 0:
            return "partial"
        return "missing"


class UnifiedRollup(BaseModel):
    total: int = 0
    by_vertical: dict[str, int] = Field(default_factory=dict)
    by_origin: dict[str, int] = Field(default_factory=dict)
    by_stage: dict[str, int] = Field(default_factory=dict)
    needs_attention: int = 0
    promoted: int = 0

    @computed_field
    @property
    def working(self) -> int:
        return max(0, self.total - self.promoted)

    @computed_field
    @property
    def real_estate(self) -> int:
        return self.by_vertical.get("real_estate", 0)

    @computed_field
    @property
    def main_street(self) -> int:
        return self.by_vertical.get("main_street", 0)

    @computed_field
    @property
    def dealer(self) -> int:
        return self.by_vertical.get("dealer", 0)

    @computed_field
    @property
    def mca(self) -> int:
        return self.by_vertical.get("mca", 0)


class UnifiedFilePage(BaseModel):
    items: list[UnifiedFileRow]
    rollup: UnifiedRollup
    limit: int = 250
    filters: dict[str, str | None] = Field(default_factory=dict)


class UnifiedAuditItem(BaseModel):
    id: UUID
    action: str
    actor_name: str | None = None
    actor_role: str | None = None
    detail: str | None = None
    created_at: datetime


class UnifiedFileDetail(BaseModel):
    file: UnifiedFileRow
    audit: list[UnifiedAuditItem] = Field(default_factory=list)


class BucketIntakeLinkRequest(BaseModel):
    bucket_id: UUID
    intake_id: UUID
    relationship: Literal["primary", "supporting", "source"] = "primary"
    file_ids: list[UUID] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)


class BucketIntakeLinkResult(BaseModel):
    ok: bool = True
    bucket_id: UUID
    intake_id: UUID
    relationship: Literal["primary", "supporting", "source"] = "primary"
    linked_file_ids: list[UUID]
    audit_ids: list[UUID] = Field(default_factory=list)
    audit_action: str
    bucket_context: dict
