"""An uploaded financial form is received evidence before its facts are accepted."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.models.application_profile import (
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.funding_program import ApplicationRequirementEvidenceDecision
from app.services import application_programs as programs
from app.services import collection_tasks
from app.services.bucket_evidence import analysis_classifications, analysis_usability
from app.services.collection_tasks import sync_collection_tasks
from app.services.financial_version_selection import (
    PendingFinancialAnalysis,
    current_financial_file_clause,
    financial_coverage_file_clause,
)
from app.services.missing_item_automation import _client_submission_needed


class Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def scalar_one_or_none(self):
        return self.values[0] if len(self.values) == 1 else None


def _selector_sql(clause):
    return str(select(BucketFile.id).where(clause(BucketFile.id)).compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True},
    ))


def test_coverage_receives_latest_original_once_while_numbers_use_accepted_pdf():
    # Execute the actual generated selector SQL against minimal ephemeral
    # tables, so aliases/correlation and historical count rules are tested.
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE bucket_files (id TEXT PRIMARY KEY);
            CREATE TABLE financial_form_families (id TEXT PRIMARY KEY, current_version_id TEXT);
            CREATE TABLE financial_form_versions (
                id TEXT PRIMARY KEY, family_id TEXT, version_number INTEGER,
                source_file_id TEXT, pdf_file_id TEXT, status TEXT
            );
        """)
        family, original, accepted, update, latest = [uuid4().hex for _ in range(5)]
        version1, version2, version3, version4 = [uuid4().hex for _ in range(4)]
        db.executemany("INSERT INTO bucket_files VALUES (?)", [(key,) for key in (original, accepted, update, latest)])
        db.execute("INSERT INTO financial_form_families VALUES (?,?)", (family, version2))
        db.executemany("INSERT INTO financial_form_versions VALUES (?,?,?,?,?,?)", [
            (version1, family, 1, original, original, "original"),
            (version2, family, 2, None, accepted, "filed"),
            (version3, family, 3, update, update, "original"),
        ])
        # The not-yet-created v4 PDF is unrelated until it enters version history.
        db.execute("DELETE FROM bucket_files WHERE id = ?", (latest,))
        assert db.execute(_selector_sql(financial_coverage_file_clause)).fetchall() == [(update,)]
        assert db.execute(_selector_sql(current_financial_file_clause)).fetchall() == [(accepted,)]
        db.execute("INSERT INTO bucket_files VALUES (?)", (latest,))
        db.execute("INSERT INTO financial_form_versions VALUES (?,?,?,?,?,?)", (version4, family, 4, None, latest, "filed"))
        db.execute("UPDATE financial_form_families SET current_version_id=?", (version4,))
        assert db.execute(_selector_sql(financial_coverage_file_clause)).fetchall() == [(latest,)]
        assert db.execute(_selector_sql(current_financial_file_clause)).fetchall() == [(latest,)]


