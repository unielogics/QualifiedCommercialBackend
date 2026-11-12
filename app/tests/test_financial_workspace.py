"""The publication boundary: drafts, immutable PDFs, and source-scoped imports."""

from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormImport,
    FinancialFormVersion,
)
from app.services import dealer_forms_pdf
from app.services import financial_workspace as ws


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


class Memory:
    def __init__(self):
        self.info = {}
        self.added = []
        self.versions = []
        self.objects = {}
        self.current_rows = []

    def add(self, row):
        self.added.append(row)
        if isinstance(row, FinancialFormVersion):
            self.versions.append(row)

    async def flush(self):
        pass

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def execute(self, statement):
        from app.models.file_event import FileEvent

        if statement.column_descriptions[0].get("entity") is FileEvent:
            return Result(row for row in self.added if isinstance(row, FileEvent))
        if str(statement).startswith("SELECT bucket_files.id"):
            # These publication fixtures contain independent baseline sources;
            # material replacement SQL is exercised by its own selector tests.
            return Result(key for model, key in self.objects if model is BucketFile)
        if "financial_form_families.current_version_id" in str(statement) and "JOIN" in str(statement):
            return Result(self.current_rows)
        if "idempotency_key =" in str(statement):
            values = statement.compile().params
            key = next((value for name, value in values.items() if "idempotency" in name), None)
            return Result(scalar=next((version for version in self.versions if version.idempotency_key == key), None))
        return Result(self.versions)


def context(kind="pfs"):
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind=kind, identity_key="test", identity={}, label=ws.LABELS[kind], created_at=ws.now())
    draft = FinancialFormDraft(family_id=family.id, body=ws.empty_body(kind), revision=4, base_version_id=None, updated_at=ws.now())
    return profile, family, draft


def test_all_four_empty_forms_preserve_unknown_headline_figures():
    for kind, key in (("pfs", "net_worth"), ("p_and_l", "net_income"), ("balance_sheet", "total_assets")):
        assert ws.facts_for_body(kind, ws.empty_body(kind))[key] is None
    body = {"debts": [{"id": str(uuid4()), "lender": "Oakwood", "balance": "80000", "monthly_payment": None}]}
    facts = ws.facts_for_body("debt_schedule", body)
    assert facts["total_outstanding_balance"] == 80000
    assert facts["total_monthly_debt_service"] is None


def test_explicit_zero_is_distinct_from_unknown():
    body = ws.empty_body("pfs")
    body["assets"] = dict.fromkeys(body["assets"], "0")
    body["liabilities"] = dict.fromkeys(body["liabilities"], "0")
    facts = ws.facts_for_body("pfs", body)
    assert facts["total_assets"] == facts["total_liabilities"] == facts["net_worth"] == 0


def test_only_active_canonical_debts_contribute_to_generated_facts():
    canonical, duplicate, removed = [str(uuid4()) for _ in range(3)]
    body = {"debts": [
        {"id": canonical, "lender": "Name LLC", "balance": "100", "monthly_payment": "10"},
        {"id": duplicate, "lender": "Name", "balance": "100", "monthly_payment": "10", "status": "duplicate", "duplicate_of_id": canonical},
        {"id": removed, "lender": "Other", "balance": "80", "monthly_payment": "8", "status": "dismissed"},
    ]}
    facts = ws.facts_for_body("debt_schedule", body)
    assert len(facts["debts"]) == 1
    assert facts["total_outstanding_balance"] == 100
    assert facts["total_monthly_debt_service"] == 10


@pytest.mark.parametrize("value", ["NaN", "Infinity", "oops", {"bad": 1}])
def test_invalid_import_numbers_cannot_become_zero(value):
    body = ws.empty_body("pfs")
    body["assets"]["cash_on_hand"] = value
    with pytest.raises(HTTPException) as error:
        ws.validate_body("pfs", body)
    assert error.value.status_code == 422


def test_summary_pfs_totals_are_never_invented_as_detailed_assets():
    body = ws._body_from_facts("pfs", {"total_assets": 100000, "total_liabilities": 20000, "net_worth": 80000, "liquid_assets": 5000, "statement_date": "2026-11-05"})
    assert all(value is None for value in body["assets"].values())
    assert all(value is None for value in body["liabilities"].values())
    assert body["as_of"] == "2026-11-05"


