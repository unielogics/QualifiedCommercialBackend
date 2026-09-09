"""Every employee is linked to a business relationship profile.

The owner's rule for the Production Package's sponsor: whatever profile the
agent on the file is linked to is what the sponsor defaults to, and super
admin and underwriting override it. The link itself has existed since 0102
(`users.referral_partner_company_id`), but internal staff could never be
linked to anything, because the house is not a ReferralPartnerCompany and the
router refused any company without a signed Referral Protection agreement.

So: a `kind` on the company — `referral_partner` (every row today) or `house`
(exactly one, ever) — a seeded house row named for the constant the
arrangement already prints as the manager's employer, and every active super
admin, underwriter and field rep with no link pointed at it. The house is
never a sponsor; it is what makes "every employee is linked" true.

A row someone typed with the house's name becomes the house only if it never
signed an agreement — a real partner that happens to share the name keeps
its kind.

Revision ID: 0198_referral_company_kind_and_house
Revises: 0197_retire_the_exclusivity_default
"""

import sqlalchemy as sa

from alembic import op

revision = "0198_referral_company_kind_and_house"
down_revision = "0197_retire_the_exclusivity_default"
branch_labels = None
depends_on = None

HOUSE_NAME = "Qualified Commercial LLC"


def upgrade() -> None:
    op.add_column(
        "referral_partner_companies",
        sa.Column("kind", sa.String(16), nullable=False, server_default="referral_partner"),
    )
    op.create_check_constraint(
        "ck_referral_partner_companies_kind", "referral_partner_companies",
        "kind IN ('referral_partner', 'house')",
    )
    op.create_index(
        "uq_referral_partner_companies_house", "referral_partner_companies", ["kind"],
        unique=True, postgresql_where=sa.text("kind = 'house'"),
    )
    op.execute(
        f"""
        UPDATE referral_partner_companies c SET kind = 'house'
         WHERE c.name = '{HOUSE_NAME}' AND c.kind <> 'house'
           AND NOT EXISTS (SELECT 1 FROM contract_agreements a
                            WHERE a.subject_type = 'company' AND a.subject_id = c.id
                              AND a.contract_type = 'referral_protection')
        """
    )
    op.execute(
        f"""
        INSERT INTO referral_partner_companies (id, name, kind, entity_type, created_at, updated_at)
        SELECT gen_random_uuid(), '{HOUSE_NAME}', 'house', 'Limited liability company', now(), now()
         WHERE NOT EXISTS (SELECT 1 FROM referral_partner_companies WHERE kind = 'house')
        """
    )
    op.execute(
        """
        UPDATE users u SET referral_partner_company_id = h.id
          FROM referral_partner_companies h
         WHERE h.kind = 'house' AND u.referral_partner_company_id IS NULL AND u.deleted_at IS NULL
           AND u.role IN ('super_admin', 'loan_exec', 'field_rep')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE users SET referral_partner_company_id = NULL
         WHERE referral_partner_company_id IN (SELECT id FROM referral_partner_companies WHERE kind = 'house')
        """
    )
    op.execute("DELETE FROM referral_partner_companies WHERE kind = 'house'")
    op.drop_index("uq_referral_partner_companies_house", table_name="referral_partner_companies")
    op.drop_constraint("ck_referral_partner_companies_kind", "referral_partner_companies")
    op.drop_column("referral_partner_companies", "kind")
