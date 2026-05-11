"""AI Playbook templates + per-row collection requirements.

A playbook is a versioned bundle of collection requirements owned by
the platform, the funding team, or an individual agent. Each
requirement (`AICollectionRequirement`) carries condition logic,
override flags, and an AI message template the orchestrator uses
when chasing the missing item.

`Client.realtor_profile` (alembic 0030) is the per-client realtor-side
fact bag; `client_ai_plan` (alembic 0032) is the AI's resolved active
plan, which references playbook rows by version.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class AIPlaybookTemplate(TimestampMixin, Base):
    __tablename__ = "ai_playbook_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    """One of: platform | funding | agent."""

    owner_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    """User id of the funding admin / broker. NULL when owner_type=platform."""

    playbook_type: Mapped[str] = mapped_column(String(32), nullable=False)
    """One of: loan_product | buyer | seller | handoff | cadence."""

    product_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """For playbook_type=loan_product: dscr_purchase | dscr_refi | bridge | fix_flip | construction."""

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """Free-form playbook-level rules (handoff gate, AI tone, etc.)."""

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    """Monotonic per (owner_type, owner_id, playbook_type, product_key).
    Active client plans pin to a specific version so a config edit
    doesn't disrupt an in-flight deal."""

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    """draft | published | archived."""

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    """Legacy soft-delete flag. New code reads `status` instead."""

    requirements: Mapped[list["AICollectionRequirement"]] = relationship(
        back_populates="playbook",
        cascade="all, delete-orphan",
        order_by="AICollectionRequirement.display_order",
    )


class AICollectionRequirement(TimestampMixin, Base):
    __tablename__ = "ai_collection_requirements"
    __table_args__ = (
        UniqueConstraint("playbook_id", "requirement_key", name="uq_collection_req_playbook_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_playbook_templates.id", ondelete="CASCADE"),
        nullable=False,
    )

    requirement_key: Mapped[str] = mapped_column(String(120), nullable=False)
    """Stable identifier — e.g. "purchase_contract", "buyer_agency_agreement"."""

    label: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    """RequirementCategory enum (alembic 0038). 12-value closed taxonomy:
    borrower_info | property_data | financials | credit | agreements |
    insurance | title_and_escrow | appraisal_and_inspection | scheduling |
    compliance | communication | ai_internal. Legacy 5-value set
    (fact / document / appointment / agreement / task) was remapped by
    the 0038 upgrade()."""

    required_level: Mapped[str] = mapped_column(String(16), nullable=False)
    """required | recommended | optional."""

    applies_when: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Conditional gate. e.g. {"under_contract": true, "borrower_type": "entity"}.
    NULL = always applies."""

    blocks_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Stage the missing item blocks: prequalification | term_sheet |
    underwriting | closing | showings | listed."""

    visibility: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False,
        default=lambda: ["agent", "underwriter"],
    )
    """Audiences allowed to see this requirement: agent | borrower | underwriter."""

    can_agent_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_underwriter_waive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    verification_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    expiration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Document validity window. NULL = no expiration."""

    ai_request_message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Template the AI uses when asking the borrower/agent for this item."""

    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # ---- AI Deal Secretary fields (alembic 0038) ----
    # These define the catalog-level DEFAULTS that a fresh deal inherits.
    # Per-deal overrides live on AITaskAssignment.

    default_owner_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="human", server_default="human",
    )
    """TaskOwnerType default — what a fresh CRS row's owner is set to
    when bootstrap_requirement_status_rows() walks the resolver. Most
    rows are 'human'; funding-locked items ship as 'funding_locked'."""

    default_channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["portal"], server_default='["portal"]',
    )
    """OutreachChannel default list. Typically ["portal"] for sensitive
    items and ["portal", "email"] for less-sensitive ones."""

    default_cadence_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=48, server_default="48",
    )
    """Hours between follow-up attempts when this requirement lands on
    the AI side of the workbench. Per-deal cadence can override."""

    link_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    """Optional external URL (DocuSign envelope, e-sign template,
    intake form, marketing one-pager). Rendered as a clickable action
    in the AI's borrower-facing message when present."""

    link_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """Display label for link_url (e.g. "Sign Buyer Agency Agreement")."""

    link_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """LinkKind enum — drives icon rendering. docusign | esign |
    external_form | reference."""

    objective_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """One-line plain-language objective ("Collect 2 months of bank
    statements"). Tells the AI what success looks like for this task."""

    completion_criteria: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """Explicit "done" definition. The AI consults this + the
    completion_mode to decide whether to mark the task verified or
    hand off to a human."""

    completion_mode: Mapped[str] = mapped_column(
        String(24), nullable=False,
        default="ai_can_complete", server_default="ai_can_complete",
    )
    """CompletionMode enum — ai_can_complete | requires_human_verify |
    borrower_self_attest. Sensitive items (purchase_contract,
    operating_agreement) ship as requires_human_verify so the AI
    confirms receipt but never marks the task fully complete alone."""

    wrong_upload_response_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Template the AI uses when the document scanner flags a
    wrong-file upload ("Thanks for sending that — looks like it's a
    single statement page; underwriting needs all pages from the
    last two months. Could you upload the full PDF?")."""

    # ---- Timeline + grouping (alembic 0040) ----
    depends_on: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    """Confirmed dependencies — requirement_keys this task waits on
    before it can be marked 'next up'. Drives the timeline view's
    sectioning (Next / In Progress / Upcoming)."""

    parent_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """Optional parent task. Sub-tasks roll up under a parent card in
    the workbench. The parent is virtual from the timeline's
    perspective — its state is the aggregate of its children."""

    inferred_depends_on: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    """AI's suggested dependencies that haven't been confirmed yet.
    Surfaces as dim 'Suggested' chips in the Settings UI; user
    clicks to confirm which moves the keys into `depends_on`."""

    deps_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    """False after the AI inference pass runs and there are
    suggestions waiting for review. UI shows a 'Review suggested
    order' affordance until the user accepts/rejects."""

    playbook: Mapped[AIPlaybookTemplate] = relationship(back_populates="requirements")