def test_owner_and_reporting_periods_cannot_silently_merge():
    family = SimpleNamespace(identity={"subject": "Alice", "as_of": "2026-10-31"})
    assert not ws._compatible_identity(family, {"subject": "Bob", "as_of": "2026-10-31"})
    assert not ws._compatible_identity(family, {"subject": "Alice", "as_of": "2026-11-30"})
    assert ws._compatible_identity(family, {"subject": "Alice", "as_of": "2026-10-31"})


@pytest.mark.asyncio
async def test_staging_a_draft_never_projects_figures_or_writes_a_pdf():
    db = Memory()
    profile, family, draft = context()
    original = copy.deepcopy(draft.body)
    body = copy.deepcopy(original)
    body["assets"]["cash_on_hand"] = "1200"
    with patch.object(ws, "_project", AsyncMock()) as project, patch.object(ws, "_write_pdf") as storage:
        await ws.stage_draft(db, profile, family, draft, body)
    assert draft.revision == 5
    assert draft.body["assets"]["cash_on_hand"] == "1200"
    assert original["assets"]["cash_on_hand"] is None
    project.assert_not_called()
    storage.assert_not_called()
    assert family.current_version_id is None


@pytest.mark.asyncio
async def test_workspace_writers_lock_bucket_before_profile_like_readiness():
    from app.models.application_profile import ApplicationProfile
    from app.models.bucket import Bucket

    profile, _, _ = context("debt_schedule")
    db = SimpleNamespace(execute=AsyncMock())
    await ws.lock_financial_profile(db, profile)
    assert [call.args[0].column_descriptions[0]["entity"] for call in db.execute.await_args_list] == [Bucket, ApplicationProfile]
    assert all("FOR UPDATE" in str(call.args[0]) for call in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_pdf_storage_failure_leaves_pointer_projections_and_version_rows_untouched():
    db = Memory()
    profile, family, draft = context()
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_render", return_value=b"%PDF saved"), patch.object(ws, "_write_pdf", side_effect=RuntimeError("offline")), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch.object(ws, "_project", AsyncMock()) as project:
        with pytest.raises(HTTPException) as error:
            await ws.publish_version(db, profile, kind="pfs", expected_revision=4, idempotency_key="request-1")
    assert error.value.status_code == 502
    assert family.current_version_id is None
    assert draft.base_version_id is None
    assert db.added == []
    project.assert_not_called()


@pytest.mark.asyncio
async def test_successful_publication_freezes_pdf_snapshot_and_analysis_together():
    db = Memory()
    profile, family, draft = context()
    actor = SimpleNamespace(id=uuid4(), name="Desk", email="desk@example.com")
    slot = SimpleNamespace(id=uuid4(), status="requested")
    pdf = b"%PDF immutable"
    async def project(*args, **kwargs):
        assert db.info["financial_workspace_publishing"] is True
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), patch.object(ws, "_render", return_value=pdf), patch.object(ws, "_write_pdf") as storage, patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), patch("app.services.financial_publication.after_publication", AsyncMock()) as side_effects, patch.object(ws, "_project", project):
        result = await ws.publish_version(db, profile, kind="pfs", expected_revision=4, idempotency_key="request-1", actor=actor)
        first_id = family.current_version_id
        repeated = await ws.publish_version(db, profile, kind="pfs", expected_revision=4, idempotency_key="request-1", actor=actor)
    assert result["saved_version"]["id"] == repeated["saved_version"]["id"]
    assert family.current_version_id == first_id == draft.base_version_id
    assert slot.status == "requested"  # Save version is separate from checklist filing.
    saved_file = next(row for row in db.added if isinstance(row, BucketFile))
    assert saved_file.requested_document_id is None
    assert db.versions[0].status == "published"
    assert storage.call_count == 1
    side_effects.assert_awaited_once()
    version = db.versions[0]
    assert version.pdf_sha256 == hashlib.sha256(pdf).hexdigest()
    assert version.body_sha256 == ws.body_hash(draft.body)
    analysis = next(row for row in db.added if isinstance(row, BucketFileAnalysis))
    assert analysis.analysis["financial_version"]["version_id"] == str(first_id)
    assert analysis.analysis["key_facts"]["net_worth"] is None
    assert "financial_workspace_publishing" not in db.info


