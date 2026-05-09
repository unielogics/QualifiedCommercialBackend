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
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    """fact | document | appointment | agreement | task."""

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

    playbook: Mapped[AIPlaybookTemplate] = relationship(back_populates="requirements")
