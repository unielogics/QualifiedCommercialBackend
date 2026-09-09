"""The exclusivity window follows the size of the request now; the 45-day
default it used to carry retires with it.

Every stage-one draft was seeded with 45 when DEFAULTS still held it. Under
the tier — thirty days at or under $350,000, sixty over — a stored 45 reads
as the desk having typed past a thirty-day tier, which it never did. Clear
it on drafts, and only where it is exactly the old default: a number the
desk chose stays. Executed and sent packages are evidence and are untouched.

Revision ID: 0197_retire_the_exclusivity_default
Revises: 0196_production_share_link_kind
"""

from alembic import op

revision = "0197_retire_the_exclusivity_default"
down_revision = "0196_production_share_link_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE production_packages
           SET arrangement = jsonb_set(arrangement, '{exclusivity}', '""'::jsonb),
               computed_cache = NULL
         WHERE stage = 1 AND status = 'draft'
           AND (arrangement->>'exclusivity') IN ('45', '45.0')
        """
    )


def downgrade() -> None:
    # The default is gone from the code; nothing to put back.
    pass
