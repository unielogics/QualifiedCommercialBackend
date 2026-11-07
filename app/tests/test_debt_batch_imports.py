"""Durable batch review, explicit loan identity, source proof, and atomic drafts."""
import copy
import hashlib
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile
from app.models.financial_form_version import FinancialFormImport
from app.models.user import User
from app.routers import financial_workspace as routes
from app.schemas.financial_workspace import DebtBatchApply, DebtBatchCreate
from app.services import financial_debt_batch_imports as batches
from app.services import financial_workspace as workspace
from app.tests.test_debt_document_imports import DB as BaseDB
from app.tests.test_debt_document_imports import Result, context, debt
from app.tests.test_debt_image_imports import image_bytes


class DB(BaseDB):
    def __init__(self):
        super().__init__()
        self.commits, self.rollbacks, self.active_sessions, self.max_sessions = [], 0, 0, 0

    async def execute(self, query):
        if query.column_descriptions[0].get("entity") is not FinancialFormImport:
            return await super().execute(query)
        self.queries.append(query)
        params = query.compile().params
        rows = list(self.imports)
        if "id_1" in params:
            rows = [row for row in rows if row.id == params["id_1"]]
        if "family_id_1" in params:
            rows = [row for row in rows if row.family_id == params["family_id_1"]]
        rows = [row for row in rows if (row.proposed_body or {}).get("import_scope") == batches.SCOPE]
        if query.column_descriptions[0]["name"] == "id":
            return Result([row.id for row in rows if row.status in {"queued", "processing"}])
        return Result(reversed(rows), scalar=rows[-1] if rows else None)

    async def commit(self):
        self.commits.append(copy.deepcopy([(row.status, row.proposed_body) for row in self.imports]))

    async def rollback(self):
        self.rollbacks += 1

    def session(self):
        outer = self

        class Session:
            async def __aenter__(self):
                outer.active_sessions += 1
                outer.max_sessions = max(outer.max_sessions, outer.active_sessions)
                assert outer.active_sessions == 1
                return outer

            async def __aexit__(self, *_args):
                outer.active_sessions -= 1
        return Session()


def setup(*rows):
    previous, profile, family, draft, first, _ = context(*(rows or [debt("SBA", loan_reference="0001111111", monthly_payment="100")]))
    db = DB()
    db.objects.update(previous.objects)
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db.objects.update({(ApplicationProfile, profile.id): profile, (User, actor.id): actor})
    files, raws, outputs = [], {}, {}
    for index, (reference, color) in enumerate((("0001111111", "white"), ("0002222222", "blue"), ("0003333333", "red")), 1):
        file = SimpleNamespace(**vars(first))
        file.id, file.file_name, file.content_type = uuid4(), f"SBA account {index}.png", "image/png"
        raw = image_bytes(color=color)
        file.content_hash = hashlib.sha256(raw).hexdigest()
        db.objects[(BucketFile, file.id)] = file
        files.append(file)
        raws[file.id] = raw
        source = debt("SBA", balance=str(index * 10000), monthly_payment=str(index * 100), debt_type="sba_loan")
        outputs[file.id] = ({"debts": [source]}, {"r1.loan_reference": {"value": reference, "quote": f"Loan account {reference}"}}, [], {})
    return SimpleNamespace(db=db, profile=profile, family=family, draft=draft, actor=actor, files=files, raws=raws, outputs=outputs)


class Harness:
    def __init__(self, state):
        self.state = state
        self.stack = ExitStack()

    def __enter__(self):
        state = self.state
        self.stack.enter_context(patch.object(workspace, "resolve_family", AsyncMock(return_value=(state.family, state.draft))))
        self.stack.enter_context(patch.object(workspace, "_read_file", side_effect=lambda file: state.raws[file.id]))
        self.model = self.stack.enter_context(patch.object(workspace, "_extract_full_form", AsyncMock(side_effect=lambda _db, _kind, file, _raw, **_kw: state.outputs[file.id])))
        self.stack.enter_context(patch.object(workspace, "load_workspace", AsyncMock(side_effect=lambda *_args, **_kw: {
            "body": copy.deepcopy(state.draft.body), "draft": {"revision": state.draft.revision}, "family_id": str(state.family.id)})))
        self.stack.enter_context(patch.object(batches, "SessionLocal", side_effect=state.db.session))
        self.stack.enter_context(patch("app.services.application_profiles.load_profile", AsyncMock(return_value=state.profile)))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