@pytest.mark.asyncio
async def test_stale_revision_fails_before_rendering():
    profile, family, draft = context()
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_render") as render:
        with pytest.raises(HTTPException) as error:
            await ws.publish_version(Memory(), profile, kind="pfs", expected_revision=3, idempotency_key="request-2")
    assert error.value.status_code == 409
    render.assert_not_called()


@pytest.mark.asyncio
async def test_latest_pointer_filters_history_but_keeps_other_owners_and_periods():
    db = Memory()
    source, old_pdf, current_pdf, other_owner, unrelated = [uuid4() for _ in range(5)]
    current_id = uuid4()
    db.current_rows = [
        (SimpleNamespace(id=uuid4(), source_file_id=source, pdf_file_id=source), current_id),
        (SimpleNamespace(id=uuid4(), source_file_id=None, pdf_file_id=old_pdf), current_id),
        (SimpleNamespace(id=current_id, source_file_id=None, pdf_file_id=current_pdf), current_id),
        (SimpleNamespace(id=current_id, source_file_id=other_owner, pdf_file_id=other_owner), current_id),
    ]
    assert await ws.current_financial_file_ids(db, [source, old_pdf, current_pdf, other_owner, unrelated]) == {current_pdf, other_owner, unrelated}


@pytest.mark.asyncio
async def test_cross_profile_row_and_file_links_are_rejected():
    db = Memory()
    profile, _family, _draft = context("debt_schedule")
    row_id, file_id = uuid4(), uuid4()
    db.versions = [SimpleNamespace(id=row_id, profile_id=uuid4())]
    with pytest.raises(HTTPException) as error:
        await ws.validate_debt_scope(db, profile, {"debts": [{"id": str(row_id), "lender": "Other file"}]})
    assert error.value.status_code == 404
    db.versions = []
    with pytest.raises(HTTPException) as error:
        await ws.validate_debt_scope(db, profile, {"debts": [{"id": str(row_id), "lender": "Same file", "evidence_links": [{"file_id": str(file_id), "role": "payoff"}]}]})
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_target_must_be_active_in_the_same_snapshot():
    db = Memory()
    profile, _family, _draft = context("debt_schedule")
    row = {"id": str(uuid4()), "lender": "Name", "status": "duplicate", "duplicate_of_id": str(uuid4())}
    with pytest.raises(HTTPException) as error:
        await ws.validate_debt_scope(db, profile, {"debts": [row]})
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_restore_only_stages_the_historical_body():
    db = Memory()
    profile, family, draft = context()
    historical = ws.empty_body("pfs")
    historical["assets"]["cash_on_hand"] = "100"
    version = SimpleNamespace(body=historical)
    current_pointer = uuid4()
    family.current_version_id = current_pointer
    with patch.object(ws, "version_for_profile", AsyncMock(return_value=(family, version))), patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), patch.object(ws, "_write_pdf") as storage:
        await ws.restore_version(db, profile, uuid4(), expected_revision=4)
    assert draft.body["assets"]["cash_on_hand"] == "100"
    assert draft.revision == 5
    assert family.current_version_id == current_pointer
    storage.assert_not_called()


def test_unknown_values_stay_blank_in_versioned_pdf_html():
    body = ws.empty_body("pfs")
    rendered = dealer_forms_pdf.build_pfs_413_html(body=body, statement_date="not stated", financial_totals=ws.facts_for_body("pfs", body))
    assert "$0.00" not in rendered
    assert "&mdash;" in rendered


def test_pdf_mapping_requires_a_real_exact_page_quote_for_high_confidence():
    file = SimpleNamespace(id=uuid4())
    body = ws.empty_body("pfs")
    body["assets"]["cash_on_hand"] = "100"
    records = ws._mapping_records("pfs", body, file, None, "a" * 64, {"cash_on_hand": {"page": 1, "quote": "Cash $100"}}, {1: "Cash $100"})
    cash = next(record for record in records if record["key"] == "cash_on_hand")
    assert cash["confidence"] == "high"
    assert cash["source"]["content_hash"] == "a" * 64


