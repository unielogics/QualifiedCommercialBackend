from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.dealer_os.services import buckets_link
from app.models.application_profile import ApplicationOwner, ApplicationProfile
from app.models.bucket import Bucket
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.retention import later_deadline, retention_state, two_year_deadline
from app.routers import application_profiles, buckets, dealer_ai_intake
from app.schemas.application_profile import ApplicationProfileRead
from app.schemas.bucket import BucketFileRead, BucketRead
from app.schemas.retention import LegalHoldChange
from app.services import (
    bucket_archive,
    bucket_storage_integrity,
    intake_retention,
    retention_holds,
)


def _route_contract(router) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }


def _migration_source() -> str:
    return Path("alembic/versions/0239_ai_intake_record_retention.py").read_text(
        encoding="utf-8"
    )


def _migration_function_body(source: str, name: str) -> str:
    function_sql = source.split(
        f"CREATE OR REPLACE FUNCTION {name}()", 1
    )[1]
    return function_sql.split("$$", 2)[1]


def test_two_year_deadline_uses_calendar_years_for_leap_day() -> None:
    assert two_year_deadline(datetime(2024, 2, 29, 12, tzinfo=UTC)) == datetime(
        2026, 2, 28, 12, tzinfo=UTC
    )


def test_later_deadline_never_shortens_a_stronger_hold() -> None:
    current = datetime(2032, 1, 1, tzinfo=UTC)
    result = later_deadline(
        current,
        anchors=(datetime(2026, 1, 1, tzinfo=UTC),),
    )
    assert result == current