async def enqueue(state, files=None, key=None):
    return await batches.create(state.db, state.profile, file_ids=[file.id for file in files or state.files],
        expected_revision=state.draft.revision, idempotency_key=key or uuid4(), family_id=state.family.id, actor=state.actor)


async def process(state):
    queued = await enqueue(state)
    await batches.run_batch(UUID(queued["id"]))
    return await batches.get(state.db, state.profile, UUID(queued["id"]))


def decision(candidate, action, target=None, fields=None, **extra):
    selected = [mapping["key"] for mapping in candidate["mappings"] if fields is None or mapping["field"] in fields]
    return {"candidate_id": candidate["id"], "action": action, "target_row_id": target,
            "selected_keys": selected if action not in {"link", "skip"} else [], "confirmed": action != "skip",
            "confirm_separate_loan": True, **extra}


@pytest.mark.asyncio
async def test_post_is_fast_persisted_and_recoverable_without_model_or_draft_writes():
    state = setup()
    before = copy.deepcopy(state.draft.body)
    key = uuid4()
    with Harness(state) as test:
        response = await enqueue(state, key=key)
        assert response["status"] == "queued" and response["progress"] == {"total": 3, "done": 0}
        test.model.assert_not_awaited()
        state.draft.revision += 1
        replay = await batches.create(state.db, state.profile, file_ids=[file.id for file in state.files], expected_revision=4,
            idempotency_key=key, family_id=state.family.id, actor=state.actor)
        assert replay["id"] == response["id"] and replay["stale"]
        recovered = await batches.list_reviews(state.db, state.profile, family_id=state.family.id)
    assert recovered["items"][0]["id"] == response["id"] and state.draft.body == before


@pytest.mark.asyncio
async def test_existing_one_sba_plus_two_new_are_reviewed_then_applied_atomically():
    state = setup()
    prior = copy.deepcopy(state.draft.body["debts"][0])
    with Harness(state):
        review = await process(state)
        assert review["status"] == "proposed" and review["progress"]["done"] == 3
        assert len(review["candidates"]) == 3 and state.draft.revision == 4 and state.draft.body["debts"] == [prior]
        choices = []
        for candidate in review["candidates"]:
            if candidate["loan_reference"] == "0001111111":
                assert candidate["suggested_action"] == "update" and candidate["suggested_target_row_id"] == prior["id"]
                choices.append(decision(candidate, "update", prior["id"], {"balance"}))
            else:
                choices.append(decision(candidate, "create", fields={"lender", "debt_type", "balance", "monthly_payment"}))
        result = await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    rows = result["body"]["debts"]
    assert len(rows) == 3 and state.draft.revision == 5 and not state.db.versions
    assert rows[0]["id"] == prior["id"] and rows[0]["monthly_payment"] == "100.00"
    assert {row["loan_reference"] for row in rows} == {"0001111111", "0002222222", "0003333333"}
    assert all(len(row["evidence_links"]) == 1 for row in rows)
    assert len({row["id"] for row in rows}) == 3 and state.db.max_sessions == 1
    assert any(body["files"][0]["status"] == "processing" for commit in state.db.commits for _status, body in commit)


@pytest.mark.asyncio
async def test_legacy_sba_display_reference_is_review_suggestion_not_silent_duplicate():
    existing = debt("SBA COV 0001111111", monthly_payment="100")
    state = setup(existing)
    with Harness(state):
        review = await process(state)
    first = next(row for row in review["candidates"] if row["loan_reference"] == "0001111111")
    assert first["suggested_action"] == "review" and first["suggested_target_row_id"] == existing["id"]
    assert existing["id"] in first["possible_duplicate_row_ids"]
    assert not batches._legacy_reference_match({"lender": "SBA 900011111119"}, "0001111111")
    assert len(review["candidates"]) == 3


