"""One vocabulary for a room's type and a profile's funding category.

`buckets.bucket_type` was free text: machine kinds beside "Loan File" and a
partner's name used as a type. `application_profiles.funding_category` carried
`purchase` and `Purchase`, `mca_refinance` and "MCA refinance". Neither could be
filtered honestly. The type becomes a closed set the model enforces; the
category keeps the rule the code already had (lead_types.normalize_funding_intent)
and the known variants present in production are folded in. Values that are sentences rather than keys are left
alone for a person to classify — the migration does not guess intent.

Revision ID: 0244_bucket_type_and_funding_category_vocabulary
Revises: 0243_requested_document_closure
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0244_bucket_type_and_funding_category_vocabulary"
down_revision = "0243_requested_document_closure"
branch_labels = None
depends_on = None

BUCKET_TYPES = (
    "general",
    "loan_file",
    "underwriting",
    "application_profile",
    "dealer_ai_intake",
    "main_street_intake",
    "real_estate_ai_intake",
    "mca_refinance_intake",
    "commercial_foreclosure_rescue",
    "business_capital_referral",
)
CK_NAME = "ck_buckets_bucket_type"


def upgrade() -> None:
    allowed = ", ".join(f"'{value}'" for value in BUCKET_TYPES)
    # Human labels that were typed into the type column survive — as the room's
    # purpose when it has none, otherwise appended to its description — so
    # nothing an operator wrote is lost; the type itself becomes the neutral kind.
    op.execute(
        sa.text(
            f"""
            UPDATE buckets
               SET purpose = CASE WHEN purpose IS NULL THEN bucket_type ELSE purpose END,
                   description = CASE
                       WHEN purpose IS NULL THEN description
                       ELSE concat_ws(E'\n', NULLIF(description, ''), 'Room type as entered: ' || bucket_type)
                   END
             WHERE bucket_type IS NOT NULL
               AND bucket_type NOT IN ({allowed})
               AND bucket_type <> 'Loan File'
            """
        )
    )
    op.execute(sa.text("UPDATE buckets SET bucket_type = 'loan_file' WHERE bucket_type = 'Loan File'"))
    op.execute(
        sa.text(
            f"""
            UPDATE buckets
               SET bucket_type = 'general'
             WHERE bucket_type IS NULL OR bucket_type NOT IN ({allowed})
            """
        )
    )
    op.create_check_constraint(CK_NAME, "buckets", f"bucket_type IS NULL OR bucket_type IN ({allowed})")

    # Funding category: the rule is app.lead_types.normalize_funding_intent —
    # a known alias becomes one of the eight intents, anything else is kept
    # exactly as written. Applying a Python alias table from a migration would
    # mean importing app code, so the rows present on 2026-10-30 were run
    # through the rule by hand and the result is written out explicitly,
    # scoped to the vertical that makes each mapping true. The two values that
    # are sentences rather than keys are left for a person to classify.
    op.execute(
        sa.text(
            """
            UPDATE application_profiles
               SET funding_category = CASE
                   WHEN funding_category IN ('purchase', 'Purchase') AND vertical = 'real_estate' THEN 'real_estate'
                   WHEN funding_category = 'Refinance' AND vertical = 'real_estate' THEN 'debt_refinance'
                   WHEN funding_category = 'MCA refinance' THEN 'mca_refinance'
                   ELSE funding_category
               END
             WHERE (funding_category IN ('purchase', 'Purchase', 'Refinance') AND vertical = 'real_estate')
                OR funding_category = 'MCA refinance'
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(CK_NAME, "buckets", type_="check")
    op.execute(sa.text("UPDATE buckets SET bucket_type = 'Loan File' WHERE bucket_type = 'loan_file'"))
    op.execute(sa.text("UPDATE buckets SET bucket_type = NULL WHERE bucket_type = 'general'"))
    # Funding-category casing cannot be reconstructed; the canonical keys stand.
