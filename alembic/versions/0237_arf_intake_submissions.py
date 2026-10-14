"""ARF channel, mixed collection tasks, flexible evidence and lender submission batches.

Revision ID: 0237_arf_intake_submissions
Revises: 0236_equipment_financing_sync

DDL is frozen here rather than importing evolving ORM model definitions.
Product snapshots are imported idempotently by the startup catalog initializer.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0237_arf_intake_submissions"
down_revision = "0236_equipment_financing_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_collection_requirements", sa.Column("requirement_kind", sa.String(24), nullable=False, server_default="document"))
    op.add_column("ai_collection_requirements", sa.Column("canonical_key", sa.String(120)))
    op.add_column("ai_collection_requirements", sa.Column("collection_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("bucket_file_analyses", sa.Column("usability_status", sa.String(32), nullable=False, server_default="processing"))
    op.add_column("bucket_file_analyses", sa.Column("detected_content_type", sa.String(160)))
    op.add_column("bucket_file_analyses", sa.Column("document_type_candidates", postgresql.JSONB()))
    op.add_column("bucket_file_analyses", sa.Column("evidence_claims", postgresql.JSONB()))
    op.add_column("application_requirement_evidence_decisions", sa.Column("audit_context", postgresql.JSONB()))
    op.execute(sa.text("CREATE TABLE application_requirement_responses (\n\tid UUID NOT NULL, \n\tprofile_id UUID NOT NULL, \n\trequirement_state_id UUID NOT NULL, \n\tvalue JSONB NOT NULL, \n\tsource VARCHAR(32) NOT NULL, \n\tsource_version VARCHAR(80), \n\treview_state VARCHAR(24) DEFAULT 'submitted' NOT NULL, \n\tprovenance JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tsubmitted_by_user_id UUID, \n\treviewed_by_user_id UUID, \n\treviewed_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(profile_id) REFERENCES application_profiles (id) ON DELETE CASCADE, \n\tFOREIGN KEY(requirement_state_id) REFERENCES application_requirement_states (id) ON DELETE CASCADE, \n\tFOREIGN KEY(submitted_by_user_id) REFERENCES users (id) ON DELETE SET NULL, \n\tFOREIGN KEY(reviewed_by_user_id) REFERENCES users (id) ON DELETE SET NULL\n)"))
    op.execute(sa.text("CREATE INDEX ix_application_requirement_responses_profile ON application_requirement_responses (profile_id)"))
    op.execute(sa.text("CREATE INDEX ix_application_requirement_responses_state ON application_requirement_responses (requirement_state_id, created_at)"))
    op.execute(sa.text("CREATE TABLE application_collection_tasks (\n\tid UUID NOT NULL, \n\tprofile_id UUID NOT NULL, \n\tcanonical_identity VARCHAR(200) NOT NULL, \n\trequirement_state_ids JSONB DEFAULT '[]'::jsonb NOT NULL, \n\trequirement_keys JSONB DEFAULT '[]'::jsonb NOT NULL, \n\taction_type VARCHAR(24) NOT NULL, \n\tlabel VARCHAR(200) NOT NULL, \n\tdescription TEXT, \n\tassignee VARCHAR(24) DEFAULT 'borrower' NOT NULL, \n\tstatus VARCHAR(24) DEFAULT 'open' NOT NULL, \n\tchannel VARCHAR(16) DEFAULT 'portal' NOT NULL, \n\tconfig JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tautomation_approved BOOLEAN DEFAULT 'false' NOT NULL, \n\tcadence_hours INTEGER DEFAULT '24' NOT NULL, \n\tmax_attempts INTEGER DEFAULT '3' NOT NULL, \n\tattempts INTEGER DEFAULT '0' NOT NULL, \n\tnext_attempt_at TIMESTAMP WITH TIME ZONE, \n\tlast_requested_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tassigned_user_id UUID, \n\tstate_reason TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_application_collection_task_identity UNIQUE (profile_id, canonical_identity), \n\tFOREIGN KEY(profile_id) REFERENCES application_profiles (id) ON DELETE CASCADE, \n\tFOREIGN KEY(assigned_user_id) REFERENCES users (id) ON DELETE SET NULL\n)"))
    op.execute(sa.text("CREATE INDEX ix_application_collection_tasks_profile_status ON application_collection_tasks (profile_id, status)"))
    op.execute(sa.text("CREATE TABLE arf_channel_configurations (\n\tchannel_key VARCHAR(32) NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tintake_mode VARCHAR(40) DEFAULT 'referral_only' NOT NULL, \n\tpublication_state VARCHAR(32) DEFAULT 'draft_pending_approval' NOT NULL, \n\tapprovals JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tpermitted_fields JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tpermitted_documents JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tupdated_by_user_id UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (channel_key), \n\tCONSTRAINT ck_arf_intake_mode CHECK (intake_mode IN ('referral_only','partner_hosted_authorized')), \n\tCONSTRAINT ck_arf_publication_state CHECK (publication_state IN ('draft_pending_approval','published','paused')), \n\tFOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE SET NULL\n)"))
    op.execute(sa.text("CREATE TABLE application_assessment_snapshots (\n\tid UUID NOT NULL, \n\tprofile_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\talgorithm_version VARCHAR(48) NOT NULL, \n\tsource_fingerprint VARCHAR(64) NOT NULL, \n\tassessed_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdocument_usability JSONB NOT NULL, \n\tfile_completeness JSONB NOT NULL, \n\tproducts JSONB NOT NULL, \n\tlender_fit JSONB NOT NULL, \n\tfacts JSONB NOT NULL, \n\trisk_flags JSONB NOT NULL, \n\tstrengths JSONB NOT NULL, \n\texceptions JSONB NOT NULL, \n\texplanation TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_application_assessment_profile_version UNIQUE (profile_id, version), \n\tFOREIGN KEY(profile_id) REFERENCES application_profiles (id) ON DELETE CASCADE\n)"))
    op.execute(sa.text("CREATE INDEX ix_application_assessment_profile_version ON application_assessment_snapshots (profile_id, version)"))
    op.execute(sa.text("CREATE TABLE arf_referrals (\n\tid UUID NOT NULL, \n\tidempotency_key VARCHAR(64) NOT NULL, \n\tprofile_id UUID NOT NULL, \n\tfull_name VARCHAR(160) NOT NULL, \n\temail VARCHAR(320) NOT NULL, \n\tphone VARCHAR(32), \n\tbusiness_name VARCHAR(180), \n\tprogram_key VARCHAR(64), \n\tindustry_key VARCHAR(32), \n\tattribution JSONB NOT NULL, \n\tconsent_version VARCHAR(48) NOT NULL, \n\tconsented_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (idempotency_key), \n\tFOREIGN KEY(profile_id) REFERENCES application_profiles (id) ON DELETE CASCADE\n)"))
    op.execute(sa.text("CREATE TABLE lender_offerings (\n\tid UUID NOT NULL, \n\tlender_id UUID NOT NULL, \n\tprogram_key VARCHAR(64) DEFAULT '*' NOT NULL, \n\tversion INTEGER NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'active' NOT NULL, \n\tchecklist_confirmed BOOLEAN DEFAULT 'false' NOT NULL, \n\tsource_reference TEXT, \n\trequirements JSONB DEFAULT '[]' NOT NULL, \n\tfit_rules JSONB DEFAULT '{}' NOT NULL, \n\tofficial_form_file_ids JSONB DEFAULT '[]' NOT NULL, \n\tofficial_forms_required BOOLEAN DEFAULT 'false' NOT NULL, \n\tcreated_by_user_id UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_lender_offering_version UNIQUE (lender_id, program_key, version), \n\tFOREIGN KEY(lender_id) REFERENCES lenders (id) ON DELETE CASCADE, \n\tFOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL\n)"))
    op.execute(sa.text("CREATE INDEX ix_lender_offerings_lender_id ON lender_offerings (lender_id)"))
    op.execute(sa.text("CREATE UNIQUE INDEX uq_lender_offering_active ON lender_offerings (lender_id, program_key) WHERE status = 'active'"))
    op.execute(sa.text("CREATE TABLE submission_batches (\n\tid UUID NOT NULL, \n\tprofile_id UUID NOT NULL, \n\tidempotency_key VARCHAR(160) NOT NULL, \n\trequest_fingerprint VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(32) DEFAULT 'prepared' NOT NULL, \n\tcreated_by_user_id UUID, \n\tapproved_by_user_id UUID, \n\tapproved_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_submission_batch_request UNIQUE (profile_id, idempotency_key), \n\tFOREIGN KEY(profile_id) REFERENCES application_profiles (id) ON DELETE CASCADE, \n\tFOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL, \n\tFOREIGN KEY(approved_by_user_id) REFERENCES users (id) ON DELETE SET NULL\n)"))
    op.execute(sa.text("CREATE INDEX ix_submission_batches_profile_id ON submission_batches (profile_id)"))
    op.execute(sa.text("CREATE TABLE lender_submissions (\n\tid UUID NOT NULL, \n\tbatch_id UUID NOT NULL, \n\tlender_id UUID NOT NULL, \n\toffering_id UUID, \n\trecipient_email VARCHAR(320), \n\tlender_name VARCHAR(160) NOT NULL, \n\tstatus VARCHAR(32) DEFAULT 'prepared' NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tsource_fingerprint VARCHAR(64) NOT NULL, \n\tfit_status VARCHAR(40) NOT NULL, \n\twarning_acknowledged BOOLEAN DEFAULT 'false' NOT NULL, \n\tblockers JSONB DEFAULT '[]' NOT NULL, \n\trequirements_snapshot JSONB DEFAULT '[]' NOT NULL, \n\tmanifest JSONB NOT NULL, \n\ttoken_hash VARCHAR(64) NOT NULL, \n\tencrypted_token TEXT NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\trevoked_at TIMESTAMP WITH TIME ZONE, \n\tqueued_at TIMESTAMP WITH TIME ZONE, \n\tsending_at TIMESTAMP WITH TIME ZONE, \n\tsent_at TIMESTAMP WITH TIME ZONE, \n\tdelivered_at TIMESTAMP WITH TIME ZONE, \n\tviewed_at TIMESTAMP WITH TIME ZONE, \n\tdownloaded_at TIMESTAMP WITH TIME ZONE, \n\tattempt_count INTEGER DEFAULT '0' NOT NULL, \n\tprovider_message_id VARCHAR(320), \n\tlast_error TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_lender_submission_recipient UNIQUE (batch_id, lender_id), \n\tFOREIGN KEY(batch_id) REFERENCES submission_batches (id) ON DELETE CASCADE, \n\tFOREIGN KEY(lender_id) REFERENCES lenders (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(offering_id) REFERENCES lender_offerings (id) ON DELETE RESTRICT, \n\tUNIQUE (token_hash)\n)"))
    op.execute(sa.text("CREATE INDEX ix_lender_submissions_batch_id ON lender_submissions (batch_id)"))
    op.execute(sa.text("CREATE INDEX ix_lender_submissions_lender_id ON lender_submissions (lender_id)"))
    op.execute(sa.text("CREATE INDEX ix_lender_submissions_provider_message_id ON lender_submissions (provider_message_id)"))
    op.execute(sa.text("CREATE INDEX ix_lender_submissions_queue ON lender_submissions (status, queued_at)"))
    op.execute(sa.text("CREATE TABLE lender_submission_events (\n\tid UUID NOT NULL, \n\tsubmission_id UUID NOT NULL, \n\tevent VARCHAR(48) NOT NULL, \n\tactor_user_id UUID, \n\tdetail JSONB NOT NULL, \n\toccurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(submission_id) REFERENCES lender_submissions (id) ON DELETE CASCADE, \n\tFOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE SET NULL\n)"))
    op.execute(sa.text("CREATE INDEX ix_lender_submission_events_submission_id ON lender_submission_events (submission_id)"))


def downgrade() -> None:
    op.drop_table("lender_submission_events")
    op.drop_table("lender_submissions")
    op.drop_table("submission_batches")
    op.drop_table("lender_offerings")
    op.drop_table("arf_referrals")
    op.drop_table("application_assessment_snapshots")
    op.drop_table("arf_channel_configurations")
    op.drop_table("application_collection_tasks")
    op.drop_table("application_requirement_responses")
    op.drop_column("application_requirement_evidence_decisions", "audit_context")
    op.drop_column("bucket_file_analyses", "evidence_claims")
    op.drop_column("bucket_file_analyses", "document_type_candidates")
    op.drop_column("bucket_file_analyses", "detected_content_type")
    op.drop_column("bucket_file_analyses", "usability_status")
    op.drop_column("ai_collection_requirements", "collection_config")
    op.drop_column("ai_collection_requirements", "canonical_key")
    op.drop_column("ai_collection_requirements", "requirement_kind")