@pytest.mark.asyncio
async def test_one_reference_groups_files_but_conflicting_values_remain_source_specific_choices():
    state = setup()
    source2 = state.outputs[state.files[1].id]
    source2[1]["r1.loan_reference"]["value"] = "0001111111"
    source2[1]["r1.loan_reference"]["quote"] = "Account 0001111111"
    with Harness(state):
        review = await process(state)
        candidate = next(row for row in review["candidates"] if row["loan_reference"] == "0001111111")
        balances = [mapping for mapping in candidate["mappings"] if mapping["field"] == "balance"]
        assert len(candidate["source_file_ids"]) == 2 and len(balances) == 2
        assert {mapping["value"] for mapping in balances} == {"10000", "20000"}
        assert all(mapping["conflict"] for mapping in balances) and len({mapping["key"] for mapping in balances}) == 2
        choices = [decision(row, "skip") for row in review["candidates"]]
        choices[review["candidates"].index(candidate)] = decision(candidate, "update", state.draft.body["debts"][0]["id"], {"balance"})
        before = copy.deepcopy(state.draft.body)
        with pytest.raises(HTTPException, match="one source value"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.body == before and state.draft.revision == 4


@pytest.mark.asyncio
async def test_duplicate_bytes_reuse_extraction_without_creating_another_candidate():
    state = setup()
    state.raws[state.files[1].id] = state.raws[state.files[0].id]
    state.files[1].content_hash = state.files[0].content_hash
    with Harness(state) as test:
        review = await process(state)
    assert test.model.await_count == 2 and len(review["candidates"]) == 2
    duplicate = next(file for file in review["files"] if file["file_id"] == str(state.files[1].id))
    assert duplicate["status"] == "duplicate" and duplicate["duplicate_of_file_id"] == str(state.files[0].id)


@pytest.mark.asyncio
async def test_partial_extraction_retry_preserves_successful_rows_and_stable_candidates():
    state = setup()
    def extract(_db, _kind, file, _raw, **_kw):
        if file.id == state.files[1].id:
            raise HTTPException(422, "Unreadable screenshot")
        return state.outputs[file.id]
    with Harness(state) as test:
        test.model.side_effect = extract
        review = await process(state)
        assert len(review["candidates"]) == 2 and review["files"][1]["status"] == "unreadable"
        prior_ids = {row["id"] for row in review["candidates"]}
        test.model.side_effect = lambda _db, _kind, file, _raw, **_kw: state.outputs[file.id]
        await batches.retry(state.db, state.profile, UUID(review["id"]), expected_revision=4, file_ids=[state.files[1].id], actor=state.actor)
        await batches.run_batch(UUID(review["id"]))
        final = await batches.get(state.db, state.profile, UUID(review["id"]))
    assert len(final["candidates"]) == 3 and prior_ids <= {row["id"] for row in final["candidates"]}
    assert test.model.await_count == 4 and state.draft.revision == 4


@pytest.mark.asyncio
async def test_stale_refresh_reuses_retained_extraction_and_requires_new_confirmation():
    state = setup()
    with Harness(state) as test:
        review = await process(state)
        state.draft.revision = 5
        with pytest.raises(HTTPException) as error:
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=5, idempotency_key=uuid4(), decisions=[decision(row, "skip") for row in review["candidates"]], actor=state.actor)
        assert error.value.detail["code"] == "debt_batch_stale"
        refreshed = await batches.refresh(state.db, state.profile, UUID(review["id"]), expected_revision=5, idempotency_key=uuid4(), actor=state.actor)
        await batches.run_batch(UUID(refreshed["id"]))
        new = await batches.get(state.db, state.profile, UUID(refreshed["id"]))
    assert new["id"] != review["id"] and new["base_revision"] == 5 and test.model.await_count == 3
    assert {row["id"] for row in new["candidates"]} == {row["id"] for row in review["candidates"]}
    assert {mapping["key"] for row in new["candidates"] for mapping in row["mappings"]} == {mapping["key"] for row in review["candidates"] for mapping in row["mappings"]}


