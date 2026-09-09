"""One phone per person.

The rep app kept a rep's phone on the business card
(dos_field_desk_profiles.phone) and the funding desk kept an operator's on
the user row (users.phone, 0190). Neither ever reached the other, so the
number a rep typed for their card never printed as the relationship
manager's phone on a production agreement, and the desk asked again.

From here users.phone is the record and the card mirrors it on every save.
This copies the card's number onto the user row where the row has none —
never over a number someone typed there — and, the other way, fills a blank
card from the user row so a shared business card keeps showing a phone.
Raw strings, no normalisation: both stores hold exactly what was typed and
the write path decides E.164 from now on.

Downgrade is a no-op: there is nothing to put back without guessing.

Revision ID: 0199_user_phone_reconciliation
Revises: 0198_referral_company_kind_and_house
"""

from alembic import op

revision = "0199_user_phone_reconciliation"
down_revision = "0198_referral_company_kind_and_house"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE users u SET phone = p.phone
          FROM dos_field_desk_profiles p
         WHERE p.user_id = u.id AND u.phone IS NULL
           AND NULLIF(TRIM(p.phone), '') IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE dos_field_desk_profiles p SET phone = u.phone
          FROM users u
         WHERE p.user_id = u.id AND NULLIF(TRIM(p.phone), '') IS NULL
           AND u.phone IS NOT NULL
        """
    )


def downgrade() -> None:
    pass
