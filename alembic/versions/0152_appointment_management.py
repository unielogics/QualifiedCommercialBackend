"""shared appointment management, outcomes, archive, and conversion

Revision ID: 0152_appointment_management
Revises: 0151_booking_reminder_schedules
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0152_appointment_management"
down_revision = "0151_booking_reminder_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_rep_appointments", sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("company", sa.String(length=180), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("program_name", sa.String(length=180), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("requested_amount", sa.String(length=40), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("full_address", sa.String(length=500), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("outcome", sa.String(length=24), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("outcome_note", sa.Text(), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("outcome_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("archived_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("conversion_target", sa.String(length=24), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("converted_dealer_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("converted_intake_id", postgresql.UUID(as_uuid=True), nullable=True))

    op.create_foreign_key("fk_dos_rep_appointment_contact", "dos_rep_appointments", "dos_rep_contacts", ["contact_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_dos_rep_appointment_outcome_actor", "dos_rep_appointments", "users", ["outcome_by_user_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_dos_rep_appointment_archive_actor", "dos_rep_appointments", "users", ["archived_by_user_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_dos_rep_appointment_converted_dealer", "dos_rep_appointments", "dos_dealers", ["converted_dealer_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_dos_rep_appointment_converted_intake", "dos_rep_appointments", "public_underwriting_intakes", ["converted_intake_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_dos_rep_appointments_contact", "dos_rep_appointments", ["contact_id"])
    op.create_index("ix_dos_rep_appointments_outcome", "dos_rep_appointments", ["outcome", "starts_at"])
    op.create_index("ix_dos_rep_appointments_archived", "dos_rep_appointments", ["archived_at", "starts_at"])

    # Existing appointments already have these immutable booking snapshots in
    # booking_notifications. Backfill them so historical meetings render with
    # the same detail as newly-created ones.
    op.execute(
        """
        UPDATE dos_rep_appointments appt
        SET program_name = bn.program_name,
            requested_amount = bn.requested_amount,
            full_address = bn.full_address
        FROM booking_notifications bn
        WHERE bn.event_id = appt.calendar_event_id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_dos_rep_appointments_archived", table_name="dos_rep_appointments")
    op.drop_index("ix_dos_rep_appointments_outcome", table_name="dos_rep_appointments")
    op.drop_index("ix_dos_rep_appointments_contact", table_name="dos_rep_appointments")
    for name in (
        "fk_dos_rep_appointment_converted_intake",
        "fk_dos_rep_appointment_converted_dealer",
        "fk_dos_rep_appointment_archive_actor",
        "fk_dos_rep_appointment_outcome_actor",
        "fk_dos_rep_appointment_contact",
    ):
        op.drop_constraint(name, "dos_rep_appointments", type_="foreignkey")
    for column in (
        "converted_intake_id", "converted_dealer_id", "conversion_target",
        "cancellation_reason", "archived_by_user_id", "archived_at",
        "outcome_by_user_id", "outcome_at", "outcome_note", "outcome",
        "full_address", "requested_amount", "program_name", "company", "contact_id",
    ):
        op.drop_column("dos_rep_appointments", column)
