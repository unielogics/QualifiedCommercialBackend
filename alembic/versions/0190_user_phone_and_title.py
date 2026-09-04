"""A phone number and a title on an operator's own profile.

The Production Package requires the relationship manager's phone to send stage
one — `rm_phone` has been `required_for="stage_one"` all along — but nothing in
the system held an operator's phone number. It exists only in Clerk, which the
backend never reads. So the field was required and had no source, and every
package had it typed by hand.

Title is the same shape of gap from the other direction: the RM's signature on
file already records one, and nothing ever read it back.

Revision ID: 0190_user_phone_and_title
Revises: 0189_sponsor_notice_details
"""

import sqlalchemy as sa

from alembic import op

revision = "0190_user_phone_and_title"
down_revision = "0189_sponsor_notice_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("title", sa.String(120), nullable=True))
    # The signature on file is the only place a title was ever recorded. Take
    # the most recent one per user; leave the rest blank rather than guessing.
    op.execute("""
        UPDATE users u
           SET title = s.title
          FROM (
            SELECT DISTINCT ON (subject_id)
                   subject_id, NULLIF(TRIM(title), '') AS title
              FROM stored_signatures
             WHERE subject_type = 'user'
               AND subject_id IS NOT NULL
               AND revoked_at IS NULL
               AND NULLIF(TRIM(title), '') IS NOT NULL
             ORDER BY subject_id, adopted_at DESC
          ) s
         WHERE u.id = s.subject_id AND u.title IS NULL
    """)


def downgrade() -> None:
    op.drop_column("users", "title")
    op.drop_column("users", "phone")