@pytest.mark.asyncio
async def test_apply_idempotent_replay_before_revision_check_never_changes_body_twice():
    state = setup()
    with Harness(state):
        review = await process(state)
        key = uuid4()
        choices = [decision(row, "link", state.draft.body["debts"][0]["id"]) if row["loan_reference"] == "0001111111" else decision(row, "skip") for row in review["candidates"]]
        await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=key, decisions=choices, actor=state.actor)
        after = copy.deepcopy(state.draft.body)
        await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=key, decisions=choices, actor=state.actor)
        assert state.draft.revision == 5 and state.draft.body == after
        with pytest.raises(HTTPException, match="different review"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=5, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    locks = [query for query in state.db.queries if query._for_update_arg is not None]
    assert any(query.get_execution_options().get("populate_existing") for query in locks)


@pytest.mark.asyncio
async def test_all_skip_finishes_without_financial_revision_or_any_source_reads():
    state = setup()
    with Harness(state):
        review = await process(state)
        before = copy.deepcopy(state.draft.body)
        with patch.object(batches, "_source", AsyncMock(side_effect=AssertionError("Skipped sources must not be read"))):
            result = await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=[decision(row, "skip") for row in review["candidates"]], actor=state.actor)
    assert result["body"] == before and state.draft.revision == 4 and state.db.imports[0].status == "applied"


