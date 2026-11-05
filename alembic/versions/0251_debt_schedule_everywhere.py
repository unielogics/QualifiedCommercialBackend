"""Every file carries a business debt schedule.

The desk needs a debt schedule on every file, whatever the vertical, and sets
it aside only by declaring the business carries no debt. The business baseline
policy already asked for one; the MCA and real estate baselines did not, so a
file on either never showed the schedule at all. This adds the requirement to
every published evidence policy that lacks it, borrower-visible and waivable,
in the shape the business baseline uses.

Revision ID: 0251_debt_schedule_everywhere
Revises: 0250_bank_account_aliases
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0251_debt_schedule_everywhere"
down_revision = "0250_bank_account_aliases"
branch_labels = None
depends_on = None

KEY = "business_debt_schedule"


def upgrade() -> None:
    connection = op.get_bind()
    targets = (
        connection.execute(
            sa.text(
                """
                SELECT t.id FROM ai_playbook_templates t
                 WHERE t.playbook_type = 'evidence_policy'
                   AND t.status = 'published' AND t.is_active = true
                   AND NOT EXISTS (
                       SELECT 1 FROM ai_collection_requirements r
                        WHERE r.playbook_id = t.id
                          AND r.requirement_key = CAST(:key AS VARCHAR(120))
                   )
                """
            ),
            {"key": KEY},
        )
        .scalars()
        .all()
    )
    for playbook_id in targets:
        playbook_uuid = (
            playbook_id if isinstance(playbook_id, uuid.UUID) else uuid.UUID(str(playbook_id))
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO ai_collection_requirements
                    (id, playbook_id, requirement_key, label, category, required_level,
                     applies_when, blocks_stage, visibility, can_agent_override,
                     can_underwriter_waive, verification_required, expiration_days,
                     ai_request_message_template, display_order, default_owner_type,
                     default_channels, default_cadence_hours, objective_text,
                     completion_criteria, completion_mode, depends_on,
                     inferred_depends_on, deps_confirmed, review_checks,
                     requirement_kind, canonical_key, collection_config,
                     created_at, updated_at)
                SELECT :id, :playbook_id, CAST(:key AS VARCHAR(120)),
                       'Business debt schedule', 'financials', 'required',
                       NULL, 'underwriting', '["borrower","underwriter"]'::jsonb, false,
                       true, true, NULL,
                       'Please provide the business debt schedule: every loan, advance, lease and card the business pays, with the balance and the payment.',
                       (SELECT COALESCE(MAX(display_order), 0) + 1
                          FROM ai_collection_requirements WHERE playbook_id = :playbook_id),
                       'human', '["portal","email"]'::jsonb, 24,
                       'Collect the business debt schedule',
                       'A readable debt_schedule document is linked and verified, or the desk voided the schedule as no debt on the business.',
                       'requires_human_verify', '[]'::jsonb,
                       '[]'::jsonb, true, '[]'::jsonb,
                       'document', NULL, '{}'::jsonb,
                       now(), now()
                """
            ),
            {"id": uuid.uuid5(playbook_uuid, KEY), "playbook_id": playbook_uuid, "key": KEY},
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            DELETE FROM ai_collection_requirements
             WHERE requirement_key = CAST(:key AS VARCHAR(120))
               AND playbook_id IN (
                   SELECT id FROM ai_playbook_templates
                    WHERE playbook_type = 'evidence_policy'
                      AND product_key IN ('mca_baseline', 'real_estate_baseline')
               )
            """
        ),
        {"key": KEY},
    )
