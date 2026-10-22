"""Retain production agreements, signatures, and term sheets for two years.

Revision ID: 0241_production_legal_retention
Revises: 0240_payment_retention_guards
"""

from __future__ import annotations

from alembic import op

revision = "0241_production_legal_retention"
down_revision = "0240_payment_retention_guards"
branch_labels = None
depends_on = None


PRODUCTION_LEGAL_TABLES = (
    "production_packages",
    "production_term_sheets",
    "production_package_revisions",
    "production_package_signatures",
)


def upgrade() -> None:
    # Resolve indirect ownership for the append-only revision and signature
    # tables. A missing ancestor is handled fail-closed by the delete guard.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_production_legal_profile_uuid(
            table_name text,
            record_json jsonb
        )
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            profile_uuid uuid;
        BEGIN
            IF table_name IN ('production_packages', 'production_term_sheets') THEN
                profile_uuid := NULLIF(record_json ->> 'profile_id', '')::uuid;
            ELSIF table_name IN (
                'production_package_revisions',
                'production_package_signatures'
            ) THEN
                SELECT package.profile_id
                INTO profile_uuid
                FROM production_packages AS package
                WHERE package.id = NULLIF(record_json ->> 'package_id', '')::uuid;
            END IF;
            RETURN profile_uuid;
        END;
        $$
        """
    )

    # The minimum retention clock is anchored to the newest event stored on
    # the row, not merely the first creation time. This covers sends,
    # execution, signatures, completion, supersession, withdrawal, and voids.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_production_legal_anchor(
            record_json jsonb
        )
        RETURNS timestamptz
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            history_anchor timestamptz;
            timestamp_key text;
            timestamp_value timestamptz;
        BEGIN
            history_anchor := NULLIF(record_json ->> 'created_at', '')::timestamptz;
            FOREACH timestamp_key IN ARRAY ARRAY[
                'updated_at', 'presentation_generated_at', 'sent_at',
                'executed_at', 'voided_at', 'completed_at', 'confirmed_at',
                'viewed_at',
                'esign_consent_at', 'signed_at', 'recorded_at', 'placed_at',
                'entered_at', 'superseded_at', 'withdrawn_at', 'consumed_at',
                'received_on', 'verified_at', 'actual_funding_date'
            ] LOOP
                timestamp_value := NULLIF(record_json ->> timestamp_key, '')::timestamptz;
                IF timestamp_value IS NOT NULL THEN
                    history_anchor := GREATEST(
                        COALESCE(history_anchor, timestamp_value),
                        timestamp_value
                    );
                END IF;
            END LOOP;
            RETURN history_anchor;
        END;
        $$
        """
    )

    # Extend ApplicationProfile retention whenever a durable production legal
    # record is created or receives a newer lifecycle event. Revision 0239's
    # monotonic profile trigger then carries the deadline through the linked
    # Intake/Bucket/File graph; it can never be shortened by a stale update.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_production_legal_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            profile_uuid uuid;
            history_anchor timestamptz;
            retention_deadline timestamptz;
        BEGIN
            profile_uuid := qc_production_legal_profile_uuid(
                TG_TABLE_NAME,
                to_jsonb(NEW)
            );
            history_anchor := qc_production_legal_anchor(to_jsonb(NEW));

            IF profile_uuid IS NULL OR history_anchor IS NULL THEN
                RAISE EXCEPTION 'production legal record has no retained ApplicationProfile or timestamp anchor'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            retention_deadline := history_anchor + INTERVAL '2 years';
            UPDATE application_profiles AS profile
            SET protected_until = GREATEST(
                profile.protected_until,
                retention_deadline
            )
            WHERE profile.id = profile_uuid
              AND profile.protected_until < retention_deadline;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in PRODUCTION_LEGAL_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_production_legal_retention
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_propagate_production_legal_retention()
            """
        )

    # Existing legal artifacts must extend their owning graph before the
    # delete guards become active. Each row contributes its own latest event.
    op.execute(
        """
        WITH production_deadlines AS (
            SELECT package.profile_id,
                   qc_production_legal_anchor(to_jsonb(package))
                       + INTERVAL '2 years' AS deadline
            FROM production_packages AS package
            UNION ALL
            SELECT term.profile_id,
                   qc_production_legal_anchor(to_jsonb(term))
                       + INTERVAL '2 years' AS deadline
            FROM production_term_sheets AS term
            UNION ALL
            SELECT package.profile_id,
                   qc_production_legal_anchor(to_jsonb(revision))
                       + INTERVAL '2 years' AS deadline
            FROM production_package_revisions AS revision
            JOIN production_packages AS package ON package.id = revision.package_id
            UNION ALL
            SELECT package.profile_id,
                   qc_production_legal_anchor(to_jsonb(signature))
                       + INTERVAL '2 years' AS deadline
            FROM production_package_signatures AS signature
            JOIN production_packages AS package ON package.id = signature.package_id
        ), strongest AS (
            SELECT profile_id, MAX(deadline) AS deadline
            FROM production_deadlines
            WHERE profile_id IS NOT NULL
              AND deadline IS NOT NULL
            GROUP BY profile_id
        )
        UPDATE application_profiles AS profile
        SET protected_until = GREATEST(profile.protected_until, strongest.deadline)
        FROM strongest
        WHERE profile.id = strongest.profile_id
          AND profile.protected_until < strongest.deadline
        """
    )

    # Funding confirmations and bank-direct receipts may use a legacy Loan
    # Document as their supporting proof. Keep the owning profile and any
    # mirrored BucketFile at the evidence event's own deadline. Replaying the
    # current links provides the historical backfill without changing their
    # business values.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_payment_evidence_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            record_json jsonb;
            document_uuid uuid;
            profile_uuid uuid;
            history_anchor timestamptz;
            retention_deadline timestamptz;
        BEGIN
            record_json := to_jsonb(NEW);
            document_uuid := NULLIF(record_json ->> 'evidence_document_id', '')::uuid;
            IF document_uuid IS NULL THEN
                RETURN NEW;
            END IF;

            IF TG_TABLE_NAME = 'actual_funding_confirmations' THEN
                profile_uuid := NULLIF(record_json ->> 'application_profile_id', '')::uuid;
            ELSIF TG_TABLE_NAME = 'bank_direct_fee_receipts' THEN
                SELECT obligation.application_profile_id
                INTO profile_uuid
                FROM fee_obligations AS obligation
                WHERE obligation.id = NULLIF(record_json ->> 'obligation_id', '')::uuid;
            END IF;
            history_anchor := qc_production_legal_anchor(record_json);
            IF profile_uuid IS NULL OR history_anchor IS NULL THEN
                RAISE EXCEPTION 'payment evidence has no retained ApplicationProfile or timestamp anchor'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            retention_deadline := history_anchor + INTERVAL '2 years';

            UPDATE application_profiles AS profile
            SET protected_until = GREATEST(
                profile.protected_until,
                retention_deadline
            )
            WHERE profile.id = profile_uuid
              AND profile.protected_until < retention_deadline;

            UPDATE bucket_files AS file
            SET protected_until = GREATEST(
                file.protected_until,
                retention_deadline
            )
            FROM documents AS document
            WHERE document.id = document_uuid
              AND document.s3_key IS NOT NULL
              AND file.s3_key = document.s3_key
              AND file.protected_until < retention_deadline;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in (
        "actual_funding_confirmations",
        "bank_direct_fee_receipts",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_payment_evidence_retention
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_propagate_payment_evidence_retention()
            """
        )
    op.execute(
        """
        UPDATE actual_funding_confirmations
        SET evidence_document_id = evidence_document_id
        WHERE evidence_document_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE bank_direct_fee_receipts
        SET evidence_document_id = evidence_document_id
        WHERE evidence_document_id IS NOT NULL
        """
    )

    # The legacy Document remains freely mergeable/deletable unless it is
    # currently the evidence for one of the two retained payment records. A
    # referenced document receives the same governed-purge, own-age, and
    # effective graph checks as the payment ledger itself. This catches direct
    # SQL and Loan cascade deletes before ON DELETE SET NULL can erase lineage.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_payment_evidence_document_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            is_payment_evidence boolean;
            document_anchor timestamptz;
            payment_deadline timestamptz;
            graph_blocked boolean;
        BEGIN
            SELECT EXISTS (
                SELECT 1
                FROM actual_funding_confirmations AS confirmation
                WHERE confirmation.evidence_document_id = OLD.id
                UNION ALL
                SELECT 1
                FROM bank_direct_fee_receipts AS receipt
                WHERE receipt.evidence_document_id = OLD.id
            )
            INTO is_payment_evidence;
            IF NOT is_payment_evidence THEN
                RETURN OLD;
            END IF;

            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of payment evidence requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            document_anchor := qc_production_legal_anchor(to_jsonb(OLD));
            SELECT MAX(deadline)
            INTO payment_deadline
            FROM (
                SELECT qc_production_legal_anchor(to_jsonb(confirmation))
                           + INTERVAL '2 years' AS deadline
                FROM actual_funding_confirmations AS confirmation
                WHERE confirmation.evidence_document_id = OLD.id
                UNION ALL
                SELECT qc_production_legal_anchor(to_jsonb(receipt))
                           + INTERVAL '2 years' AS deadline
                FROM bank_direct_fee_receipts AS receipt
                WHERE receipt.evidence_document_id = OLD.id
            ) AS retained_payment_evidence;
            payment_deadline := GREATEST(
                payment_deadline,
                document_anchor + INTERVAL '2 years'
            );
            IF document_anchor IS NULL
               OR payment_deadline IS NULL
               OR payment_deadline > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'payment evidence is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM actual_funding_confirmations AS confirmation
                WHERE confirmation.evidence_document_id = OLD.id
                  AND qc_payment_profile_retention_blocked(
                      confirmation.application_profile_id
                  )
                UNION ALL
                SELECT 1
                FROM bank_direct_fee_receipts AS receipt
                JOIN fee_obligations AS obligation
                  ON obligation.id = receipt.obligation_id
                WHERE receipt.evidence_document_id = OLD.id
                  AND qc_payment_profile_retention_blocked(
                      obligation.application_profile_id
                  )
            )
            INTO graph_blocked;
            IF graph_blocked THEN
                RAISE EXCEPTION 'payment evidence is protected by ApplicationProfile retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_payment_evidence_document_delete
        BEFORE DELETE ON documents
        FOR EACH ROW
        EXECUTE FUNCTION qc_guard_payment_evidence_document_delete()
        """
    )

    # Ordinary hard deletion is always prohibited. Even a governed purge must
    # wait for this row's own two-year window and every effective deadline or
    # legal hold in the linked ApplicationProfile/Intake/Bucket/File graph.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_production_legal_record_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            record_json jsonb;
            profile_uuid uuid;
            history_created_at timestamptz;
            history_anchor timestamptz;
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of production legal history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            record_json := to_jsonb(OLD);
            history_created_at := NULLIF(record_json ->> 'created_at', '')::timestamptz;
            history_anchor := qc_production_legal_anchor(record_json);
            IF history_created_at IS NULL
               OR history_anchor IS NULL
               OR history_anchor + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'production legal history is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            profile_uuid := qc_production_legal_profile_uuid(
                TG_TABLE_NAME,
                record_json
            );
            IF profile_uuid IS NULL THEN
                RAISE EXCEPTION 'production legal history has no retained ApplicationProfile reference'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF qc_payment_profile_retention_blocked(profile_uuid) THEN
                RAISE EXCEPTION 'production legal history is protected by ApplicationProfile retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in PRODUCTION_LEGAL_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_production_legal_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_production_legal_record_delete()
            """
        )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_payment_evidence_document_delete ON documents"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_payment_evidence_document_delete()")

    for table in (
        "bank_direct_fee_receipts",
        "actual_funding_confirmations",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_payment_evidence_retention ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_propagate_payment_evidence_retention()")

    for table in reversed(PRODUCTION_LEGAL_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_production_legal_delete ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_production_legal_record_delete()")

    for table in reversed(PRODUCTION_LEGAL_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_production_legal_retention ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_propagate_production_legal_retention()")
    op.execute("DROP FUNCTION IF EXISTS qc_production_legal_anchor(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS qc_production_legal_profile_uuid(text, jsonb)")
