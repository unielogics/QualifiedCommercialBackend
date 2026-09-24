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

from .schemas import RepAppointmentRead


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
    follow_up_state: Literal["none", "upcoming", "due", "overdue"] = "none"
    last_activity_at: datetime | None
    call_attempt_count: int
    last_outcome_key: str | None = None
    last_outcome_label: str | None = None
    last_outcome_at: datetime | None = None
    do_not_contact: bool
    do_not_contact_reason: str | None
    appointment_id: UUID | None
    conversion_target: Literal["portfolio_application", "dealer_ai_intake"] | None = None
    converted_application_id: UUID | None = None
    converted_intake_id: UUID | None
    converted_at: datetime | None
    version: int
    owner_name: str | None = None
    marketing_sms_consent: bool = False
    created_at: datetime
    updated_at: datetime
    activities: list[ProspectActivityRead] = Field(default_factory=list)


class ProspectAppointmentCreate(BaseModel):
    """Book from authoritative prospect data, never retyped contact fields."""

    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=80)
    starts_at: datetime
    duration_min: int | None = Field(default=None, ge=15, le=180)
    meeting_mode: Literal["video", "phone", "in_person"] = "video"
    location: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)
    transactional_sms_consent: bool = False
    # Outcome definitions are admin-configurable. The route resolves this key
    # against the active definitions and verifies that its configured action
    # is a booking action; keeping a hard-coded Literal here made valid custom
    # outcomes impossible to use.
    trigger_outcome_key: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def strip_idempotency_key(cls, value: object) -> object:
        return str(value).strip() if value is not None else value

    @field_validator("trigger_outcome_key", mode="before")
    @classmethod
    def normalize_trigger_outcome_key(cls, value: object) -> object:
        return str(value).strip().lower() if value is not None else value


class ProspectAppointmentDeliveryRead(BaseModel):
    state: Literal["queued", "meet_ready", "action_required"]
    google_sync_status: str
    email_status: str
    sms_status: str
    meet_url: str | None = None
    error: str | None = None


class ProspectAppointmentResult(BaseModel):
    appointment: RepAppointmentRead
    prospect: ProspectRead
    delivery: ProspectAppointmentDeliveryRead
    idempotent_replay: bool = False


class ProspectListRead(BaseModel):
    items: list[ProspectRead]
    total: int
    limit: int
    offset: int
    stages: list[ProspectStageRead]
    outcomes: list[ProspectOutcomeRead]
    server_now: datetime
    follow_up_timezone: str


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
    initial_note: str | None = Field(default=None, max_length=4000)

    @field_validator("contact_name", "dealer_name", "source", mode="before")
    @classmethod
    def strip_required(cls, value: object) -> object:
        return str(value).strip() if value is not None else value

    @field_validator("initial_note", mode="before")
    @classmethod
    def strip_initial_note(cls, value: object) -> object:
        if value is None:
            return None
        return str(value).strip() or None


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


class ProspectCallAttemptCreate(BaseModel):
    method: Literal["google_voice", "device_dialer"]
    # Optional for one-release compatibility with older Field Desk clients.
    # Current clients keep this stable while retrying an uncertain audit write.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=80)


FollowUpChoice = Literal["next_business_day", "two_business_days", "custom"]


class ProspectFollowUpSuggestionRead(BaseModel):
    scheduled_at: datetime
    timezone: str
    business_days: int


class ProspectDuplicateMatchRead(BaseModel):
    entity_type: Literal["prospect", "contact"] = "prospect"
    prospect_id: UUID | None = None
    contact_id: UUID
    owner_user_id: UUID | None = None
    archived: bool
    # Capability is record-specific. Assignment can grant visibility without
    # granting authority to restore the underlying contact relationship.
    can_restore: bool = False
    version: int | None = None
    matched_on: list[Literal["email", "phone"]] = Field(default_factory=list)


class ProspectDuplicateCheckRead(BaseModel):
    blocked: bool
    state: Literal[
        "clear",
        "active_match",
        "archived_match",
        "hidden_match",
        "identity_conflict",
    ]
    email_normalized: str | None = None
    phone_normalized: str | None = None
    visible_matches: list[ProspectDuplicateMatchRead] = Field(default_factory=list)
    assignment_required: bool = False
    can_restore: bool = False
    message: str


