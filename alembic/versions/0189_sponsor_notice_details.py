"""The sponsor's notice and signatory details, and a way to correct them.

A sponsor company held four facts: name, entity type, state of formation and
principal address. The Production Package needs six more — the notice email
notice under both agreements is served to, who it is marked for, who signs, in
what title, the administration platform Schedule A names, and a phone. They
lived nowhere, so an operator retyped them onto every package, and
`sponsor_platform` had no source at all despite being required to send stage one.

Five of the six are already recorded, in the company's own signed Strategic
Referral agreement, so this backfills from executed evidence rather than
guessing. A company with no signed agreement stays blank rather than being
invented; there is now an editor for those.

Revision ID: 0189_sponsor_notice_details
Revises: 0188_inline_images
"""

import sqlalchemy as sa

from alembic import op

revision = "0189_sponsor_notice_details"
down_revision = "0188_inline_images"
branch_labels = None
depends_on = None

COLUMNS = (
    ("notice_email", sa.String(320)),
    ("notice_attention", sa.String(255)),
    ("notice_address", sa.String(512)),
    ("platform_name", sa.String(255)),
    ("signatory_name", sa.String(255)),
    ("signatory_title", sa.String(128)),
    ("phone", sa.String(40)),
)

# The signed Strategic Referral agreement is the source of record. Newest wins
# where a company has signed more than once, and a blank in the agreement stays
# blank here — nothing is inferred.
BACKFILL = """
WITH signed AS (
    SELECT DISTINCT ON (a.subject_id)
           a.subject_id AS company_id, a.field_values AS fv
      FROM contract_agreements a
     WHERE a.contract_type = 'referral_protection'
       AND a.subject_type = 'company'
       AND a.field_values IS NOT NULL
     ORDER BY a.subject_id, a.created_at DESC
)
UPDATE referral_partner_companies c
   SET notice_email     = COALESCE(NULLIF(TRIM(s.fv->>'referral_partner_notice_email'), ''), c.notice_email),
       notice_attention = COALESCE(NULLIF(TRIM(s.fv->>'referral_partner_notice_attn'), ''), c.notice_attention),
       notice_address   = COALESCE(NULLIF(TRIM(CONCAT_WS(', ',
                              NULLIF(TRIM(s.fv->>'referral_partner_notice_address_line1'), ''),
                              NULLIF(TRIM(s.fv->>'referral_partner_notice_address_line2'), ''))), ''), c.notice_address),
       signatory_name   = COALESCE(NULLIF(TRIM(s.fv->>'counterparty_signatory_name'), ''), c.signatory_name),
       signatory_title  = COALESCE(NULLIF(TRIM(s.fv->>'counterparty_signatory_title'), ''), c.signatory_title),
       -- Only where the company row is blank: a hand-corrected value wins.
       entity_type      = COALESCE(NULLIF(TRIM(c.entity_type), ''),
                                   NULLIF(TRIM(s.fv->>'referral_partner_entity_type'), '')),
       state_of_formation = COALESCE(NULLIF(TRIM(c.state_of_formation), ''),
                                   NULLIF(TRIM(s.fv->>'referral_partner_state_of_organization'), '')),
       principal_address = COALESCE(NULLIF(TRIM(c.principal_address), ''),
                                   NULLIF(TRIM(s.fv->>'referral_partner_principal_place_of_business'), ''))
  FROM signed s
 WHERE c.id = s.company_id
"""


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column("referral_partner_companies", sa.Column(name, kind, nullable=True))
    op.execute(BACKFILL)


def downgrade() -> None:
    for name, _kind in reversed(COLUMNS):
        op.drop_column("referral_partner_companies", name)
