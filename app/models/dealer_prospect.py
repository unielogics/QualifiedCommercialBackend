"""Pre-application dealer prospect pipeline models.

These records deliberately sit before :class:`DealerBusiness` and
``DealerRepLead``.  A prospect is a sales contact, not an underwriting file;
conversion links it to the durable AI-intake record without copying financial
evidence into the CRM.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class DealerProspectStageDefinition(TimestampMixin, Base):
    """Configurable board column with an immutable machine key."""

    __tablename__ = "dealer_prospect_stage_definitions"
    __table_args__ = (
        UniqueConstraint("key", name="uq_dealer_prospect_stage_key"),
        Index("ix_dealer_prospect_stage_order", "is_active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = _pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    behavior: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class DealerProspectOutcomeDefinition(TimestampMixin, Base):
    """Admin-managed call result and its declarative, audited effects."""

    __tablename__ = "dealer_prospect_outcome_definitions"
    __table_args__ = (
        UniqueConstraint("key", name="uq_dealer_prospect_outcome_key"),
        Index("ix_dealer_prospect_outcome_order", "is_active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = _pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    action_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class DealerProspect(TimestampMixin, Base):
    """One dealer prospect shared through owner/assignment access rules."""

    __tablename__ = "dealer_prospects"
    __table_args__ = (
        UniqueConstraint("primary_contact_id", name="uq_dealer_prospect_primary_contact"),
        Index("ix_dealer_prospect_owner_stage", "owner_user_id", "stage_definition_id"),
        Index("ix_dealer_prospect_follow_up", "next_follow_up_at"),
        Index("ix_dealer_prospect_activity", "last_activity_at"),
        Index("ix_dealer_prospect_company", "company_id"),
        Index("ix_dealer_prospect_last_outcome", "last_outcome_definition_id"),
        Index(
            "ix_dealer_prospect_email_identity",
            "email_normalized",
            postgresql_where=text("email_normalized IS NOT NULL"),
        ),
        Index(
            "ix_dealer_prospect_phone_identity",
            "phone_normalized",
            postgresql_where=text("phone_normalized IS NOT NULL"),
        ),
        Index(
            "uq_dealer_prospect_email_active",
            "dealer_name_normalized",
            "email_normalized",
            unique=True,
            postgresql_where=text("archived_at IS NULL AND email_normalized IS NOT NULL"),
        ),
        Index(
            "uq_dealer_prospect_phone_active",
            "dealer_name_normalized",
            "phone_normalized",
            unique=True,
            postgresql_where=text("archived_at IS NULL AND phone_normalized IS NOT NULL"),
        ),
        CheckConstraint("version > 0", name="ck_dealer_prospect_version_positive"),
        CheckConstraint(
            "conversion_target IS NULL OR conversion_target IN "
            "('portfolio_application','dealer_ai_intake')",
            name="ck_dealer_prospect_conversion_target",
        ),
        CheckConstraint(
            "(conversion_target IS NULL AND converted_application_id IS NULL "
            "AND converted_intake_id IS NULL) OR "
            "(conversion_target = 'portfolio_application' AND converted_application_id IS NOT NULL "
            "AND converted_intake_id IS NULL) OR "
            "(conversion_target = 'dealer_ai_intake' AND converted_intake_id IS NOT NULL "
            "AND converted_application_id IS NULL)",
            name="ck_dealer_prospect_conversion_destination",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dos_rep_companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    primary_contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dos_rep_contacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stage_definition_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dealer_prospect_stage_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    last_outcome_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dealer_prospect_outcome_definitions.id", ondelete="SET NULL"),
        nullable=True,
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dos_rep_appointments.id", ondelete="SET NULL"),
        nullable=True,
    )
    converted_intake_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public_underwriting_intakes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # ``DealerBusiness`` is the durable Portfolio application record exposed
    # at /applications/{id}.  Keep this separate from the AI Intake link so a
    # prospect can be converted to exactly the workflow the operator chose.
    conversion_target: Mapped[str | None] = mapped_column(String(32), nullable=True)
    converted_application_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dos_dealers.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # Normalized identity is stored on the prospect as a race-safe duplicate
    # guard.  Contact rows predate this pipeline and do not have global unique
    # constraints, so relying on a read-before-write check alone can duplicate
    # a person when two agents submit at the same time.
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    phone_normalized: Mapped[str] = mapped_column(String(20), nullable=False)
    dealer_name_normalized: Mapped[str] = mapped_column(String(180), nullable=False)
    # Prospect-scoped outreach defaults.  A draft always snapshots these
    # addresses so later preference changes cannot rewrite delivery history.
    default_cc_emails: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="quick_add", server_default="quick_add"
    )
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    call_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    do_not_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    do_not_contact_reason: Mapped[str | None] = mapped_column(String(240))
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class DealerProspectActivity(Base):
    """Append-only event stream for notes and state-changing actions."""

    __tablename__ = "dealer_prospect_activities"
    __table_args__ = (
        Index("ix_dealer_prospect_activity_prospect", "prospect_id", "created_at"),
        Index("ix_dealer_prospect_activity_actor", "actor_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dealer_prospects.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