class ProspectReassignmentRequestCreate(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    idempotency_key: str = Field(min_length=8, max_length=80)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_identity(self) -> ProspectReassignmentRequestCreate:
        if not self.email and not (self.phone or "").strip():
            raise ValueError("Enter an email address or phone number")
        return self

    @field_validator("idempotency_key", "reason", mode="before")
    @classmethod
    def strip_reassignment_text(cls, value: object) -> object:
        if value is None:
            return None
        return str(value).strip()


class ProspectReassignmentRequestRead(BaseModel):
    status: Literal["accepted"] = "accepted"
    request_token: str


class ProspectTimelineItemRead(BaseModel):
    id: str
    source: Literal["prospect", "message", "sms", "appointment"]
    source_id: UUID
    kind: str
    body: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor_user_id: UUID | None = None
    actor_name: str | None = None
    occurred_at: datetime


class ProspectTimelineRead(BaseModel):
    items: list[ProspectTimelineItemRead] = Field(default_factory=list)
    next_cursor: str | None = None


class ProspectUndoRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ProspectOutcomeApply(BaseModel):
    outcome_key: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)
    next_follow_up_at: datetime | None = None
    follow_up_choice: FollowUpChoice | None = None
    appointment_id: UUID | None = None

    @model_validator(mode="after")
    def validate_follow_up_choice(self) -> ProspectOutcomeApply:
        if self.follow_up_choice == "custom" and self.next_follow_up_at is None:
            raise ValueError("next_follow_up_at is required for a custom follow-up")
        if self.follow_up_choice not in {None, "custom"} and self.next_follow_up_at is not None:
            raise ValueError("next_follow_up_at is only valid for a custom follow-up")
        return self

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


class ProspectPortfolioApplicationCreate(BaseModel):
    entity_type: str = Field(min_length=1, max_length=32)
    requested_amount: float = Field(gt=0, le=999_999_999_999.99)
    funding_purpose: Literal[
        "working_capital", "equipment", "real_estate", "refinance", "floorplan", "other"
    ]
    use_of_proceeds_note: str = Field(min_length=1, max_length=4000)
    secure_room_pin: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("entity_type", "use_of_proceeds_note", mode="before")
    @classmethod
    def strip_portfolio_text(cls, value: object) -> object:
        return str(value).strip() if value is not None else value


class ProspectGeneralConversionRequest(BaseModel):
    target: Literal["portfolio_application", "dealer_ai_intake"]
    action: Literal["detect", "link", "reactivate", "create"] = "detect"
    expected_version: int = Field(ge=1)
    candidate_id: UUID | None = None
    portfolio_application: ProspectPortfolioApplicationCreate | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_conversion_choice(self) -> ProspectGeneralConversionRequest:
        if self.action in {"link", "reactivate"} and self.candidate_id is None:
            raise ValueError("candidate_id is required to link or reactivate")
        if self.action in {"detect", "create"} and self.candidate_id is not None:
            raise ValueError("candidate_id is only valid for link or reactivate")
        if self.target == "portfolio_application":
            if self.action == "create" and self.portfolio_application is None:
                raise ValueError("portfolio_application is required to create an application")
        elif self.portfolio_application is not None:
            raise ValueError("portfolio_application is only valid for Portfolio conversion")
        return self


class ProspectConversionCandidate(BaseModel):
    id: UUID
    target: Literal["portfolio_application", "dealer_ai_intake"]
    status: str
    archived: bool = False
    display_name: str
    email: str | None = None
    phone: str | None = None
    created_at: datetime
    match_reasons: list[Literal["email", "phone", "dealer_name"]] = Field(default_factory=list)
    route: str


class ProspectConversionCandidateList(BaseModel):
    target: Literal["portfolio_application", "dealer_ai_intake"]
    prospect_id: UUID
    already_converted: bool = False
    candidates: list[ProspectConversionCandidate] = Field(default_factory=list)


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
    conversion_target: Literal["portfolio_application", "dealer_ai_intake"] = "dealer_ai_intake"
    application_id: UUID | None = None
    route: str | None = None


class ProspectGeneralConversionResult(BaseModel):
    status: Literal["choice_required", "linked", "reactivated", "created", "already_converted"]
    conversion_target: Literal["portfolio_application", "dealer_ai_intake"]
    prospect: ProspectRead
    candidates: list[ProspectConversionCandidate] = Field(default_factory=list)
    application_id: UUID | None = None
    intake_id: UUID | None = None
    route: str | None = None
