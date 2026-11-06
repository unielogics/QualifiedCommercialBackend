"""A reviewed statement closes exactly one loan's actual collection workflow."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList, Null

from app.enums import Role
from app.models.application_profile import ApplicationCollectionTask, ApplicationRequirementState
from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.models.financial_form_version import FinancialFormFamily, FinancialFormImport
from app.models.user import User
from app.schemas.application_profile import EvidenceDecisionOverride
from app.services import application_programs, collection_tasks, file_requests
from app.services import real_estate_evidence as evidence
from app.services import real_estate_mortgage_requests as requests


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


def evaluate(expression, row):
    if isinstance(expression, BindParameter):
        return expression.value
    if isinstance(expression, Null):
        return None
    if isinstance(expression, BooleanClauseList):
        values = [evaluate(clause, row) for clause in expression.clauses]
        return all(values) if expression.operator == operators.and_ else any(values)
    if isinstance(expression, BinaryExpression):
        left, right = evaluate(expression.left, row), evaluate(expression.right, row)
        if expression.operator == operators.in_op:
            return left in right
        if expression.operator == operators.not_in_op:
            return left not in right
        if expression.operator in (operators.is_, operators.eq):
            return left == right
        if expression.operator in (operators.is_not, operators.ne):
            return left != right
        raise AssertionError(f"Unsupported test query operator {expression.operator}")
    if hasattr(expression, "key"):
        return getattr(row, expression.key)
    raise AssertionError(f"Unsupported test query {expression}")


class MemoryDB:
    """Only persistence is substituted; matching, decisions, coverage and tasks run."""
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    async def get(self, model, identifier):
        return next((row for row in self.rows if isinstance(row, model) and row.id == identifier), None)

    async def flush(self):
        for row in self.rows:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
            if getattr(row, "created_at", None) is None:
                row.created_at = datetime.now(UTC)

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if isinstance(row, model)]
        for criterion in query._where_criteria:
            rows = [row for row in rows if evaluate(criterion, row)]
        return Result(rows)


@pytest.mark.asyncio
async def test_one_reviewed_statement_closes_only_its_loan_and_survives_readiness_refresh():
    db, raw = MemoryDB(), b"synthetic exact mortgage statement source"
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None, intake_id=None,
                              funding_category=None, client_id=None, extra_requirements=[])
    staff = User(id=uuid4(), role=Role.LOAN_EXEC, email="reviewer@example.com", name="Reviewer")
    property_id, first_id, second_id = map(str, (uuid4(), uuid4(), uuid4()))
    body = {"properties": [{"id": property_id, "address": "123 Main Street", "mortgage_status": "active"}],
            "mortgages": [{"id": first_id, "property_row_id": property_id, "lender": "Example Bank", "loan_reference": "1111", "status": "active", "balance": "100000"},
                          {"id": second_id, "property_row_id": property_id, "lender": "Example Bank", "loan_reference": "2222", "status": "active", "balance": "50000"}]}
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind=evidence.KIND,
                                 identity_key="synthetic", identity={}, label="Real estate schedule")
    source = BucketFile(id=uuid4(), bucket_id=profile.primary_bucket_id, file_name="Latest mortgage 1111.pdf",
                        content_type="application/pdf", content_hash=sha256(raw).hexdigest(), created_at=datetime.now(UTC),
                        requested_document_id=uuid4(), source_kind="operator_upload", deleted_at=None, status="uploaded")
    original_destination = source.requested_document_id
    analysis = BucketFileAnalysis(id=uuid4(), bucket_file_id=source.id, bucket_id=source.bucket_id,
        content_hash=source.content_hash, classification="payoff_or_mortgage_statement", confidence="high",
        analysis_version=1, status="completed", analysis={"key_facts": {}}, provider="test", analyzed_at=datetime.now(UTC))
    proof = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=source.id,
        content_hash=source.content_hash, base_revision=0, status="applied", mappings=[], warnings=[],
        proposed_body={"import_scope": evidence.SCOPE, "import_context": {"kind": "real_estate_mortgage_statement",
            "target_property_id": property_id, "target_mortgage_id": first_id},
            "review": {"accepted": True, "reviewed_by_user_id": str(staff.id),
                "reviewed_at": datetime.now(UTC).isoformat(), "selected_keys": [], "statement_date": "2026-11-01",
                "identity_hash": evidence.identity_fingerprint(body["properties"][0], body["mortgages"][0])}})
    body["mortgages"][0][evidence.LINK_KEY] = [str(proof.id)]
    db.rows.extend([staff, family, source, analysis, proof])

    async def scoped_source(_db, _profile, file_id):
        assert _profile.id == profile.id and file_id == source.id
        return source

    async def requested_slot(_db, _profile, requirement, *_args):
        state = next(row for row in db.rows if isinstance(row, ApplicationRequirementState)
                     and row.requirement_key == requirement.requirement_key)
        return await db.get(BucketRequestedDocument, state.requested_document_id)

    async def readiness(_db, _profile):
        states, _ = await application_programs._materialize_requirements(db, profile, [], {}, [], {})
        await collection_tasks.sync_collection_tasks(db, profile, states)
        return SimpleNamespace(requirements=[SimpleNamespace(requirement_key=state.requirement_key,
            label=state.label, state_reason=state.state_reason, status=state.status,
            requested_document_id=state.requested_document_id, client_visible=True, provenance=state.provenance)
            for state in states])

    with ExitStack() as stack:
        stack.enter_context(patch.object(file_requests, "lock_profile", AsyncMock()))
        stack.enter_context(patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)))
        stack.enter_context(patch.object(file_requests.profiles, "log_profile_action", AsyncMock()))
        stack.enter_context(patch.object(evidence.workspace, "source_file_for_profile", scoped_source))
        stack.enter_context(patch.object(evidence.workspace, "resolve_family", AsyncMock(return_value=(family, SimpleNamespace(body=body, revision=0)))))
        stack.enter_context(patch.object(evidence.workspace, "_read_file", return_value=raw))
        stack.enter_context(patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=([source], {source.id: analysis}, set()))))
        stack.enter_context(patch.object(application_programs, "_accepted_entity_aliases", AsyncMock(return_value=set())))
        stack.enter_context(patch.object(application_programs, "_requested_document", requested_slot))
        stack.enter_context(patch.object(application_programs, "get_program_readiness", readiness))
        stack.enter_context(patch("app.dealer_os.services.client_room.active_link", AsyncMock(return_value=SimpleNamespace(token="scoped-room"))))
        no_email = stack.enter_context(patch.object(file_requests.missing_item_automation, "send_requirements_email", AsyncMock()))
        destination_ids = None
        for number in (1, 2):
            version = SimpleNamespace(id=uuid4(), family_id=family.id, body=deepcopy(body), version_number=number,
                                      source="form", status="published", actor_name=staff.name)
            family.current_version_id = version.id
            states = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version, actor=staff)
            await readiness(db, profile)
            by_loan = {state.provenance["source_context"]["mortgage_row_id"]: state for state in states}
            assert by_loan[first_id].status == "verified"
            assert by_loan[first_id].provenance["verified_coverage"]["complete"] is True
            assert by_loan[second_id].status == "requested"
            displayed = await evidence.list_evidence(db, profile, family_id=family.id, actor=staff)
            assert len(displayed["links"]) == 1 and displayed["links"][0]["status"] == "accepted"
            pending = await file_requests.loan_todo_items(db, profile, want_pending=True, want_completed=False)
            completed = await file_requests.loan_todo_items(db, profile, want_pending=False, want_completed=True)
            assert len(pending) == len(completed) == 1
            assert "2222" in pending[0]["title"] and "1111" in completed[0]["title"]
            current_ids = {state.requested_document_id for state in states}
            assert destination_ids is None or current_ids == destination_ids
            destination_ids = current_ids
            tasks = [row for row in db.rows if isinstance(row, ApplicationCollectionTask)]
            assert len(tasks) == 2
            complete_task = next(row for row in tasks if row.requirement_keys == [by_loan[first_id].requirement_key])
            assert complete_task.status == "completed" and complete_task.next_attempt_at is None
            assert source.requested_document_id == original_destination
        await application_programs.override_evidence_decision(db, profile=profile,
            requirement_key=by_loan[first_id].requirement_key, file_id=source.id,
            payload=EvidenceDecisionOverride(decision="rejected", reason_code="other", confirmed=True,
                reason="The statement was later determined to concern a different loan."), user=staff)
        version = SimpleNamespace(id=uuid4(), family_id=family.id, body=deepcopy(body), version_number=3,
                                  source="form", status="published", actor_name=staff.name)
        family.current_version_id = version.id
        await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version, actor=staff)
        await readiness(db, profile)
        assert by_loan[first_id].status != "verified"
        assert by_loan[first_id].provenance["verified_coverage"]["current"] == 0
        assert complete_task.status != "completed"
        pending = await file_requests.loan_todo_items(db, profile, want_pending=True, want_completed=False)
        assert len(pending) == 2
        displayed = await evidence.list_evidence(db, profile, family_id=family.id, actor=staff)
        assert len(displayed["links"]) == 1 and displayed["links"][0]["status"] == "invalid"
        no_email.assert_not_awaited()