def test_a_quote_containing_a_different_value_is_not_verified():
    file = SimpleNamespace(id=uuid4())
    body = ws.empty_body("pfs")
    body["assets"]["cash_on_hand"] = "100"
    records = ws._mapping_records("pfs", body, file, None, "a" * 64, {"cash_on_hand": {"page": 1, "quote": "Cash $200"}}, {1: "Cash $200"})
    assert records[0]["confidence"] == "needs_review"


@pytest.mark.asyncio
async def test_workspace_grid_layout_carries_the_frontend_cell_edit_contract():
    db = Memory()
    profile, family, draft = context()
    profile.primary_bucket_id = None
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_families", AsyncMock(return_value=[family])):
        result = await ws.load_workspace(db, profile, kind="pfs")
    layout = result["worksheet"]["layout"]
    assert layout["kind"] == "pfs"
    assert layout["title"] and layout["schema_version"] and layout["layout_version"]
    assert layout["name_prefix"] == "pfs"
    assert layout["freeze"] == [0, 1]
    cash = next(cell for row in layout["rows"] for cell in row["cells"] if cell.get("key") == "cash_on_hand")
    assert cash["path"] == ["assets", "cash_on_hand"]


@pytest.mark.asyncio
async def test_import_preserves_original_v1_and_only_proposes_editable_fields():
    db = Memory()
    profile, family, draft = context()
    file_id = uuid4()
    raw = b"%PDF original bytes"
    content_hash = hashlib.sha256(raw).hexdigest()
    source = SimpleNamespace(id=file_id, bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded", content_type="application/pdf", file_name="Client.pdf", content_hash=content_hash, uploaded_by_name="Client", created_at=ws.now())
    analysis = SimpleNamespace(id=uuid4(), content_hash=content_hash, classification="personal_financial_statement", analysis={"key_facts": {"statement_date": "2026-11-05"}})
    db.objects[(BucketFile, file_id)] = source
    proposed = ws.empty_body("pfs")
    proposed["assets"]["cash_on_hand"] = "400"
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(ws, "_extract_full_form", AsyncMock(return_value=(proposed, {}, [], {}))), patch.object(ws, "_write_pdf") as storage:
        result = await ws.propose_import(db, profile, kind="pfs", file_id=file_id, family_id=family.id, analysis=analysis)
    original = db.versions[0]
    assert original.version_number == 1 and original.source_file_id == source.id
    assert original.pdf_sha256 == content_hash and original.body == {}
    assert family.current_version_id is None
    assert result["proposed_body"]["assets"]["cash_on_hand"] == "400"
    assert draft.body["assets"]["cash_on_hand"] is None
    storage.assert_not_called()


@pytest.mark.asyncio
async def test_applying_selected_mappings_changes_only_draft_and_keeps_source_provenance():
    db = Memory()
    profile, family, draft = context()
    file_id, import_id = uuid4(), uuid4()
    imported = FinancialFormImport(id=import_id, family_id=family.id, source_file_id=file_id, content_hash="a" * 64, base_revision=4, status="proposed", proposed_body={}, mappings=[{"key": "cash_on_hand", "value": "400", "source": {"file_id": str(file_id), "content_hash": "a" * 64}}, {"key": "savings_accounts", "value": "600", "source": {"file_id": str(file_id)}}], warnings=[])
    db.objects.update({(FinancialFormImport, import_id): imported, (FinancialFormFamily, family.id): family, (BucketFile, file_id): SimpleNamespace(id=file_id, bucket_id=profile.primary_bucket_id, deleted_at=None, content_hash="a" * 64)})
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})):
        await ws.apply_import(db, profile, import_id, expected_revision=4, selected_keys=["cash_on_hand"])
    assert draft.revision == 5
    assert draft.body["assets"]["cash_on_hand"] == "400"
    assert draft.body["assets"]["savings_accounts"] is None
    assert draft.body["field_sources"]["cash_on_hand"]["file_id"] == str(file_id)
    assert imported.status == "applied"
    assert family.current_version_id is None


