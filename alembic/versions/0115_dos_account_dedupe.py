"""dos: de-duplicate bank accounts and make the mask unique per dealer

Two concurrent bucket-ingest sweeps (linking a bucket schedules one,
"ingest all" schedules another) both ran match_or_create_account's
read-then-create with nothing enforcing uniqueness, so a dealer could end up
with two identical accounts created milliseconds apart — and their statements
split across the pair, which makes the primary-operating ADB read from half
the data.

This migration merges any existing duplicates onto the oldest row (repointing
periods, cash events and documents) and then adds the partial unique index
that makes the race impossible. The index is partial because a NULL mask
carries no identity — several accounts may legitimately have none.

Revision ID: 0115_dos_account_dedupe
Revises: 0114_dos_doc_hub
"""

from alembic import op

revision = "0115_dos_account_dedupe"
down_revision = "0114_dos_doc_hub"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Oldest row per (dealer_id, mask) wins; everything pointing at a loser is
    # repointed to it. Ordered by created_at then id so the choice is stable.
    op.execute(
        """
        CREATE TEMP TABLE dos_account_merge AS
        SELECT a.id AS loser_id, w.winner_id
        FROM dos_accounts a
        JOIN (
            SELECT dealer_id, mask,
                   (ARRAY_AGG(id ORDER BY created_at, id))[1] AS winner_id
            FROM dos_accounts
            WHERE mask IS NOT NULL
            GROUP BY dealer_id, mask
            HAVING COUNT(*) > 1
        ) w ON w.dealer_id = a.dealer_id AND w.mask = a.mask
        WHERE a.id <> w.winner_id;
        """
    )

    # Cash events and documents have no per-account uniqueness — repoint freely.
    for table in ("dos_cash_events", "dos_documents"):
        op.execute(
            f"""
            UPDATE {table} t
            SET account_id = m.winner_id
            FROM dos_account_merge m
            WHERE t.account_id = m.loser_id;
            """
        )

    # Periods DO have one row per (dealer, account, month) — uq_dos_period_acct
    # from 0112. Where the loser holds a month the winner already has, a blind
    # repoint would violate it, so drop the loser's row instead: deposits and
    # withdrawals are derived from the event ledger and are rebuilt from the
    # merged events after this migration (scripts/dos_recompute_dealer.py),
    # while the balance fields the rebuild does NOT recompute (ending, average
    # daily, low) are already present on the winner's row.
    op.execute(
        """
        DELETE FROM dos_financial_periods p
        USING dos_account_merge m
        WHERE p.account_id = m.loser_id
          AND EXISTS (
              SELECT 1 FROM dos_financial_periods w
              WHERE w.dealer_id = p.dealer_id
                AND w.account_id = m.winner_id
                AND w.period = p.period
          );
        """
    )
    # Whatever months the winner did not have are now safe to move over.
    op.execute(
        """
        UPDATE dos_financial_periods p
        SET account_id = m.winner_id
        FROM dos_account_merge m
        WHERE p.account_id = m.loser_id;
        """
    )

    # A duplicate never carried an admin decision worth keeping: it was created
    # by the racing AI path seconds after its twin. If one somehow did, promote
    # that correction onto the survivor before deleting it.
    op.execute(
        """
        UPDATE dos_accounts w
        SET role = l.role,
            role_set_by = 'admin'
        FROM dos_account_merge m
        JOIN dos_accounts l ON l.id = m.loser_id
        WHERE w.id = m.winner_id
          AND l.role_set_by = 'admin'
          AND w.role_set_by <> 'admin';
        """
    )

    op.execute("DELETE FROM dos_accounts WHERE id IN (SELECT loser_id FROM dos_account_merge);")
    op.execute("DROP TABLE dos_account_merge;")

    op.execute(
        """
        CREATE UNIQUE INDEX uq_dos_account_mask
        ON dos_accounts (dealer_id, mask)
        WHERE mask IS NOT NULL;
        """
    )


def downgrade() -> None:
    # The merge is not reversible — deleted duplicates are gone by design.
    op.execute("DROP INDEX IF EXISTS uq_dos_account_mask;")
