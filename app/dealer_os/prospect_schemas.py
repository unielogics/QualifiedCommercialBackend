"""Request/response contracts for the Field Desk prospect pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.phone import RequiredPhone


class ProspectStageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    label: str
    sort_order: int
    position: int
    is_active: bool
    is_terminal: bool
    is_system: bool
    behavior: dict[str, Any] = Field(default_factory=dict)


class ProspectOutcomeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    label: str
    sort_order: int
    is_active: bool
    is_system: bool
    action_config: dict[str, Any] = Field(default_factory=dict)
    position: int
    requires_follow_up: bool = False
    requires_appointment: bool = False
    creates_email_draft: bool = False


class ProspectActivityRead(BaseModel):
    id: UUID
    prospect_id: UUID
    actor_user_id: UUID | None
    actor_name: str | None = None
    kind: str
    body: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProspectRead(BaseModel):
    id: UUID
    owner_user_id: UUID | None
    company_id: UUID
    primary_contact_id: UUID
    contact_id: UUID
    contact_name: str
    name: str
    dealer_name: str
    email: str
    phone: str
    stage_id: UUID
    stage_key: str
    stage_label: str
    stage_sort_order: int
    source: str
    next_follow_up_at: datetime | None
    last_activity_at: datetime | None
    call_attempt_count: int
    last_outcome_key: str | None = None
    last_outcome_label: str | None = None
    last_outcome_at: datetime | None = None
    do_not_contact: bool
    do_not_contact_reason: str | None
    appointment_id: UUID | None
    converted_intake_id: UUID | None
    converted_at: datetime | None
    version: int
    owner_name: str | None = None
    marketing_sms_consent: bool = False
    created_at: datetime
    updated_at: datetime
    activities: list[ProspectActivityRead] = Field(default_factory=list)


class ProspectListRead(BaseModel):
    items: list[ProspectRead]
    total: int
    limit: int
    offset: int
    stages: list[ProspectStageRead]
    outcomes: list[ProspectOutcomeRead]


class ProspectOwnerRead(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str | None = None
    title: str | None = None
    role: str


class ProspectUserAccessRead(BaseModel):
    """One operator's persisted and effective pipeline access state."""

    user_id: UUID
    name: str
    email: str
    role: str
    account_status: str
    field_desk_access: bool
    eligible: bool
    enabled: bool
    effective_enabled: bool
    updated_at: datetime | None = None


class ProspectUserAccessList(BaseModel):
    global_enabled: bool
    items: list[ProspectUserAccessRead]


class ProspectUserAccessPatch(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        if value is None:
            return None
        return str(value).strip() or None


class ProspectCreate(BaseModel):
    contact_id: UUID | None = None
    contact_name: str = Field(
        min_length=1,
        max_length=160,
        validation_alias=AliasChoices("contact_name", "name"),
    )
    dealer_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    source: str = Field(default="quick_add", min_length=1, max_length=32)
    owner_user_id: UUID | None = None

    @field_validator("contact_name", "dealer_name", "source", mode="before")
    @classmethod
    def strip_required(cls, value: object) -> object:
        return str(value).strip() if value is not None else value


class ProspectPatch(BaseModel):
    expected_version: int = Field(ge=1)
    contact_name: str | None = Field(default=None, min_length=1, max_length=160)
    dealer_name: str | None = Field(default=None, min_length=1, max_length=180)
    email: EmailStr | None = None
    phone: RequiredPhone | None = None
    owner_user_id: UUID | None = None
    next_follow_up_at: datetime | None = None

    @field_validator("contact_name", "dealer_name", mode="before")
    @classmethod
    def strip_optional(cls, value: object) -> object:
        return str(value).strip() if value is not None else value


ProspectMoveAction = Literal["none", "draft_email", "book_appointment"]


class ProspectMoveStage(BaseModel):
    stage_key: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)
    next_follow_up_at: datetime | None = None
    action: ProspectMoveAction | None = None
    appointment_id: UUID | None = None
    confirm_do_not_contact: bool = False

    @field_validator("stage_key", mode="before")
    @classmethod
    def normalize_stage_key(cls, value: object) -> object:
        return str(value).strip().lower() if value is not None else value


class ProspectMoveResult(ProspectRead):
    """Move envelope plus legacy top-level fields during the UI rollout."""

    prospect: ProspectRead
    email_draft_id: UUID | None = None


class ProspectActivityCreate(BaseModel):
    kind: Literal["internal_note", "call_note"] = "internal_note"
    body: str = Field(min_length=1, max_length=5000)

    @field_validator("body", mode="before")
    @classmethod
    def strip_body(cls, value: object) -> object:
        return str(value).strip() if value is not None else value


class ProspectUndoRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ProspectOutcomeApply(BaseModel):
    outcome_key: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)
    next_follow_up_at: datetime | None = None
    appointment_id: UUID | None = None

    @field_validator("outcome_key", mode="before")
    @classmethod
    def normalize_outcome_key(cls, value: object) -> object:
        return str(value).strip().lower() if value is not None else value


class ProspectOutcomeResult(BaseModel):
    prospect: ProspectRead
    outcome: ProspectOutcomeRead
    email_action: str | None = None
    workflow_action: str | None = None
    email_draft_id: UUID | None = None


class ProspectStageCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    key: str | None = Field(default=None, min_length=1, max_length=64)
    is_terminal: bool = False
    behavior: dict[str, Any] = Field(default_factory=dict)


class ProspectStagePatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None
    is_terminal: bool | None = None
    behavior: dict[str, Any] | None = None


class ProspectOutcomeCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    key: str | None = Field(default=None, min_length=1, max_length=64)
    action_config: dict[str, Any] = Field(default_factory=dict)


class ProspectOutcomePatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None
    action_config: dict[str, Any] | None = None


class ProspectDefinitionReorder(BaseModel):
    ordered_ids: list[UUID] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> ProspectDefinitionReorder:
        if len(set(self.ordered_ids)) != len(self.ordered_ids):
            raise ValueError("ordered_ids cannot contain duplicates")
        return self


class ProspectConversionRequest(BaseModel):
    action: Literal["detect", "link", "reactivate", "create"] = "detect"
    expected_version: int = Field(ge=1)
    intake_id: UUID | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def intake_for_existing_action(self) -> ProspectConversionRequest:
        if self.action in {"link", "reactivate"} and self.intake_id is None:
            raise ValueError("intake_id is required to link or reactivate")
        if self.action in {"detect", "create"} and self.intake_id is not None:
            raise ValueError("intake_id is only valid for link or reactivate")
        return self


class ProspectIntakeCandidate(BaseModel):
    id: UUID
    status: str
    outcome_status: str
    full_name: str
    business_name: str | None
    email: str
    phone: str | None
    created_at: datetime
    match_reasons: list[Literal["email", "phone", "dealer_name"]] = Field(default_factory=list)


class ProspectConversionResult(BaseModel):
    status: Literal["choice_required", "linked", "reactivated", "created", "already_converted"]
    prospect: ProspectRead
    candidates: list[ProspectIntakeCandidate] = Field(default_factory=list)
    intake_id: UUID | None = None
    route: str | None = None