@pytest.mark.asyncio
async def test_import_changed_after_mapping_does_not_apply():
    db = Memory()
    profile, family, draft = context()
    file_id, import_id = uuid4(), uuid4()
    imported = SimpleNamespace(id=import_id, family_id=family.id, source_file_id=file_id, content_hash="a" * 64, base_revision=4, status="proposed", proposed_body={})
    db.objects.update({(FinancialFormImport, import_id): imported, (FinancialFormFamily, family.id): family, (BucketFile, file_id): SimpleNamespace(bucket_id=profile.primary_bucket_id, deleted_at=None, content_hash="b" * 64)})
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))):
        with pytest.raises(HTTPException) as error:
            await ws.apply_import(db, profile, import_id, expected_revision=4)
    assert error.value.status_code == 409
    assert draft.revision == 4 and imported.status == "proposed"


@pytest.mark.asyncio
async def test_new_original_is_excluded_until_mapping_is_accepted_and_saved():
    db = Memory()
    original_id = uuid4()
    original = SimpleNamespace(id=uuid4(), source_file_id=original_id, pdf_file_id=original_id)
    db.current_rows = [(original, None)]
    assert await ws.current_financial_file_ids(db, {original_id}) == set()


@pytest.mark.asyncio
async def test_stale_or_applied_mapping_can_be_remapped_from_cache_without_another_model_call():
    db = Memory()
    profile, family, draft = context()
    file_id = uuid4()
    source = SimpleNamespace(id=file_id, bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded", content_type="application/pdf", file_name="Client.pdf", content_hash="a" * 64)
    db.objects[(BucketFile, file_id)] = source
    analysis = SimpleNamespace(content_hash="a" * 64, classification="personal_financial_statement", analysis={"key_facts": {}})
    cached = SimpleNamespace(id=uuid4(), family_id=family.id, source_file_id=file_id, content_hash="a" * 64, status="applied", base_revision=1, proposed_body=ws.empty_body("pfs"), mappings=[], warnings=[])
    db.execute = AsyncMock(return_value=Result(scalar=cached))
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_extract_full_form", AsyncMock()) as model, patch.object(ws, "_read_file") as storage, patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value={file_id})):
        result = await ws.propose_import(db, profile, kind="pfs", file_id=file_id, family_id=family.id, analysis=analysis)
    assert result["id"] != str(cached.id)
    assert result["status"] == "proposed" and result["base_revision"] == draft.revision
    model.assert_not_called()
    storage.assert_not_called()


@pytest.mark.asyncio
async def test_file_on_checklist_ensures_a_missing_business_form_slot():
    db = Memory()
    profile, family, draft = context("p_and_l")
    slot = SimpleNamespace(id=uuid4(), status="requested")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), patch.object(ws, "_render", return_value=b"%PDF"), patch.object(ws, "_write_pdf"), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=None)), patch.object(ws.business_statements, "ensure_slot", AsyncMock(return_value=slot)) as ensure, patch("app.services.financial_publication.after_publication", AsyncMock()), patch.object(ws, "_project", AsyncMock()):
        await ws.publish_version(db, profile, kind="p_and_l", expected_revision=4, idempotency_key="file-missing-slot", submit=True)
    ensure.assert_awaited_once()
    assert slot.status == "uploaded"
    assert db.versions[-1].status == "filed"
    file = next(row for row in db.added if isinstance(row, BucketFile))
    assert file.requested_document_id == slot.id


def test_pfs_pdf_keeps_schedule_values_stored_under_stable_schema_keys():
    body = ws.empty_body("pfs")
    body["schedules"]["real_estate"] = [{"id": str(uuid4()), "property_address": "123 Main Street", "present_market_value": "450000"}]
    rendered = dealer_forms_pdf.build_pfs_413_html(body=body, statement_date="not stated", financial_totals=ws.facts_for_body("pfs", body))
    assert "123 Main Street" in rendered and "450000" in rendered


def test_pfs_version_pdf_accepts_formatted_currency_and_negative_parentheses():
    body = ws.empty_body("pfs")
    body["assets"]["cash_on_hand"] = "$1,250.50"
    body["assets"]["savings_accounts"] = "(500)"
    rendered = dealer_forms_pdf.build_pfs_413_html(body=body, statement_date="not stated", financial_totals=ws.facts_for_body("pfs", body))
    assert "$1,250.50" in rendered and "$-500.00" in rendered


