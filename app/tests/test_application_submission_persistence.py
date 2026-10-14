"""Exercise collection -> consent -> tailored approval using a real local DB.

SQLite tests ORM persistence and workflow transitions; PostgreSQL migration DDL
and partial indexes are verified separately by Alembic's offline compiler.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import Base
from app.enums import Role
from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationExtractedFact,
    ApplicationProfile,
    ApplicationProgramSelection,
    ApplicationRequirementResponse,
    ApplicationRequirementState,
)
from app.models.arf_channel import ApplicationAssessmentSnapshot
from app.models.funding_program import FundingProgramCatalog
from app.models.lender import Lender
from app.models.submission_batch import (
    LenderOffering,
    LenderSubmission,
    LenderSubmissionEvent,
    SubmissionBatch,
)
from app.models.user import User
from app.schemas.submission_batch import SubmissionBatchCreate
from app.services import arf_channel, collection_tasks, submission_batches


@compiles(JSONB, "sqlite")
def sqlite_json_type(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.mark.asyncio
async def test_two_lenders_share_collection_but_keep_policy_and_send_approval_separate():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = sa.MetaData()
    # Clone metadata; do not mutate the production ORM's defaults or constraints.
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    classes = [User, ApplicationProfile, ApplicationProgramSelection, ApplicationRequirementState, ApplicationRequirementResponse,
               ApplicationCollectionTask, ApplicationExtractedFact, Lender, LenderOffering,
               SubmissionBatch, LenderSubmission, LenderSubmissionEvent, FundingProgramCatalog, ApplicationAssessmentSnapshot]
    tables = [metadata.tables[model.__tablename__] for model in classes]
    for table in tables:
        for column in table.columns:
            if column.server_default is not None:
                literal = str(column.server_default.arg)
                if "::jsonb" in literal:
                    column.server_default = sa.DefaultClause(sa.text(literal.replace("::jsonb", "")))
                elif literal == "now()":
                    column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
        for index in table.indexes:
            condition = index.dialect_options["postgresql"].get("where")
            if condition is not None:
                index.dialect_options["sqlite"]["where"] = condition
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: metadata.create_all(sync, tables=tables))
        async with AsyncSession(engine, expire_on_commit=False) as db:
            operator = User(clerk_id="workflow-test", email="desk@example.com", name="Underwriter", role=Role.LOAN_EXEC)
            profile = ApplicationProfile(vertical="main_street", funding_category="general")
            lenders = [Lender(name="Institution A", submission_email="a@institution.example"), Lender(name="Institution B", submission_email="b@institution.example")]
            db.add_all([operator, profile, *lenders])
            await db.flush()
            offerings = []
            for index, lender in enumerate(lenders):
                row = LenderOffering(lender_id=lender.id, program_key="*", version=1, checklist_confirmed=True,
                    source_reference="Confirmed institutional checklist", requirements=[{
                        "requirement_key": "annual_revenue", "label": "Annual revenue", "requirement_kind": "field",
                        "canonical_key": "annual_revenue", "required_level": "required", "visibility": "client",
                        "collection_config": {"field_key": "annual_revenue", "value_type": "number", "min": 200000 + index * 100000},
                    }], fit_rules={"fit": {"field": "annual_revenue", "op": "gte", "value": 200000}})
                db.add(row)
                offerings.append(row)
            await db.flush()

            fit_context = {"annual_revenue": 350000}

            async def actual_collection_source(session, _profile):
                states = list((await session.scalars(sa.select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id))).all())
                responses = await collection_tasks.latest_responses(session, profile.id)
                return {"readiness": NS(selections=[], programs=[], requirements=[]), "files": {}, "analyses": {},
                    "context": fit_context, "states": {state.requirement_key: state for state in states},
                    "responses": responses, "borrower": {"business_name": "Workflow test"}}

            payload = SubmissionBatchCreate(idempotency_key=str(uuid4()), targets=[{"lender_id": lender.id, "offering_id": row.id} for lender, row in zip(lenders, offerings, strict=True)])
            with patch.object(submission_batches, "source_state", actual_collection_source):
                pending = await submission_batches.prepare_batch(db, profile, operator, payload)
                children = list((await db.scalars(sa.select(LenderSubmission).where(LenderSubmission.batch_id == pending.id))).all())
                assert len(children) == 2
                assert all({blocker["code"] for blocker in child.blockers} >= {"sharing_consent_required", "missing_lender_requirement"} for child in children)

                tasks = list((await db.scalars(sa.select(ApplicationCollectionTask))).all())
                assert len(tasks) == 3  # One shared question, two separate institutional consents.
                question = next(task for task in tasks if task.action_type == "answer_fields")
                assert len(question.requirement_state_ids) == 2
                assert question.config["min"] == 300000
                await collection_tasks.answer_task(db, profile, question, value=350000, source="staff", user_id=operator.id)
                for task in tasks:
                    if task.action_type == "sign_consent":
                        await collection_tasks.answer_task(db, profile, task, value={"granted": True, "consenter_name": "Business Owner"}, source="staff", user_id=operator.id)
                fresh_payload = payload.model_copy(update={"idempotency_key": str(uuid4())})
                ready = await submission_batches.prepare_batch(db, profile, operator, fresh_payload)
                prepared = await submission_batches.read_batch(db, ready)
                assert all(not child.blockers for child in prepared.submissions)
                assert all(child.version == 2 for child in prepared.submissions)
                assert all(child.manifest["responses"] for child in prepared.submissions)
                for child in prepared.submissions:
                    consents = [item for item in child.manifest["responses"] if item["kind"] == "consent"]
                    assert len(consents) == 1
                    assert consents[0]["provenance"]["scope"]["lender_ids"] == [str(child.lender_id)]
                # Incoming evidence changes the file. A new internal version is
                # prepared automatically and must receive fresh human approval.
                fit_context["annual_revenue"] = 360000

                class BorrowedSession:
                    async def __aenter__(self):
                        return db

                    async def __aexit__(self, *_args):
                        return False

                with patch("app.db.SessionLocal", return_value=BorrowedSession()):
                    refreshed = await submission_batches.refresh_pending_previews()
                    assert refreshed["refreshed"] == 1
                    assert (await submission_batches.refresh_pending_previews())["refreshed"] == 0
                ready = (await db.scalars(sa.select(SubmissionBatch).order_by(SubmissionBatch.created_at.desc()).limit(1))).one()
                assert ready.approved_at is None
                refreshed_read = await submission_batches.read_batch(db, ready)
                assert all(child.version == 3 and child.status == "prepared" for child in refreshed_read.submissions)
                approved = await submission_batches.approve_batch(db, profile, ready, operator)
                assert approved.status == "queued"
                assert all(child.status == "queued" for child in approved.submissions)
                repeated = await submission_batches.approve_batch(db, profile, ready, operator)
                assert all(child.status == "queued" for child in repeated.submissions)
                with (
                    patch.object(arf_channel, "profile_facts", AsyncMock(return_value={"evidence_available": {"bank_statement", "tax_return"}})),
                    patch.object(arf_channel, "get_configuration", AsyncMock(return_value=NS(version=1, intake_mode="referral_only"))),
                    patch("app.services.application_programs._evidence_inventory", AsyncMock(return_value=([], {}, set()))),
                ):
                    assessed = await arf_channel.assessment(db, profile, persist=True)
                    again = await arf_channel.assessment(db, profile, persist=True)
                    assert assessed["id"] == again["id"]
                    persisted = await db.get(ApplicationAssessmentSnapshot, UUID(assessed["id"]))
                    assert set(persisted.facts["evidence_available"]) == {"bank_statement", "tax_return"}
                await db.commit()
    finally:
        await engine.dispose()