def test_fresh_original_is_received_but_never_numeric_evidence():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE bucket_files (id TEXT PRIMARY KEY);
            CREATE TABLE financial_form_families (id TEXT PRIMARY KEY, current_version_id TEXT);
            CREATE TABLE financial_form_versions (
                id TEXT PRIMARY KEY, family_id TEXT, version_number INTEGER,
                source_file_id TEXT, pdf_file_id TEXT, status TEXT
            );
        """)
        family, source, version = [uuid4().hex for _ in range(3)]
        db.execute("INSERT INTO bucket_files VALUES (?)", (source,))
        db.execute("INSERT INTO financial_form_families VALUES (?,NULL)", (family,))
        db.execute("INSERT INTO financial_form_versions VALUES (?,?,?,?,?,?)", (version, family, 1, source, source, "original"))
        assert db.execute(_selector_sql(financial_coverage_file_clause)).fetchall() == [(source,)]
        assert db.execute(_selector_sql(current_financial_file_clause)).fetchall() == []


def test_save_is_numeric_only_until_file_and_later_saves_keep_coverage():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE bucket_files (id TEXT PRIMARY KEY);
            CREATE TABLE financial_form_families (id TEXT PRIMARY KEY, current_version_id TEXT);
            CREATE TABLE financial_form_versions (
                id TEXT PRIMARY KEY, family_id TEXT, version_number INTEGER,
                source_file_id TEXT, pdf_file_id TEXT, status TEXT
            );
        """)
        family, source, saved, filed, later = [uuid4().hex for _ in range(5)]
        v1, v2, v3, v4 = [uuid4().hex for _ in range(4)]
        db.executemany("INSERT INTO bucket_files VALUES (?)", [(key,) for key in (source, saved)])
        db.execute("INSERT INTO financial_form_families VALUES (?,?)", (family, v2))
        db.executemany("INSERT INTO financial_form_versions VALUES (?,?,?,?,?,?)", [
            (v1, family, 1, source, source, "original"),
            (v2, family, 2, None, saved, "published"),
        ])
        assert db.execute(_selector_sql(financial_coverage_file_clause)).fetchall() == [(source,)]
        assert db.execute(_selector_sql(current_financial_file_clause)).fetchall() == [(saved,)]
        db.execute("INSERT INTO bucket_files VALUES (?)", (filed,))
        db.execute("INSERT INTO financial_form_versions VALUES (?,?,?,?,?,?)", (v3, family, 3, None, filed, "filed"))
        db.execute("UPDATE financial_form_families SET current_version_id=?", (v3,))
        assert db.execute(_selector_sql(financial_coverage_file_clause)).fetchall() == [(filed,)]
        db.execute("INSERT INTO bucket_files VALUES (?)", (later,))
        db.execute("INSERT INTO financial_form_versions VALUES (?,?,?,?,?,?)", (v4, family, 4, None, later, "filed"))
        db.execute("UPDATE financial_form_families SET current_version_id=?", (v4,))
        assert db.execute(_selector_sql(financial_coverage_file_clause)).fetchall() == [(later,)]


def source_analysis(file_id):
    return SimpleNamespace(
        id=uuid4(), bucket_file_id=file_id, status="completed", content_hash="a" * 64,
        analysis_version=4, classification="debt_schedule", confidence="high",
        analyzed_at=datetime.now(UTC),
        analysis={"key_facts": {"total_outstanding_balance": 900000, "total_monthly_debt_service": 50000}},
        summary="$900,000 owed", document_type_candidates=[], evidence_claims=[{"value": 900000}],
    )


@pytest.mark.asyncio
async def test_collection_inventory_redacts_pending_facts_without_mutating_source_analysis():
    file_id = uuid4()
    file = SimpleNamespace(id=file_id, content_hash="a" * 64, source_kind=None, source_detail=None)
    original = source_analysis(file_id)
    db = SimpleNamespace(info={}, execute=AsyncMock(side_effect=[Rows([file]), Rows([original]), Rows([file_id])]))
    with patch.object(programs.profiles, "evidence_state", AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)]))), patch("app.services.financial_version_selection.financial_coverage_file_ids", AsyncMock(return_value={file_id})):
        files, analyses, classes = await programs._evidence_inventory(db, SimpleNamespace(), include_pending_financial=True)
    pending = analyses[file_id]
    assert files == [file] and classes == set()
    assert pending.status == "pending" and pending.analysis["key_facts"] == {}
    assert pending.evidence_claims == [] and analysis_usability(pending) == "processing"
    assert analysis_classifications(pending) == set()
    assert original.status == "completed"
    assert original.analysis["key_facts"]["total_outstanding_balance"] == 900000
    assert original.summary == "$900,000 owed" and pending.summary != original.summary