@pytest.mark.asyncio
async def test_saving_a_previously_filed_family_keeps_the_filed_latch():
    db = Memory()
    profile, family, draft = context()
    old = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=1, schema_version="sba413.v1", body={}, key_facts={}, body_sha256=ws.body_hash({}), source="form", status="filed", idempotency_key="prior-file", created_at=ws.now())
    db.versions.append(old)
    family.current_version_id = draft.base_version_id = old.id
    slot = SimpleNamespace(id=uuid4(), status="uploaded")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), patch.object(ws, "_render", return_value=b"%PDF"), patch.object(ws, "_write_pdf"), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), patch("app.services.financial_publication.after_publication", AsyncMock()), patch.object(ws, "_project", AsyncMock()) as project:
        await ws.publish_version(db, profile, kind="pfs", expected_revision=4, base_version_id=old.id, idempotency_key="later-save", submit=False)
    assert db.versions[-1].status == "filed"
    assert project.await_args.kwargs["submit"] is True
    assert next(row for row in db.added if isinstance(row, BucketFile)).requested_document_id == slot.id


@pytest.mark.asyncio
@pytest.mark.parametrize("no_debt", [False, True])
async def test_complete_debt_save_automatically_files_immutable_pdf_and_ensures_slot(no_debt):
    db = Memory()
    profile, family, draft = context("debt_schedule")
    draft.body = {"debts": [], "no_debt": True} if no_debt else {"debts": [{
        "id": str(uuid4()), "lender": "Kapfi", "balance": "0", "monthly_payment": "0",
    }]}
    slot = SimpleNamespace(id=uuid4(), status="requested")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), patch.object(ws, "validate_debt_scope", AsyncMock()), patch.object(ws, "_render", return_value=b"%PDF saved debt"), patch.object(ws, "_write_pdf"), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch("app.routers.application_profiles._requested_slot", AsyncMock(side_effect=[None, slot])), patch("app.routers.application_profiles._ensure_legacy_form_slot", AsyncMock()) as ensure, patch("app.services.financial_publication.after_publication", AsyncMock()) as effects, patch.object(ws, "_project", AsyncMock()) as project:
        result = await ws.publish_version(db, profile, kind="debt_schedule", expected_revision=4, idempotency_key="complete-debt-save", submit=False)
    ensure.assert_awaited_once()
    assert db.versions[-1].status == "filed" and slot.status == "uploaded"
    assert db.versions[-1].key_facts["debt_schedule_complete"]
    assert db.versions[-1].pdf_sha256 == hashlib.sha256(b"%PDF saved debt").hexdigest()
    assert next(row for row in db.added if isinstance(row, BucketFile)).requested_document_id == slot.id
    assert project.await_args.kwargs["submit"] and effects.await_args.kwargs["submit"]
    assert result["saved_completion"]["complete"]


@pytest.mark.asyncio
async def test_incomplete_first_debt_save_retains_unknowns_and_does_not_file():
    db = Memory()
    profile, family, draft = context("debt_schedule")
    draft.body = {"debts": [{"id": str(uuid4()), "lender": "Kapfi", "balance": "40000", "monthly_payment": None}]}
    slot = SimpleNamespace(id=uuid4(), status="requested")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), patch.object(ws, "validate_debt_scope", AsyncMock()), patch.object(ws, "_render", return_value=b"%PDF partial debt"), patch.object(ws, "_write_pdf"), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), patch("app.services.financial_publication.after_publication", AsyncMock()), patch.object(ws, "_project", AsyncMock()):
        await ws.publish_version(db, profile, kind="debt_schedule", expected_revision=4, idempotency_key="partial-debt-save")
    assert db.versions[-1].status == "published" and slot.status == "requested"
    assert not db.versions[-1].key_facts["debt_schedule_complete"]
    assert db.versions[-1].key_facts["total_monthly_debt_service"] is None
    assert next(row for row in db.added if isinstance(row, BucketFile)).requested_document_id is None


