from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel


class OfferingRequirement(BaseModel):
    requirement_key: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=200)
    requirement_kind: Literal["field", "document", "form", "consent", "human_review"] = "document"
    canonical_key: str | None = Field(default=None, max_length=120)
    required_level: Literal["required", "recommended", "optional"] = "required"
    waivable: bool = False
    verification_required: bool = False
    completion_mode: Literal["ai_can_complete", "requires_human_verify"] = "ai_can_complete"
    expiration_days: int | None = Field(default=None, ge=1, le=3650)
    objective_text: str | None = Field(default=None, max_length=4000)
    completion_criteria: str | None = Field(default=None, max_length=4000)
    visibility: Literal["client", "internal"] = "client"
    collection_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_collection_policy(self):
        limits = {"required_months": 60, "required_years": 10, "minimum_count": 100, "expiration_days": 3650}
        for key, maximum in limits.items():
            value = self.collection_config.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum):
                raise ValueError(f"{key} must be an integer between 1 and {maximum}")
        if self.collection_config.get("required_months") and self.collection_config.get("required_years"):
            raise ValueError("Choose month coverage or year coverage for one requirement")
        classes = self.collection_config.get("evidence_classes")
        if classes is not None and (not isinstance(classes, list) or len(classes) > 30 or any(not isinstance(item, str) or not item.strip() or len(item) > 120 for item in classes)):
            raise ValueError("Evidence classes must be a list of document classifications")
        return self


class LenderOfferingCreate(BaseModel):
    program_key: str = Field(default="*", min_length=1, max_length=64)
    checklist_confirmed: bool = False
    source_reference: str | None = Field(default=None, max_length=2000)
    requirements: list[OfferingRequirement] = Field(default_factory=list, max_length=100)
    fit_rules: dict[str, Any] = Field(default_factory=dict)
    official_forms_required: bool = False
    official_form_file_ids: list[UUID] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def confirmed_source(self):
        if self.checklist_confirmed and not self.source_reference:
            raise ValueError("A confirmed lender checklist requires a source reference")
        keys = [item.requirement_key for item in self.requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("Each requirement key must be unique")
        return self


class LenderOfferingRead(ORMModel):
    id: UUID
    lender_id: UUID
    program_key: str
    version: int
    status: str
    checklist_confirmed: bool
    source_reference: str | None
    requirements: list[OfferingRequirement]
    fit_rules: dict
    official_forms_required: bool
    official_form_file_ids: list[UUID]


class SubmissionTarget(BaseModel):
    lender_id: UUID
    offering_id: UUID | None = None
    additional_file_ids: list[UUID] = Field(default_factory=list, max_length=100)
    warning_acknowledged: bool = False


class SubmissionBatchCreate(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    targets: list[SubmissionTarget] = Field(min_length=1, max_length=30)
    expires_in_days: int = Field(default=7, ge=1, le=30)

    @model_validator(mode="after")
    def unique_lenders(self):
        ids = [item.lender_id for item in self.targets]
        if len(set(ids)) != len(ids):
            raise ValueError("Choose each lender once per batch")
        return self


class SubmissionApproval(BaseModel):
    confirmed: Literal[True]
    submission_ids: list[UUID] | None = Field(default=None, max_length=30)


class LenderSubmissionRead(ORMModel):
    id: UUID
    lender_id: UUID
    lender_name: str
    offering_id: UUID | None
    recipient_email: str | None
    status: str
    version: int
    fit_status: str
    warning_acknowledged: bool
    blockers: list[dict]
    requirements_snapshot: list[dict]
    manifest: dict
    expires_at: datetime
    revoked_at: datetime | None
    queued_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    viewed_at: datetime | None
    downloaded_at: datetime | None
    attempt_count: int
    last_error: str | None


class SubmissionBatchRead(ORMModel):
    id: UUID
    profile_id: UUID
    status: str
    created_at: datetime
    approved_at: datetime | None
    submissions: list[LenderSubmissionRead] = Field(default_factory=list)