@pytest.mark.asyncio
async def test_received_financial_upload_routes_to_checklist_and_pauses_add_file_requests():
    file_id, slot_id = uuid4(), uuid4()
    analysis = PendingFinancialAnalysis(source_analysis(file_id))
    file = SimpleNamespace(id=file_id, requested_document_id=None, content_hash="a" * 64, created_at=datetime.now(UTC))
    requirement = SimpleNamespace(
        requirement_key="business_debt_schedule", canonical_key="business_debt_schedule",
        label="Business debt schedule", category="Debts",
        collection_config={"evidence_classes": ["debt_schedule"]},
    )
    matches, complete, coverage = programs._matching_evidence_files(
        requirement, SimpleNamespace(id=slot_id), [file], {file_id: analysis},
    )
    assert matches == [file] and complete and coverage["current"] == 1
    decision = programs._automatic_evidence_decision(
        requirement=requirement, file=file, analysis=analysis, expected_entity=None,
        duplicate_content=False,
    )
    assert decision[0:2] == ("processing", "financial_mapping_pending")
    state = SimpleNamespace(
        id=uuid4(), profile_id=uuid4(), requirement_key=requirement.requirement_key,
        label=requirement.label, required_level="required", status="received_unverified",
        state_reason="Financial mapping and filing are in review",
        requested_document_id=slot_id, source_program_keys=[], source_policy_keys=[],
        provenance={"requirement_kind": "document", "canonical_key": requirement.canonical_key,
                    "catalog_approved": True, "client_visible": True,
                    "collection_config": requirement.collection_config, "coverage": coverage},
    )
    assert not programs._requirement_needs_client_evidence(state)
    assert not _client_submission_needed(SimpleNamespace(status=state.status, coverage_complete=True))
    tasks = []
    db = SimpleNamespace(execute=AsyncMock(return_value=Rows([])), add=tasks.append, flush=AsyncMock())
    await sync_collection_tasks(db, SimpleNamespace(id=state.profile_id, funding_category=None), [state])
    assert tasks[0].status == "in_review" and tasks[0].next_attempt_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["pending", "filed", "legacy_complete_save", "staff_reviewed_save", "borrower_save"])
