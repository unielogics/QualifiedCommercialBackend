"""Field Desk CRM, product catalog and discovery sessions.

Revision ID: 0147_field_desk_crm_products
Revises: 0146_public_contract_sign_idempotency
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0147_field_desk_crm_products"
down_revision = "0146_public_contract_sign_idempotency"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("client_requested_amount", sa.Numeric(14, 2)))
    op.add_column(
        "dos_dealers",
        sa.Column("application_lifecycle", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column("dos_rep_inbox_threads", sa.Column("subject_key", sa.String(200)))
    op.execute("UPDATE dos_rep_inbox_threads SET subject_key = lower(trim(subject)) WHERE channel = 'email'")

    op.create_table(
        "dos_rep_companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("industry", sa.String(80)), sa.Column("address", sa.String(240)),
        sa.Column("city", sa.String(120)), sa.Column("state", sa.String(8)),
        sa.Column("zip", sa.String(12)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        *_timestamps(),
    )
    op.create_index("ix_dos_rep_companies_owner", "dos_rep_companies", ["owner_user_id", "updated_at"])
    op.create_index("ix_dos_rep_companies_name", "dos_rep_companies", ["owner_user_id", "name"])
    op.add_column(
        "dos_rep_contacts",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_companies.id", ondelete="SET NULL")),
    )

    op.create_table(
        "dos_rep_contact_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("contact_id", "user_id", name="uq_dos_rep_contact_assignment"),
    )
    op.create_index("ix_dos_rep_contact_assignments_user", "dos_rep_contact_assignments", ["user_id", "contact_id"])

    op.create_table(
        "dos_application_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship", sa.String(24), nullable=False, server_default="owner"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.UniqueConstraint("dealer_id", "contact_id", name="uq_dos_application_contact"),
    )
    op.create_index("ix_dos_application_contacts_contact", "dos_application_contacts", ["contact_id", "dealer_id"])

    op.create_table(
        "dos_product_catalog",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("program_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("copy", postgresql.JSONB(), nullable=False),
        sa.Column("pricing", postgresql.JSONB()), sa.Column("eligibility", postgresql.JSONB()),
        sa.Column("disclosures", postgresql.JSONB()), sa.Column("amount_min", sa.Numeric(14, 2)),
        sa.Column("amount_max", sa.Numeric(14, 2)), sa.Column("term_min_months", sa.Integer()),
        sa.Column("term_max_months", sa.Integer()), sa.Column("effective_at", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("program_key", "version", name="uq_dos_product_catalog_version"),
    )
    op.create_index("ix_dos_product_catalog_active", "dos_product_catalog", ["active", "category", "sort_order"])

    op.create_table(
        "dos_product_finder_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False, server_default="en"),
        sa.Column("status", sa.String(24), nullable=False, server_default="screening"),
        sa.Column("answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("current_result", postgresql.JSONB()),
        sa.Column("client_requested_amount", sa.Numeric(14, 2)),
        sa.Column("recommended_amount", sa.Numeric(14, 2)),
        sa.Column("funding_goal_confirmed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_dos_product_finder_owner", "dos_product_finder_sessions", ["owner_user_id", "updated_at"])
    op.create_index("ix_dos_product_finder_contact", "dos_product_finder_sessions", ["contact_id", "updated_at"])

    op.create_table(
        "dos_product_screening_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_product_finder_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(24), nullable=False, server_default="self_reported"),
        sa.Column("inputs", postgresql.JSONB(), nullable=False), sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_dos_product_screening_session", "dos_product_screening_snapshots", ["session_id", "created_at"])

    op.create_table(
        "dos_product_presentations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_companies.id", ondelete="SET NULL")),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_product_finder_sessions.id", ondelete="SET NULL")),
        sa.Column("program_keys", postgresql.JSONB(), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False, server_default="en"),
        sa.Column("channel", sa.String(16), nullable=False, server_default="in_person"),
        sa.Column("delivery_status", sa.String(24), nullable=False, server_default="presented"),
        sa.Column("contact_share_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_contact_shares.id", ondelete="SET NULL")),
        sa.Column("inbox_thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_rep_inbox_threads.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_dos_product_presentations_contact", "dos_product_presentations", ["contact_id", "created_at"])
    op.create_index("ix_dos_product_presentations_owner", "dos_product_presentations", ["owner_user_id", "created_at"])

    # One company per existing application prevents unsafe same-name merges.
    op.execute("""
        INSERT INTO dos_rep_companies (id, owner_user_id, name, industry, address, city, state, zip, status)
        SELECT id, owner_user_id, name, industry, address, city, state, zip, 'active'
        FROM dos_dealers
    """)
    op.execute("""
        UPDATE dos_rep_contacts SET company_id = dealer_id WHERE dealer_id IS NOT NULL
    """)
    op.execute("UPDATE dos_dealers SET client_requested_amount = funding_goal WHERE funding_goal IS NOT NULL")
    op.execute("""
        INSERT INTO dos_application_contacts (id, dealer_id, contact_id, relationship, is_primary)
        SELECT gen_random_uuid(), dealer_id, id, 'owner', true FROM dos_rep_contacts WHERE dealer_id IS NOT NULL
        ON CONFLICT (dealer_id, contact_id) DO NOTHING
    """)

    now = datetime.now(timezone.utc)
    programs = [
        ("term_loan_3_5_year", "term", 25000, 500000, 36, 60, 10, "EZ Term Loan", "Préstamo EZ a plazo"),
        ("term_loan_10_year", "term", 15000, 50000, 120, 120, 20, "MicroCap Working Capital", "Capital de trabajo MicroCap"),
        ("line_of_credit", "revolving", 25000, 500000, 6, 24, 30, "Business Line of Credit", "Línea de crédito comercial"),
        ("term_loan_loc_hybrid", "term", 100000, 1000000, 24, 60, 40, "Hybrid Term / LOC", "Híbrido de plazo y línea"),
        ("equipment_financing", "asset", 25000, 2000000, 24, 84, 50, "Equipment Financing", "Financiamiento de equipo"),
        ("jumbo_term_loan", "term", 1000000, 10000000, 36, 120, 60, "Jumbo Term Loan", "Préstamo jumbo a plazo"),
        ("transportation_finance", "industry", 25000, 1000000, 24, 72, 70, "Transportation Finance", "Financiamiento de transporte"),
        ("sba", "government", 50000, 5000000, 60, 300, 80, "SBA 7(a)", "SBA 7(a)"),
        ("sba_grocery", "government", 50000, 5000000, 60, 300, 90, "SBA Grocery", "SBA para supermercados"),
        ("sba_made_in_america", "government", 50000, 5000000, 60, 300, 100, "SBA Made in America", "SBA Hecho en EE. UU."),
    ]
    for key, category, low, high, tmin, tmax, order, en, es in programs:
        copy_json = json.dumps({"en": {"name": en}, "es": {"name": es}}, ensure_ascii=True).replace("'", "''")
        pricing = ({"en": "13.99%-29.99%", "es": "13.99%-29.99%"} if key == "term_loan_3_5_year" else {"en": "Prime + 6.5%", "es": "Prime + 6.5%"} if key == "term_loan_10_year" else {"en": "Pricing subject to review", "es": "Precio sujeto a revision"})
        pricing_json = json.dumps(pricing).replace("'", "''")
        eligibility_json = json.dumps({"engine": "quidity_exact_v1" if key in {"term_loan_3_5_year", "term_loan_10_year"} else "catalog_only_v1"}).replace("'", "''")
        disclosures_json = json.dumps({"en": "Preliminary fit only; not a commitment to lend.", "es": "Evaluacion preliminar; no es un compromiso de prestamo."}).replace("'", "''")
        op.execute(sa.text(
            "INSERT INTO dos_product_catalog "
            "(id, program_key, version, category, copy, pricing, eligibility, disclosures, amount_min, amount_max, term_min_months, term_max_months, effective_at, active, sort_order) VALUES "
            f"('{uuid.uuid4()}', '{key}', 1, '{category}', '{copy_json}'::jsonb, '{pricing_json}'::jsonb, "
            f"'{eligibility_json}'::jsonb, '{disclosures_json}'::jsonb, {low}, {high}, {tmin}, {tmax}, "
            f"'{now.isoformat()}', true, {order})"
        ))


def downgrade() -> None:
    op.drop_table("dos_product_presentations")
    op.drop_table("dos_product_screening_snapshots")
    op.drop_table("dos_product_finder_sessions")
    op.drop_table("dos_product_catalog")
    op.drop_table("dos_application_contacts")
    op.drop_table("dos_rep_contact_assignments")
    op.drop_column("dos_rep_contacts", "company_id")
    op.drop_table("dos_rep_companies")
    op.drop_column("dos_rep_inbox_threads", "subject_key")
    op.drop_column("dos_dealers", "application_lifecycle")
    op.drop_column("dos_dealers", "client_requested_amount")
