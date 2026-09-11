"""Hold a form's PDF back until the typing stops.

Every save on the four financial forms rendered a PDF and put it to S3 on the
spot. That was defensible on a form somebody fills in and presses Save on; on
the live worksheet, where a save is a cell, it is a document rewritten on every
keystroke — and a half-typed figure briefly filed as fact is worse than a
document two minutes behind.

`form_pdf_refresh_queue` is the whole of the hold: one row per (profile, form),
carrying `due_at`, the moment the redraw comes due. A save does not add a row,
it pushes `due_at` out — which is what makes this settle 120 seconds after the
*last* edit rather than firing on the first and dropping the rest. The unique
constraint on (profile_id, kind) is not a hygiene index, it is the debounce: it
is what makes "one more save" an UPDATE rather than another render.

Nothing about the form's contents lives here. The drain re-reads the body off
the file when it renders, so a row that waited through six more edits still
draws today's numbers. A submit is unaffected and still files immediately —
and clears its row on the way through, so the job cannot overwrite a
just-filed document a minute later.

⚠️ **The downgrade drops the table, and with it every pending redraw.** Rows
that had not yet come due are lost, which means the PDFs behind those forms
stay at whatever they last rendered until somebody saves the form again. No
figure is lost — the numbers live in `business_financial_statements`,
`financial_statements` and `dos_debts`, never here — but the documents in the
room can be out of date with no record of which ones. Down, then up, on a
live box means going round the affected files and touching each form once.

Revision ID: 0206_form_pdf_refresh_queue
Revises: 0205_worksheet_link_pins
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0206_form_pdf_refresh_queue"
down_revision = "0205_worksheet_link_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "form_pdf_refresh_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_name", sa.String(180), nullable=True),
        sa.Column("actor_email", sa.String(320), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind in ('pfs','debt_schedule','p_and_l','balance_sheet')",
            name="ck_form_pdf_refresh_queue_kind",
        ),
        # The debounce. One pending redraw per form, so a burst of saves is one
        # row whose deadline keeps moving rather than a queue of identical work.
        sa.UniqueConstraint(
            "profile_id", "kind", name="uq_form_pdf_refresh_queue_profile_kind"
        ),
    )
    # The drain's only question, every 30 seconds: what is due now.
    op.create_index("ix_form_pdf_refresh_queue_due_at", "form_pdf_refresh_queue", ["due_at"])


def downgrade() -> None:
    # ⚠️ Every pending redraw goes with it. See the module docstring: no figure
    # is lost, but the documents behind any form saved in the last two minutes
    # stay stale until that form is saved again, and nothing records which.
    op.drop_index("ix_form_pdf_refresh_queue_due_at", table_name="form_pdf_refresh_queue")
    op.drop_table("form_pdf_refresh_queue")
