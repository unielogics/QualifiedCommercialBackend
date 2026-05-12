"""Pydantic schemas for the AI Deal Secretary API.

Two surfaces:
  • Read shapes returned by GET /loans/{id}/deal-secretary — the
    two-column "Human owns" vs "AI owns" view plus the file-level
    settings strip.
  • Write shapes for POST /assign, /unassign, PATCH assignment,
    PATCH file-settings, POST /wizard-intent (pre-loan buffer),
    POST /bootstrap (repair).

Literals mirror the enums in app/enums.py exactly. Adding a new
value is a 3-place change: the enum, this file, and the matching
catalog schema in lending_admin / me.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


# Mirrored from app/enums.py.
TaskOwnerTypeLiteral = Literal["human", "ai", "shared", "funding_locked"]
OutreachChannelLiteral = Literal["portal", "email", "sms", "voice"]
OutreachModeLiteral = Literal[
    "off", "draft_first", "portal_auto", "portal_email", "portal_email_sms",
]
ApprovalModeLiteral = Literal[
    "draft_first", "auto_send_portal", "require_per_message",
]
CompletionModeLiteral = Literal[
    "ai_can_complete", "requires_human_verify", "borrower_self_attest",
]
LinkKindLiteral = Literal["docusign", "esign", "external_form", "reference"]
RequirementCategoryLiteral = Literal[
    "borrower_info", "property_data", "financials", "credit", "agreements",
    "insurance", "title_and_escrow", "appraisal_and_inspection", "scheduling",
    "compliance", "communication", "ai_internal",
]


# ── Per-task cadence policy (lives in AITaskAssignment.cadence JSONB) ─


class CadencePolicy(BaseModel):
    """Per-task pacing rules. None values fall back to the catalog
    default_cadence_hours + a hard system max of 7 attempts."""
    hours_between_attempts: int = 48
    max_attempts: int = 3
    escalation_user_id: UUID | None = None
    quiet_hours_start: str | None = None  # "08:00" 24h local format
    quiet_hours_end: str | None = None


# ── Consent state (lives in AITaskAssignment.consent_state JSONB) ─


class SmsConsent(BaseModel):
    state: Literal["granted", "not_granted", "revoked"] = "not_granted"
    captured_at: datetime | None = None
    language: str | None = None  # exact opt-in text the borrower agreed to


class EmailOptOut(BaseModel):
    opted_out_at: datetime | None = None
    reason: str | None = None


class ConsentState(BaseModel):
    sms: SmsConsent | None = None
    email: EmailOptOut | None = None
    voice: Literal["disabled"] = "disabled"  # voice off in v1, no override


# ── File-level settings (lives in ClientAIPlan.ai_secretary_settings) ─


class FollowUpSettingsSchema(BaseModel):
    """Per-file AI re-engagement cadence. All optional — server falls
    back to firm default → hard-coded floor when any field is unset.

    See app/services/ai/follow_up.py for the resolver logic."""
    stall_threshold_minutes: int | None = None
    max_attempts_per_day: int | None = None
    max_days_without_reply: int | None = None
    quiet_hours_start: int | None = None  # 0-23
    quiet_hours_end: int | None = None    # 0-23


class FileSettings(BaseModel):
    """The file-level OutreachMode kill switch + supporting config.

    `outreach_mode` is the prominent toggle at the top of the workbench:
      off              — nothing sends; AI may still track + draft.
      draft_first      — drafts land in AI Inbox; human reviews/sends.
      portal_auto      — portal sends auto; email/SMS still draft-first.
      portal_email     — portal + email auto; SMS still draft-first.
      portal_email_sms — everything auto (consent + quiet hours still apply).
    """
    outreach_mode: OutreachModeLiteral = "draft_first"
    complete_file_by: date | None = None
    sms_consent: SmsConsent | None = None
    email_opt_out: EmailOptOut | None = None
    default_cadence: CadencePolicy | None = None
    follow_up: FollowUpSettingsSchema | None = None


# ── Read shapes ────────────────────────────────────────────────────


class TaskRow(BaseModel):
    """One requirement on the workbench — left or right column. The
    front-end picker treats this as the canonical TaskCard payload.

    Joins ClientRequirementStatus ⋈ AICollectionRequirement so the
    UI has every field it needs in one round trip (label, category,
    objective, completion criteria, link)."""
    client_requirement_status_id: UUID
    requirement_key: str
    label: str
    category: RequirementCategoryLiteral
    required_level: Literal["required", "recommended", "optional"]
    status: str
    """missing | asked | provided_unverified | uploaded | verified |
    not_applicable | waived | needed_later | conflicted | stalled."""

    owner_type: TaskOwnerTypeLiteral
    source: str
    """platform | funding_required | agent_playbook | client_custom |
    ai_detected | underwriter_added."""

    # Catalog-level metadata the UI needs to render TaskCard.
    objective_text: str
    completion_criteria: str
    completion_mode: CompletionModeLiteral
    visibility: list[str]
    can_agent_override: bool
    can_underwriter_waive: bool
    blocks_stage: str | None
    display_order: int

    # Effective link (assignment override winning over catalog default).
    link_url: str | None = None
    link_label: str | None = None
    link_kind: LinkKindLiteral | None = None

    # ---- Timeline + grouping (alembic 0040) ----
    depends_on: list[str] = Field(default_factory=list)
    parent_key: str | None = None
    inferred_depends_on: list[str] = Field(default_factory=list)
    deps_confirmed: bool = True
    # Computed timeline state — set by the resolver, not stored.
    timeline_state: Literal["next_up", "in_progress", "upcoming", "done", "waived"] | None = None
    blocked_by: list[str] = Field(default_factory=list)
    """When timeline_state='upcoming', the requirement_keys still
    pending (subset of depends_on that aren't done yet)."""

    # Per-deal state.
    due_at: date | None
    last_requested_at: datetime | None
    last_response_at: datetime | None

    # AITaskAssignment data, present only when owner_type='ai'.
    assignment_id: UUID | None = None
    instructions: str | None = None
    instructions_visibility: str | None = None
    channels: list[OutreachChannelLiteral] | None = None
    cadence: CadencePolicy | None = None
    approval_mode: ApprovalModeLiteral | None = None
    complete_file_by: date | None = None
    consent_state: ConsentState | None = None
    attempts_made: int | None = None
    next_run_at: datetime | None = None


class DealSecretaryView(BaseModel):
    """Top-level GET /loans/{id}/deal-secretary response. The picker
    renders `left` and `right` as two columns; the strip at the top
    reads `file_settings`.

    loan_id is nullable since the client-scoped variant
    (GET /clients/{id}/ai-follow-up) returns the same shape scoped to
    a deal — pre-promotion the agent file has no loan yet."""
    loan_id: UUID | None = None
    client_id: UUID
    left: list[TaskRow]        # owner_type IN (human, shared, funding_locked)
    right: list[TaskRow]       # owner_type = ai
    file_settings: FileSettings
    funding_locked_count: int  # client-side tooltip: "N items locked by funding"
    # Timeline sections — same TaskRow shapes, just bucketed by
    # computed timeline_state. Workbench renders these instead of /
    # alongside left+right depending on view mode.
    next_up: list[TaskRow] = Field(default_factory=list)
    in_progress: list[TaskRow] = Field(default_factory=list)
    upcoming: list[TaskRow] = Field(default_factory=list)
    done: list[TaskRow] = Field(default_factory=list)


# ── Write shapes ───────────────────────────────────────────────────


class AssignRequest(BaseModel):
    """Move a requirement to the AI column. The handler creates an
    AITaskAssignment with catalog-default channels/cadence/objective/
    completion_mode unless explicitly overridden here."""
    requirement_key: str
    instructions: str | None = None
    instructions_visibility: Literal["internal", "agent", "borrower"] = "agent"
    channels: list[OutreachChannelLiteral] | None = None
    cadence: CadencePolicy | None = None
    approval_mode: ApprovalModeLiteral | None = None
    complete_file_by: date | None = None
    link_url: str | None = None
    link_label: str | None = None
    link_kind: LinkKindLiteral | None = None
    objective_text: str | None = None
    completion_criteria: str | None = None
    completion_mode: CompletionModeLiteral | None = None


class UnassignRequest(BaseModel):
    """Move a requirement back to the human column. Soft-deletes the
    AITaskAssignment so the partial unique index lets a future
    re-assign create a fresh row."""
    requirement_key: str


class AssignmentUpdateRequest(BaseModel):
    """PATCH editable fields on an existing assignment. Anything None
    keeps the current value."""
    instructions: str | None = None
    instructions_visibility: Literal["internal", "agent", "borrower"] | None = None
    channels: list[OutreachChannelLiteral] | None = None
    cadence: CadencePolicy | None = None
    approval_mode: ApprovalModeLiteral | None = None
    complete_file_by: date | None = None
    link_url: str | None = None
    link_label: str | None = None
    link_kind: LinkKindLiteral | None = None
    objective_text: str | None = None
    completion_criteria: str | None = None
    completion_mode: CompletionModeLiteral | None = None


class FileSettingsUpdate(BaseModel):
    """PATCH the file-level OutreachMode + supporting fields. Anything
    None leaves the existing value untouched."""
    outreach_mode: OutreachModeLiteral | None = None
    complete_file_by: date | None = None
    sms_consent: SmsConsent | None = None
    email_opt_out: EmailOptOut | None = None
    default_cadence: CadencePolicy | None = None
    follow_up: FollowUpSettingsSchema | None = None


# ── Wizard intent (pre-loan buffer) ────────────────────────────────


class WizardAssignmentIntent(BaseModel):
    """One row from Step 4 of AgentLeadModal — the agent picked these
    AI-handled tasks before the loan even existed. Buffered on the
    Client's ClientAIPlan row (loan_id IS NULL); materialized into
    real AITaskAssignment rows by materialize_pending_assignments()
    in deal_secretary.py once the prequal converts to a Loan."""
    requirement_key: str
    instructions: str | None = None
    channels: list[OutreachChannelLiteral] | None = None
    cadence: CadencePolicy | None = None
    approval_mode: ApprovalModeLiteral | None = None
    complete_file_by: date | None = None
    link_url: str | None = None
    link_label: str | None = None
    link_kind: LinkKindLiteral | None = None


class WizardIntentRequest(BaseModel):
    """POSTed by AgentLeadModal / SmartIntakeModal Step 4 just before
    the wizard submits its main create-client / create-intake call.
    No-op if `assignments` is empty and `file_settings` is None."""
    assignments: list[WizardAssignmentIntent] = Field(default_factory=list)
    file_settings: FileSettings | None = None


# ── Action responses ───────────────────────────────────────────────


class BootstrapResponse(BaseModel):
    crs_inserted: int
    crs_skipped: int
    plan_created: bool


class WizardIntentResponse(BaseModel):
    pending_count: int
    file_settings: FileSettings


class AnyOk(BaseModel):
    ok: bool = True