@pytest.mark.asyncio
async def test_partial_debt_edit_cannot_replace_a_complete_accepted_version():
    db = Memory()
    profile, family, draft = context("debt_schedule")
    old_body = {"debts": [{"id": str(uuid4()), "lender": "Kapfi", "balance": "40000", "monthly_payment": "1000"}]}
    old = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=1, schema_version="qc_ds.v2", body=old_body, key_facts={}, body_sha256=ws.body_hash(old_body), source="form", status="published", created_at=ws.now())
    db.versions.append(old)
    family.current_version_id = draft.base_version_id = old.id
    draft.body = copy.deepcopy(old_body)
    draft.body["debts"][0]["monthly_payment"] = None
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "validate_debt_scope", AsyncMock()), patch.object(ws, "_render") as render, patch.object(ws, "_project", AsyncMock()) as project:
        with pytest.raises(HTTPException) as error:
            await ws.publish_version(db, profile, kind="debt_schedule", expected_revision=4, base_version_id=old.id, idempotency_key="incomplete-replacement")
    assert error.value.status_code == 422 and error.value.detail["code"] == "debt_schedule_incomplete"
    assert error.value.detail["missing_fields"] == [f"{old_body['debts'][0]['id']}.monthly_payment"]
    assert family.current_version_id == old.id and draft.body["debts"][0]["monthly_payment"] is None
    assert db.versions == [old]
    render.assert_not_called()
    project.assert_not_called()


@pytest.mark.asyncio
async def test_older_idempotent_save_receipt_keeps_its_exact_completion_after_later_version():
    db = Memory()
    profile, family, draft = context("debt_schedule")
    partial_body = {"debts": [{"id": str(uuid4()), "lender": "Kapfi", "balance": "40000", "monthly_payment": None}]}
    complete_body = copy.deepcopy(partial_body)
    complete_body["debts"][0]["monthly_payment"] = "1000"
    prior = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=1, schema_version="qc_ds.v2", body=partial_body, key_facts={}, body_sha256=ws.body_hash(partial_body), source="form", status="published", idempotency_key="older-partial-save", created_at=ws.now())
    latest = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=2, schema_version="qc_ds.v2", body=complete_body, key_facts={}, body_sha256=ws.body_hash(complete_body), source="form", status="filed", idempotency_key="later-complete-save", created_at=ws.now())
    db.versions.extend([prior, latest])
    family.current_version_id = draft.base_version_id = latest.id
    draft.body = complete_body
    accepted = ws.debt_schedule_completion(complete_body)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={"accepted_completion": accepted})), patch.object(ws, "_render") as render:
        result = await ws.publish_version(db, profile, kind="debt_schedule", expected_revision=0, idempotency_key=prior.idempotency_key)
    assert result["saved_version"]["id"] == str(prior.id)
    assert not result["saved_completion"]["complete"] and result["accepted_completion"]["complete"]
    assert result["saved_completion"]["missing_fields"] == [f"{partial_body['debts'][0]['id']}.monthly_payment"]
    assert prior.body["debts"][0]["monthly_payment"] is None and family.current_version_id == latest.id
    render.assert_not_called()


@pytest.mark.asyncio
async def test_incomplete_new_family_cannot_dismiss_another_accepted_profile_schedule():
    db = Memory()
    profile, family, draft = context("debt_schedule")
    old_body = {"debts": [{"id": str(uuid4()), "lender": "Kapfi", "balance": "40000", "monthly_payment": "1000"}]}
    other_family_id = uuid4()
    accepted = FinancialFormVersion(id=uuid4(), family_id=other_family_id, version_number=2, schema_version="qc_ds.v2", body=old_body, key_facts={}, body_sha256=ws.body_hash(old_body), source="form", status="filed", created_at=ws.now())
    draft.body = {"debts": [{"id": str(uuid4()), "lender": "New reporting period", "balance": "30000", "monthly_payment": None}]}
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "validate_debt_scope", AsyncMock()), patch.object(ws, "accepted_debt_schedule_snapshot", AsyncMock(return_value=accepted)) as lookup, patch.object(ws, "_render") as render, patch.object(ws, "_write_pdf") as storage, patch.object(ws, "_project", AsyncMock()) as project:
        with pytest.raises(HTTPException) as error:
            await ws.publish_version(db, profile, kind="debt_schedule", expected_revision=4, idempotency_key="incomplete-new-period")
    assert error.value.status_code == 422 and error.value.detail["code"] == "debt_schedule_incomplete"
    lookup.assert_awaited_once_with(db, profile.id)
    assert family.current_version_id is None and draft.base_version_id is None
    assert draft.body["debts"][0]["monthly_payment"] is None and accepted.body == old_body
    assert db.added == []
    render.assert_not_called()
    storage.assert_not_called()
    project.assert_not_called()