@pytest.mark.parametrize("model", [Bucket, PublicUnderwritingIntake, ApplicationProfile])
def test_core_business_record_physical_delete_fails_closed_without_purge_context(
    model,
) -> None:
    expired = datetime(2020, 1, 1, tzinfo=UTC)
    row = model(legal_hold=False, protected_until=expired)

    assert row.is_physical_deletion_prohibited(
        at=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert not row.is_physical_deletion_prohibited(
        at=datetime(2030, 1, 1, tzinfo=UTC),
        governed_purge=True,
    )


def test_retention_state_distinguishes_legal_hold_and_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert retention_state(
        protected_until=now - timedelta(seconds=1), legal_hold=True, at=now
    ) == "legal_hold"
    assert retention_state(
        protected_until=now + timedelta(seconds=1), legal_hold=False, at=now
    ) == "protected"
    assert retention_state(
        protected_until=now - timedelta(seconds=1), legal_hold=False, at=now
    ) == "expired"


def test_public_contracts_expose_retention_state() -> None:
    for schema in (BucketRead, BucketFileRead, ApplicationProfileRead):
        assert "protected_until" in schema.model_fields
        assert "legal_hold" in schema.model_fields
        assert "retention_state" in schema.model_fields


def test_standalone_bucket_archive_discovery_and_restore_are_registered() -> None:
    signature = inspect.signature(buckets.list_buckets)
    assert signature.parameters["archived_filter"].default == "active"
    contract = _route_contract(buckets.router)
    assert any(
        path.endswith("/admin/{bucket_id}/restore") and method == "POST"
        for path, method in contract
    )
    assert "include_archived=True" in inspect.getsource(buckets.get_bucket)
    # Direct admin download remains available for retained files whether the
    # parent workspace is active or archived; the file/bucket identity check
    # is the authorization boundary here.
    admin_url_source = inspect.getsource(buckets.admin_file_url)
    assert "file.bucket_id != bucket_id" in admin_url_source
    assert "_load_bucket_or_404" not in admin_url_source
    assert "include_archived=True" in inspect.getsource(buckets.admin_file_review)


def test_archived_bucket_annotations_are_rejected_until_restore() -> None:
    source = inspect.getsource(buckets.create_admin_file_annotation)

    assert "include_archived=True" in source
    assert "bucket.archived_at is not None" in source
    assert "HTTP_409_CONFLICT" in source
    assert "Restore the Bucket before adding annotations" in source


def test_legal_hold_apis_require_a_reason_and_cover_all_core_records() -> None:
    with pytest.raises(ValidationError):
        LegalHoldChange.model_validate({"reason": ""})
    with pytest.raises(ValidationError):
        LegalHoldChange.model_validate({"reason": "     "})
    assert LegalHoldChange(reason="  Litigation preservation  ").reason == (
        "Litigation preservation"
    )
    assert LegalHoldChange(reason="Litigation preservation").reason

    contracts = (
        _route_contract(buckets.router)
        | _route_contract(dealer_ai_intake.admin_router)
        | _route_contract(application_profiles.router)
    )
    expected_suffixes = {
        "/admin/{bucket_id}/legal-hold",
        "/admin/{bucket_id}/legal-hold/release",
        "/{intake_id}/legal-hold",
        "/{intake_id}/legal-hold/release",
        "/{profile_id}/legal-hold",
        "/{profile_id}/legal-hold/release",
    }
    for suffix in expected_suffixes:
        assert any(path.endswith(suffix) and method == "POST" for path, method in contracts)


def test_python_retention_extension_uses_atomic_monotonic_updates() -> None:
    source = inspect.getsource(intake_retention._atomic_extend_record)
    assert "update(model)" in source
    assert ".returning(" in source
    assert "model.protected_until < candidate" in source
    assert "set_committed_value" in source


def test_migration_enforces_database_level_retention_and_parent_propagation() -> None:
    migration = _migration_source()

    assert "qc_normalize_bucket_file_retention" in migration
    assert "qc_propagate_bucket_file_retention" in migration
    assert "UPDATE application_profiles AS ap" in migration
    assert "UPDATE public_underwriting_intakes AS i" in migration
    assert "UPDATE buckets AS b" in migration
    assert "NEW.protected_until" in migration
    assert migration.count("OLD.protected_until") >= 4
    assert "AND b.protected_until < NEW.protected_until" in migration
    assert "qc.retention_purge_context" in migration
    assert "qc.retention_purge_id" in migration
    assert "physical deletion requires an explicit governed retention purge" in migration
    assert "qc_guard_retained_application_history_delete" in migration
    assert "application_capital_readiness_snapshots" in migration
    assert "file_events" in migration
    assert "qc_guard_retained_bucket_history_delete" in migration
    assert "qc_guard_ai_audit_event_delete" in migration
    assert "application profile has linked records protected" in migration
    assert "SELECT MAX(bf.protected_until)" in migration


def test_application_history_guard_enforces_each_rows_own_two_year_window() -> None:
    migration = _migration_source()
    upgrade = migration.split("def downgrade()", 1)[0]
    guard = _migration_function_body(
        upgrade, "qc_guard_retained_application_history_delete"
    )

    assert "history_created_at := NULLIF(" in guard
    assert "to_jsonb(OLD) ->> 'created_at'" in guard
    assert "history_created_at IS NULL" in guard
    assert "qc_case_record_retention_anchor(to_jsonb(OLD))" in guard
    assert "+ INTERVAL '2 years' > CURRENT_TIMESTAMP" in guard
    assert "profile_uuid IS NULL" in guard
    assert "application history has no retained parent reference" in guard
    assert '"application_program_selections"' in upgrade


def test_late_created_case_records_extend_roots_and_have_direct_delete_guards() -> None:
    migration = _migration_source()
    upgrade, downgrade = migration.split("def downgrade()", 1)
    application_propagation = _migration_function_body(
        upgrade, "qc_propagate_application_history_retention"
    )
    bucket_propagation = _migration_function_body(
        upgrade, "qc_propagate_bucket_history_retention"
    )

    assert "qc_case_record_retention_anchor" in upgrade
    assert "STABLE" in upgrade
    assert "Scheduled future dates" in upgrade
    for table in (
        "application_owners",
        "dos_debts",
        "application_collection_tasks",
    ):
        assert f'"{table}"' in upgrade
        assert f'"{table}"' in downgrade
    for table in (
        "bucket_ai_action_items",
        "bucket_ai_chat_actions",
        "bucket_upload_links",
        "bucket_shares",
        "bucket_vendor_access",
        "bucket_public_shares",
    ):
        assert f'"{table}"' in upgrade
        assert f'"{table}"' in downgrade

    assert "FROM {table} AS record" in upgrade
    assert "UPDATE application_profiles AS profile" in application_propagation
    assert "profile.protected_until < child_deadline" in application_propagation
    assert "UPDATE buckets AS bucket" in bucket_propagation
    assert "bucket.protected_until < child_deadline" in bucket_propagation
    assert "trg_qc_application_history_retention_propagate" in upgrade
    assert "trg_qc_bucket_history_retention_propagate" in upgrade
    assert "trg_qc_retained_history_delete" in upgrade
    assert "trg_qc_retained_bucket_history_delete" in upgrade
    assert "qc_case_record_retention_anchor(jsonb)" in downgrade

    bucket_router_source = inspect.getsource(buckets)
    assert 'item.status = "discarded"' in bucket_router_source
    assert "await db.delete(item)" not in bucket_router_source


def test_application_owner_removal_is_recoverable_case_state() -> None:
    migration = _migration_source()
    columns = ApplicationOwner.__table__.c

    assert "archived_at" in columns
    assert "archived_by_user_id" in columns
    assert "archive_reason" in columns
    assert "is_primary AND archived_at IS NULL" in str(
        next(
            index
            for index in ApplicationOwner.__table__.indexes
            if index.name == "uq_application_owners_primary"
        ).dialect_options["postgresql"]["where"]
    )
    assert "email IS NOT NULL AND archived_at IS NULL" in str(
        next(
            index
            for index in ApplicationOwner.__table__.indexes
            if index.name == "uq_application_owners_email"
        ).dialect_options["postgresql"]["where"]
    )
    assert "fk_application_owners_archived_by_user" in migration
    assert "ck_application_owners_archived_token_revoked" in migration
    assert any(
        constraint.name == "ck_application_owners_archived_token_revoked"
        for constraint in ApplicationOwner.__table__.constraints
    )
    staff_removal = inspect.getsource(application_profiles.delete_application_owner)
    client_removal = inspect.getsource(
        application_profiles.public_application_room_delete_owner
    )
    for removal in (staff_removal, client_removal):
        assert "owner.archived_at = datetime.now(UTC)" in removal
        assert "owner.invite_token_hash = None" in removal
        assert "if not isinstance(owner, ApplicationOwner)" in removal
        assert "owner.invite_sent_at is not None" in removal
    assert "cannot downgrade owner retention while archived application owners exist" in migration


def test_archived_owner_identity_remains_visible_on_retained_statements_only() -> None:
    read_source = inspect.getsource(application_profiles._owner_links)
    write_source = inspect.getsource(application_profiles._apply_owner_links)

    assert "ApplicationOwner.archived_at.is_not(None)" in read_source
    assert "missing_application_owner_ids" in read_source
    assert "profiles.owner_rows" in write_source
    assert "archived_at.is_not" not in write_source


def test_debt_removal_retires_rows_without_erasing_underwriting_history() -> None:
    source = Path("app/dealer_os/router.py").read_text(encoding="utf-8")
    delete_route = source.split("async def delete_debt(", 1)[1].split(
        "# --- MCA-style statement-only readiness", 1
    )[0]
    document_removal = source.split(
        "async def delete_dealer_document(", 1
    )[1].split("async def", 1)[0]

    assert 'row.status = "dismissed"' in delete_route
    assert "await db.delete(row)" not in delete_route
    assert "sa_update(DealerDebt)" in document_removal
    assert '.values(status="dismissed", document_id=None)' in document_removal


def test_intake_artifacts_and_email_sends_have_direct_retention_guards() -> None:
    migration = _migration_source()
    upgrade = migration.split("def downgrade()", 1)[0]
    guard = _migration_function_body(
        upgrade, "qc_guard_retained_intake_history_delete"
    )

    for table in (
        "public_underwriting_intake_artifacts",
        "public_underwriting_intake_email_sends",
    ):
        assert f'"{table}"' in upgrade
        assert migration.count(f'"{table}"') >= 2
    assert "history_created_at IS NULL" in guard
    assert "history_anchor + INTERVAL '2 years' > CURRENT_TIMESTAMP" in guard
    assert "parent_intake.legal_hold" in guard
    assert "parent_intake.protected_until" in guard
    assert "FROM buckets AS b" in guard
    assert "FROM application_profiles AS ap" in guard
    assert "FROM bucket_files AS bf" in guard
    assert "CREATE TRIGGER trg_qc_retained_intake_history_delete" in upgrade


def test_effective_status_traverses_durable_bucket_intake_links_both_ways() -> None:
    source = inspect.getsource(retention_holds.effective_status_read)

    assert "from app.models.operator_file import BucketIntakeLink" in source
    assert "BucketIntakeLink.intake_id.in_(intake_ids)" in source
    assert "BucketIntakeLink.bucket_id.in_(bucket_ids)" in source
    assert "intake_ids.add(intake_id)" in source
    assert "bucket_ids.add(bucket_id)" in source
    assert 'resource_type == "bucket_file"' in source
    # Historical unlinks remain durable retention edges.
    assert "BucketIntakeLink.unlinked_at" not in source

    helper_source = inspect.getsource(
        retention_holds.is_effective_file_soft_deletion_prohibited
    )
    assert 'resource_type="bucket_file"' in helper_source
    assert 'effective["effective_legal_hold"]' in helper_source
    assert "is_effective_file_soft_deletion_prohibited" in inspect.getsource(
        buckets.delete_admin_file
    )
    assert "is_effective_file_soft_deletion_prohibited" in inspect.getsource(
        buckets_link.soft_delete_mirrored_file
    )


def test_migration_retains_and_propagates_bucket_intake_link_graph() -> None:
    migration = _migration_source()
    upgrade, downgrade = migration.split("def downgrade()", 1)
    core_guard = _migration_function_body(
        upgrade, "qc_guard_retained_business_record_delete"
    )
    intake_history_guard = _migration_function_body(
        upgrade, "qc_guard_retained_intake_history_delete"
    )

    assert "DO $$" in upgrade
    assert "FROM bucket_intake_links AS link" in upgrade
    assert "FROM bucket_intake_link_files AS link_file" in upgrade
    assert "link.unlinked_at + INTERVAL '2 years'" in upgrade
    assert "link_file.removed_at" in upgrade
    assert "qc_propagate_bucket_intake_link_retention" in upgrade
    assert "qc_propagate_bucket_intake_link_file_retention" in upgrade
    assert "trg_bucket_intake_links_retention_propagate" in upgrade
    assert "trg_bucket_intake_link_files_retention_propagate" in upgrade

    for table in ("bucket_intake_links", "bucket_intake_link_files"):
        assert f'"{table}"' in upgrade
        assert f'"{table}"' in downgrade
    assert "WITH RECURSIVE retention_edges" in core_guard
    assert "FROM bucket_intake_links AS link" in core_guard
    assert "connected_bucket_ids" in core_guard
    assert "connected_intake_ids" in core_guard
    assert "bucket_intake_link_files AS link_file" in core_guard
    assert "TG_TABLE_NAME = 'bucket_intake_link_files'" in intake_history_guard
    assert "history_anchor + INTERVAL '2 years'" in intake_history_guard
    assert "WITH RECURSIVE retention_edges" in intake_history_guard
    assert "trg_bucket_intake_links_retention_propagate" in downgrade
    assert "trg_bucket_intake_link_files_retention_propagate" in downgrade


def test_migration_protects_nested_durable_case_and_delivery_history() -> None:
    migration = _migration_source()
    upgrade, downgrade = migration.split("def downgrade()", 1)
    delete_guard = _migration_function_body(
        upgrade, "qc_guard_retained_nested_history_delete"
    )
    propagation = _migration_function_body(
        upgrade, "qc_propagate_nested_history_retention"
    )
    durable_tables = (
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

    for table in durable_tables:
        assert f'"{table}"' in upgrade
        assert f'"{table}"' in downgrade
        assert f"WHEN '{table}'" in delete_guard
        assert f"WHEN '{table}'" in propagation
    assert "to_jsonb(OLD) ->> 'occurred_at'" in delete_guard
    assert "history_anchor + INTERVAL '2 years'" in delete_guard
    assert "application_requirement_states AS state" in delete_guard
    assert "application_program_selections AS selection" in delete_guard
    assert "application_offer_deliveries AS delivery" in delete_guard
    assert "application_term_sheets AS term" in delete_guard
    assert "financial_worksheets AS worksheet" in delete_guard
    assert "submission_batches AS batch" in delete_guard
    assert "application_capital_readiness_snapshots AS snapshot" in delete_guard
    assert "FROM profitability_assessments AS assessment" in upgrade
    assert "ON snapshot.id = assessment.snapshot_id" in upgrade
    assert "FROM capital_readiness_reviews AS review" in upgrade
    assert "ON snapshot.id = review.snapshot_id" in upgrade
    assert "WITH RECURSIVE retention_edges" in delete_guard
    assert "UPDATE application_profiles AS ap" in propagation
    assert "UPDATE buckets AS b" in propagation
    assert "UPDATE bucket_files AS bf" in propagation
    assert "trg_qc_nested_history_retention_propagate" in upgrade
    assert "trg_qc_retained_nested_history_delete" in upgrade
    assert "mutable file-scope association rows" in upgrade
    assert "mutable file-scope association rows, team membership" in upgrade


def test_terminal_transitions_extend_retention_without_archiving_evidence() -> None:
    application_source = inspect.getsource(
        application_profiles.apply_underwriting_changes
    )
    assert "extend_application_profile_retention" in application_source
    assert "closed_won" in application_source
    assert "closed_lost" in application_source
    assert "denied" in application_source
    assert "archived_at" not in inspect.getsource(
        intake_retention.extend_application_profile_retention
    )


@pytest.mark.asyncio
async def test_restore_storage_verification_uses_exact_object_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeClient:
        def head_object(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        bucket_storage_integrity,
        "_bounded_s3_client",
        lambda: FakeClient(),
    )
    file_id = uuid4()
    await bucket_storage_integrity.verify_bucket_file_objects(
        [
            SimpleNamespace(
                id=file_id,
                file_name="tax-return.pdf",
                s3_key="buckets/case/tax-return.pdf",
                s3_version_id="version-7",
            )
        ]
    )

    assert calls == [
        {
            "Bucket": bucket_storage_integrity.get_settings().s3_bucket,
            "Key": "buckets/case/tax-return.pdf",
            "VersionId": "version-7",
        }
    ]


@pytest.mark.asyncio
async def test_restore_storage_verification_distinguishes_missing_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bucket_storage_integrity,
        "_bounded_s3_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        bucket_storage_integrity,
        "_head_object",
        lambda *_args: "missing",
    )
    file = SimpleNamespace(
        id=uuid4(),
        file_name="bank-statement.pdf",
        s3_key="buckets/case/bank-statement.pdf",
        s3_version_id=None,
    )

    with pytest.raises(
        bucket_storage_integrity.RetainedObjectMissingError
    ) as exc_info:
        await bucket_storage_integrity.verify_bucket_file_objects([file])
    assert exc_info.value.objects[0].file_id == file.id


def test_restore_checks_storage_before_changing_reversible_state() -> None:
    bucket_restore = inspect.getsource(buckets.restore_bucket)
    file_restore = inspect.getsource(buckets.restore_admin_file)
    assert bucket_restore.index("_verify_files_before_restore") < bucket_restore.index(
        "bucket.archived_at = None"
    )
    assert file_restore.index("_verify_files_before_restore") < file_restore.index(
        "file.deleted_at = None"
    )
    coordinated_restore = inspect.getsource(buckets_link.restore_mirrored_file)
    assert coordinated_restore.index("verify_bucket_file_objects") < (
        coordinated_restore.index("bucket_file.deleted_at = None")
    )
    intake_restore = inspect.getsource(dealer_ai_intake.admin_restore_lead)
    assert intake_restore.index("_verify_files_before_restore") < (
        intake_restore.index("intake.bucket.archived_at = None")
    )


def test_bucket_archive_revokes_old_external_access_and_loaders_fail_closed() -> None:
    archive_source = inspect.getsource(buckets.delete_bucket)
    revoke_source = inspect.getsource(bucket_archive.revoke_external_access)
    assert "revoke_external_access" in archive_source
    assert "revoke_external_access" in inspect.getsource(
        dealer_ai_intake.admin_confirm_lead_deletion
    )
    assert 'model.status == "active"' in revoke_source
    assert '.values(status="revoked")' in revoke_source
    for loader in (
        buckets._load_upload_link_or_404,
        buckets._load_share_or_404,
        buckets._load_public_share_or_404,
    ):
        assert "bucket.archived_at is not None" in inspect.getsource(loader)

