from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    AmortizationStyle,
    DealHealth,
    EntityType,
    ExitStrategy,
    ExperienceTier,
    LoanPurpose,
    LoanSide,
    LoanStage,
    LoanType,
    PrepayPenalty,
    PropertyType,
)
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.activity import Activity
    from app.models.ai_task import AITask
    from app.models.broker import Broker
    from app.models.client import Client
    from app.models.document import Document
    from app.models.email_draft import EmailDraft
    from app.models.event import CalendarEvent
    from app.models.hud import HudLineItem
    from app.models.lender import Lender
    from app.models.loan_participant import LoanParticipant
    from app.models.message import Message
    from app.models.prequal_request import PrequalRequest


class Loan(TimestampMixin, Base):
    __tablename__ = "loans"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Human-friendly deal ID — used in email subject [QC-{deal_id}]
    deal_id: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE")
    )
    broker_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )
    # Connected lender — set when the operator runs the Connect-Lender
    # flow. Adding a row here also creates a hide_identity=True
    # LoanParticipant for the lender, which is what the orchestrator's
    # Gateway Rule machinery actually keys off.
    lender_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="SET NULL"), nullable=True
    )

    # Property
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # USPS 2-letter state code (alembic 0028). Separate from city so
    # the address columns are queryable / sortable / filterable.
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    property_type: Mapped[PropertyType] = mapped_column(String(32), default=PropertyType.SFR)
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    beds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baths: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Number of dwelling units on the property. Drives the
    # per-unit fan-out in the doc checklist (one Lease per unit on a
    # 4-plex, etc.) and surfaces as a column on the Property
    # Details tab. Defaults to 1 so legacy rows behave like SFRs.
    unit_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    annual_taxes: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    annual_insurance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    monthly_hoa: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    # Listing-style fields (alembic 0043) — power the property tab's
    # listing-page render + the Geoapify map.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    lot_size_sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    zoning: Mapped[str | None] = mapped_column(String(40), nullable=True)
    parcel_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    listing_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    highlight_features: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    street_view_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)

    # Loan
    type: Mapped[LoanType] = mapped_column(String(32), nullable=False)
    purpose: Mapped[LoanPurpose | None] = mapped_column(String(32), nullable=True)
    # Buyer-side or seller-side transaction (alembic 0023). Drives
    # checklist filtering — items tagged for the opposite side are
    # not materialized for this loan.
    side: Mapped[LoanSide] = mapped_column(
        String(8), nullable=False, default=LoanSide.BUYER, server_default="buyer"
    )
    stage: Mapped[LoanStage] = mapped_column(String(32), default=LoanStage.PREQUALIFIED)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    ltv: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    ltc: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    arv: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Numeric(9, 6): up to 999.999999 — covers fix-and-flip / bridge
    # rates that routinely sit in the 10-15% range. Old Numeric(7, 6)
    # capped at 9.999999 and 500'd on intake (alembic 0027).
    base_rate: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    discount_points: Mapped[float] = mapped_column(Numeric(5, 3), default=0)
    final_rate: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    origination_pct: Mapped[float] = mapped_column(Numeric(6, 4), default=0.015)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Underwriting
    monthly_rent: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    dscr: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Underwriter fine-tuning (alembic 0044) ──────────────────────────
    # These let the Criteria tab feel like a real underwriter calculator:
    # the operator can fine-tune amortization style, prepay penalty,
    # rental income assumptions, reserves, lender fees, and borrower
    # credit on a per-loan basis. All nullable so legacy rows still work.
    amortization_style: Mapped[AmortizationStyle | None] = mapped_column(String(24), nullable=True)
    prepay_penalty: Mapped[PrepayPenalty | None] = mapped_column(String(16), nullable=True)
    # 0..1 fraction; 0.05 = 5% vacancy. Reduces effective NOI for DSCR.
    vacancy_pct: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    # 0..1 fraction; 0.25 = 25% expense ratio. Reduces NOI for DSCR.
    expense_ratio_pct: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    reserves_required: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    lender_fees: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Underwriter override for the borrower's FICO when the bound client
    # record doesn't yet have a pull. Stored separately so it never
    # silently overwrites a verified pull on the client row.
    fico_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entity_type: Mapped[EntityType | None] = mapped_column(String(24), nullable=True)
    # Borrowing entity display name (alembic 0045) — e.g. "Smith
    # Properties LLC". Distinct from client.name (natural person).
    entity_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    experience_tier: Mapped[ExperienceTier | None] = mapped_column(String(24), nullable=True)

    # ── Funding-file fields (alembic 0048, Phase 4) ───────────────────
    # Loan IS the FundingFile in this product. These four columns turn
    # a freshly-promoted Loan into the durable handoff record. None of
    # them are set on legacy rows; promote_deal_to_loan() stamps them
    # at promotion time.
    source_deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("deals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    """The Deal this loan was promoted from. NULL for legacy /
    operator-created loans that didn't go through the agent flow."""

    source_intake_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    """The public AI intake promoted into this funding file. Explicit
    lineage only; identity fields are never used to infer this link."""

    baseline_profile_snapshot: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
    )
    """Frozen JSON snapshot of the deal-stage ClientAIPlan +
    verified_facts + missing_lending_items + document_refs at promote
    time. Audit-grade: never mutated after creation."""

    handoff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    """AI-generated narrative captured at promote time. Distinct from
    status_summary (which the live summarizer keeps rewriting)."""

    funding_file_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """bridge | dscr_purchase | dscr_refi | fix_flip | construction |
    other. Derived initially from type+purpose; humans may edit."""
    # Type-specific fields. Only surfaced on the matching loan type.
    # F&F + Ground Up:
    construction_holdback_pct: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    draw_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # F&F / Ground Up / Bridge:
    exit_strategy: Mapped[ExitStrategy | None] = mapped_column(String(16), nullable=True)
    # Cash-Out Refi:
    cash_to_borrower: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    seasoning_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Portfolio:
    property_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Date document collection / AI outreach begins. NULL = started
    # immediately at kickoff (current/default behavior). Set only when
    # a broker new-file modal requests a delayed start.
    collection_starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Source attribution / ownership (alembic 0029).
    #
    # source_attribution     direct_borrower | agent_referral | existing_client |
    #                        website | phone_call | other
    # referring_agent_id     FK users.id when source_attribution =
    #                        agent_referral. Drives downstream rev-share.
    # assigned_owner_id      Operational owner — who's running this deal
    #                        from the firm side (defaults to creator).
    # invite_behavior        send_immediately (default) | save_draft |
    #                        send_after_review. Gates whether the
    #                        Clerk invite fires at /intake submit time.
    source_attribution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    referring_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    invite_behavior: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="send_immediately",
        server_default="send_immediately",
    )

    # Living Loan File — generated by app.services.ai.summarizer after every
    # activity event. Always reflects the current state of the deal.
    status_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    deal_health: Mapped[DealHealth] = mapped_column(String(32), default=DealHealth.ON_TRACK, nullable=False)
    # Structured "Living Loan Profile" produced by 'The Associate' summarizer.
    # Shape: {current_status, market_context, bottlenecks, next_actions} —
    # see app/services/ai/summarizer.py and app/schemas/loan.LivingLoanProfile.
    # Nullable so older loans without a refresh still load.
    living_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Phase 6 — debounced Living Loan File refresh. log_activity() flips
    # `summary_dirty=True` whenever a router writes an Activity row for
    # this loan; the scheduler's job_summary_dirty_drain picks dirty
    # loans up every 5 min and runs the summarizer. `summary_refreshed_at`
    # records the last successful drain so the UI can show staleness
    # ("updated 3 min ago"). Both columns added in alembic 0013.
    summary_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    summary_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Deal Workspace — when set, the AI auto-reply path skips this loan until
    # the timestamp passes. Set to now()+1h whenever a super-admin sends a
    # manual message in the client thread (handled in routers/loan_workspace.py).
    ai_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set by the AI's `complete_property_intake` tool once enough
    # property facts have been gathered (beds/baths/sqft/units/year).
    # The intake opener stops asking once this is set; the chat moves
    # on to doc collection.
    intake_complete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client] = relationship(back_populates="loans")
    # Broker is denormalized onto Loan.broker_id (set from Client.broker_id at
    # creation, optionally re-pointed for transfers). One-way relationship —
    # the Broker.clients collection is the canonical reverse, and we don't
    # want a Broker.loans collection-load slowing every broker fetch.
    broker: Mapped["Broker | None"] = relationship(foreign_keys="Loan.broker_id")
    lender: Mapped["Lender | None"] = relationship(back_populates="loans")
    documents: Mapped[list[Document]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    hud_items: Mapped[list[HudLineItem]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    activities: Mapped[list[Activity]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    messages: Mapped[list[Message]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    ai_tasks: Mapped[list[AITask]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    calendar_events: Mapped[list[CalendarEvent]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    participants: Mapped[list[LoanParticipant]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    email_drafts: Mapped[list[EmailDraft]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    prequal_requests: Mapped[list[PrequalRequest]] = relationship(back_populates="loan", cascade="all, delete-orphan")
