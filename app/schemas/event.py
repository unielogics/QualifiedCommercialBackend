from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums import (
    AITaskPriority,
    CalendarEventKind,
    CalendarEventSource,
    CalendarEventStatus,
)
from app.schemas.common import ORMModel


class CalendarEventCreate(BaseModel):
    loan_id: UUID | None = None
    kind: CalendarEventKind
    title: str
    description: str | None = None
    who: str | None = None
    starts_at: datetime
    duration_min: int | None = None
    priority: AITaskPriority | None = None
    owner_user_id: UUID | None = None


class CalendarEventUpdate(BaseModel):
    """All fields optional — partial update semantics. The router
    only persists keys present in the payload (model_dump
    exclude_unset=True)."""
    kind: CalendarEventKind | None = None
    title: str | None = None
    description: str | None = None
    who: str | None = None
    starts_at: datetime | None = None
    duration_min: int | None = None
    priority: AITaskPriority | None = None
    owner_user_id: UUID | None = None
    status: CalendarEventStatus | None = None


class CalendarEventRead(ORMModel):
    id: UUID
    loan_id: UUID | None
    kind: CalendarEventKind
    title: str
    description: str | None = None
    who: str | None
    starts_at: datetime
    duration_min: int | None
    priority: AITaskPriority | None
    status: CalendarEventStatus
    source: CalendarEventSource
    owner_user_id: UUID | None = None
    external_ref_kind: str | None = None
    external_ref_id: str | None = None


class CalendarActivityItem(BaseModel):
    id: UUID
    loan_id: UUID | None
    client_id: UUID | None
    kind: str
    summary: str
    actor_label: str | None = None
    occurred_at: datetime
    payload: dict | None = None


AppointmentCrmStatus = Literal[
    "scheduled",
    "confirmed",
    "completed",
    "follow_up",
    "no_show",
    "not_qualified",
    "converted",
    "cancelled",
]
AppointmentOutcomeEffect = Literal[
    "log_activity",
    "file_action",
    "schedule_follow_up",
    "request_documents",
    "send_no_show_rebooking",
    "close_enquiry",
]


class AppointmentOutcomeDefinitionBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    color: Literal["blue", "green", "amber", "red", "violet", "gray"] = "blue"
    target_crm_status: AppointmentCrmStatus
    effects: list[AppointmentOutcomeEffect] = Field(default_factory=lambda: ["log_activity"], max_length=6)
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=999)

    @model_validator(mode="after")
    def _normalize_effects(self) -> "AppointmentOutcomeDefinitionBase":
        self.name = " ".join(self.name.split())
        self.description = self.description.strip() if self.description else None
        self.effects = list(dict.fromkeys(["log_activity", *self.effects]))
        return self


class AppointmentOutcomeDefinitionCreate(AppointmentOutcomeDefinitionBase):
    pass


class AppointmentOutcomeDefinitionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    color: Literal["blue", "green", "amber", "red", "violet", "gray"] | None = None
    target_crm_status: AppointmentCrmStatus | None = None
    effects: list[AppointmentOutcomeEffect] | None = Field(default=None, max_length=6)
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=999)

    @model_validator(mode="after")
    def _normalize_patch(self) -> "AppointmentOutcomeDefinitionPatch":
        if self.name is not None:
            self.name = " ".join(self.name.split())
        if self.description is not None:
            self.description = self.description.strip() or None
        if self.effects is not None:
            self.effects = list(dict.fromkeys(["log_activity", *self.effects]))
        return self


class AppointmentOutcomeDefinitionRead(AppointmentOutcomeDefinitionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_user_id: UUID
    created_at: datetime
    updated_at: datetime


class CalendarWorkspaceEvent(BaseModel):
    id: str
    event_type: Literal["appointment", "internal"]
    appointment_id: UUID | None = None
    calendar_event_id: UUID | None = None
    loan_id: UUID | None = None
    title: str
    kind: str
    starts_at: datetime
    ends_at: datetime
    status: str
    crm_status: AppointmentCrmStatus | None = None
    invitee_name: str | None = None
    company: str | None = None
    meeting_mode: str | None = None
    join_url: str | None = None
    has_outcome: bool = False
    color: str = "blue"
    can_edit: bool = False


class CalendarWorkspaceMetrics(BaseModel):
    appointments: int = 0
    outcome_logged: int = 0
    awaiting_outcome: int = 0
    files_created: int = 0


class CalendarAppointmentTypeCount(BaseModel):
    key: str
    label: str
    count: int


class CalendarWorkspaceCapabilities(BaseModel):
    can_create: bool = False
    can_manage_all: bool = False
    can_drag: bool = False
    can_create_funding_loan: bool = False


class CalendarWorkspaceRead(BaseModel):
    range_start: datetime
    range_end: datetime
    timezone: str
    events: list[CalendarWorkspaceEvent]
    metrics: CalendarWorkspaceMetrics
    appointment_types: list[CalendarAppointmentTypeCount]
    capabilities: CalendarWorkspaceCapabilities
