"""Allow HTML content, explicit audience selections and scheduled dispatch."""
import sqlalchemy as sa

from alembic import op

revision = "0287_campaign_composer"
down_revision = "0286_email_marketing"
branch_labels = None
depends_on = None


def repair_missing_subscriptions(execute):
    # Only the original address paired with the actual ongoing-updates choice
    # qualifies. Application contact consent and the short funding series
    # predate this program. Neither can establish a subscription.
    execute(sa.text("""
        CREATE TEMP TABLE qc0287_missing_grants AS
        WITH file_grants AS (
            SELECT i.id AS intake_id, i.client_id, i.created_at, i.preferred_language AS locale,
                   i.intake_state -> 'legal_acceptance' AS acceptance,
                   CASE WHEN i.intake_state ->> 'source' = 'dealer_ai_intake' AND
                                 (i.intake_state -> 'equipment_financing_details' IS NOT NULL OR
                                  replace(replace(lower(trim(i.intake_state ->> 'funding_intent')), '-', '_'), ' ', '_')
                                    IN ('equipment','equipment_finance','equipment_financing','machinery','vehicle_financing'))
                        THEN 'equipment_financing_intake' ELSE i.intake_state ->> 'source' END AS form,
                   lower(trim(i.email)) AS current_email
              FROM public_underwriting_intakes i
             WHERE i.source_kind = 'public_form' AND i.archived_at IS NULL
               AND i.intake_state ->> 'source' <> 'capital_readiness_diagnostic'
               AND (i.intake_state ->> 'source' <> 'foreclosure_rescue' OR i.referral_source = 'owner_direct')
            UNION ALL
            SELECT i.id, i.client_id, i.created_at, i.preferred_language,
                   i.intake_state -> 'capital_readiness_claim', 'capital_readiness_diagnostic', lower(trim(i.email))
              FROM public_underwriting_intakes i JOIN clients c ON c.id = i.client_id
             WHERE i.source_kind = 'client_capital_readiness' AND i.archived_at IS NULL
               AND replace(i.intake_state -> 'capital_readiness_claim' ->> 'user_id', '-', '') =
                   replace(CAST(c.user_id AS TEXT), '-', '')
        ), originals AS (
            SELECT lower(trim(acceptance -> 'application_email_consent' ->> 'email')) AS email,
                   locale, intake_id, client_id, created_at,
                   CASE form WHEN 'dealer_ai_intake' THEN 'dealer_intake'
                             WHEN 'equipment_financing_intake' THEN 'equipment_intake'
                             WHEN 'mca_refinance' THEN 'mca_refinance_intake' ELSE form END AS source,
                   CASE form WHEN 'business_term_intake' THEN 'term_loan'
                             WHEN 'mca_funding_intake' THEN 'merchant_cash_advance'
                             WHEN 'funding_review' THEN 'real_estate'
                             WHEN 'capital_readiness_diagnostic' THEN 'main_street'
                             WHEN 'equipment_financing_intake' THEN 'equipment_financing'
                             WHEN 'dealer_ai_intake' THEN 'dealer'
                             ELSE form END AS program,
                   CASE WHEN form = 'foreclosure_rescue' THEN 'new_rescue' ELSE 'application_started' END AS outcome
              FROM file_grants
             WHERE form IN ('business_term_intake','mca_funding_intake','funding_review',
                            'equipment_financing_intake','dealer_ai_intake','mca_refinance','foreclosure_rescue',
                            'capital_readiness_diagnostic')
               AND CAST(acceptance -> 'email_marketing_consent' AS TEXT) = 'true'
               AND CAST(acceptance -> 'application_email_consent' -> 'granted' AS TEXT) = 'true'
               AND acceptance -> 'application_email_consent' ->> 'version' = 'application-email-v1'
               AND lower(trim(acceptance -> 'application_email_consent' ->> 'email')) = current_email
            UNION ALL
            SELECT lower(trim(r.contact ->> 'email')), r.locale, NULL, r.client_id, r.created_at,
                   'business_funding_check', r.goal, 'resource_requested'
              FROM resource_assessments r JOIN activities a
                ON a.client_id = r.client_id AND a.kind = 'resource.lead_received'
               AND a.actor_id IS NULL AND a.actor_label = 'public_resource'
               AND replace(a.payload ->> 'assessment_id', '-', '') = replace(CAST(r.id AS TEXT), '-', '')
               AND a.payload ->> 'resource_id' = r.resource_id
               AND a.payload ->> 'consent_version' = r.consent_version
               AND a.payload ->> 'goal' = r.goal
             WHERE r.resource_id = 'business_funding_check' AND r.consent_version = 'business-funding-resource-v1'
               AND CAST(a.payload -> 'lending_updates_consent' AS TEXT) = 'true'
        ), identities AS MATERIALIZED (
            SELECT email, gen_random_uuid() AS subscriber_id FROM originals GROUP BY email
        ), ranked AS (
            SELECT originals.*,
                   row_number() OVER (PARTITION BY email ORDER BY created_at, source, program, intake_id) AS subscriber_rank,
                   row_number() OVER (PARTITION BY email, source, program, client_id, intake_id ORDER BY created_at) AS grant_rank
              FROM originals
        )
        SELECT identities.subscriber_id, r.*
          FROM ranked r JOIN identities ON identities.email = r.email
         WHERE r.grant_rank = 1 AND r.email LIKE '%@%.%' AND length(r.email) <= 320
           AND NOT EXISTS (SELECT 1 FROM email_marketing_subscribers s WHERE s.email = r.email)
           AND NOT EXISTS (SELECT 1 FROM email_suppressions s WHERE s.email_normalized = r.email AND s.active)
           AND NOT EXISTS (SELECT 1 FROM dealer_prospects p WHERE p.email_normalized = r.email AND p.do_not_contact)
           AND NOT EXISTS (SELECT 1 FROM clients c WHERE lower(trim(c.email)) = r.email AND c.contact_permission = 'save_lead_only')
           AND NOT EXISTS (
               SELECT 1 FROM public_underwriting_intakes h LEFT JOIN clients c ON c.id = h.client_id
               WHERE h.client_contact_suppressed AND
                  (lower(trim(h.email)) = r.email OR lower(trim(c.email)) = r.email OR
                   lower(trim(h.intake_state -> 'foreclosure_rescue' ->> 'client_email')) = r.email)
           )
    """))
    execute(sa.text("""
        INSERT INTO email_marketing_subscribers (id, email, locale, created_at)
        SELECT subscriber_id, email, CASE WHEN locale = 'es' THEN 'es' ELSE 'en' END, created_at
          FROM qc0287_missing_grants WHERE subscriber_rank = 1
        ON CONFLICT (email) DO NOTHING
    """))
    # A concurrent real enrollment wins. Its own provenance and delivery queue
    # remain intact. Restoring missing history never queues a welcome message.
    execute(sa.text("""
        INSERT INTO email_marketing_enrollments
            (id, subscriber_id, source, program, outcome, consent_version, client_id, intake_id, created_at)
        SELECT gen_random_uuid(), g.subscriber_id, g.source, g.program, g.outcome, 'lending_updates_v1',
               g.client_id, g.intake_id, g.created_at
          FROM qc0287_missing_grants g JOIN email_marketing_subscribers s ON s.id = g.subscriber_id
         WHERE s.unsubscribed_at IS NULL
    """))
    execute(sa.text("DROP TABLE qc0287_missing_grants"))


def upgrade():
    op.add_column("email_marketing_campaigns", sa.Column("body_html", sa.Text(), nullable=True))
    op.add_column("email_marketing_campaigns", sa.Column("selection", sa.JSON(), nullable=True))
    op.add_column("email_marketing_campaigns", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    repair_missing_subscriptions(op.execute)


def downgrade():
    op.drop_column("email_marketing_campaigns", "scheduled_at")
    op.drop_column("email_marketing_campaigns", "selection")
    op.drop_column("email_marketing_campaigns", "body_html")