@pytest.mark.asyncio
async def test_skipped_unavailable_source_cannot_block_valid_other_candidate():
    state = setup()
    with Harness(state):
        review = await process(state)
        selected = next(row for row in review["candidates"] if row["loan_reference"] == "0001111111")
        state.files[1].bucket_id = uuid4()
        choices = [decision(row, "link", state.draft.body["debts"][0]["id"]) if row is selected else decision(row, "skip") for row in review["candidates"]]
        await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.revision == 5 and len(state.draft.body["debts"][0]["evidence_links"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hash", "scope", "revision", "superseded"])
async def test_apply_validation_failure_leaves_every_row_and_receipt_unchanged(failure):
    state = setup()
    with Harness(state):
        review = await process(state)
        choices = [decision(row, "create", fields={"lender", "balance"}) for row in review["candidates"]]
        before = copy.deepcopy(state.draft.body)
        if failure == "hash":
            state.raws[state.files[-1].id] = image_bytes(color="black")
        elif failure == "scope":
            state.files[-1].bucket_id = uuid4()
        elif failure == "revision":
            state.draft.revision += 1
        else:
            old = state.files[-1]
            old.document_family_id, old.document_version_number = old.id, 1
            new = SimpleNamespace(**vars(old))
            new.id, new.document_family_id, new.document_version_number = uuid4(), old.id, 2
            new.document_version_status = "current"
            state.db.objects[(BucketFile, new.id)] = new
        with pytest.raises(HTTPException):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.body == before and state.db.imports[0].status == "proposed" and "apply_receipt" not in state.db.imports[0].proposed_body


@pytest.mark.asyncio
async def test_excluded_identity_requires_explicit_separate_loan_confirmation_and_never_restores():
    excluded = debt("SBA", loan_reference="0001111111", status="dismissed", evidence_links=[{"file_id": str(uuid4()), "role": "supporting"}])
    state = setup(excluded)
    with Harness(state):
        review = await process(state)
        candidate = next(row for row in review["candidates"] if row["loan_reference"] == "0001111111")
        assert excluded["id"] in candidate["possible_duplicate_row_ids"] and candidate["suggested_target_row_id"] is None
        choices = [decision(row, "create", fields={"lender"}, confirm_separate_loan=False) if row is candidate else decision(row, "skip") for row in review["candidates"]]
        with pytest.raises(HTTPException, match="separate loan"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.body["debts"][0] == excluded and state.draft.revision == 4


@pytest.mark.asyncio
async def test_partial_reference_stays_review_only_and_never_replaces_full_existing_reference():
    state = setup()
    state.outputs[state.files[0].id][1]["r1.loan_reference"].update(value="***1111", quote="Loan ***1111")
    with Harness(state):
        review = await process(state)
        candidate = next(row for row in review["candidates"] if row["loan_reference"] == "***1111")
        assert candidate["reference_strength"] == "partial" and candidate["suggested_action"] == "review"
        choices = [decision(row, "link", state.draft.body["debts"][0]["id"]) if row is candidate else decision(row, "skip") for row in review["candidates"]]
        await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.body["debts"][0]["loan_reference"] == "0001111111"


@pytest.mark.asyncio
async def test_multi_loan_reference_mapping_uses_correct_ordinal_even_with_reversed_json_order():
    state = setup()
    file = state.files[0]
    file.file_name, file.content_type = "Two accounts.pdf", "application/pdf"
    raw = b"%PDF two account fixture"
    state.raws[file.id] = raw
    file.content_hash = hashlib.sha256(raw).hexdigest()
    rows = [debt("SBA", rate="9.5"), debt("SBA", rate="9.5")]
    refs = {"r2.rate": {"page": 2, "quote": "Second APR 9.5"}, "r1.rate": {"page": 1, "quote": "First APR 9.5"}}
    state.outputs[file.id] = ({"debts": rows}, refs, [], {1: "First APR 9.5", 2: "Second APR 9.5"})
    with Harness(state):
        extracted = await batches.extract_file(state.db, state.profile, file.id)
    assert [next(mapping["source"]["page"] for mapping in row["mappings"] if mapping["field"] == "rate") for row in extracted["rows"]] == [1, 2]


@pytest.mark.asyncio
async def test_worker_lease_prevents_second_claim_and_expired_lease_resumes_only_unfinished_files():
    state = setup()
    with Harness(state) as test:
        queued = await enqueue(state)
        imported = state.db.imports[0]
        body = copy.deepcopy(imported.proposed_body)
        # The first file was durably finished before a worker restart; only the
        # remaining two queued files may call extraction when the lease expires.
        body["files"][0].update(await batches.extract_file(state.db, state.profile, state.files[0].id))
        body.update(lease_until="2999-01-01T00:00:00+00:00", worker_token="other")
        imported.proposed_body, imported.status = body, "processing"
        assert not await batches.run_batch(UUID(queued["id"]))
        assert test.model.await_count == 1
        body["lease_until"] = "2000-01-01T00:00:00+00:00"
        imported.proposed_body = body
        assert await batches.run_batch(UUID(queued["id"]))
    assert test.model.await_count == 3 and imported.status == "proposed"
    assert [call.args[2].id for call in test.model.await_args_list].count(state.files[0].id) == 1


@pytest.mark.asyncio
async def test_generic_apply_cannot_bypass_batch_decisions_and_external_partner_cannot_create():
    state = setup()
    with Harness(state):
        queued = await enqueue(state)
        with pytest.raises(HTTPException, match="dedicated source review"):
            await workspace.apply_import(state.db, state.profile, UUID(queued["id"]), expected_revision=4)
        with patch.object(routes.profiles, "load_profile", AsyncMock()) as profile:
            with pytest.raises(HTTPException) as error:
                await routes.create_debt_batch(state.profile.id, DebtBatchCreate(file_ids=[state.files[0].id], expected_revision=4, idempotency_key=uuid4()),
                    BackgroundTasks(), SimpleNamespace(id=uuid4(), role=Role.DEALER_PARTNER), state.db)
        assert error.value.status_code == 403
        profile.assert_not_awaited()


def test_wire_requires_explicit_decisions_but_allows_empty_completed_review():
    assert DebtBatchApply(expected_revision=4, idempotency_key=uuid4(), decisions=[]).decisions == []


@pytest.mark.asyncio
async def test_empty_failed_review_can_finish_without_creating_a_row():
    state = setup()
    with Harness(state):
        queued = await enqueue(state)
        imported = state.db.imports[0]
        imported.status = "proposed"
        body = copy.deepcopy(imported.proposed_body)
        for file in body["files"]:
            file.update(status="unreadable", error="No readable fields")
        imported.proposed_body = body
        before = copy.deepcopy(state.draft.body)
        with patch.object(workspace, "stage_draft", AsyncMock()) as save:
            await batches.apply(state.db, state.profile, UUID(queued["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=[], actor=state.actor)
    save.assert_not_awaited()
    assert state.draft.body == before and state.draft.revision == 4 and imported.status == "applied"


@pytest.mark.asyncio
async def test_same_target_conflicting_candidate_fields_are_rejected_atomically():
    state = setup()
    for file in state.files:
        state.outputs[file.id] = (state.outputs[file.id][0], {}, [], {})
    with Harness(state):
        review = await process(state)
        choices = [decision(row, "update", state.draft.body["debts"][0]["id"], {"balance"}) for row in review["candidates"]]
        before = copy.deepcopy(state.draft.body)
        with pytest.raises(HTTPException, match="contradictory values"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.body == before and state.draft.revision == 4


@pytest.mark.asyncio
async def test_native_payment_selection_cannot_bypass_unchecked_conflicting_monthly():
    state = setup()
    source = state.outputs[state.files[0].id][0]["debts"][0]
    source.update(payment_amount="10", payment_frequency="daily", monthly_payment=None)
    with Harness(state):
        review = await process(state)
        selected = next(row for row in review["candidates"] if row["loan_reference"] == "0001111111")
        monthly = next(item for item in selected["mappings"] if item["field"] == "monthly_payment")
        assert monthly["derived"] and monthly["source"]["kind"] == "calculation"
        choices = [decision(row, "update", state.draft.body["debts"][0]["id"], {"payment_amount", "payment_frequency"}) if row is selected else decision(row, "skip") for row in review["candidates"]]
        before = copy.deepcopy(state.draft.body)
        with pytest.raises(HTTPException) as error:
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
        assert error.value.detail["code"] == "monthly_equivalent_review_required"
    assert state.draft.body == before and state.draft.revision == 4


@pytest.mark.asyncio
async def test_conflicting_full_reference_and_inactive_target_are_never_overwritten():
    state = setup()
    with Harness(state):
        review = await process(state)
        selected = next(row for row in review["candidates"] if row["loan_reference"] == "0002222222")
        choices = [decision(row, "link", state.draft.body["debts"][0]["id"]) if row is selected else decision(row, "skip") for row in review["candidates"]]
        with pytest.raises(HTTPException, match="reference conflicts"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
        state.draft.body["debts"][0]["status"] = "dismissed"
        with pytest.raises(HTTPException, match="active obligation"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4, idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert state.draft.revision == 4 and state.draft.body["debts"][0]["loan_reference"] == "0001111111"


@pytest.mark.asyncio
async def test_generated_financial_pdf_cannot_feed_its_own_batch():
    state = setup()
    state.files[0].source_kind = "drafted_form"
    with Harness(state) as test:
        queued = await enqueue(state, files=state.files[:1])
        await batches.run_batch(UUID(queued["id"]))
        review = await batches.get(state.db, state.profile, UUID(queued["id"]))
    assert review["files"][0]["status"] == "unavailable" and review["candidates"] == []
    test.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_idempotency_key_cannot_be_reused_for_other_sources():
    state = setup()
    with Harness(state):
        key = uuid4()
        await enqueue(state, key=key)
        with pytest.raises(HTTPException, match="different batch"):
            await enqueue(state, files=state.files[:1], key=key)
    assert len(state.db.imports) == 1


@pytest.mark.asyncio
async def test_batch_worker_supports_real_heic_original_hash_without_pdf_or_publication():
    from app.tests.test_debt_image_imports import heic_bytes

    state = setup()
    source = state.files[0]
    raw = heic_bytes()
    source.file_name, source.content_type, source.content_hash = "SBA source.heic", "image/heic", hashlib.sha256(raw).hexdigest()
    state.raws[source.id] = raw
    with Harness(state):
        queued = await enqueue(state, files=[source])
        await batches.run_batch(UUID(queued["id"]))
        review = await batches.get(state.db, state.profile, UUID(queued["id"]))
    assert review["files"][0]["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert review["candidates"][0]["mappings"][0]["source"]["content_type"] == "image/heic"
    assert state.draft.revision == 4 and not state.db.versions


@pytest.mark.asyncio
async def test_wire_uuid_decisions_apply_without_string_identity_mismatch():
    state = setup()
    with Harness(state):
        review = await process(state)
        payload = DebtBatchApply(expected_revision=4, idempotency_key=uuid4(), decisions=[decision(row, "skip") for row in review["candidates"]])
        await batches.apply(state.db, state.profile, UUID(review["id"]), actor=state.actor, **payload.model_dump())
    assert state.db.imports[0].status == "applied" and state.draft.revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["apply", "retry"])
async def test_waiting_mutation_refreshes_warm_import_after_another_request_resolved_all_skip(operation):
    state = setup()
    with Harness(state):
        review = await process(state)
        imported = state.db.imports[0]
        stale = copy.deepcopy(imported)
        receipt_key = uuid4()
        choices = [decision(row, "skip") for row in review["candidates"]]
        await batches.apply(state.db, state.profile, imported.id, expected_revision=4,
            idempotency_key=receipt_key, decisions=choices, actor=state.actor)
        resolved = copy.deepcopy(imported.proposed_body)
        execute = state.db.execute

        async def warm_identity_map(query):
            entity = query.column_descriptions[0].get("entity")
            if entity is FinancialFormImport and "id_1" in query.compile().params:
                # Mirror SQLAlchemy: a warm identity returns its old fields
                # unless the locked read explicitly refreshes them.
                row = imported if query.get_execution_options().get("populate_existing") else stale
                return Result([row], scalar=row)
            return await execute(query)

        with patch.object(state.db, "execute", side_effect=warm_identity_map):
            if operation == "apply":
                with pytest.raises(HTTPException, match="different review decision"):
                    await batches.apply(state.db, state.profile, imported.id, expected_revision=4,
                        idempotency_key=uuid4(), decisions=choices, actor=state.actor)
            else:
                with pytest.raises(HTTPException) as error:
                    await batches.retry(state.db, state.profile, imported.id, expected_revision=4, actor=state.actor)
                assert error.value.status_code == 409
    assert imported.status == "applied" and imported.proposed_body == resolved
    assert state.draft.revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("lenders", [("SBA", "Small Business Administration"), ("Name", "Name LLC")])
async def test_same_full_reference_across_lender_aliases_requires_peer_duplicate_review_without_merging(lenders):
    state = setup()
    state.draft.body["debts"] = []
    for file, lender in zip(state.files[:2], lenders, strict=True):
        state.outputs[file.id][0]["debts"][0]["lender"] = lender
        state.outputs[file.id][1]["r1.loan_reference"].update(value="0001111111", quote="Loan account 0001111111")
    with Harness(state):
        review = await process(state)
        peers = [candidate for candidate in review["candidates"] if candidate["loan_reference"] == "0001111111"]
        assert len(peers) == 2 and len({candidate["id"] for candidate in peers}) == 2
        assert all(candidate["suggested_action"] == "review" and candidate["requires_separate_loan_confirmation"] for candidate in peers)
        assert all(any("Possible duplicate within this batch" in warning for warning in candidate["warnings"]) for candidate in peers)
        assert all(candidate["possible_duplicate_row_ids"] == [] for candidate in peers)
        separate = next(candidate for candidate in review["candidates"] if candidate["loan_reference"] == "0003333333")
        assert separate["suggested_action"] == "create" and not separate["requires_separate_loan_confirmation"]
        choices = [decision(candidate, "create", fields={"lender"}, confirm_separate_loan=False) if candidate in peers else decision(candidate, "skip") for candidate in review["candidates"]]
        with pytest.raises(HTTPException, match="separate loan"):
            await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4,
                idempotency_key=uuid4(), decisions=choices, actor=state.actor)
        assert state.draft.body["debts"] == [] and state.draft.revision == 4
        for choice in choices:
            choice["confirm_separate_loan"] = True
        result = await batches.apply(state.db, state.profile, UUID(review["id"]), expected_revision=4,
            idempotency_key=uuid4(), decisions=choices, actor=state.actor)
    assert len(result["body"]["debts"]) == 2
    assert {row["lender"] for row in result["body"]["debts"]} == set(lenders)
    assert len({row["id"] for row in result["body"]["debts"]}) == 2


@pytest.mark.asyncio
async def test_same_lender_with_different_full_references_has_no_within_batch_duplicate_gate():
    state = setup()
    state.draft.body["debts"] = []
    with Harness(state):
        review = await process(state)
    assert len(review["candidates"]) == 3
    assert all(candidate["lender"] == "SBA" and candidate["suggested_action"] == "create" for candidate in review["candidates"])
    assert all(not candidate["requires_separate_loan_confirmation"] for candidate in review["candidates"])
    assert all(not any("Possible duplicate within this batch" in warning for warning in candidate["warnings"]) for candidate in review["candidates"])
