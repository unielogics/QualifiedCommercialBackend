"""Capital Partners core schema — all cp_* tables (isolated program).

Revision ID: 0246_cp_core
Revises: 0245_intake_operational_columns
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0246_cp_core"
down_revision = "0245_intake_operational_columns"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _user_fk(name: str):
    return sa.Column(name, pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


def _fk(name: str, table: str, *, nullable: bool = False, ondelete: str = "CASCADE"):
    return sa.Column(name, pg.UUID(as_uuid=True), sa.ForeignKey(f"{table}.id", ondelete=ondelete), nullable=nullable)


def _in(column: str, values: list[str], name: str):
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=name)


MEMBER_STATUS = ["requested", "questionnaire", "under_review", "waiting", "approved", "suspended"]
INVESTING_AS = ["individual", "family_office", "entity"]
ENTITY_KIND = ["individual", "trust", "llc", "partnership", "family_office", "other"]
ACCESS_STATUS = ["new", "contacted", "invited", "declined"]
Q_OUTCOME = ["pending", "accepted", "needs_changes", "rejected"]
LISTING_STATUS = ["draft", "open", "closing", "fully_subscribed", "funded", "repaid", "workout", "withdrawn"]
SOURCE_KIND = ["loan", "profile", "manual"]
VERTICAL = ["real_estate", "auto", "business"]
LIEN = ["first", "second"]
LTV_KIND = ["ltv", "ltc", "cltv"]
FREQ = ["monthly", "quarterly", "at_maturity"]
DOC_KIND = ["ppm", "subscription_agreement", "note", "valuation", "title", "servicing_summary", "other"]
DOC_STATUS = ["pending", "ready", "superseded"]
SUB_STATUS = ["requested", "documents_sent", "signed", "funded", "cancelled", "declined"]
SERVICER_STATUS = ["current", "late", "paid_in_full", "in_workout"]
TRANSFER_STATUS = ["listed", "requested", "pending_consent", "approved", "completed", "withdrawn", "declined"]
FILING_KIND = ["form_d", "form_d_amendment", "state_notice", "bad_actor_check"]
FILING_STATUS = ["due", "filed", "not_required", "cleared", "pending"]


def upgrade() -> None:
    op.create_table(
        "cp_issuer_settings",
        _id(),
        sa.Column("key", sa.String(16), nullable=False, unique=True),
        sa.Column("issuer_name", sa.String(200)),
        sa.Column("cik", sa.String(20)),
        sa.Column("edgar_status", sa.String(32), nullable=False, server_default="not_set"),
        sa.Column("edgar_access_granted_at", sa.Date()),
        sa.Column("admin_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("default_waiting_period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("servicer_label", sa.String(120), nullable=False, server_default="Independent third-party servicer"),
        sa.Column("notify_emails", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("wiring_instructions", sa.Text()),
        *_ts(),
    )

    op.create_table(
        "cp_access_requests",
        _id(),
        sa.Column("investing_as", sa.String(24), nullable=False),
        sa.Column("entity_name", sa.String(200)),
        sa.Column("capital_band", sa.String(40), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, index=True),
        sa.Column("phone", sa.String(40), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("locale", sa.String(5), nullable=False, server_default="en"),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("emailed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="new", index=True),
        _user_fk("invited_user_id"),
        _user_fk("handled_by_id"),
        sa.Column("handled_at", sa.DateTime(timezone=True)),
        sa.Column("handled_note", sa.Text()),
        *_ts(),
        _in("investing_as", INVESTING_AS, "ck_cp_access_requests_investing_as"),
        _in("status", ACCESS_STATUS, "ck_cp_access_requests_status"),
    )

    op.create_table(
        "cp_members",
        _id(),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="requested", index=True),
        sa.Column("investing_as", sa.String(24)),
        sa.Column("display_name", sa.String(200)),
        sa.Column("phone", sa.String(40)),
        sa.Column("state_of_residence", sa.String(2)),
        sa.Column("capital_band", sa.String(40)),
        sa.Column("source", sa.String(120)),
        _fk("access_request_id", "cp_access_requests", nullable=True, ondelete="SET NULL"),
        _user_fk("invited_by_id"),
        sa.Column("invited_at", sa.DateTime(timezone=True)),
        sa.Column("relationship_established_at", sa.DateTime(timezone=True)),
        sa.Column("waiting_period_days", sa.Integer()),
        sa.Column("waiting_period_ends_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        _user_fk("approved_by_id"),
        sa.Column("accreditation_basis", sa.String(64)),
        sa.Column("review_notes", sa.Text()),
        sa.Column("suspended_at", sa.DateTime(timezone=True)),
        sa.Column("suspended_reason", sa.Text()),
        sa.Column("last_questionnaire_id", pg.UUID(as_uuid=True)),
        *_ts(),
        _in("status", MEMBER_STATUS, "ck_cp_members_status"),
    )

    op.create_table(
        "cp_member_entities",
        _id(),
        _fk("member_id", "cp_members"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("state", sa.String(2)),
        sa.Column("accreditation_basis", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_ts(),
        _in("kind", ENTITY_KIND, "ck_cp_member_entities_kind"),
    )
    op.create_index("ix_cp_member_entities_member_id", "cp_member_entities", ["member_id"])

    op.create_table(
        "cp_questionnaires",
        _id(),
        _fk("member_id", "cp_members"),
        _fk("entity_id", "cp_member_entities", nullable=True, ondelete="SET NULL"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("answers", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("basis", sa.String(64)),
        sa.Column("investing_as", sa.String(24)),
        sa.Column("state_of_residence", sa.String(2)),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        _user_fk("reviewed_by_id"),
        sa.Column("outcome", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("review_notes", sa.Text()),
        *_ts(),
        _in("outcome", Q_OUTCOME, "ck_cp_questionnaires_outcome"),
    )
    op.create_index("ix_cp_questionnaires_member_id", "cp_questionnaires", ["member_id"])

    op.create_table(
        "cp_listings",
        _id(),
        sa.Column("ref", sa.String(24), nullable=False, unique=True),
        sa.Column("source_kind", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("loan_id", pg.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="SET NULL"), index=True),
        sa.Column(
            "profile_id", pg.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="SET NULL"), index=True
        ),
        sa.Column("vertical", sa.String(16), nullable=False),
        sa.Column("program_label", sa.String(80), nullable=False, server_default="Bridge"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("city", sa.String(120)),
        sa.Column("state", sa.String(2)),
        sa.Column("collateral_type", sa.String(80)),
        sa.Column("lien_position", sa.String(8), nullable=False, server_default="first"),
        sa.Column("note_rate_pct", sa.Numeric(7, 3)),
        sa.Column("term_months", sa.Integer()),
        sa.Column("ltv_pct", sa.Numeric(7, 3)),
        sa.Column("ltv_kind", sa.String(8), nullable=False, server_default="ltv"),
        sa.Column("loan_amount", sa.Numeric(14, 2)),
        sa.Column("offered_amount", sa.Numeric(14, 2)),
        sa.Column("minimum_investment", sa.Numeric(14, 2)),
        sa.Column("payment_frequency", sa.String(16), nullable=False, server_default="monthly"),
        sa.Column("personal_guarantee", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("guarantee_text", sa.String(200)),
        sa.Column("borrower_track_record", sa.String(200)),
        sa.Column("exit_plan", sa.Text()),
        sa.Column("valuation_amount", sa.Numeric(14, 2)),
        sa.Column("valuation_source", sa.String(120)),
        sa.Column("title_status", sa.String(120)),
        sa.Column("servicer_label", sa.String(120)),
        sa.Column("maturity_date", sa.Date()),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft", index=True),
        sa.Column("outcome", sa.String(32)),
        sa.Column("opens_at", sa.DateTime(timezone=True)),
        sa.Column("closes_at", sa.DateTime(timezone=True)),
        sa.Column("first_sale_at", sa.DateTime(timezone=True)),
        sa.Column("funded_at", sa.DateTime(timezone=True)),
        sa.Column("repaid_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        _user_fk("published_by_id"),
        sa.Column("publish_reason", sa.Text()),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("withdrawn_reason", sa.Text()),
        sa.Column("source_snapshot", pg.JSONB(), nullable=False, server_default="{}"),
        _user_fk("created_by_id"),
        *_ts(),
        _in("status", LISTING_STATUS, "ck_cp_listings_status"),
        _in("source_kind", SOURCE_KIND, "ck_cp_listings_source_kind"),
        _in("vertical", VERTICAL, "ck_cp_listings_vertical"),
        _in("lien_position", LIEN, "ck_cp_listings_lien_position"),
        _in("ltv_kind", LTV_KIND, "ck_cp_listings_ltv_kind"),
        _in("payment_frequency", FREQ, "ck_cp_listings_payment_frequency"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('performing', 'repaid_in_full', 'repaid_after_extension', 'in_workout')",
            name="ck_cp_listings_outcome",
        ),
    )

    op.create_table(
        "cp_listing_documents",
        _id(),
        _fk("listing_id", "cp_listings"),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("s3_key", sa.String(600), nullable=False),
        sa.Column("content_type", sa.String(120)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        _user_fk("uploaded_by_id"),
        sa.Column("uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        *_ts(),
        _in("kind", DOC_KIND, "ck_cp_listing_documents_kind"),
        _in("status", DOC_STATUS, "ck_cp_listing_documents_status"),
    )
    op.create_index("ix_cp_listing_documents_listing_id", "cp_listing_documents", ["listing_id"])

    op.create_table(
        "cp_listing_events",
        _id(),
        _fk("listing_id", "cp_listings"),
        sa.Column("kind", sa.String(48), nullable=False),
        _user_fk("actor_user_id"),
        sa.Column("detail", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cp_listing_events_listing_id", "cp_listing_events", ["listing_id"])

    op.create_table(
        "cp_subscriptions",
        _id(),
        _fk("listing_id", "cp_listings"),
        _fk("member_id", "cp_members"),
        _fk("entity_id", "cp_member_entities", nullable=True, ondelete="SET NULL"),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="requested", index=True),
        sa.Column("ack_ppm", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ack_accredited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("documents_sent_at", sa.DateTime(timezone=True)),
        sa.Column("signed_at", sa.DateTime(timezone=True)),
        sa.Column("funded_at", sa.DateTime(timezone=True)),
        sa.Column("funded_amount", sa.Numeric(14, 2)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.Text()),
        sa.Column("declined_at", sa.DateTime(timezone=True)),
        sa.Column("admin_notes", sa.Text()),
        _user_fk("acted_by_id"),
        *_ts(),
        _in("status", SUB_STATUS, "ck_cp_subscriptions_status"),
        sa.CheckConstraint("amount > 0", name="ck_cp_subscriptions_amount_positive"),
    )
    op.create_index("ix_cp_subscriptions_listing_id", "cp_subscriptions", ["listing_id"])
    op.create_index("ix_cp_subscriptions_member_id", "cp_subscriptions", ["member_id"])
    op.create_index("ix_cp_subscriptions_listing_member", "cp_subscriptions", ["listing_id", "member_id"])

    op.create_table(
        "cp_participations",
        _id(),
        sa.Column(
            "subscription_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("cp_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        _fk("listing_id", "cp_listings"),
        _fk("member_id", "cp_members"),
        _fk("entity_id", "cp_member_entities", nullable=True, ondelete="SET NULL"),
        sa.Column("invested_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("principal_outstanding", sa.Numeric(14, 2), nullable=False),
        sa.Column("interest_received_ytd", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("interest_received_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("next_payment_on", sa.Date()),
        sa.Column("servicer_status", sa.String(24), nullable=False, server_default="current"),
        sa.Column("status_as_of", sa.Date()),
        sa.Column("funded_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        _user_fk("updated_by_id"),
        *_ts(),
        _in("servicer_status", SERVICER_STATUS, "ck_cp_participations_servicer_status"),
    )
    op.create_index("ix_cp_participations_listing_id", "cp_participations", ["listing_id"])
    op.create_index("ix_cp_participations_member_id", "cp_participations", ["member_id"])

    op.create_table(
        "cp_transfers",
        _id(),
        sa.Column("ref", sa.String(24), nullable=False, unique=True),
        _fk("participation_id", "cp_participations"),
        _fk("listing_id", "cp_listings"),
        _fk("seller_member_id", "cp_members"),
        sa.Column("principal_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("ask_pct", sa.Numeric(7, 3), nullable=False, server_default="100"),
        sa.Column("listed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="listed", index=True),
        _fk("buyer_member_id", "cp_members", nullable=True, ondelete="SET NULL"),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        _user_fk("consented_by_id"),
        sa.Column("consented_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("declined_at", sa.DateTime(timezone=True)),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        *_ts(),
        _in("status", TRANSFER_STATUS, "ck_cp_transfers_status"),
        sa.CheckConstraint("principal_amount > 0", name="ck_cp_transfers_principal_positive"),
    )
    op.create_index("ix_cp_transfers_participation_id", "cp_transfers", ["participation_id"])
    op.create_index("ix_cp_transfers_listing_id", "cp_transfers", ["listing_id"])
    op.create_index("ix_cp_transfers_seller_member_id", "cp_transfers", ["seller_member_id"])

    op.create_table(
        "cp_filings",
        _id(),
        sa.Column("kind", sa.String(24), nullable=False, index=True),
        _fk("listing_id", "cp_listings", nullable=True, ondelete="SET NULL"),
        sa.Column("state", sa.String(2)),
        sa.Column("subject", sa.String(200)),
        sa.Column("subject_role", sa.String(120)),
        sa.Column("first_sale_at", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.Date()),
        sa.Column("filed_at", sa.Date()),
        sa.Column("checked_at", sa.Date()),
        sa.Column("status", sa.String(16), nullable=False, server_default="due", index=True),
        sa.Column("fee_amount", sa.Numeric(10, 2)),
        sa.Column("reference", sa.String(120)),
        sa.Column("sources", sa.String(300)),
        sa.Column("notes", sa.Text()),
        _user_fk("created_by_id"),
        *_ts(),
        _in("kind", FILING_KIND, "ck_cp_filings_kind"),
        _in("status", FILING_STATUS, "ck_cp_filings_status"),
    )
    op.create_index("ix_cp_filings_listing_id", "cp_filings", ["listing_id"])

    op.create_table(
        "cp_document_access",
        _id(),
        _fk("member_id", "cp_members", nullable=True),
        _user_fk("actor_user_id"),
        _fk("listing_id", "cp_listings"),
        _fk("document_id", "cp_listing_documents"),
        sa.Column("accessed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
    )
    op.create_index("ix_cp_document_access_member_id", "cp_document_access", ["member_id"])
    op.create_index("ix_cp_document_access_listing_id", "cp_document_access", ["listing_id"])
    op.create_index("ix_cp_document_access_document_id", "cp_document_access", ["document_id"])


def downgrade() -> None:
    for table in (
        "cp_document_access",
        "cp_filings",
        "cp_transfers",
        "cp_participations",
        "cp_subscriptions",
        "cp_listing_events",
        "cp_listing_documents",
        "cp_listings",
        "cp_questionnaires",
        "cp_member_entities",
        "cp_members",
        "cp_access_requests",
        "cp_issuer_settings",
    ):
        op.drop_table(table)
