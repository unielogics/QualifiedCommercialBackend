"""Enable versioned real estate schedules and explicitly scoped form links."""

from alembic import op

revision = "0261_real_estate_schedule"
down_revision = "0260_financial_debt_evidence"
branch_labels = None
depends_on = None

CHECKS = (
    ("financial_form_families", "ck_financial_form_family_kind", "kind", "'pfs','debt_schedule','p_and_l','balance_sheet'"),
    ("financial_form_links", "ck_financial_form_links_kind", "kind", "'pfs','debt_schedule','p_and_l','balance_sheet','worksheet'"),
    ("financial_form_link_sheets", "ck_financial_form_link_sheets_kind", "sheet_kind", "'pfs','debt_schedule','p_and_l','balance_sheet'"),
)


def upgrade() -> None:
    for table, constraint, column, allowed in CHECKS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"{column} in ({allowed},'real_estate_schedule')")


def downgrade() -> None:
    # Refuse to erase financial records or invalidate outstanding share scopes.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM financial_form_families WHERE kind='real_estate_schedule')
        OR EXISTS (SELECT 1 FROM financial_form_links WHERE kind='real_estate_schedule')
        OR EXISTS (SELECT 1 FROM financial_form_link_sheets WHERE sheet_kind='real_estate_schedule')
        THEN RAISE EXCEPTION 'Real estate schedules or share scopes exist; preserve them before downgrading';
        END IF;
    END; $$""")
    for table, constraint, column, allowed in CHECKS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"{column} in ({allowed})")
