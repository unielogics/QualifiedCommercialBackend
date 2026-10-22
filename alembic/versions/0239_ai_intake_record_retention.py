"""Two-year AI Intake, Bucket, and evidence retention.

Revision ID: 0239_ai_intake_record_retention
Revises: 0238_plaid_lifecycle_safety
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0239_ai_intake_record_retention"
down_revision = "0238_plaid_lifecycle_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    deadline_default = sa.text("(CURRENT_TIMESTAMP + INTERVAL '2 years')")

    op.add_column(
        "buckets",
        sa.Column(
            "retention_class",
            sa.String(32),
            nullable=False,
            server_default="bucket_record",
        ),
    )
    op.add_column(
        "buckets",
        sa.Column(
            "protected_until",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=deadline_default,
        ),
    )
    op.add_column(
        "buckets",
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "retention_class",
            sa.String(32),
            nullable=False,
            server_default="ai_intake_record",
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "retention_class",
            sa.String(32),
            nullable=False,
            server_default="application_record",
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "protected_until",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=deadline_default,
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "protected_until",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=deadline_default,
        ),
    )
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Application owners can be removed from the active application without
    # erasing their retained record. Rebuild the active-only unique indexes so
    # a corrected replacement owner may reuse the primary flag or email.
    op.add_column(
        "application_owners",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "application_owners",
        sa.Column("archived_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "application_owners",
        sa.Column("archive_reason", sa.String(160), nullable=True),
    )
    op.create_foreign_key(
        "fk_application_owners_archived_by_user",
        "application_owners",
        "users",
        ["archived_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_application_owners_archived_token_revoked",
        "application_owners",
        "archived_at IS NULL OR invite_token_hash IS NULL",
    )
    op.drop_index("uq_application_owners_primary", table_name="application_owners")
    op.drop_index("uq_application_owners_email", table_name="application_owners")
    op.create_index(
        "uq_application_owners_primary",
        "application_owners",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND archived_at IS NULL"),
    )
    op.create_index(
        "uq_application_owners_email",
        "application_owners",
        ["profile_id", sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND archived_at IS NULL"),
    )

    # One shared clock for durable child records.  The child tables use
    # different lifecycle vocabularies, so reading through JSON keeps this
    # function safe for every trigger while still anchoring retention to the
    # newest event actually present on a row.  Scheduled future dates (for
    # example expires_at/next_attempt_at) are intentionally excluded: they are
    # not proof that an event occurred.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_case_record_retention_anchor(
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
            history_anchor := COALESCE(
                NULLIF(record_json ->> 'created_at', '')::timestamptz,
                NULLIF(record_json ->> 'occurred_at', '')::timestamptz
            );
            FOREACH timestamp_key IN ARRAY ARRAY[
                'updated_at', 'completed_at', 'approved_at', 'executed_at',
                'verified_at', 'revoked_at', 'sent_at', 'delivered_at',
                'responded_at', 'opened_at', 'signed_at', 'recorded_at',
                'started_at', 'analyzed_at', 'credit_pulled_at',
                'invite_sent_at', 'invite_opened_at', 'last_requested_at',
                'last_accessed_at', 'passcode_set_by_client_at',
                'archived_at', 'superseded_at', 'withdrawn_at', 'consumed_at'
            ] LOOP
                timestamp_value := NULLIF(
                    record_json ->> timestamp_key,
                    ''
                )::timestamptz;
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

    # Existing workspaces start with creation + 2 years. An archive or a
    # terminal/final lifecycle value resets the clock from its latest durable
    # timestamp. GREATEST ensures an ACH/legal hold deadline is never shortened.
    op.execute(
        """
        UPDATE buckets AS b
        SET protected_until = GREATEST(
            b.created_at + INTERVAL '2 years',
            COALESCE(b.archived_at + INTERVAL '2 years', b.created_at + INTERVAL '2 years'),
            COALESCE((
                SELECT MAX(
                    GREATEST(
                        i.created_at,
                        COALESCE(i.completed_at, i.created_at),
                        CASE
                            WHEN i.status IN (
                                'closed', 'closed_won', 'closed_lost', 'funded',
                                'denied', 'disqualified', 'lost', 'withdrawn'
                            )
                              OR i.outcome_status IN (
                                'closed', 'closed_won', 'closed_lost', 'funded',
                                'denied', 'disqualified', 'lost', 'withdrawn'
                              )
                              OR i.foreclosure_rescue_status IN ('funded', 'declined', 'withdrawn_expired')
                            THEN i.updated_at
                            ELSE i.created_at
                        END
                    ) + INTERVAL '2 years'
                )
                FROM public_underwriting_intakes AS i
                WHERE i.bucket_id = b.id
            ), b.created_at + INTERVAL '2 years')
        )
        """
    )
    op.execute(
        """
        UPDATE public_underwriting_intakes AS i
        SET protected_until = GREATEST(
            i.created_at + INTERVAL '2 years',
            COALESCE(i.completed_at + INTERVAL '2 years', i.created_at + INTERVAL '2 years'),
            CASE
                WHEN i.status IN (
                    'closed', 'closed_won', 'closed_lost', 'funded',
                    'denied', 'disqualified', 'lost', 'withdrawn'
                )
                  OR i.outcome_status IN (
                    'closed', 'closed_won', 'closed_lost', 'funded',
                    'denied', 'disqualified', 'lost', 'withdrawn'
                  )
                  OR i.foreclosure_rescue_status IN ('funded', 'declined', 'withdrawn_expired')
                THEN i.updated_at + INTERVAL '2 years'
                ELSE i.created_at + INTERVAL '2 years'
            END,
            b.protected_until
        )
        FROM buckets AS b
        WHERE b.id = i.bucket_id
        """
    )
    op.execute(
        """
        UPDATE application_profiles AS ap
        SET protected_until = GREATEST(
            ap.created_at + INTERVAL '2 years',
            CASE
                WHEN ap.underwriting_status IN ('closed_won', 'closed_lost', 'denied')
                THEN COALESCE(ap.underwriting_updated_at, ap.updated_at, ap.created_at)
                     + INTERVAL '2 years'
                ELSE ap.created_at + INTERVAL '2 years'
            END,
            COALESCE(b.protected_until, ap.created_at + INTERVAL '2 years'),
            COALESCE(i.protected_until, ap.created_at + INTERVAL '2 years')
        )
        FROM application_profiles AS source
        LEFT JOIN buckets AS b ON b.id = source.primary_bucket_id
        LEFT JOIN public_underwriting_intakes AS i ON i.id = source.intake_id
        WHERE ap.id = source.id
        """
    )
    op.execute(
        """
        UPDATE buckets AS b
        SET protected_until = GREATEST(
            b.protected_until,
            COALESCE((
                SELECT MAX(ap.protected_until)
                FROM application_profiles AS ap
                WHERE ap.primary_bucket_id = b.id
            ), b.protected_until)
        )
        """
    )
    op.execute(
        """
        UPDATE public_underwriting_intakes AS i
        SET protected_until = GREATEST(
            i.protected_until,
            COALESCE((
                SELECT MAX(ap.protected_until)
                FROM application_profiles AS ap
                WHERE ap.intake_id = i.id
            ), i.protected_until),
            b.protected_until
        )
        FROM buckets AS b
        WHERE b.id = i.bucket_id
        """
    )
    op.execute(
        """
        UPDATE bucket_files AS bf
        SET protected_until = GREATEST(bf.protected_until, b.protected_until)
        FROM buckets AS b
        WHERE b.id = bf.bucket_id
        """
    )
    op.execute(
        """
        UPDATE buckets AS b
        SET protected_until = GREATEST(
            b.protected_until,
            COALESCE((
                SELECT MAX(i.protected_until)
                FROM public_underwriting_intakes AS i
                WHERE i.bucket_id = b.id
            ), b.protected_until)
        )
        """
    )
    op.execute(
        """
        UPDATE bucket_files AS bf
        SET retention_class = COALESCE(bf.retention_class, 'bucket_evidence'),
            protected_until = GREATEST(
                COALESCE(bf.protected_until, bf.created_at + INTERVAL '2 years'),
                bf.created_at + INTERVAL '2 years',
                b.protected_until
            )
        FROM buckets AS b
        WHERE b.id = bf.bucket_id
        """
    )
    op.execute(
        """
        UPDATE buckets AS b
        SET protected_until = GREATEST(
            b.protected_until,
            COALESCE((
                SELECT MAX(bf.protected_until)
                FROM bucket_files AS bf
                WHERE bf.bucket_id = b.id
            ), b.protected_until)
        )
        """
    )
    op.execute(
        """
        UPDATE public_underwriting_intakes AS i
        SET protected_until = GREATEST(i.protected_until, b.protected_until)
        FROM buckets AS b
        WHERE b.id = i.bucket_id
        """
    )
    # BucketFile creation may have moved the Bucket/Intake deadline after the
    # first ApplicationProfile backfill above.  Converge the profile once more
    # so every linked root advertises the strongest evidence deadline.
    op.execute(
        """
        UPDATE application_profiles AS ap
        SET protected_until = GREATEST(
            ap.protected_until,
            COALESCE((
                SELECT i.protected_until
                FROM public_underwriting_intakes AS i
                WHERE i.id = ap.intake_id
            ), ap.protected_until),
            COALESCE((
                SELECT MAX(b.protected_until)
                FROM buckets AS b
                WHERE b.id = ap.primary_bucket_id
                   OR b.id IN (
                       SELECT i.bucket_id
                       FROM public_underwriting_intakes AS i
                       WHERE i.id = ap.intake_id
                   )
            ), ap.protected_until),
            COALESCE((
                SELECT MAX(bf.protected_until)
                FROM bucket_files AS bf
                WHERE bf.bucket_id = ap.primary_bucket_id
                   OR bf.bucket_id IN (
                       SELECT i.bucket_id
                       FROM public_underwriting_intakes AS i
                       WHERE i.id = ap.intake_id
                   )
            ), ap.protected_until)
        )
        """
    )

    # Existing durable profile history may be newer than the profile itself.
    # Extend the root from every child's latest completed lifecycle event so a
    # later governed parent purge cannot cascade through a younger record.
    direct_profile_history_tables = (
        "application_extracted_facts",
        "application_financial_periods",
        "application_capital_readiness_snapshots",
        "application_addback_verifications",
        "application_capital_readiness_actions",
        "application_assessment_snapshots",
        "file_events",
        "business_financial_statements",
        "financial_statements",
        "application_offer_deliveries",
        "application_term_sheets",
        "application_requirement_states",
        "application_requirement_responses",
        "application_verification_invitations",
        "application_bank_consents",
        "plaid_asset_reports",
        "application_evidence_policy_selections",
        "application_program_selections",
        "merchant_processing_offers",
        "submission_batches",
        "financial_worksheets",
        "application_owners",
        "dos_debts",
        "application_collection_tasks",
    )
    for table in direct_profile_history_tables:
        op.execute(
            f"""
            UPDATE application_profiles AS profile
            SET protected_until = GREATEST(
                profile.protected_until,
                history.deadline
            )
            FROM (
                SELECT profile_id,
                       MAX(qc_case_record_retention_anchor(to_jsonb(record)))
                           + INTERVAL '2 years' AS deadline
                FROM {table} AS record
                WHERE profile_id IS NOT NULL
                  AND qc_case_record_retention_anchor(to_jsonb(record)) IS NOT NULL
                GROUP BY profile_id
            ) AS history
            WHERE profile.id = history.profile_id
              AND profile.protected_until < history.deadline
            """
        )

    # Bucket activity, AI review/action history, notes, annotations, and
    # document requests are case records too.  Backfill their own clocks before
    # the graph-convergence pass copies the strongest deadline everywhere.
    direct_bucket_history_tables = (
        "bucket_activity_logs",
        "bucket_ai_reviews",
        "bucket_file_analyses",
        "bucket_ai_messages",
        "bucket_notes",
        "bucket_file_annotations",
        "bucket_requested_documents",
        "bucket_ai_action_items",
        "bucket_ai_chat_actions",
        "bucket_upload_links",
        "bucket_shares",
        "bucket_vendor_access",
        "bucket_public_shares",
    )
    for table in direct_bucket_history_tables:
        op.execute(
            f"""
            UPDATE buckets AS bucket
            SET protected_until = GREATEST(
                bucket.protected_until,
                history.deadline
            )
            FROM (
                SELECT bucket_id,
                       MAX(qc_case_record_retention_anchor(to_jsonb(record)))
                           + INTERVAL '2 years' AS deadline
                FROM {table} AS record
                WHERE bucket_id IS NOT NULL
                  AND qc_case_record_retention_anchor(to_jsonb(record)) IS NOT NULL
                GROUP BY bucket_id
            ) AS history
            WHERE bucket.id = history.bucket_id
              AND bucket.protected_until < history.deadline
            """
        )
    op.execute(
        """
        UPDATE buckets AS bucket
        SET protected_until = GREATEST(bucket.protected_until, history.deadline)
        FROM (
            SELECT requested.bucket_id,
                   MAX(qc_case_record_retention_anchor(to_jsonb(signature)))
                       + INTERVAL '2 years' AS deadline
            FROM bucket_document_signatures AS signature
            JOIN bucket_requested_documents AS requested
              ON requested.id = signature.requested_document_id
            WHERE qc_case_record_retention_anchor(to_jsonb(signature)) IS NOT NULL
            GROUP BY requested.bucket_id
        ) AS history
        WHERE bucket.id = history.bucket_id
          AND bucket.protected_until < history.deadline
        """
    )

    # Later-created durable descendants extend the root record before graph
    # convergence. This keeps API-visible deadlines truthful instead of
    # relying solely on a child trigger to reject an eventual cascade.
    op.execute(
        """
        WITH nested_profile_deadlines AS (
            SELECT state.profile_id,
                   GREATEST(
                       evidence.created_at,
                       evidence.updated_at,
                       evidence.linked_at,
                       COALESCE(evidence.verified_at, evidence.created_at),
                       COALESCE(evidence.removed_at, evidence.created_at)
                   ) + INTERVAL '2 years' AS deadline
            FROM application_requirement_evidence_files AS evidence
            JOIN application_requirement_states AS state
              ON state.id = evidence.requirement_state_id
            UNION ALL
            SELECT state.profile_id,
                   GREATEST(decision.created_at, decision.updated_at)
                       + INTERVAL '2 years'
            FROM application_requirement_evidence_decisions AS decision
            JOIN application_requirement_evidence_files AS evidence
              ON evidence.id = decision.requirement_evidence_id
            JOIN application_requirement_states AS state
              ON state.id = evidence.requirement_state_id
            UNION ALL
            SELECT selection.profile_id,
                   GREATEST(
                       requirement_override.created_at,
                       requirement_override.updated_at,
                       COALESCE(
                           requirement_override.restored_at,
                           requirement_override.created_at
                       )
                   ) + INTERVAL '2 years'
            FROM application_program_requirement_overrides AS requirement_override
            JOIN application_program_selections AS selection
              ON selection.id = requirement_override.selection_id
            UNION ALL
            SELECT delivery.profile_id,
                   GREATEST(
                       item.created_at,
                       item.updated_at,
                       COALESCE(item.responded_at, item.created_at),
                       COALESCE(item.expires_at, item.created_at)
                   ) + INTERVAL '2 years'
            FROM application_offer_delivery_items AS item
            JOIN application_offer_deliveries AS delivery
              ON delivery.id = item.delivery_id
            UNION ALL
            SELECT term.profile_id,
                   GREATEST(
                       delivery.created_at,
                       delivery.updated_at,
                       COALESCE(delivery.sent_at, delivery.created_at)
                   ) + INTERVAL '2 years'
            FROM application_term_sheet_deliveries AS delivery
            JOIN application_term_sheets AS term
              ON term.id = delivery.term_sheet_id
            UNION ALL
            SELECT worksheet.profile_id,
                   edit.created_at + INTERVAL '2 years'
            FROM financial_worksheet_edits AS edit
            JOIN financial_worksheets AS worksheet
              ON worksheet.id = edit.worksheet_id
            UNION ALL
            SELECT batch.profile_id,
                   GREATEST(
                       submission.created_at,
                       submission.updated_at,
                       COALESCE(submission.revoked_at, submission.created_at),
                       COALESCE(submission.queued_at, submission.created_at),
                       COALESCE(submission.sending_at, submission.created_at),
                       COALESCE(submission.sent_at, submission.created_at),
                       COALESCE(submission.delivered_at, submission.created_at),
                       COALESCE(submission.viewed_at, submission.created_at),
                       COALESCE(submission.downloaded_at, submission.created_at)
                   ) + INTERVAL '2 years'
            FROM lender_submissions AS submission
            JOIN submission_batches AS batch ON batch.id = submission.batch_id
            UNION ALL
            SELECT batch.profile_id,
                   event.occurred_at + INTERVAL '2 years'
            FROM lender_submission_events AS event
            JOIN lender_submissions AS submission
              ON submission.id = event.submission_id
            JOIN submission_batches AS batch ON batch.id = submission.batch_id
            UNION ALL
            SELECT delivery.profile_id,
                   GREATEST(delivery.created_at, delivery.updated_at)
                       + INTERVAL '2 years'
            FROM application_room_deliveries AS delivery
            WHERE delivery.profile_id IS NOT NULL
            UNION ALL
            SELECT referral.profile_id,
                   GREATEST(
                       referral.created_at,
                       referral.updated_at,
                       referral.consented_at
                   ) + INTERVAL '2 years'
            FROM arf_referrals AS referral
            UNION ALL
            SELECT snapshot.profile_id,
                   GREATEST(
                       assessment.created_at,
                       assessment.updated_at
                   ) + INTERVAL '2 years'
            FROM profitability_assessments AS assessment
            JOIN application_capital_readiness_snapshots AS snapshot
              ON snapshot.id = assessment.snapshot_id
            UNION ALL
            SELECT snapshot.profile_id,
                   GREATEST(review.created_at, review.updated_at)
                       + INTERVAL '2 years'
            FROM capital_readiness_reviews AS review
            JOIN application_capital_readiness_snapshots AS snapshot
              ON snapshot.id = review.snapshot_id
        ), strongest AS (
            SELECT profile_id, MAX(deadline) AS deadline
            FROM nested_profile_deadlines
            GROUP BY profile_id
        )
        UPDATE application_profiles AS ap
        SET protected_until = GREATEST(ap.protected_until, strongest.deadline)
        FROM strongest
        WHERE ap.id = strongest.profile_id
        """
    )
    op.execute(
        """
        WITH evidence_deadlines AS (
            SELECT evidence.file_id,
                   GREATEST(
                       evidence.created_at,
                       evidence.updated_at,
                       evidence.linked_at,
                       COALESCE(evidence.verified_at, evidence.created_at),
                       COALESCE(evidence.removed_at, evidence.created_at)
                   ) + INTERVAL '2 years' AS deadline
            FROM application_requirement_evidence_files AS evidence
            UNION ALL
            SELECT evidence.file_id,
                   GREATEST(decision.created_at, decision.updated_at)
                       + INTERVAL '2 years'
            FROM application_requirement_evidence_decisions AS decision
            JOIN application_requirement_evidence_files AS evidence
              ON evidence.id = decision.requirement_evidence_id
        ), strongest AS (
            SELECT file_id, MAX(deadline) AS deadline
            FROM evidence_deadlines
            GROUP BY file_id
        )
        UPDATE bucket_files AS bf
        SET protected_until = GREATEST(bf.protected_until, strongest.deadline)
        FROM strongest
        WHERE bf.id = strongest.file_id
        """
    )
    op.execute(
        """
        UPDATE buckets AS b
        SET protected_until = GREATEST(b.protected_until, delivery.deadline)
        FROM (
            SELECT room.bucket_id,
                   MAX(GREATEST(room.created_at, room.updated_at))
                       + INTERVAL '2 years' AS deadline
            FROM application_room_deliveries AS room
            GROUP BY room.bucket_id
        ) AS delivery
        WHERE b.id = delivery.bucket_id
        """
    )

    # Converge every historical Bucket/Intake link as a durable retention
    # edge.  A link stays in this graph after unlinked_at is set because the
    # evidence relationship remains part of the retained case history.
    op.execute(
        """
        DO $$
        DECLARE
            changed_count integer;
            changed_total integer;
        BEGIN
            LOOP
                changed_total := 0;

                UPDATE buckets AS b
                SET protected_until = graph.max_deadline
                FROM (
                    SELECT edge.bucket_id, MAX(edge.deadline) AS max_deadline
                    FROM (
                        SELECT i.bucket_id, i.protected_until AS deadline
                        FROM public_underwriting_intakes AS i
                        WHERE i.bucket_id IS NOT NULL
                        UNION ALL
                        SELECT link.bucket_id,
                               GREATEST(
                                   i.protected_until,
                                   link.created_at + INTERVAL '2 years',
                                   COALESCE(
                                       link.unlinked_at + INTERVAL '2 years',
                                       link.created_at + INTERVAL '2 years'
                                   ),
                                   COALESCE((
                                       SELECT MAX(
                                           GREATEST(
                                               link_file.created_at,
                                               COALESCE(
                                                   link_file.removed_at,
                                                   link_file.created_at
                                               )
                                           ) + INTERVAL '2 years'
                                       )
                                       FROM bucket_intake_link_files AS link_file
                                       WHERE link_file.link_id = link.id
                                   ), link.created_at + INTERVAL '2 years')
                               ) AS deadline
                        FROM bucket_intake_links AS link
                        JOIN public_underwriting_intakes AS i
                          ON i.id = link.intake_id
                    ) AS edge
                    GROUP BY edge.bucket_id
                ) AS graph
                WHERE b.id = graph.bucket_id
                  AND b.protected_until < graph.max_deadline;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE public_underwriting_intakes AS i
                SET protected_until = graph.max_deadline
                FROM (
                    SELECT edge.intake_id, MAX(edge.deadline) AS max_deadline
                    FROM (
                        SELECT i.id AS intake_id, b.protected_until AS deadline
                        FROM public_underwriting_intakes AS i
                        JOIN buckets AS b ON b.id = i.bucket_id
                        UNION ALL
                        SELECT link.intake_id,
                               GREATEST(
                                   b.protected_until,
                                   link.created_at + INTERVAL '2 years',
                                   COALESCE(
                                       link.unlinked_at + INTERVAL '2 years',
                                       link.created_at + INTERVAL '2 years'
                                   ),
                                   COALESCE((
                                       SELECT MAX(
                                           GREATEST(
                                               link_file.created_at,
                                               COALESCE(
                                                   link_file.removed_at,
                                                   link_file.created_at
                                               )
                                           ) + INTERVAL '2 years'
                                       )
                                       FROM bucket_intake_link_files AS link_file
                                       WHERE link_file.link_id = link.id
                                   ), link.created_at + INTERVAL '2 years')
                               ) AS deadline
                        FROM bucket_intake_links AS link
                        JOIN buckets AS b ON b.id = link.bucket_id
                    ) AS edge
                    GROUP BY edge.intake_id
                ) AS graph
                WHERE i.id = graph.intake_id
                  AND i.protected_until < graph.max_deadline;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE buckets AS b
                SET protected_until = graph.max_deadline
                FROM (
                    SELECT bf.bucket_id, MAX(bf.protected_until) AS max_deadline
                    FROM bucket_files AS bf
                    GROUP BY bf.bucket_id
                ) AS graph
                WHERE b.id = graph.bucket_id
                  AND b.protected_until < graph.max_deadline;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE bucket_files AS bf
                SET protected_until = b.protected_until
                FROM buckets AS b
                WHERE b.id = bf.bucket_id
                  AND bf.protected_until < b.protected_until;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE application_profiles AS ap
                SET protected_until = i.protected_until
                FROM public_underwriting_intakes AS i
                WHERE i.id = ap.intake_id
                  AND ap.protected_until < i.protected_until;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE application_profiles AS ap
                SET protected_until = b.protected_until
                FROM buckets AS b
                WHERE b.id = ap.primary_bucket_id
                  AND ap.protected_until < b.protected_until;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE public_underwriting_intakes AS i
                SET protected_until = graph.max_deadline
                FROM (
                    SELECT ap.intake_id, MAX(ap.protected_until) AS max_deadline
                    FROM application_profiles AS ap
                    WHERE ap.intake_id IS NOT NULL
                    GROUP BY ap.intake_id
                ) AS graph
                WHERE i.id = graph.intake_id
                  AND i.protected_until < graph.max_deadline;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                UPDATE buckets AS b
                SET protected_until = graph.max_deadline
                FROM (
                    SELECT ap.primary_bucket_id AS bucket_id,
                           MAX(ap.protected_until) AS max_deadline
                    FROM application_profiles AS ap
                    WHERE ap.primary_bucket_id IS NOT NULL
                    GROUP BY ap.primary_bucket_id
                ) AS graph
                WHERE b.id = graph.bucket_id
                  AND b.protected_until < graph.max_deadline;
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                changed_total := changed_total + changed_count;

                EXIT WHEN changed_total = 0;
            END LOOP;
        END;
        $$
        """
    )

    op.alter_column("buckets", "protected_until", nullable=False)
    op.alter_column(
        "public_underwriting_intakes",
        "protected_until",
        nullable=False,
    )
    op.alter_column("application_profiles", "protected_until", nullable=False)
    op.alter_column(
        "bucket_files",
        "retention_class",
        nullable=False,
        server_default="bucket_evidence",
    )
    op.alter_column(
        "bucket_files",
        "protected_until",
        nullable=False,
        server_default=deadline_default,
    )

    op.create_check_constraint(
        "ck_buckets_minimum_retention",
        "buckets",
        "protected_until >= (GREATEST(created_at, COALESCE(archived_at, created_at)) + INTERVAL '2 years')",
    )
    op.create_check_constraint(
        "ck_public_underwriting_intakes_minimum_retention",
        "public_underwriting_intakes",
        "protected_until >= (GREATEST(created_at, COALESCE(completed_at, created_at)) + INTERVAL '2 years')",
    )
    op.create_check_constraint(
        "ck_bucket_files_minimum_retention",
        "bucket_files",
        "protected_until >= (created_at + INTERVAL '2 years')",
    )
    op.create_check_constraint(
        "ck_application_profiles_minimum_retention",
        "application_profiles",
        "protected_until >= (created_at + INTERVAL '2 years')",
    )
    op.create_index(
        "ix_buckets_retention",
        "buckets",
        ["retention_class", "protected_until"],
    )
    op.create_index(
        "ix_public_underwriting_intakes_retention",
        "public_underwriting_intakes",
        ["retention_class", "protected_until"],
    )
    op.create_index(
        "ix_application_profiles_retention",
        "application_profiles",
        ["retention_class", "protected_until"],
    )

    # Normalize retention inside the database before constraints are checked.
    # This makes the guarantee hold for ORM writes, bulk SQL, background jobs,
    # and future call sites that do not know about the Python retention helper.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_normalize_bucket_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            retention_anchor timestamptz;
        BEGIN
            retention_anchor := GREATEST(
                COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.archived_at, NEW.created_at, CURRENT_TIMESTAMP)
            );
            NEW.retention_class := COALESCE(NEW.retention_class, 'bucket_record');
            NEW.protected_until := GREATEST(
                COALESCE(NEW.protected_until, retention_anchor + INTERVAL '2 years'),
                COALESCE(
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD.protected_until END,
                    retention_anchor + INTERVAL '2 years'
                ),
                retention_anchor + INTERVAL '2 years'
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_buckets_retention_normalize
        BEFORE INSERT OR UPDATE ON buckets
        FOR EACH ROW
        EXECUTE FUNCTION qc_normalize_bucket_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_normalize_intake_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            retention_anchor timestamptz;
            is_terminal boolean;
        BEGIN
            retention_anchor := GREATEST(
                COALESCE(NEW.created_at, CURRENT_TIMESTAMP),
                COALESCE(NEW.completed_at, NEW.created_at, CURRENT_TIMESTAMP)
            );
            is_terminal := (
                COALESCE(NEW.status, '') IN (
                    'closed', 'closed_won', 'closed_lost', 'funded',
                    'denied', 'disqualified', 'lost', 'withdrawn'
                )
                OR COALESCE(NEW.outcome_status, '') IN (
                    'closed', 'closed_won', 'closed_lost', 'funded',
                    'denied', 'disqualified', 'lost', 'withdrawn'
                )
                OR COALESCE(NEW.foreclosure_rescue_status, '') IN (
                    'funded', 'declined', 'withdrawn_expired'
                )
            );
            IF is_terminal THEN
                IF TG_OP = 'INSERT' THEN
                    retention_anchor := GREATEST(
                        retention_anchor,
                        COALESCE(NEW.updated_at, CURRENT_TIMESTAMP),
                        CURRENT_TIMESTAMP
                    );
                ELSIF OLD.status IS DISTINCT FROM NEW.status
                   OR OLD.outcome_status IS DISTINCT FROM NEW.outcome_status
                   OR OLD.foreclosure_rescue_status IS DISTINCT FROM NEW.foreclosure_rescue_status THEN
                    retention_anchor := GREATEST(
                        retention_anchor,
                        COALESCE(NEW.updated_at, CURRENT_TIMESTAMP),
                        CURRENT_TIMESTAMP
                    );
                END IF;
            END IF;
            NEW.retention_class := COALESCE(
                NEW.retention_class,
                'ai_intake_record'
            );
            NEW.protected_until := GREATEST(
                COALESCE(NEW.protected_until, retention_anchor + INTERVAL '2 years'),
                COALESCE(
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD.protected_until END,
                    retention_anchor + INTERVAL '2 years'
                ),
                retention_anchor + INTERVAL '2 years'
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_intakes_retention_normalize
        BEFORE INSERT OR UPDATE ON public_underwriting_intakes
        FOR EACH ROW
        EXECUTE FUNCTION qc_normalize_intake_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_normalize_application_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            retention_anchor timestamptz;
        BEGIN
            retention_anchor := COALESCE(NEW.created_at, CURRENT_TIMESTAMP);
            IF COALESCE(NEW.underwriting_status, '') IN (
                'closed_won', 'closed_lost', 'denied'
            ) THEN
                IF TG_OP = 'INSERT' THEN
                    retention_anchor := GREATEST(
                        retention_anchor,
                        COALESCE(NEW.underwriting_updated_at, NEW.updated_at, CURRENT_TIMESTAMP),
                        CURRENT_TIMESTAMP
                    );
                ELSIF OLD.underwriting_status IS DISTINCT FROM NEW.underwriting_status THEN
                    retention_anchor := GREATEST(
                        retention_anchor,
                        COALESCE(NEW.underwriting_updated_at, NEW.updated_at, CURRENT_TIMESTAMP),
                        CURRENT_TIMESTAMP
                    );
                END IF;
            END IF;
            NEW.retention_class := COALESCE(
                NEW.retention_class,
                'application_record'
            );
            NEW.protected_until := GREATEST(
                COALESCE(NEW.protected_until, retention_anchor + INTERVAL '2 years'),
                COALESCE(
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD.protected_until END,
                    retention_anchor + INTERVAL '2 years'
                ),
                retention_anchor + INTERVAL '2 years'
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_app_profiles_retention_normalize
        BEFORE INSERT OR UPDATE ON application_profiles
        FOR EACH ROW
        EXECUTE FUNCTION qc_normalize_application_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_normalize_bucket_file_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_deadline timestamptz;
            created_deadline timestamptz;
        BEGIN
            SELECT b.protected_until
            INTO parent_deadline
            FROM buckets AS b
            WHERE b.id = NEW.bucket_id;

            created_deadline := COALESCE(NEW.created_at, CURRENT_TIMESTAMP)
                + INTERVAL '2 years';
            NEW.retention_class := COALESCE(
                NEW.retention_class,
                'bucket_evidence'
            );
            NEW.protected_until := GREATEST(
                COALESCE(NEW.protected_until, created_deadline),
                COALESCE(
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD.protected_until END,
                    created_deadline
                ),
                created_deadline,
                COALESCE(parent_deadline, created_deadline)
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bucket_files_retention_normalize
        BEFORE INSERT OR UPDATE ON bucket_files
        FOR EACH ROW
        EXECUTE FUNCTION qc_normalize_bucket_file_retention()
        """
    )

    # Propagate the strongest deadline in either direction. A file uploaded
    # years after Bucket creation therefore extends both its Bucket and linked
    # Intake records. WHERE clauses prevent no-op trigger recursion.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_bucket_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE public_underwriting_intakes AS i
            SET protected_until = NEW.protected_until
            WHERE (
                    i.bucket_id = NEW.id
                    OR i.id IN (
                        SELECT link.intake_id
                        FROM bucket_intake_links AS link
                        WHERE link.bucket_id = NEW.id
                    )
                  )
              AND i.protected_until < NEW.protected_until;

            UPDATE bucket_files AS bf
            SET protected_until = NEW.protected_until
            WHERE bf.bucket_id = NEW.id
              AND bf.protected_until < NEW.protected_until;

            UPDATE application_profiles AS ap
            SET protected_until = NEW.protected_until
            WHERE ap.primary_bucket_id = NEW.id
              AND ap.protected_until < NEW.protected_until;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_buckets_retention_propagate
        AFTER INSERT OR UPDATE ON buckets
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_bucket_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_intake_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE buckets AS b
            SET protected_until = NEW.protected_until
            WHERE (
                    b.id = NEW.bucket_id
                    OR b.id IN (
                        SELECT link.bucket_id
                        FROM bucket_intake_links AS link
                        WHERE link.intake_id = NEW.id
                    )
                  )
              AND b.protected_until < NEW.protected_until;

            UPDATE bucket_files AS bf
            SET protected_until = NEW.protected_until
            WHERE (
                    bf.bucket_id = NEW.bucket_id
                    OR bf.bucket_id IN (
                        SELECT link.bucket_id
                        FROM bucket_intake_links AS link
                        WHERE link.intake_id = NEW.id
                    )
                  )
              AND bf.protected_until < NEW.protected_until;

            UPDATE application_profiles AS ap
            SET protected_until = NEW.protected_until
            WHERE ap.intake_id = NEW.id
              AND ap.protected_until < NEW.protected_until;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_intakes_retention_propagate
        AFTER INSERT OR UPDATE ON public_underwriting_intakes
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_intake_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_bucket_file_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE buckets AS b
            SET protected_until = NEW.protected_until
            WHERE b.id = NEW.bucket_id
              AND b.protected_until < NEW.protected_until;

            UPDATE public_underwriting_intakes AS i
            SET protected_until = NEW.protected_until
            WHERE (
                    i.bucket_id = NEW.bucket_id
                    OR i.id IN (
                        SELECT link.intake_id
                        FROM bucket_intake_links AS link
                        WHERE link.bucket_id = NEW.bucket_id
                    )
                  )
              AND i.protected_until < NEW.protected_until;

            UPDATE application_profiles AS ap
            SET protected_until = NEW.protected_until
            WHERE ap.primary_bucket_id = NEW.bucket_id
              AND ap.protected_until < NEW.protected_until;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bucket_files_retention_propagate
        AFTER INSERT OR UPDATE ON bucket_files
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_bucket_file_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_application_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE buckets AS b
            SET protected_until = NEW.protected_until
            WHERE b.id = NEW.primary_bucket_id
              AND b.protected_until < NEW.protected_until;

            UPDATE public_underwriting_intakes AS i
            SET protected_until = NEW.protected_until
            WHERE i.id = NEW.intake_id
              AND i.protected_until < NEW.protected_until;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_app_profiles_retention_propagate
        AFTER INSERT OR UPDATE ON application_profiles
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_application_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_bucket_intake_link_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            linked_deadline timestamptz;
        BEGIN
            SELECT GREATEST(
                b.protected_until,
                i.protected_until,
                NEW.created_at + INTERVAL '2 years',
                COALESCE(
                    NEW.unlinked_at + INTERVAL '2 years',
                    NEW.created_at + INTERVAL '2 years'
                )
            )
            INTO linked_deadline
            FROM buckets AS b
            JOIN public_underwriting_intakes AS i ON i.id = NEW.intake_id
            WHERE b.id = NEW.bucket_id;

            IF linked_deadline IS NULL THEN
                RETURN NEW;
            END IF;

            UPDATE buckets AS b
            SET protected_until = linked_deadline
            WHERE b.id = NEW.bucket_id
              AND b.protected_until < linked_deadline;

            UPDATE public_underwriting_intakes AS i
            SET protected_until = linked_deadline
            WHERE i.id = NEW.intake_id
              AND i.protected_until < linked_deadline;

            UPDATE bucket_files AS bf
            SET protected_until = linked_deadline
            WHERE bf.bucket_id = NEW.bucket_id
              AND bf.protected_until < linked_deadline;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bucket_intake_links_retention_propagate
        AFTER INSERT OR UPDATE OF bucket_id, intake_id, unlinked_at ON bucket_intake_links
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_bucket_intake_link_retention()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_bucket_intake_link_file_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            linked_bucket_id uuid;
            linked_intake_id uuid;
            linked_deadline timestamptz;
        BEGIN
            SELECT link.bucket_id,
                   link.intake_id,
                   GREATEST(
                       b.protected_until,
                       i.protected_until,
                       link.created_at + INTERVAL '2 years',
                       COALESCE(
                           link.unlinked_at + INTERVAL '2 years',
                           link.created_at + INTERVAL '2 years'
                       ),
                       GREATEST(
                           NEW.created_at,
                           COALESCE(NEW.removed_at, NEW.created_at)
                       ) + INTERVAL '2 years'
                   )
            INTO linked_bucket_id, linked_intake_id, linked_deadline
            FROM bucket_intake_links AS link
            JOIN buckets AS b ON b.id = link.bucket_id
            JOIN public_underwriting_intakes AS i ON i.id = link.intake_id
            WHERE link.id = NEW.link_id;

            IF linked_deadline IS NULL THEN
                RETURN NEW;
            END IF;

            UPDATE buckets AS b
            SET protected_until = linked_deadline
            WHERE b.id = linked_bucket_id
              AND b.protected_until < linked_deadline;

            UPDATE public_underwriting_intakes AS i
            SET protected_until = linked_deadline
            WHERE i.id = linked_intake_id
              AND i.protected_until < linked_deadline;

            UPDATE bucket_files AS bf
            SET protected_until = linked_deadline
            WHERE bf.bucket_id = linked_bucket_id
              AND bf.protected_until < linked_deadline;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bucket_intake_link_files_retention_propagate
        AFTER INSERT OR UPDATE OF link_id, removed_at ON bucket_intake_link_files
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_bucket_intake_link_file_retention()
        """
    )

    # These guards cover ORM delete, bulk SQL, and database cascades. The
    # Bucket guard checks children before a parent CASCADE could hide a longer
    # ACH/legal hold. Reversible archived_at/deleted_at updates are unaffected.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_business_record_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            bucket_seed uuid;
            intake_seed uuid;
            connected_bucket_ids uuid[];
            connected_intake_ids uuid[];
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF OLD.legal_hold
               OR OLD.protected_until IS NULL
               OR OLD.protected_until > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'record is protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF TG_TABLE_NAME = 'buckets' THEN
                bucket_seed := OLD.id;
            ELSIF TG_TABLE_NAME = 'bucket_files' THEN
                bucket_seed := OLD.bucket_id;
            ELSIF TG_TABLE_NAME = 'public_underwriting_intakes' THEN
                intake_seed := OLD.id;
            ELSIF TG_TABLE_NAME = 'application_profiles' THEN
                bucket_seed := OLD.primary_bucket_id;
                intake_seed := OLD.intake_id;
            END IF;

            WITH RECURSIVE retention_edges(
                from_kind, from_id, to_kind, to_id
            ) AS (
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
                SELECT 'bucket'::text, bucket_seed
                WHERE bucket_seed IS NOT NULL
                UNION
                SELECT 'ai_intake'::text, intake_seed
                WHERE intake_seed IS NOT NULL
            ), connected(kind, id) AS (
                SELECT kind, id FROM seeds
                UNION
                SELECT edge.to_kind, edge.to_id
                FROM connected AS current_node
                JOIN retention_edges AS edge
                  ON edge.from_kind = current_node.kind
                 AND edge.from_id = current_node.id
            )
            SELECT ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'bucket'),
                   ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'ai_intake')
            INTO connected_bucket_ids, connected_intake_ids
            FROM connected;

            IF EXISTS (
                SELECT 1
                FROM buckets AS b
                WHERE b.id = ANY(
                    COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                )
                  AND (
                      b.legal_hold
                      OR b.protected_until IS NULL
                      OR b.protected_until > CURRENT_TIMESTAMP
                  )
            ) OR EXISTS (
                SELECT 1
                FROM public_underwriting_intakes AS i
                WHERE i.id = ANY(
                    COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                )
                  AND (
                      i.legal_hold
                      OR i.protected_until IS NULL
                      OR i.protected_until > CURRENT_TIMESTAMP
                  )
            ) OR EXISTS (
                SELECT 1
                FROM bucket_files AS bf
                WHERE bf.bucket_id = ANY(
                    COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                )
                  AND (
                      bf.legal_hold
                      OR bf.protected_until IS NULL
                      OR bf.protected_until > CURRENT_TIMESTAMP
                  )
            ) OR EXISTS (
                SELECT 1
                FROM application_profiles AS ap
                WHERE (
                    ap.intake_id = ANY(
                        COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                    )
                    OR ap.primary_bucket_id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                )
                  AND (
                      ap.legal_hold
                      OR ap.protected_until IS NULL
                      OR ap.protected_until > CURRENT_TIMESTAMP
                  )
            ) OR EXISTS (
                SELECT 1
                FROM bucket_intake_links AS link
                WHERE (
                    link.intake_id = ANY(
                        COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                    )
                    OR link.bucket_id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
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
                                ) + INTERVAL '2 years'
                                > CURRENT_TIMESTAMP
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'record has connected records protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF TG_TABLE_NAME = 'buckets' THEN
                IF EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id = OLD.id
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.bucket_id = OLD.id
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM application_profiles AS ap
                    WHERE (
                        ap.primary_bucket_id = OLD.id
                        OR ap.intake_id IN (
                            SELECT i.id
                            FROM public_underwriting_intakes AS i
                            WHERE i.bucket_id = OLD.id
                        )
                    )
                      AND (
                          ap.legal_hold
                          OR ap.protected_until IS NULL
                          OR ap.protected_until > CURRENT_TIMESTAMP
                      )
                ) THEN
                    RAISE EXCEPTION 'bucket has child records protected by retention or legal hold'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            IF TG_TABLE_NAME = 'bucket_files' AND EXISTS (
                SELECT 1
                FROM buckets AS b
                WHERE b.id = OLD.bucket_id
                  AND (
                      b.legal_hold
                      OR b.protected_until IS NULL
                      OR b.protected_until > CURRENT_TIMESTAMP
                      OR EXISTS (
                          SELECT 1
                          FROM bucket_files AS bf
                          WHERE bf.bucket_id = b.id
                            AND (
                                bf.legal_hold
                                OR bf.protected_until IS NULL
                                OR bf.protected_until > CURRENT_TIMESTAMP
                            )
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM public_underwriting_intakes AS i
                          WHERE i.bucket_id = b.id
                            AND (
                                i.legal_hold
                                OR i.protected_until IS NULL
                                OR i.protected_until > CURRENT_TIMESTAMP
                            )
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM application_profiles AS ap
                          WHERE (
                              ap.primary_bucket_id = b.id
                              OR ap.intake_id IN (
                                  SELECT i.id
                                  FROM public_underwriting_intakes AS i
                                  WHERE i.bucket_id = b.id
                              )
                          )
                            AND (
                                ap.legal_hold
                                OR ap.protected_until IS NULL
                                OR ap.protected_until > CURRENT_TIMESTAMP
                            )
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'bucket evidence is governed by a parent legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF TG_TABLE_NAME = 'public_underwriting_intakes' AND EXISTS (
                SELECT 1
                FROM buckets AS b
                WHERE b.id = OLD.bucket_id
                  AND (
                      b.legal_hold
                      OR b.protected_until IS NULL
                      OR b.protected_until > CURRENT_TIMESTAMP
                      OR EXISTS (
                          SELECT 1
                          FROM bucket_files AS bf
                          WHERE bf.bucket_id = b.id
                            AND (
                                bf.legal_hold
                                OR bf.protected_until IS NULL
                                OR bf.protected_until > CURRENT_TIMESTAMP
                            )
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM application_profiles AS ap
                          WHERE (
                              ap.intake_id = OLD.id
                              OR ap.primary_bucket_id = b.id
                          )
                            AND (
                                ap.legal_hold
                                OR ap.protected_until IS NULL
                                OR ap.protected_until > CURRENT_TIMESTAMP
                            )
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'AI Intake is governed by a parent legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF TG_TABLE_NAME = 'application_profiles' AND (
                EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.id = OLD.intake_id
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM buckets AS b
                    WHERE (
                        b.id = OLD.primary_bucket_id
                        OR b.id IN (
                            SELECT i.bucket_id
                            FROM public_underwriting_intakes AS i
                            WHERE i.id = OLD.intake_id
                        )
                    )
                      AND (
                          b.legal_hold
                          OR b.protected_until IS NULL
                          OR b.protected_until > CURRENT_TIMESTAMP
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id IN (
                        SELECT b.id
                        FROM buckets AS b
                        WHERE b.id = OLD.primary_bucket_id
                           OR b.id IN (
                               SELECT i.bucket_id
                               FROM public_underwriting_intakes AS i
                               WHERE i.id = OLD.intake_id
                           )
                    )
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
                )
            ) THEN
                RAISE EXCEPTION 'application profile has linked records protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in (
        "application_profiles",
        "buckets",
        "bucket_files",
        "public_underwriting_intakes",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_retained_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_business_record_delete()
            """
        )

    # Append-only case history and evidence must not be removable around the
    # retained parent through a direct child DELETE. Mutable working rows (for
    # example team membership or a draft queue) are deliberately excluded.
    retained_history_tables = (
        "application_extracted_facts",
        "application_financial_periods",
        "application_capital_readiness_snapshots",
        "application_addback_verifications",
        "application_capital_readiness_actions",
        "application_assessment_snapshots",
        "file_events",
        "business_financial_statements",
        "financial_statements",
        "application_offer_deliveries",
        "application_term_sheets",
        "application_requirement_states",
        "application_requirement_responses",
        "application_verification_invitations",
        "application_bank_consents",
        "plaid_asset_reports",
        "application_evidence_policy_selections",
        "application_program_selections",
        "merchant_processing_offers",
        "submission_batches",
        "financial_worksheets",
        "application_owners",
        "dos_debts",
        "application_collection_tasks",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_application_history_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            profile_uuid uuid;
            history_anchor timestamptz;
            child_deadline timestamptz;
        BEGIN
            profile_uuid := NULLIF(to_jsonb(NEW) ->> 'profile_id', '')::uuid;
            history_anchor := qc_case_record_retention_anchor(to_jsonb(NEW));
            IF profile_uuid IS NULL OR history_anchor IS NULL THEN
                RETURN NEW;
            END IF;

            child_deadline := history_anchor + INTERVAL '2 years';
            UPDATE application_profiles AS profile
            SET protected_until = child_deadline
            WHERE profile.id = profile_uuid
              AND profile.protected_until < child_deadline;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_application_history_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            profile_uuid uuid;
            parent_profile application_profiles%ROWTYPE;
            history_created_at timestamptz;
            connected_bucket_ids uuid[];
            connected_intake_ids uuid[];
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of case history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            history_created_at := NULLIF(
                to_jsonb(OLD) ->> 'created_at',
                ''
            )::timestamptz;
            IF history_created_at IS NULL
               OR qc_case_record_retention_anchor(to_jsonb(OLD)) IS NULL
               OR qc_case_record_retention_anchor(to_jsonb(OLD))
                    + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'application history is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            profile_uuid := NULLIF(to_jsonb(OLD) ->> 'profile_id', '')::uuid;
            IF profile_uuid IS NULL THEN
                RAISE EXCEPTION 'application history has no retained parent reference'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT ap.* INTO parent_profile
            FROM application_profiles AS ap
            WHERE ap.id = profile_uuid;

            WITH RECURSIVE retention_edges(
                from_kind, from_id, to_kind, to_id
            ) AS (
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
                SELECT 'bucket'::text, parent_profile.primary_bucket_id
                WHERE parent_profile.primary_bucket_id IS NOT NULL
                UNION
                SELECT 'ai_intake'::text, parent_profile.intake_id
                WHERE parent_profile.intake_id IS NOT NULL
            ), connected(kind, id) AS (
                SELECT kind, id FROM seeds
                UNION
                SELECT edge.to_kind, edge.to_id
                FROM connected AS current_node
                JOIN retention_edges AS edge
                  ON edge.from_kind = current_node.kind
                 AND edge.from_id = current_node.id
            )
            SELECT ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'bucket'),
                   ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'ai_intake')
            INTO connected_bucket_ids, connected_intake_ids
            FROM connected;

            IF parent_profile.id IS NULL
               OR parent_profile.legal_hold
               OR parent_profile.protected_until IS NULL
               OR parent_profile.protected_until > CURRENT_TIMESTAMP
               OR EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.id = ANY(
                        COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM buckets AS b
                    WHERE b.id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          b.legal_hold
                          OR b.protected_until IS NULL
                          OR b.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
               ) THEN
                RAISE EXCEPTION 'application history is protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in retained_history_tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_application_history_retention_propagate
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_propagate_application_history_retention()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_retained_history_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_application_history_delete()
            """
        )

    # Intake-generated packets and delivery records are durable case history,
    # not disposable cache rows. Protect the child row's own two-year window
    # and every effective hold/deadline reachable through its Intake.
    retained_intake_history_tables = (
        "bucket_intake_links",
        "bucket_intake_link_files",
        "public_underwriting_intake_artifacts",
        "public_underwriting_intake_email_sends",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_intake_history_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            intake_uuid uuid;
            linked_bucket_uuid uuid;
            history_created_at timestamptz;
            history_anchor timestamptz;
            parent_intake public_underwriting_intakes%ROWTYPE;
            connected_bucket_ids uuid[];
            connected_intake_ids uuid[];
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of Intake history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            history_created_at := NULLIF(
                to_jsonb(OLD) ->> 'created_at',
                ''
            )::timestamptz;
            history_anchor := GREATEST(
                history_created_at,
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'unlinked_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'removed_at', '')::timestamptz,
                    history_created_at
                )
            );
            IF history_created_at IS NULL
               OR history_anchor IS NULL
               OR history_anchor + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'Intake history is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF TG_TABLE_NAME = 'bucket_intake_link_files' THEN
                SELECT link.intake_id, link.bucket_id
                INTO intake_uuid, linked_bucket_uuid
                FROM bucket_intake_links AS link
                WHERE link.id = NULLIF(
                    to_jsonb(OLD) ->> 'link_id',
                    ''
                )::uuid;
            ELSE
                intake_uuid := NULLIF(
                    to_jsonb(OLD) ->> 'intake_id',
                    ''
                )::uuid;
                linked_bucket_uuid := NULLIF(
                    to_jsonb(OLD) ->> 'bucket_id',
                    ''
                )::uuid;
            END IF;
            IF intake_uuid IS NULL THEN
                RAISE EXCEPTION 'Intake history has no retained parent reference'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT i.* INTO parent_intake
            FROM public_underwriting_intakes AS i
            WHERE i.id = intake_uuid;

            WITH RECURSIVE retention_edges(
                from_kind, from_id, to_kind, to_id
            ) AS (
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
                SELECT 'ai_intake'::text, intake_uuid
                WHERE intake_uuid IS NOT NULL
                UNION
                SELECT 'bucket'::text, linked_bucket_uuid
                WHERE linked_bucket_uuid IS NOT NULL
            ), connected(kind, id) AS (
                SELECT kind, id FROM seeds
                UNION
                SELECT edge.to_kind, edge.to_id
                FROM connected AS current_node
                JOIN retention_edges AS edge
                  ON edge.from_kind = current_node.kind
                 AND edge.from_id = current_node.id
            )
            SELECT ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'bucket'),
                   ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'ai_intake')
            INTO connected_bucket_ids, connected_intake_ids
            FROM connected;

            IF parent_intake.id IS NULL
               OR parent_intake.legal_hold
               OR parent_intake.protected_until IS NULL
               OR parent_intake.protected_until > CURRENT_TIMESTAMP
               OR EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.id = ANY(
                        COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM buckets AS b
                    WHERE b.id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          b.legal_hold
                          OR b.protected_until IS NULL
                          OR b.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM application_profiles AS ap
                    WHERE (
                        ap.intake_id = ANY(
                            COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                        )
                        OR ap.primary_bucket_id = ANY(
                            COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                        )
                    )
                      AND (
                          ap.legal_hold
                          OR ap.protected_until IS NULL
                          OR ap.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
               ) THEN
                RAISE EXCEPTION 'Intake history is protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in retained_intake_history_tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_retained_intake_history_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_intake_history_delete()
            """
        )

    # These rows are durable case evidence nested below another retained
    # history row.  Their foreign keys do not expose profile_id directly, so
    # resolve the authoritative ancestor before applying the child's own
    # two-year clock and the complete linked retention graph. The access-grant
    # parent rows and their counters are retained separately as Bucket history,
    # while mutable file-scope association rows, team membership, draft queues,
    # and live session rows remain intentionally excluded. Revocation and scope
    # changes must stay possible without deleting the durable audit parents.
    retained_nested_history_tables = (
        "application_requirement_evidence_files",
        "application_requirement_evidence_decisions",
        "application_program_requirement_overrides",
        "application_offer_delivery_items",
        "application_term_sheet_deliveries",
        "financial_worksheet_edits",
        "lender_submissions",
        "lender_submission_events",
        "application_room_deliveries",
        "arf_referrals",
        "profitability_assessments",
        "capital_readiness_reviews",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_nested_history_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            profile_uuid uuid;
            bucket_uuid uuid;
            file_uuid uuid;
            history_created_at timestamptz;
            history_anchor timestamptz;
            child_deadline timestamptz;
        BEGIN
            history_created_at := COALESCE(
                NULLIF(to_jsonb(NEW) ->> 'created_at', '')::timestamptz,
                NULLIF(to_jsonb(NEW) ->> 'occurred_at', '')::timestamptz
            );
            history_anchor := GREATEST(
                history_created_at,
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'updated_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'linked_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'verified_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'removed_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'restored_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'responded_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'expires_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'sent_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'revoked_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'queued_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'sending_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'delivered_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'viewed_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'downloaded_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(NEW) ->> 'consented_at', '')::timestamptz,
                    history_created_at
                )
            );
            IF history_anchor IS NULL THEN
                RETURN NEW;
            END IF;
            child_deadline := history_anchor + INTERVAL '2 years';

            CASE TG_TABLE_NAME
                WHEN 'application_requirement_evidence_files' THEN
                    file_uuid := NULLIF(to_jsonb(NEW) ->> 'file_id', '')::uuid;
                    SELECT state.profile_id INTO profile_uuid
                    FROM application_requirement_states AS state
                    WHERE state.id = NULLIF(
                        to_jsonb(NEW) ->> 'requirement_state_id',
                        ''
                    )::uuid;
                WHEN 'application_requirement_evidence_decisions' THEN
                    SELECT state.profile_id, evidence.file_id
                    INTO profile_uuid, file_uuid
                    FROM application_requirement_evidence_files AS evidence
                    JOIN application_requirement_states AS state
                      ON state.id = evidence.requirement_state_id
                    WHERE evidence.id = NULLIF(
                        to_jsonb(NEW) ->> 'requirement_evidence_id',
                        ''
                    )::uuid;
                WHEN 'application_program_requirement_overrides' THEN
                    SELECT selection.profile_id INTO profile_uuid
                    FROM application_program_selections AS selection
                    WHERE selection.id = NULLIF(
                        to_jsonb(NEW) ->> 'selection_id',
                        ''
                    )::uuid;
                WHEN 'application_offer_delivery_items' THEN
                    SELECT delivery.profile_id INTO profile_uuid
                    FROM application_offer_deliveries AS delivery
                    WHERE delivery.id = NULLIF(
                        to_jsonb(NEW) ->> 'delivery_id',
                        ''
                    )::uuid;
                WHEN 'application_term_sheet_deliveries' THEN
                    SELECT term.profile_id INTO profile_uuid
                    FROM application_term_sheets AS term
                    WHERE term.id = NULLIF(
                        to_jsonb(NEW) ->> 'term_sheet_id',
                        ''
                    )::uuid;
                WHEN 'financial_worksheet_edits' THEN
                    SELECT worksheet.profile_id INTO profile_uuid
                    FROM financial_worksheets AS worksheet
                    WHERE worksheet.id = NULLIF(
                        to_jsonb(NEW) ->> 'worksheet_id',
                        ''
                    )::uuid;
                WHEN 'lender_submissions' THEN
                    SELECT batch.profile_id INTO profile_uuid
                    FROM submission_batches AS batch
                    WHERE batch.id = NULLIF(
                        to_jsonb(NEW) ->> 'batch_id',
                        ''
                    )::uuid;
                WHEN 'lender_submission_events' THEN
                    SELECT batch.profile_id INTO profile_uuid
                    FROM lender_submissions AS submission
                    JOIN submission_batches AS batch
                      ON batch.id = submission.batch_id
                    WHERE submission.id = NULLIF(
                        to_jsonb(NEW) ->> 'submission_id',
                        ''
                    )::uuid;
                WHEN 'application_room_deliveries' THEN
                    profile_uuid := NULLIF(
                        to_jsonb(NEW) ->> 'profile_id',
                        ''
                    )::uuid;
                    bucket_uuid := NULLIF(
                        to_jsonb(NEW) ->> 'bucket_id',
                        ''
                    )::uuid;
                WHEN 'arf_referrals' THEN
                    profile_uuid := NULLIF(
                        to_jsonb(NEW) ->> 'profile_id',
                        ''
                    )::uuid;
                WHEN 'profitability_assessments' THEN
                    SELECT snapshot.profile_id INTO profile_uuid
                    FROM application_capital_readiness_snapshots AS snapshot
                    WHERE snapshot.id = NULLIF(
                        to_jsonb(NEW) ->> 'snapshot_id',
                        ''
                    )::uuid;
                WHEN 'capital_readiness_reviews' THEN
                    SELECT snapshot.profile_id INTO profile_uuid
                    FROM application_capital_readiness_snapshots AS snapshot
                    WHERE snapshot.id = NULLIF(
                        to_jsonb(NEW) ->> 'snapshot_id',
                        ''
                    )::uuid;
            END CASE;

            IF profile_uuid IS NOT NULL THEN
                UPDATE application_profiles AS ap
                SET protected_until = child_deadline
                WHERE ap.id = profile_uuid
                  AND ap.protected_until < child_deadline;
            END IF;
            IF bucket_uuid IS NOT NULL THEN
                UPDATE buckets AS b
                SET protected_until = child_deadline
                WHERE b.id = bucket_uuid
                  AND b.protected_until < child_deadline;
            END IF;
            IF file_uuid IS NOT NULL THEN
                UPDATE bucket_files AS bf
                SET protected_until = child_deadline
                WHERE bf.id = file_uuid
                  AND bf.protected_until < child_deadline;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in retained_nested_history_tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_nested_history_retention_propagate
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_propagate_nested_history_retention()
            """
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_nested_history_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            profile_uuid uuid;
            bucket_seed uuid;
            history_created_at timestamptz;
            history_anchor timestamptz;
            parent_profile application_profiles%ROWTYPE;
            connected_bucket_ids uuid[];
            connected_intake_ids uuid[];
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of nested case history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            history_created_at := COALESCE(
                NULLIF(to_jsonb(OLD) ->> 'created_at', '')::timestamptz,
                NULLIF(to_jsonb(OLD) ->> 'occurred_at', '')::timestamptz
            );
            history_anchor := GREATEST(
                history_created_at,
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'updated_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'linked_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'verified_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'removed_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'restored_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'responded_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'expires_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'sent_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'revoked_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'queued_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'sending_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'delivered_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'viewed_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'downloaded_at', '')::timestamptz,
                    history_created_at
                ),
                COALESCE(
                    NULLIF(to_jsonb(OLD) ->> 'consented_at', '')::timestamptz,
                    history_created_at
                )
            );
            IF history_created_at IS NULL
               OR history_anchor IS NULL
               OR history_anchor + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'nested case history is inside its minimum retention period'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            CASE TG_TABLE_NAME
                WHEN 'application_requirement_evidence_files' THEN
                    SELECT state.profile_id, bf.bucket_id
                    INTO profile_uuid, bucket_seed
                    FROM application_requirement_states AS state
                    LEFT JOIN bucket_files AS bf
                      ON bf.id = NULLIF(to_jsonb(OLD) ->> 'file_id', '')::uuid
                    WHERE state.id = NULLIF(
                        to_jsonb(OLD) ->> 'requirement_state_id',
                        ''
                    )::uuid;
                WHEN 'application_requirement_evidence_decisions' THEN
                    SELECT state.profile_id, bf.bucket_id
                    INTO profile_uuid, bucket_seed
                    FROM application_requirement_evidence_files AS evidence
                    JOIN application_requirement_states AS state
                      ON state.id = evidence.requirement_state_id
                    LEFT JOIN bucket_files AS bf ON bf.id = evidence.file_id
                    WHERE evidence.id = NULLIF(
                        to_jsonb(OLD) ->> 'requirement_evidence_id',
                        ''
                    )::uuid;
                WHEN 'application_program_requirement_overrides' THEN
                    SELECT selection.profile_id INTO profile_uuid
                    FROM application_program_selections AS selection
                    WHERE selection.id = NULLIF(
                        to_jsonb(OLD) ->> 'selection_id',
                        ''
                    )::uuid;
                WHEN 'application_offer_delivery_items' THEN
                    SELECT delivery.profile_id INTO profile_uuid
                    FROM application_offer_deliveries AS delivery
                    WHERE delivery.id = NULLIF(
                        to_jsonb(OLD) ->> 'delivery_id',
                        ''
                    )::uuid;
                WHEN 'application_term_sheet_deliveries' THEN
                    SELECT term.profile_id INTO profile_uuid
                    FROM application_term_sheets AS term
                    WHERE term.id = NULLIF(
                        to_jsonb(OLD) ->> 'term_sheet_id',
                        ''
                    )::uuid;
                WHEN 'financial_worksheet_edits' THEN
                    SELECT worksheet.profile_id INTO profile_uuid
                    FROM financial_worksheets AS worksheet
                    WHERE worksheet.id = NULLIF(
                        to_jsonb(OLD) ->> 'worksheet_id',
                        ''
                    )::uuid;
                WHEN 'lender_submissions' THEN
                    SELECT batch.profile_id INTO profile_uuid
                    FROM submission_batches AS batch
                    WHERE batch.id = NULLIF(
                        to_jsonb(OLD) ->> 'batch_id',
                        ''
                    )::uuid;
                WHEN 'lender_submission_events' THEN
                    SELECT batch.profile_id INTO profile_uuid
                    FROM lender_submissions AS submission
                    JOIN submission_batches AS batch
                      ON batch.id = submission.batch_id
                    WHERE submission.id = NULLIF(
                        to_jsonb(OLD) ->> 'submission_id',
                        ''
                    )::uuid;
                WHEN 'application_room_deliveries' THEN
                    profile_uuid := NULLIF(
                        to_jsonb(OLD) ->> 'profile_id',
                        ''
                    )::uuid;
                    bucket_seed := NULLIF(
                        to_jsonb(OLD) ->> 'bucket_id',
                        ''
                    )::uuid;
                WHEN 'arf_referrals' THEN
                    profile_uuid := NULLIF(
                        to_jsonb(OLD) ->> 'profile_id',
                        ''
                    )::uuid;
                WHEN 'profitability_assessments' THEN
                    SELECT snapshot.profile_id INTO profile_uuid
                    FROM application_capital_readiness_snapshots AS snapshot
                    WHERE snapshot.id = NULLIF(
                        to_jsonb(OLD) ->> 'snapshot_id',
                        ''
                    )::uuid;
                WHEN 'capital_readiness_reviews' THEN
                    SELECT snapshot.profile_id INTO profile_uuid
                    FROM application_capital_readiness_snapshots AS snapshot
                    WHERE snapshot.id = NULLIF(
                        to_jsonb(OLD) ->> 'snapshot_id',
                        ''
                    )::uuid;
                ELSE
                    RAISE EXCEPTION 'unsupported nested retained history table %', TG_TABLE_NAME
                        USING ERRCODE = 'integrity_constraint_violation';
            END CASE;

            IF profile_uuid IS NOT NULL THEN
                SELECT ap.* INTO parent_profile
                FROM application_profiles AS ap
                WHERE ap.id = profile_uuid;
            END IF;
            IF (
                TG_TABLE_NAME <> 'application_room_deliveries'
                AND (profile_uuid IS NULL OR parent_profile.id IS NULL)
            ) OR (
                TG_TABLE_NAME = 'application_room_deliveries'
                AND bucket_seed IS NULL
                AND (profile_uuid IS NULL OR parent_profile.id IS NULL)
            ) THEN
                RAISE EXCEPTION 'nested case history has no retained ancestor'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            WITH RECURSIVE retention_edges(
                from_kind, from_id, to_kind, to_id
            ) AS (
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
                SELECT 'bucket'::text, bucket_seed
                WHERE bucket_seed IS NOT NULL
                UNION
                SELECT 'bucket'::text, parent_profile.primary_bucket_id
                WHERE parent_profile.primary_bucket_id IS NOT NULL
                UNION
                SELECT 'ai_intake'::text, parent_profile.intake_id
                WHERE parent_profile.intake_id IS NOT NULL
            ), connected(kind, id) AS (
                SELECT kind, id FROM seeds
                UNION
                SELECT edge.to_kind, edge.to_id
                FROM connected AS current_node
                JOIN retention_edges AS edge
                  ON edge.from_kind = current_node.kind
                 AND edge.from_id = current_node.id
            )
            SELECT ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'bucket'),
                   ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'ai_intake')
            INTO connected_bucket_ids, connected_intake_ids
            FROM connected;

            IF (parent_profile.id IS NOT NULL AND (
                    parent_profile.legal_hold
                    OR parent_profile.protected_until IS NULL
                    OR parent_profile.protected_until > CURRENT_TIMESTAMP
                ))
               OR EXISTS (
                    SELECT 1
                    FROM application_profiles AS ap
                    WHERE (
                        ap.id = profile_uuid
                        OR ap.intake_id = ANY(
                            COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                        )
                        OR ap.primary_bucket_id = ANY(
                            COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                        )
                    )
                      AND (
                          ap.legal_hold
                          OR ap.protected_until IS NULL
                          OR ap.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.id = ANY(
                        COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM buckets AS b
                    WHERE b.id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          b.legal_hold
                          OR b.protected_until IS NULL
                          OR b.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
               ) THEN
                RAISE EXCEPTION 'nested case history is protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in retained_nested_history_tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_retained_nested_history_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_nested_history_delete()
            """
        )

    retained_bucket_history_tables = (
        "bucket_activity_logs",
        "bucket_ai_reviews",
        "bucket_file_analyses",
        "bucket_ai_messages",
        "bucket_notes",
        "bucket_file_annotations",
        "bucket_document_signatures",
        "bucket_requested_documents",
        "bucket_ai_action_items",
        "bucket_ai_chat_actions",
        "bucket_upload_links",
        "bucket_shares",
        "bucket_vendor_access",
        "bucket_public_shares",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_propagate_bucket_history_retention()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            bucket_uuid uuid;
            history_anchor timestamptz;
            child_deadline timestamptz;
        BEGIN
            IF TG_TABLE_NAME = 'bucket_document_signatures' THEN
                SELECT requested.bucket_id INTO bucket_uuid
                FROM bucket_requested_documents AS requested
                WHERE requested.id = NULLIF(
                    to_jsonb(NEW) ->> 'requested_document_id',
                    ''
                )::uuid;
            ELSE
                bucket_uuid := NULLIF(to_jsonb(NEW) ->> 'bucket_id', '')::uuid;
            END IF;
            history_anchor := qc_case_record_retention_anchor(to_jsonb(NEW));
            IF bucket_uuid IS NULL OR history_anchor IS NULL THEN
                RETURN NEW;
            END IF;

            child_deadline := history_anchor + INTERVAL '2 years';
            UPDATE buckets AS bucket
            SET protected_until = child_deadline
            WHERE bucket.id = bucket_uuid
              AND bucket.protected_until < child_deadline;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_retained_bucket_history_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_bucket buckets%ROWTYPE;
            bucket_uuid uuid;
            history_created_at timestamptz;
            connected_bucket_ids uuid[];
            connected_intake_ids uuid[];
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of Bucket history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF TG_TABLE_NAME = 'bucket_document_signatures' THEN
                SELECT rd.bucket_id INTO bucket_uuid
                FROM bucket_requested_documents AS rd
                WHERE rd.id = NULLIF(
                    to_jsonb(OLD) ->> 'requested_document_id',
                    ''
                )::uuid;
            ELSE
                bucket_uuid := NULLIF(to_jsonb(OLD) ->> 'bucket_id', '')::uuid;
            END IF;
            history_created_at := NULLIF(to_jsonb(OLD) ->> 'created_at', '')::timestamptz;
            SELECT b.* INTO parent_bucket
            FROM buckets AS b
            WHERE b.id = bucket_uuid;

            WITH RECURSIVE retention_edges(
                from_kind, from_id, to_kind, to_id
            ) AS (
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
            ), connected(kind, id) AS (
                SELECT 'bucket'::text, bucket_uuid
                WHERE bucket_uuid IS NOT NULL
                UNION
                SELECT edge.to_kind, edge.to_id
                FROM connected AS current_node
                JOIN retention_edges AS edge
                  ON edge.from_kind = current_node.kind
                 AND edge.from_id = current_node.id
            )
            SELECT ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'bucket'),
                   ARRAY_AGG(DISTINCT id) FILTER (WHERE kind = 'ai_intake')
            INTO connected_bucket_ids, connected_intake_ids
            FROM connected;

            IF history_created_at IS NULL
               OR qc_case_record_retention_anchor(to_jsonb(OLD)) IS NULL
               OR qc_case_record_retention_anchor(to_jsonb(OLD))
                    + INTERVAL '2 years' > CURRENT_TIMESTAMP
               OR parent_bucket.id IS NULL
               OR parent_bucket.legal_hold
               OR parent_bucket.protected_until IS NULL
               OR parent_bucket.protected_until > CURRENT_TIMESTAMP
               OR EXISTS (
                    SELECT 1
                    FROM public_underwriting_intakes AS i
                    WHERE i.id = ANY(
                        COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          i.legal_hold
                          OR i.protected_until IS NULL
                          OR i.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM buckets AS b
                    WHERE b.id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          b.legal_hold
                          OR b.protected_until IS NULL
                          OR b.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM bucket_files AS bf
                    WHERE bf.bucket_id = ANY(
                        COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                    )
                      AND (
                          bf.legal_hold
                          OR bf.protected_until IS NULL
                          OR bf.protected_until > CURRENT_TIMESTAMP
                      )
               )
               OR EXISTS (
                    SELECT 1
                    FROM application_profiles AS ap
                    WHERE (
                        ap.intake_id = ANY(
                            COALESCE(connected_intake_ids, ARRAY[]::uuid[])
                        )
                        OR ap.primary_bucket_id = ANY(
                            COALESCE(connected_bucket_ids, ARRAY[]::uuid[])
                        )
                    )
                      AND (
                          ap.legal_hold
                          OR ap.protected_until IS NULL
                          OR ap.protected_until > CURRENT_TIMESTAMP
                      )
               ) THEN
                RAISE EXCEPTION 'Bucket history is protected by retention or legal hold'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table in retained_bucket_history_tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_bucket_history_retention_propagate
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_propagate_bucket_history_retention()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_retained_bucket_history_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_bucket_history_delete()
            """
        )

    # AIAuditEvent is explicitly append-only and some older rows have only
    # JSON linkage. Keep the row itself for at least two years and require the
    # same future governed-purge context even when no parent can be resolved.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_ai_audit_event_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            intake_text text;
            linked_intake public_underwriting_intakes%ROWTYPE;
        BEGIN
            IF COALESCE(current_setting('qc.retention_purge_context', true), '') <> 'governed'
               OR COALESCE(current_setting('qc.retention_purge_id', true), '') = '' THEN
                RAISE EXCEPTION 'physical deletion of AI audit history requires an explicit governed retention purge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.created_at IS NULL
               OR OLD.created_at + INTERVAL '2 years' > CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'AI audit history is protected by retention'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            intake_text := COALESCE(OLD.payload ->> 'intake_id', '');
            IF intake_text ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
                SELECT i.* INTO linked_intake
                FROM public_underwriting_intakes AS i
                WHERE i.id = intake_text::uuid;
                IF linked_intake.id IS NOT NULL
                   AND (
                       linked_intake.legal_hold
                       OR linked_intake.protected_until IS NULL
                       OR linked_intake.protected_until > CURRENT_TIMESTAMP
                   ) THEN
                    RAISE EXCEPTION 'AI audit history is protected by its retained Intake'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_ai_audit_event_retained_delete
        BEFORE DELETE ON ai_audit_events
        FOR EACH ROW
        EXECUTE FUNCTION qc_guard_ai_audit_event_delete()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_ai_audit_event_retained_delete ON ai_audit_events"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_ai_audit_event_delete()")

    retained_nested_history_tables = (
        "application_requirement_evidence_files",
        "application_requirement_evidence_decisions",
        "application_program_requirement_overrides",
        "application_offer_delivery_items",
        "application_term_sheet_deliveries",
        "financial_worksheet_edits",
        "lender_submissions",
        "lender_submission_events",
        "application_room_deliveries",
        "arf_referrals",
        "profitability_assessments",
        "capital_readiness_reviews",
    )
    for table in retained_nested_history_tables:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_retained_nested_history_delete ON {table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_nested_history_retention_propagate ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_retained_nested_history_delete()")
    op.execute("DROP FUNCTION IF EXISTS qc_propagate_nested_history_retention()")

    retained_intake_history_tables = (
        "bucket_intake_links",
        "bucket_intake_link_files",
        "public_underwriting_intake_artifacts",
        "public_underwriting_intake_email_sends",
    )
    for table in retained_intake_history_tables:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_retained_intake_history_delete ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_retained_intake_history_delete()")

    retained_bucket_history_tables = (
        "bucket_activity_logs",
        "bucket_ai_reviews",
        "bucket_file_analyses",
        "bucket_ai_messages",
        "bucket_notes",
        "bucket_file_annotations",
        "bucket_document_signatures",
        "bucket_requested_documents",
        "bucket_ai_action_items",
        "bucket_ai_chat_actions",
        "bucket_upload_links",
        "bucket_shares",
        "bucket_vendor_access",
        "bucket_public_shares",
    )
    for table in retained_bucket_history_tables:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_retained_bucket_history_delete ON {table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_bucket_history_retention_propagate ON {table}"
        )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_retained_bucket_history_delete()")
    op.execute("DROP FUNCTION IF EXISTS qc_propagate_bucket_history_retention()")

    retained_history_tables = (
        "application_extracted_facts",
        "application_financial_periods",
        "application_capital_readiness_snapshots",
        "application_addback_verifications",
        "application_capital_readiness_actions",
        "application_assessment_snapshots",
        "file_events",
        "business_financial_statements",
        "financial_statements",
        "application_offer_deliveries",
        "application_term_sheets",
        "application_requirement_states",
        "application_requirement_responses",
        "application_verification_invitations",
        "application_bank_consents",
        "plaid_asset_reports",
        "application_evidence_policy_selections",
        "application_program_selections",
        "merchant_processing_offers",
        "submission_batches",
        "financial_worksheets",
        "application_owners",
        "dos_debts",
        "application_collection_tasks",
    )
    for table in retained_history_tables:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_retained_history_delete ON {table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_qc_application_history_retention_propagate ON {table}"
        )
    op.execute(
        "DROP FUNCTION IF EXISTS qc_guard_retained_application_history_delete()"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_propagate_application_history_retention()")

    propagation = (
        (
            "bucket_intake_link_files",
            "trg_bucket_intake_link_files_retention_propagate",
        ),
        (
            "bucket_intake_links",
            "trg_bucket_intake_links_retention_propagate",
        ),
        ("application_profiles", "trg_app_profiles_retention_propagate"),
        ("bucket_files", "trg_bucket_files_retention_propagate"),
        ("public_underwriting_intakes", "trg_intakes_retention_propagate"),
        ("buckets", "trg_buckets_retention_propagate"),
        ("application_profiles", "trg_app_profiles_retention_normalize"),
        ("bucket_files", "trg_bucket_files_retention_normalize"),
        ("public_underwriting_intakes", "trg_intakes_retention_normalize"),
        ("buckets", "trg_buckets_retention_normalize"),
    )
    for table, trigger in propagation:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in (
        "qc_propagate_bucket_intake_link_file_retention",
        "qc_propagate_bucket_intake_link_retention",
        "qc_propagate_application_retention",
        "qc_propagate_bucket_file_retention",
        "qc_propagate_intake_retention",
        "qc_propagate_bucket_retention",
        "qc_normalize_application_retention",
        "qc_normalize_bucket_file_retention",
        "qc_normalize_intake_retention",
        "qc_normalize_bucket_retention",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

    for table in (
        "public_underwriting_intakes",
        "bucket_files",
        "buckets",
        "application_profiles",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_retained_delete ON {table}")
    op.execute("DROP FUNCTION IF EXISTS qc_guard_retained_business_record_delete()")
    op.execute("DROP FUNCTION IF EXISTS qc_case_record_retention_anchor(jsonb)")

    op.drop_index(
        "ix_public_underwriting_intakes_retention",
        table_name="public_underwriting_intakes",
    )
    op.drop_index(
        "ix_application_profiles_retention",
        table_name="application_profiles",
    )
    op.drop_index("ix_buckets_retention", table_name="buckets")
    op.drop_constraint(
        "ck_bucket_files_minimum_retention",
        "bucket_files",
        type_="check",
    )
    op.drop_constraint(
        "ck_public_underwriting_intakes_minimum_retention",
        "public_underwriting_intakes",
        type_="check",
    )
    op.drop_constraint(
        "ck_buckets_minimum_retention",
        "buckets",
        type_="check",
    )
    op.drop_constraint(
        "ck_application_profiles_minimum_retention",
        "application_profiles",
        type_="check",
    )
    op.alter_column("bucket_files", "protected_until", nullable=True)
    op.alter_column("bucket_files", "protected_until", server_default=None)
    op.alter_column(
        "bucket_files",
        "retention_class",
        nullable=True,
        server_default=None,
    )

    # Removing these archive columns would either violate the former unique
    # indexes or force retained owner identity data to be rewritten. Refuse an
    # unsafe downgrade once the feature has been used; with no archived rows,
    # the schema can still return cleanly to 0238.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM application_owners
                WHERE archived_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'cannot downgrade owner retention while archived application owners exist';
            END IF;
        END;
        $$
        """
    )
    op.drop_index("uq_application_owners_primary", table_name="application_owners")
    op.drop_index("uq_application_owners_email", table_name="application_owners")
    op.create_index(
        "uq_application_owners_primary",
        "application_owners",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_index(
        "uq_application_owners_email",
        "application_owners",
        ["profile_id", sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_application_owners_archived_token_revoked",
        "application_owners",
        type_="check",
    )
    op.drop_constraint(
        "fk_application_owners_archived_by_user",
        "application_owners",
        type_="foreignkey",
    )
    op.drop_column("application_owners", "archive_reason")
    op.drop_column("application_owners", "archived_by_user_id")
    op.drop_column("application_owners", "archived_at")

    op.drop_column("public_underwriting_intakes", "legal_hold")
    op.drop_column("public_underwriting_intakes", "protected_until")
    op.drop_column("public_underwriting_intakes", "retention_class")
    op.drop_column("application_profiles", "legal_hold")
    op.drop_column("application_profiles", "protected_until")
    op.drop_column("application_profiles", "retention_class")
    op.drop_column("buckets", "legal_hold")
    op.drop_column("buckets", "protected_until")
    op.drop_column("buckets", "retention_class")