async def test_real_requirement_materialization_accepts_complete_saved_schedule(scenario):
    filed = scenario not in {"pending", "borrower_save"}
    strict_review = scenario in {"staff_reviewed_save", "borrower_save"}
    profile_id, file_id, slot_id = uuid4(), uuid4(), uuid4()
    state = ApplicationRequirementState(
        id=uuid4(), profile_id=profile_id, requirement_key="business_debt_schedule",
        label="Business debt schedule", category="Debts", required_level="required",
        status="requested", requested_document_id=slot_id, verification_required=strict_review,
        source_program_keys=[], source_policy_keys=[],
        provenance={"source_kind": "lender", "requirement_kind": "document",
                    "catalog_approved": True, "client_visible": True,
                    "canonical_key": "business_debt_schedule",
                    "collection_config": {"evidence_classes": ["debt_schedule"]},
                    "external_definition": {"visibility": ["borrower", "underwriter"],
                                            "completion_mode": "requires_human_verify" if strict_review else "ai_can_complete"}},
    )
    file = SimpleNamespace(id=file_id, file_name="Schedule.pdf", requested_document_id=slot_id,
                           content_hash="a" * 64, created_at=datetime.now(UTC), statement_period=None, deleted_at=None)
    raw = source_analysis(file_id)
    analysis = raw if scenario != "pending" else PendingFinancialAnalysis(raw)
    inventory = ([file], {file_id: analysis}, set())
    if scenario == "legacy_complete_save":
        # An older Save used status=published. Execute the real coverage/body
        # selection and inventory before feeding the real requirement evaluator.
        # No refiling, new PDF or immutable-history update is needed.
        saved = SimpleNamespace(id=uuid4(), family_id=uuid4(), version_number=2,
                                source="form", status="published", pdf_file_id=file_id,
                                source_file_id=None, body={"debts": [{"lender": "Kapfi", "balance": "100", "monthly_payment": "10"}]})
        inventory_db = SimpleNamespace(info={}, execute=AsyncMock(side_effect=[
            Rows([(saved, saved.id, "debt_schedule")]), Rows([file]), Rows([raw]), Rows([]),
        ]))
        with patch.object(programs.profiles, "evidence_state", AsyncMock(return_value=SimpleNamespace(files=[file]))):
            inventory = await programs._evidence_inventory(inventory_db, SimpleNamespace(), include_pending_financial=True)
        assert file_id in inventory_db.execute.await_args_list[1].args[0].compile().params["id_1"]
        assert inventory[0] == [file] and inventory[1][file_id] is raw
        assert saved.status == "published"
    link = SimpleNamespace(id=uuid4(), file_id=file_id, source="automatic", verified_at=None,
                           verified_by_user_id=None, provenance={}, reason=None)
    slot = SimpleNamespace(id=slot_id, requires_signature=False, status="uploaded")
    added = []
    decisions = []
    def add(row):
        added.append(row)
        if isinstance(row, ApplicationRequirementEvidenceDecision):
            row.id = row.id or uuid4()
            row.created_at = datetime.now(UTC)
            decisions.append(row)
    async def execute(statement):
        model = statement.column_descriptions[0]["entity"]
        if model is ApplicationRequirementState:
            return Rows([state])
        if model is ApplicationRequirementEvidence:
            return Rows([link])
        if model is BucketFileAnalysis:
            return Rows([raw])
        if model is ApplicationRequirementEvidenceDecision:
            key = statement.compile().params.get("idempotency_key_1")
            return Rows([row for row in decisions if key is None or row.idempotency_key == key])
        if getattr(model, "__name__", "") == "ApplicationCollectionTask":
            return Rows([row for row in added if row.__class__.__name__ == "ApplicationCollectionTask"])
        return Rows([])
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        get=AsyncMock(side_effect=lambda model, _id: file if model is BucketFile else None), flush=AsyncMock(), add=add,
    )
    # Decision lookup also needs the scalar accessor; no stored decisions means
    # the real local evidence validator computes processing/accepted itself.
    profile = SimpleNamespace(id=profile_id, intake_id=None, funding_category=None)
    async def refresh(_db, _profile):
        return await programs._materialize_requirements(_db, _profile, [], {}, [], {})
    with patch.object(programs, "_evidence_inventory", AsyncMock(return_value=inventory)), patch.object(programs, "_accepted_entity_aliases", AsyncMock(return_value=set())), patch.object(collection_tasks, "latest_responses", AsyncMock(return_value={})), patch.object(programs, "_requested_document", AsyncMock(return_value=slot)), patch.object(programs, "_sync_requirement_evidence", AsyncMock(return_value=[link])), patch.object(programs, "get_program_readiness", AsyncMock(side_effect=refresh)):
        if strict_review:
            from app.services.financial_publication import record_staff_debt_publication_review

            actor = SimpleNamespace(id=uuid4(), role="broker" if scenario == "staff_reviewed_save" else "client")
            saved = SimpleNamespace(id=uuid4(), version_number=2, source="form", status="filed", actor_user_id=actor.id,
                pdf_file_id=file_id, body={"debts": [{"lender": "Kapfi", "balance": "100", "monthly_payment": "10"}]})
            family = SimpleNamespace(kind="debt_schedule", current_version_id=saved.id)
            await record_staff_debt_publication_review(db, profile, family=family, version=saved, file=file, actor=actor)
        await programs._materialize_requirements(db, SimpleNamespace(id=profile_id, intake_id=None, funding_category=None), [], {}, [], {})
    assert state.status == ("verified" if filed else "received_unverified")
    assert state.provenance["coverage"]["current"] == 1
    assert state.provenance["verified_coverage"]["complete"] is filed
    assert slot.status == ("uploaded" if filed else "requested")
    task = next(row for row in added if row.__class__.__name__ == "ApplicationCollectionTask")
    assert task.status == ("completed" if filed else "in_review")
    if strict_review:
        staff = [row for row in decisions if row.actor_kind == "staff"]
        assert bool(staff) is (scenario == "staff_reviewed_save")
        if staff:
            assert staff[-1].decision == "accepted" and staff[-1].content_hash == file.content_hash
            assert staff[-1].analysis_id == raw.id and staff[-1].actor_user_id == actor.id
    assert raw.status == "completed" and raw.analysis["key_facts"]["total_outstanding_balance"] == 900000
