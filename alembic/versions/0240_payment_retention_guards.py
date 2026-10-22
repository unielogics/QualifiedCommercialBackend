"""Two-year payment ledger retention and ACH lifecycle propagation.

Revision ID: 0240_payment_retention_guards
Revises: 0239_ai_intake_record_retention
"""

from __future__ import annotations

from alembic import op

revision = "0240_payment_retention_guards"
down_revision = "0239_ai_intake_record_retention"
branch_labels = None
depends_on = None


PAYMENT_LEDGER_TABLES = (
    "fee_obligations",
    "fee_obligation_lines",
    "fee_allocation_versions",
    "actual_funding_confirmations",
    "payment_funding_sources",
    "ach_mandates",
    "payment_debit_notices",
    "payment_servicing_authorities",
    "private_funding_payment_plans",
    "payment_installments",
    "payment_transfers",
    "payment_transfer_events",
    "payment_refunds",
    "bank_direct_fee_receipts",
    "payment_audit_events",
)


def upgrade() -> None:
    # Legal holds and deadlines are effective across the same durable
    # ApplicationProfile -> Intake -> Bucket graph protected by revision 0239.
    # This helper deliberately treats a missing profile as retained; a dangling
    # financial record must never become deletable because its parent vanished.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_payment_profile_retention_blocked(
            profile_uuid uuid
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            WITH RECURSIVE parent_profile AS (
                SELECT ap.*
                FROM application_profiles AS ap
                WHERE ap.id = profile_uuid
            ), retention_edges(from_kind, from_id, to_kind, to_id) AS (
                SELECT 'ai_intake'::text, i.id, 'bucket'::text, i.bucket_id
                FROM public_underwriting_intakes AS i
                WHERE i.bucket_id IS NOT NULL
                UNION ALL
                SELECT 'bucket'::text, i.bucket_id, 'ai_intake'::text, i.id
                FROM public_underwriting_intakes AS i
                WHERE i.bucket_id IS NOT NULL
                UNION ALL
                SELECT 'ai_intake'::text, link.intake_id,
                       'bucket'::text, link.bucket_id
                FROM bucket_intake_links AS link
                UNION ALL
                SELECT 'bucket'::text, link.bucket_id,
                       'ai_intake'::text, link.intake_id
                FROM bucket_intake_links AS link
                UNION ALL
                SELECT 'ai_intake'::text, ap.intake_id,
                       'bucket'::text, ap.primary_bucket_id
                FROM application_profiles AS ap
                WHERE ap.intake_id IS NOT NULL
                  AND ap.primary_bucket_id IS NOT NULL
                UNION ALL
                SELECT 'bucket'::text, ap.primary_bucket_id,
                       'ai_intake'::text, ap.intake_id
                FROM application_profiles AS ap
                WHERE ap.intake_id IS NOT NULL
                  AND ap.primary_bucket_id IS NOT NULL
            ), seeds(kind, id) AS (
                SELECT 'bucket'::text, ap.primary_bucket_id
                FROM parent_profile AS ap
                WHERE ap.primary_bucket_id IS NOT NULL
                UNION
                SELECT 'ai_intake'::text, ap.intake_id
                FROM parent_profile AS ap
                WHERE ap.intake_id IS NOT NULL
            ), connected(kind, id) AS (
                SELECT kind, id FROM seeds
                UNION
                SELECT edge.to_kind, edge.to_id
                FROM connected AS current_node
                JOIN retention_edges AS edge
                  ON edge.from_kind = current_node.kind
                 AND edge.from_id = current_node.id
            ), connected_buckets AS (
                SELECT id FROM connected WHERE kind = 'bucket'
            ), connected_intakes AS (
                SELECT id FROM connected WHERE kind = 'ai_intake'
            )
            SELECT
                NOT EXISTS (SELECT 1 FROM parent_profile)
                OR EXISTS (
                    SELECT 1
                    FROM parent_profile AS ap
                    WHERE ap.legal_hold
                       OR ap.protected_until IS NULL
                       OR ap.protected_until > CURRENT_TIMESTAMP
                )
                OR EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.id IN (SELECT id FROM connected_intakes)
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM buckets AS b
                    WHERE b.id IN (SELECT id FROM connected_buckets)
                      AND (
                          b.legal_hold
                          OR b.protected_until IS NULL
                          OR b.protected_until > CURRENT_TIMESTAMP
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id IN (SELECT id FROM connected_buckets)
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM application_profiles AS linked_profile
                    WHERE (
                        linked_profile.intake_id IN (
                            SELECT id FROM connected_intakes
                        )
                        OR linked_profile.primary_bucket_id IN (
                            SELECT id FROM connected_buckets
                        )
                    )
                      AND (
                          linked_profile.legal_hold
                          OR linked_profile.protected_until IS NULL
                          OR linked_profile.protected_until > CURRENT_TIMESTAMP
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM bucket_intake_links AS link
                    WHERE (
                        link.intake_id IN (SELECT id FROM connected_intakes)
                        OR link.bucket_id IN (SELECT id FROM connected_buckets)
                    )
                      AND (
                          GREATEST(
                              link.created_at,
                              COALESCE(link.unlinked_at, link.created_at)
                          ) + INTERVAL '2 years' > CURRENT_TIMESTAMP
                          OR EXISTS (
                              SELECT 1
                              FROM bucket_intake_link_files AS link_file
                              WHERE link_file.link_id = link.id
                                AND GREATEST(
                                    link_file.created_at,
                                    COALESCE(
                                        link_file.removed_at,
                                        link_file.created_at
                                    )
                                ) + INTERVAL '2 years' > CURRENT_TIMESTAMP
                          )
                      )
                );
        $$
        """
    )

    # A mandate's two-year clock starts at the latest authorization lifecycle
    # event and may only move later. Provider transfer/refund triggers below
    # feed their terminal timestamps into this same monotonic field.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_normalize_ach_mandate_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            retention_anchor timestamptz;
            retention_deadline timestamptz;
        BEGIN
            retention_anchor := GREATEST(
                COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.signed_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.expires_at, NEW.signed_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.revoked_at, NEW.signed_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.terminated_at, NEW.revoked_at, NEW.signed_at, NEW.created_at, CURRENT_TIMESTAMP)
            );
            retention_deadline := retention_anchor + INTERVAL '2 years';
            NEW.retention_until := GREATEST(
                COALESCE(NEW.retention_until, retention_deadline),
                COALESCE(
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD.retention_until END,
                    retention_deadline
                ),
                retention_deadline
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ach_mandates_retention_normalize
        BEFORE INSERT OR UPDATE ON ach_mandates
        FOR EACH ROW
        EXECUTE FUNCTION qc_normalize_ach_mandate_retention()
        """
    )

    # Keep the exact agreement, mandate certificate, and debit notice at the
    # mandate's strongest deadline. Updating BucketFile/ApplicationProfile also
    # invokes revision 0239's graph propagation triggers.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_ach_mandate_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE bucket_files AS bf
            SET protected_until = GREATEST(bf.protected_until, NEW.retention_until)
            WHERE NEW.retention_until IS NOT NULL
              AND bf.id IN (
                  SELECT NEW.certificate_bucket_file_id
                  WHERE NEW.certificate_bucket_file_id IS NOT NULL
                  UNION
                  SELECT NEW.agreement_document_id
                  WHERE NEW.agreement_document_id IS NOT NULL
                  UNION
                  SELECT line.governing_agreement_document_id
                  FROM fee_obligation_lines AS line
                  WHERE line.obligation_id = NEW.fee_obligation_id
                    AND line.governing_agreement_document_id IS NOT NULL
                  UNION
                  SELECT notice.notice_bucket_file_id
                  FROM payment_debit_notices AS notice
                  WHERE notice.mandate_id = NEW.id
                    AND notice.notice_bucket_file_id IS NOT NULL
              )
              AND bf.protected_until < NEW.retention_until;

            UPDATE application_profiles AS ap
            SET protected_until = GREATEST(ap.protected_until, NEW.retention_until)
            WHERE NEW.retention_until IS NOT NULL
              AND ap.id = NEW.application_profile_id
              AND ap.protected_until < NEW.retention_until;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ach_mandates_retention_propagate
        AFTER INSERT OR UPDATE ON ach_mandates
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_ach_mandate_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_extend_ach_retention_from_transfer()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            retention_anchor timestamptz;
        BEGIN
            retention_anchor := GREATEST(
                COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.updated_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.claimed_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.submitted_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.funds_available_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.returned_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.cancelled_at, NEW.created_at, CURRENT_TIMESTAMP)
            );
            UPDATE ach_mandates AS mandate
            SET retention_until = GREATEST(
                COALESCE(mandate.retention_until, retention_anchor + INTERVAL '2 years'),
                retention_anchor + INTERVAL '2 years'
            )
            WHERE mandate.id = NEW.mandate_id;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_transfers_ach_retention
        AFTER INSERT OR UPDATE ON payment_transfers
        FOR EACH ROW
        EXECUTE FUNCTION qc_extend_ach_retention_from_transfer()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_extend_ach_retention_from_transfer_event()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            mandate_uuid uuid;
            retention_anchor timestamptz;
        BEGIN
            SELECT transfer.mandate_id
            INTO mandate_uuid
            FROM payment_transfers AS transfer
            WHERE transfer.id = NEW.transfer_id
               OR (
                    NEW.transfer_id IS NULL
                    AND NEW.plaid_transfer_id IS NOT NULL
                    AND transfer.plaid_transfer_id = NEW.plaid_transfer_id
               )
            ORDER BY CASE WHEN transfer.id = NEW.transfer_id THEN 0 ELSE 1 END
            LIMIT 1;

            retention_anchor := GREATEST(
                COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.event_timestamp, NEW.created_at, CURRENT_TIMESTAMP)
            );
            IF mandate_uuid IS NOT NULL THEN
                UPDATE ach_mandates AS mandate
                SET retention_until = GREATEST(
                    COALESCE(mandate.retention_until, retention_anchor + INTERVAL '2 years'),
                    retention_anchor + INTERVAL '2 years'
                )
                WHERE mandate.id = mandate_uuid;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_transfer_events_ach_retention
        AFTER INSERT OR UPDATE ON payment_transfer_events
        FOR EACH ROW
        EXECUTE FUNCTION qc_extend_ach_retention_from_transfer_event()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_extend_ach_retention_from_refund()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            mandate_uuid uuid;
            retention_anchor timestamptz;
        BEGIN
            SELECT transfer.mandate_id
            INTO mandate_uuid
            FROM payment_transfers AS transfer
            WHERE transfer.id = NEW.transfer_id;

            retention_anchor := GREATEST(
                COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.updated_at, NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.completed_at, NEW.created_at, CURRENT_TIMESTAMP)
            );
            IF mandate_uuid IS NOT NULL THEN
                UPDATE ach_mandates AS mandate
                SET retention_until = GREATEST(
                    COALESCE(mandate.retention_until, retention_anchor + INTERVAL '2 years'),
                    retention_anchor + INTERVAL '2 years'
                )
                WHERE mandate.id = mandate_uuid;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_refunds_ach_retention
        AFTER INSERT OR UPDATE ON payment_refunds
        FOR EACH ROW
        EXECUTE FUNCTION qc_extend_ach_retention_from_refund()
        """
    )

    # Backfill the later of signing, termination/revocation, transfer lifecycle,
    # provider events, and refunds. The mandate UPDATE invokes both new mandate
    # triggers and revision 0239's Bucket/Application propagation.
    op.execute(
        """
        UPDATE ach_mandates AS mandate
        SET retention_until = GREATEST(
            COALESCE(
                mandate.retention_until,
                COALESCE(mandate.signed_at, mandate.created_at) + INTERVAL '2 years'
            ),
            GREATEST(
                mandate.created_at,
                mandate.signed_at,
                COALESCE(mandate.expires_at, mandate.signed_at, mandate.created_at),
                COALESCE(mandate.revoked_at, mandate.signed_at, mandate.created_at),
                COALESCE(mandate.terminated_at, mandate.revoked_at, mandate.signed_at, mandate.created_at),
                COALESCE((
                    SELECT MAX(GREATEST(
                        transfer.created_at,
                        COALESCE(transfer.updated_at, transfer.created_at),
                        COALESCE(transfer.claimed_at, transfer.created_at),
                        COALESCE(transfer.submitted_at, transfer.created_at),
                        COALESCE(transfer.funds_available_at, transfer.created_at),
                        COALESCE(transfer.returned_at, transfer.created_at),
                        COALESCE(transfer.cancelled_at, transfer.created_at)
                    ))
                    FROM payment_transfers AS transfer
                    WHERE transfer.mandate_id = mandate.id
                ), mandate.signed_at, mandate.created_at),
                COALESCE((
                    SELECT MAX(COALESCE(event.event_timestamp, event.created_at))
                    FROM payment_transfer_events AS event
                    JOIN payment_transfers AS transfer
                      ON transfer.id = event.transfer_id
                      OR (
                          event.transfer_id IS NULL
                          AND event.plaid_transfer_id IS NOT NULL
                          AND transfer.plaid_transfer_id = event.plaid_transfer_id
                      )
                    WHERE transfer.mandate_id = mandate.id
                ), mandate.signed_at, mandate.created_at),
                COALESCE((
                    SELECT MAX(GREATEST(
                        refund.created_at,
                        COALESCE(refund.updated_at, refund.created_at),
                        COALESCE(refund.completed_at, refund.created_at)
                    ))
                    FROM payment_refunds AS refund
                    JOIN payment_transfers AS transfer
                      ON transfer.id = refund.transfer_id
                    WHERE transfer.mandate_id = mandate.id
                ), mandate.signed_at, mandate.created_at)
            ) + INTERVAL '2 years'
        )
        """
    )

    # The guard resolves both direct and indirect profile ownership. It blocks
    # ordinary DELETEs regardless of age, enforces each row's own two-year
    # window, honors the mandate's specialized retention deadline, and checks
    # every effective legal hold/deadline in the linked business-record graph.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_payment_record_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            record_json jsonb;
            profile_uuid uuid;
            history_created_at timestamptz;
            history_anchor timestamptz;
            timestamp_key text;
            timestamp_value timestamptz;
            mandate_deadline timestamptz;
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of payment history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            record_json := to_jsonb(OLD);
            history_created_at := NULLIF(record_json ->> 'created_at', '')::timestamptz;
            history_anchor := history_created_at;
            FOREACH timestamp_key IN ARRAY ARRAY[
                'updated_at', 'authorization_sent_at', 'superseded_at',
                'confirmed_at', 'revoked_at', 'signed_at', 'terminated_at',
                'expires_at', 'verified_at', 'consulting_milestone_confirmed_at',
                'earned_confirmed_at', 'agreement_executed_at',
                'scheduled_debit_at', 'sent_at', 'provider_accepted_at',
                'debit_window_start_at', 'debit_window_end_at',
                'revocation_cutoff_at', 'proof_copy_sent_at',
                'proof_copy_delivered_at',
                'delivered_at', 'bounced_at', 'failed_at', 'activated_at',
                'paused_at', 'cancelled_at', 'claimed_at', 'submitted_at',
                'funds_available_at', 'returned_at', 'event_timestamp',
                'completed_at', 'actual_funding_date', 'effective_from',
                'effective_to', 'first_due_date', 'next_due_date', 'due_date',
                'received_on'
            ] LOOP
                timestamp_value := NULLIF(record_json ->> timestamp_key, '')::timestamptz;
                IF timestamp_value IS NOT NULL THEN
                    history_anchor := GREATEST(
                        COALESCE(history_anchor, timestamp_value),
                        timestamp_value
                    );
                END IF;
            END LOOP;

            IF history_created_at IS NULL
               OR history_anchor IS NULL
               OR history_anchor + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'payment history is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            profile_uuid := NULLIF(record_json ->> 'application_profile_id', '')::uuid;
            IF TG_TABLE_NAME = 'fee_obligation_lines' THEN
                SELECT obligation.application_profile_id
                INTO profile_uuid
                FROM fee_obligations AS obligation
                WHERE obligation.id = NULLIF(record_json ->> 'obligation_id', '')::uuid;
            ELSIF TG_TABLE_NAME = 'payment_installments' THEN
                SELECT plan.application_profile_id
                INTO profile_uuid
                FROM private_funding_payment_plans AS plan
                WHERE plan.id = NULLIF(record_json ->> 'plan_id', '')::uuid;
            ELSIF TG_TABLE_NAME = 'payment_transfer_events' THEN
                SELECT transfer.application_profile_id
                INTO profile_uuid
                FROM payment_transfers AS transfer
                WHERE transfer.id = NULLIF(record_json ->> 'transfer_id', '')::uuid
                   OR (
                        NULLIF(record_json ->> 'transfer_id', '') IS NULL
                        AND NULLIF(record_json ->> 'plaid_transfer_id', '') IS NOT NULL
                        AND transfer.plaid_transfer_id = record_json ->> 'plaid_transfer_id'
                   )
                ORDER BY CASE
                    WHEN transfer.id = NULLIF(record_json ->> 'transfer_id', '')::uuid THEN 0
                    ELSE 1
                END
                LIMIT 1;
            ELSIF TG_TABLE_NAME = 'payment_refunds' THEN
                SELECT transfer.application_profile_id
                INTO profile_uuid
                FROM payment_transfers AS transfer
                WHERE transfer.id = NULLIF(record_json ->> 'transfer_id', '')::uuid;
            ELSIF TG_TABLE_NAME = 'bank_direct_fee_receipts' THEN
                SELECT obligation.application_profile_id
                INTO profile_uuid
                FROM fee_obligations AS obligation
                WHERE obligation.id = NULLIF(record_json ->> 'obligation_id', '')::uuid;
            END IF;

            IF TG_TABLE_NAME = 'ach_mandates' THEN
                mandate_deadline := NULLIF(record_json ->> 'retention_until', '')::timestamptz;
                IF mandate_deadline IS NULL OR mandate_deadline > CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'ACH authorization is protected by its lifecycle retention deadline'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            IF profile_uuid IS NULL THEN
                IF TG_TABLE_NAME <> 'payment_transfer_events' THEN
                    RAISE EXCEPTION 'payment history has no retained ApplicationProfile reference'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF qc_payment_profile_retention_blocked(profile_uuid) THEN
                RAISE EXCEPTION 'payment history is protected by ApplicationProfile retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in PAYMENT_LEDGER_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_retained_payment_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_payment_record_delete()
            """
        )

    # MessageSend is shared infrastructure, so only payment-compliance
    # contexts receive this append-only guard. These rows are the durable proof
    # that agreements, authorizations, debit notices, and revocations were
    # handed to the provider or delivered.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_payment_message_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            message_anchor timestamptz;
        BEGIN
            IF COALESCE(OLD.context, '') NOT IN (
                'success_fee_agreement',
                'ach_mandate_proof',
                'ach_debit_notice',
                'ach_mandate_revocation'
            ) THEN
                RETURN OLD;
            END IF;

            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of payment delivery evidence requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            message_anchor := GREATEST(
                OLD.created_at,
                COALESCE(OLD.updated_at, OLD.created_at),
                COALESCE(OLD.delivered_at, OLD.created_at),
                COALESCE(OLD.opened_at, OLD.created_at),
                COALESCE(OLD.failed_at, OLD.created_at)
            );
            IF message_anchor IS NULL
               OR message_anchor + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'payment delivery evidence is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.profile_id IS NULL THEN
                RAISE EXCEPTION 'payment delivery evidence has no retained ApplicationProfile reference'
                    USING ERRCODE = 'integrity_constraint_violation';
            ELSIF qc_payment_profile_retention_blocked(OLD.profile_id) THEN
                RAISE EXCEPTION 'payment delivery evidence is protected by ApplicationProfile retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_retained_payment_message_delete
        BEFORE DELETE ON message_sends
        FOR EACH ROW
        EXECUTE FUNCTION qc_guard_retained_payment_message_delete()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_retained_payment_message_delete ON message_sends"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_retained_payment_message_delete()")

    for table in PAYMENT_LEDGER_TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_retained_payment_delete ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_retained_payment_record_delete()")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_payment_refunds_ach_retention ON payment_refunds"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_extend_ach_retention_from_refund()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_payment_transfer_events_ach_retention "
        "ON payment_transfer_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS qc_extend_ach_retention_from_transfer_event()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_payment_transfers_ach_retention ON payment_transfers"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_extend_ach_retention_from_transfer()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ach_mandates_retention_propagate ON ach_mandates"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_propagate_ach_mandate_retention()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ach_mandates_retention_normalize ON ach_mandates"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_normalize_ach_mandate_retention()")
    op.execute("DROP FUNCTION IF EXISTS qc_payment_profile_retention_blocked(uuid)")
