"""Profile-authoritative goals, client gates and exact source identity checks."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.enums import Role
from app.models.file_event import FileEvent
from app.routers import operating_plan as routes
from app.schemas.earnings_improvement import completion, empty_body, key_facts, validate_body
from app.services import operating_plan_goals as goals
from app.tests.test_file_requests import MemoryDB, profile


class GoalDB(MemoryDB):
    def __init__(self):
        super().__init__()
        self.commit = AsyncMock()
        self.info = {}


def actor(role=Role.LOAN_EXEC):
    return SimpleNamespace(id=uuid4(), name="Synthetic staff", role=role)


async def configure(db, owner, user, *, amount="18000", currency="USD", revision=0, key="synthetic-goal-1"):
    return await goals.set_goal(db, owner, amount=amount, currency=currency, expected_revision=revision,
                                idempotency_key=key, actor=user)


@pytest.mark.asyncio
async def test_goal_is_profile_scoped_revision_ordered_audited_and_public_projection_is_private():
    db, owner, user = GoalDB(), profile(), actor()
    with patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()) as lock:
        initial = await goals.read_goal(db, owner)
        assert initial == {"status": "awaiting_staff", "metric": "annual_noi", "amount": None,
                           "currency": None, "revision": 0, "updated_at": None}
        first = await configure(db, owner, user)
        second = await configure(db, owner, user, amount="20000", revision=1, key="synthetic-goal-2")
        assert first["amount"] == 18000 and second["revision"] == 2
        # Read authority is the profile revision, even if timestamps are equal
        # or a read returns records in another order.
        db.rows.reverse()
        assert await goals.read_goal(db, owner) == second
        assert (await goals.read_goal(db, profile()))["status"] == "awaiting_staff"
        assert lock.await_count == 2
    assert all(isinstance(row, FileEvent) and row.actor_user_id == user.id and row.notice_status == "none"
               and row.notice_recipients == [] and row.visibility == "desk" for row in db.rows)
    assert set(second) == {"status", "metric", "amount", "currency", "revision", "updated_at"}
    assert "actor" not in str(second) and "request_stamp" not in str(second)


@pytest.mark.asyncio
async def test_idempotency_actor_payload_revision_and_noop_do_not_churn_goal():
    db, owner, user = GoalDB(), profile(), actor()
    with patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()):
        first = await configure(db, owner, user)
        assert await configure(db, owner, user) == first
        assert await configure(db, owner, user, revision=1, key="new-noop-key") == first
        assert len(db.rows) == 1
        for changes in ({"amount": "18000.01"}, {"user": actor()}):
            params = {"db": db, "owner": owner, "user": user, **changes}
            with pytest.raises(HTTPException) as conflict:
                await configure(**params)
            assert conflict.value.status_code == 409
            assert conflict.value.detail["code"] == "operating_goal_idempotency_conflict"
        with pytest.raises(HTTPException) as stale:
            await configure(db, owner, user, key="new-stale-key")
        assert stale.value.detail["code"] == "operating_goal_changed"
        assert len(db.rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER, Role.CAPITAL_PARTNER, Role.BROKER, Role.REGIONAL_MANAGER])
async def test_goal_write_is_underwriting_only_before_any_lock_or_event(role):
    db = GoalDB()
    with patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()) as lock:
        with pytest.raises(HTTPException) as denied:
            await configure(db, profile(), actor(role))
    assert denied.value.status_code == 403 and not db.rows
    lock.assert_not_awaited()


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", "not money", -1, 0, True])
def test_corrupt_or_unset_goal_is_waiting_not_an_exception_or_default(value):
    assert goals.public_goal({"amount": value, "currency": "USD"})["status"] == "awaiting_staff"


@pytest.mark.asyncio
async def test_goal_routes_real_schema_acl_and_positive_money_no_default():
    from app.deps import get_current_user

    app, db, owner, user = FastAPI(), GoalDB(), profile(), actor()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    path = f"/application-profiles/{owner.id}/operating-plan/goal"
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=owner)) as load, \
         patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path)).json()["amount"] is None
            for amount in (None, 0, -1, True, "NaN", "18000.001"):
                response = await client.put(path, json={"amount": amount, "currency": "USD",
                    "expected_revision": 0, "idempotency_key": "invalid-money"})
                assert response.status_code == 422
            saved = await client.put(path, json={"amount": 18000, "currency": "usd",
                "expected_revision": 0, "idempotency_key": "valid-first-goal"})
            assert saved.status_code == 200 and saved.json()["amount"] == 18000
            assert saved.json()["currency"] == "USD" and saved.json()["revision"] == 1
            assert load.await_count == 2  # Invalid requests never resolve a profile.
            user.role = Role.CLIENT
            assert (await client.get(path)).status_code == 403
            assert (await client.put(path, json={"amount": 19000, "currency": "USD",
                "expected_revision": 1, "idempotency_key": "denied-change"})).status_code == 403


@pytest.mark.asyncio
async def test_goal_gate_precedes_batch_invite_and_chat_retirement_side_effects():
    from app.schemas.file_request import FileRequestItem
    from app.services import (
        chat_form_links,
        file_requests,
        financial_statements,
        form_link_email,
        operating_plan_forms,
    )

    owner, db = profile(), GoalDB()
    calls = [
        lambda: file_requests.create_batch(db, profile=owner, request_id=uuid4(),
            items=[FileRequestItem(document_type="operating_plan", name="Operating plan")], user=actor()),
        lambda: operating_plan_forms.ensure_request(db, owner, actor=actor()),
        lambda: financial_statements.mint_link(db, owner, kind="operating_plan"),
        lambda: form_link_email.email_form_link(db, profile=owner, kind="operating_plan", label="Operating plan", slot=None, user=actor()),
        lambda: chat_form_links.link_for_action(db, profile=owner,
            action=SimpleNamespace(action_type="open_form", requirement_key="operating_expenses_and_earnings_plan")),
    ]
    with patch.object(chat_form_links, "target_for", return_value=("operating_plan", ())), \
         patch.object(chat_form_links, "retire_chat_links", AsyncMock()) as retire:
        for call in calls:
            with pytest.raises(HTTPException) as blocked:
                await call()
            assert blocked.value.status_code == 409
            assert blocked.value.detail["state"] == "goal_awaiting_staff"
        retire.assert_not_awaited()
    assert db.rows == []
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("key", ["goal", "operating_goal", "goal_progress", "goal_revision"])
def test_client_cannot_forge_or_clear_authoritative_goal(key):
    from app.services.client_financial_forms import merge_client_body

    with pytest.raises(HTTPException) as invalid:
        merge_client_body("operating_plan", empty_body(), {key: None})
    assert invalid.value.status_code == 422 and invalid.value.detail["code"] == "operating_goal_read_only"


def test_exact_import_duplicate_blocks_but_distinct_rows_and_explicit_lineage_are_preserved():
    from app.schemas.earnings_improvement import source_duplicate_missing

    parent = {"id": str(uuid4()), "label": "Wages", "category": "salaries", "current_amount": 100,
              "source_group_key": "irs:wages", "source_candidate_key": "physical-candidate-1"}
    duplicate = {**parent, "id": str(uuid4())}
    raw = validate_body({**empty_body(), "expenses": [parent, duplicate]})
    assert len(source_duplicate_missing(raw)) == 2
    assert any("source_candidate_duplicate" in path for path in completion(raw)["missing"])
    distinct = deepcopy(raw)
    distinct["expenses"][1]["source_candidate_key"] = "physical-candidate-2"
    assert not source_duplicate_missing(distinct)
    # Shared section keys, same labels/amounts/categories and a real zero are
    # not proof of duplicate source rows.
    distinct["expenses"][1]["current_amount"] = 0
    assert not source_duplicate_missing(distinct)
    children = [{**parent, "id": str(uuid4()), "current_amount": None, "itemization_parent_id": parent["id"]},
                {**parent, "id": str(uuid4()), "current_amount": 0, "itemization_parent_id": parent["id"]}]
    split = validate_body({**empty_body(), "expenses": [{**parent, "excluded": True}, *children]})
    assert not source_duplicate_missing(split)
    assert any(path.endswith("current_amount") for path in completion(split)["missing"])


def test_empty_new_allocation_metadata_preserves_legacy_financial_facts():
    raw = empty_body()
    raw["released_cash_flow"]["allocations"] = [{"id": str(uuid4()), "label": "Reserve", "amount_monthly": 100}]
    original = deepcopy(raw)
    facts = key_facts(raw)
    assert raw == original
    allocation = facts["operating_plan"]["released_cash_flow"]["allocations"][0]
    assert "purpose" not in allocation and "action_id" not in allocation
    assert "goal" not in facts and "goal_progress" not in facts


def test_suggestion_usage_obeys_summary_feature_switch():
    from app.services.ai.usage import feature_category

    assert feature_category("operating_plan_suggestions") == "summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["open", "evaluate", "draft", "submit", "suggestions", "apply", "evidence", "pdf"])
async def test_existing_goalless_client_token_waits_before_context_or_side_effects(operation):
    from app.routers import application_profiles as public
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    db, app = ReadOnlyMemory(), FastAPI()
    db.goal = None
    original = deepcopy(db.draft.body)
    app.include_router(public.router)
    app.dependency_overrides[public.get_db] = lambda: db
    base = f"/application-profiles/public/financial-forms/{TOKEN}"
    request = {"body": original}
    path = base
    if operation in {"suggestions", "apply"}:
        path += "/suggestions" + ("/apply" if operation == "apply" else "")
        request.update(expense_row_id=original["expenses"][0]["id"])
        if operation == "apply":
            request.update(suggestion_id="a" * 64, context_stamp="b" * 64)
    elif operation == "evidence":
        path += f"/evidence/{uuid4()}"
    elif operation == "pdf":
        path += f"/versions/{uuid4()}/pdf"
    elif operation != "open":
        path += "/" + operation
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock()) as load, \
         patch("app.services.operating_plan_suggestions.prepare", AsyncMock()) as model:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(path) if operation in {"open", "pdf", "evidence"} else await client.post(path, json=request)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["state"] == "goal_awaiting_staff"
    assert response.json()["detail"]["goal"]["amount"] is None
    load.assert_not_awaited()
    model.assert_not_awaited()
    db.assert_unchanged(original)


def test_live_goal_draft_point_is_source_comparable_cost_math_not_incomplete_or_mixed_projection():
    from app.services import earnings_improvement as engine
    from app.tests.test_operating_step3_independent import NOW, body, source_context

    current, raw = source_context(), body()
    current["_operating_goal"] = {"status": "set", "amount": 18000, "currency": "USD", "revision": 1}
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    progress = result["goal_progress"]
    assert progress["current_noi"] == -15000 and progress["proposed_noi"] is None
    assert progress["draft_noi_before_implementation"] == -12360 and progress["draft_gap"] == 30360
    # A changed disclosed source cannot inflate historical NOI or the draft point.
    raw["expenses"][0]["current_amount"] = 10000
    raw["adjustments"][0]["current_amount"] = 10000
    assert engine.evaluate(raw, current, now=NOW, include_expense_changes=True)["goal_progress"]["draft_noi_before_implementation"] is None
    raw = body()
    raw["adjustments"][0]["kind"] = "revenue_change"
    assert engine.evaluate(raw, current, now=NOW, include_expense_changes=True)["goal_progress"]["draft_noi_before_implementation"] is None


@pytest.mark.parametrize("issue", ["currency_unknown", "physical_total_mismatch", "exact_import_duplicate"])
def test_uncertain_cost_or_loan_source_cannot_manufacture_cash_capacity(issue):
    from app.services import operating_plan_tracking as tracking
    from app.services import released_cash_flow
    from app.tests.test_earnings_improvement import body, expense
    from app.tests.test_plan_goals_independent import assigned, assignment, saved_loan

    current, row = saved_loan(), expense(current_amount=1000)
    raw = body(expenses=[row], adjustments=[])
    if issue == "currency_unknown":
        current["debt"]["service"]["currency"] = None
    elif issue == "physical_total_mismatch":
        raw["expenses"][0].update(cost_basis="per_unit", unit_label="seat", current_units_per_period=10,
            current_unit_cost=10, current_fixed_amount=0)
    else:
        raw["expenses"][0]["source_candidate_key"] = "same-physical-row"
        raw["expenses"].append({**raw["expenses"][0], "id": str(uuid4())})
    raw = assigned(raw, current, [assignment(1000, "existing_cost", expense_row_id=row["id"])])
    result = tracking.cash_bridge(raw, current)
    assert not result["complete"]
    if issue == "currency_unknown":
        assert result["currency"] is None and result["recurring_cash_released_monthly"] is None
    else:
        assert result["allocations"][0]["capacity_monthly"] is None
    assert raw["released_cash_flow"]["source_fingerprint"] == released_cash_flow.source(current)["fingerprint"]


def test_captured_shareable_tracking_excludes_private_metadata_hashes_and_legacy_empty_defaults():
    from app.services import earnings_improvement as engine
    from app.services.operating_plan_tracking import public_summary
    from app.tests.test_earnings_improvement import body
    from app.tests.test_plan_goals_independent import assigned, assignment, saved_loan

    current = saved_loan()
    current["_operating_goal"] = {"status": "set", "amount": 18000, "currency": "USD", "revision": 1}
    raw = assigned(body(adjustments=[]), current, [assignment(3500)])
    result = engine.evaluate(raw, current)
    result["goal_progress"]["actor_user_id"] = "private-staff"
    result["cash_bridge"]["private_note"] = "private-reason"
    snapshot = public_summary(result)
    assert snapshot["goal_progress"]["target"] == 18000
    assert snapshot["cash_bridge"]["net_loan_cash_change_monthly"] == 3500
    assert not any(word in str(snapshot) for word in ("private-staff", "private-reason", "source_fingerprint"))
    original = deepcopy(snapshot)
    result["goal_progress"]["target"] = 99999
    assert snapshot == original
    current.pop("_operating_goal")
    raw["released_cash_flow"]["allocations"][0].pop("purpose")
    legacy = engine.package_summary(engine.evaluate(raw, current))
    assert "cash_bridge" not in legacy and "goal_progress" not in legacy
    assert "action_id" not in legacy["released_cash_flow"]["allocations"][0]
    assert "purpose" not in legacy["released_cash_flow"]["allocations"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [False, True])
@pytest.mark.parametrize("incomplete", ["unknown_cost", "duplicate", "unallocated", "unclassified"])
async def test_goal_flow_final_save_blocks_incomplete_cost_or_cash_but_keeps_draft(client, incomplete):
    from app.services import financial_workspace as workspace
    from app.tests.test_financial_workspace import Memory, context
    from app.tests.test_operating_plan_service import proposal
    from app.tests.test_plan_goals_independent import saved_loan

    db = Memory()
    owner, family, draft = context("operating_plan")
    draft.body = proposal()
    if incomplete == "unknown_cost":
        draft.body["expenses"][0]["current_amount"] = None
    elif incomplete == "duplicate":
        draft.body["expenses"][0]["source_candidate_key"] = "same-import"
        draft.body["expenses"].append({**draft.body["expenses"][0], "id": str(uuid4())})
    current = saved_loan()
    if incomplete == "unclassified":
        from app.services.released_cash_flow import source

        draft.body["released_cash_flow"] = {"source_fingerprint": source(current)["fingerprint"],
            "allocations": [{"id": str(uuid4()), "label": "Unclassified use", "amount_monthly": 3500}]}
    original = deepcopy(draft.body)
    db.add(FileEvent(profile_id=owner.id, kind=goals.KIND, meta={"amount": "18000", "currency": "USD", "revision": 1}))
    user = SimpleNamespace(id=None, role=Role.CLIENT) if client else actor()
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch.object(workspace, "_render") as render, patch.object(workspace, "_write_pdf") as store:
        with pytest.raises(HTTPException) as blocked:
            await workspace.publish_version(db, owner, kind="operating_plan", expected_revision=4,
                idempotency_key="blocked-final-version", actor=user, submit=client)
    assert blocked.value.status_code == 422
    assert blocked.value.detail["missing"]
    assert draft.body == original and family.current_version_id is None and not db.versions
    render.assert_not_called()
    store.assert_not_called()


@pytest.mark.asyncio
async def test_actual_staff_suggestion_route_apply_is_proposed_only_and_rejects_stale_goal():
    from app.deps import get_current_user
    from app.services import operating_plan_service, operating_plan_suggestions
    from app.tests.test_operating_plan_suggestions import quoted

    db, owner, user, app = GoalDB(), profile(), actor(), FastAPI()
    raw, current = quoted()
    db.add(FileEvent(profile_id=owner.id, kind=goals.KIND, meta={"amount": "18000", "currency": "USD", "revision": 1}))
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    path = f"/application-profiles/{owner.id}/operating-plan/suggestions"
    data = {"body": raw, "expense_row_id": raw["expenses"][0]["id"]}
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=owner)), \
         patch.object(operating_plan_service, "current_body", AsyncMock(return_value=raw)), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch.object(operating_plan_suggestions, "get_settings", return_value=SimpleNamespace(bedrock_enabled=False)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json=data)
            assert response.status_code == 200, response.text
            suggestion = response.json()
            choice = next(item for item in suggestion["suggestions"] if item["status"] == "ready")
            apply = {**data, "suggestion_id": choice["id"], "context_stamp": suggestion["context_stamp"]}
            applied = await client.post(path + "/apply", json=apply)
            assert applied.status_code == 200, applied.text
            assert applied.json()["body"]["adjustments"][0]["status"] == "proposed"
            assert applied.json()["status"] == "proposed"
            assert raw["adjustments"] == []
            db.rows[0].meta = {**db.rows[0].meta, "revision": 2, "amount": "20000"}
            stale = await client.post(path + "/apply", json=apply)
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "operating_suggestion_stale"
    assert len(db.rows) == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_suggestion_source_boundary_and_proposed_body_strip_private_staff_receipts():
    from app.services import operating_plan_service, operating_plan_suggestions
    from app.tests.test_operating_client_preview import ReadOnlyMemory
    from app.tests.test_operating_plan_suggestions import QUOTE, SOURCE, quoted

    db = ReadOnlyMemory()
    raw, current = quoted()
    # Neither quote is exposed unless its source belongs to this exact token's
    # existing evidence boundary. No global quote/offer catalog is exposed.
    current["operating_evidence"]["guidance"] = {"expense_groups": [{"key": "synthetic-rent", "category": "rent", "source_file_ids": [SOURCE]}]}
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch.object(operating_plan_suggestions, "get_settings", return_value=SimpleNamespace(bedrock_enabled=False)):
        hidden = await operating_plan_service.suggest(db, db.profile, raw, current=raw,
            expense_row_id=raw["expenses"][0]["id"], client=True)
        assert all(item["status"] != "ready" for item in hidden["suggestions"])
        assert QUOTE not in str(hidden)
        current["operating_evidence"]["guidance"]["expense_groups"][0]["source_file_ids"].append(QUOTE)
        visible = await operating_plan_service.suggest(db, db.profile, raw, current=raw,
            expense_row_id=raw["expenses"][0]["id"], client=True)
        choice = next(item for item in visible["suggestions"] if item["status"] == "ready")
        applied = await operating_plan_service.suggest(db, db.profile, raw, current=raw,
            expense_row_id=raw["expenses"][0]["id"], client=True,
            suggestion_id=choice["id"], context_stamp=visible["context_stamp"])
    row = applied["body"]["adjustments"][0]
    assert row["status"] == "proposed" and "reviewed_by_user_id" not in row and "review_source_stamp" not in row
    assert "cash_bridge" in applied["evaluation"] and "goal_progress" in applied["evaluation"]
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER, Role.CAPITAL_PARTNER])
@pytest.mark.parametrize("apply", [False, True])
async def test_staff_suggestion_routes_deny_external_roles_before_profile_context_or_model(role, apply):
    from app.deps import get_current_user
    from app.services import operating_plan_service

    db, app, user = GoalDB(), FastAPI(), actor(role)
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    data = {"body": empty_body(), "expense_row_id": str(uuid4())}
    if apply:
        data.update(suggestion_id="a" * 64, context_stamp="b" * 64)
    with patch.object(routes.profiles, "load_profile", AsyncMock()) as load, \
         patch.object(operating_plan_service, "suggest", AsyncMock()) as suggest:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/application-profiles/{uuid4()}/operating-plan/suggestions" + ("/apply" if apply else ""), json=data)
    assert response.status_code == 403
    load.assert_not_awaited()
    suggest.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_goal_snapshot_cannot_be_replaced_by_an_old_family_goal_or_staff_body():
    from app.services import financial_workspace as workspace
    from app.tests.test_financial_workspace import Memory, context
    from app.tests.test_operating_plan_service import cockpit, proposal

    db = Memory()
    owner, family, draft = context("operating_plan")
    draft.body = proposal()
    event = FileEvent(profile_id=owner.id, kind=goals.KIND, actor_user_id=uuid4(),
        meta={"amount": "18000", "currency": "USD", "revision": 4, "request_stamp": "private-audit-hash"})
    db.add(event)
    current = cockpit()
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "load_workspace", AsyncMock(return_value={})), \
         patch.object(workspace, "_render", return_value=b"%PDF synthetic"), patch.object(workspace, "_write_pdf"), \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        await workspace.publish_version(db, owner, kind="operating_plan", expected_revision=4,
            idempotency_key="saved-goal-snapshot", actor=actor())
    saved = db.versions[0].key_facts
    assert saved["operating_goal"]["amount"] == 18000 and saved["operating_goal"]["revision"] == 4
    assert saved["improvement_plan"]["goal_progress"]["target"] == 18000
    assert "private-audit-hash" not in str(saved) and str(event.actor_user_id) not in str(saved)
    event.meta["amount"] = "20000"
    assert saved["operating_goal"]["amount"] == 18000  # Immutable saved snapshot, not a live reference.


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["name", "unscoped_cost"])
async def test_documented_correction_does_not_hide_unnamed_or_unscoped_incomplete_expenses(missing):
    from app.services import financial_workspace as workspace
    from app.tests.test_financial_workspace import Memory, context
    from app.tests.test_recognizable_plan_contract import SOURCE, source_context, split_plan

    db = Memory()
    owner, family, draft = context("operating_plan")
    raw = split_plan(True)
    raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Check source allocation",
        "source_file_ids": [SOURCE], "evidence_note": "Disclosed ledger for preparer review"}]
    if missing == "name":
        raw["expenses"][-1]["label"] = ""
    else:
        raw["expenses"][-1]["itemization_parent_id"] = None
    draft.body = raw
    db.add(FileEvent(profile_id=owner.id, kind=goals.KIND, meta={"amount": "18000", "currency": "USD", "revision": 1}))
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "source_files_for_profile", AsyncMock(return_value=[])), \
         patch.object(workspace, "filter_current_files", AsyncMock(return_value=[])), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=source_context(raw))), \
         patch.object(workspace, "_render") as render:
        with pytest.raises(HTTPException) as blocked:
            await workspace.publish_version(db, owner, kind="operating_plan", expected_revision=4,
                idempotency_key="invalid-correction-cost", actor=actor())
    assert blocked.value.status_code == 422 and blocked.value.detail["code"] == "operating_expenses_incomplete"
    assert any(path.endswith("label" if missing == "name" else "current_amount") for path in blocked.value.detail["missing"])
    assert not db.versions
    render.assert_not_called()


def test_unknown_documented_lineage_cost_never_becomes_known_earnings_or_cash_capacity():
    from app.services import earnings_improvement as engine
    from app.services import operating_plan_tracking as tracking
    from app.tests.test_earnings_improvement import action
    from app.tests.test_plan_goals_independent import assigned, assignment, saved_loan
    from app.tests.test_recognizable_plan_contract import (
        NOW,
        SOURCE,
        command,
        source_context,
        split_plan,
    )

    raw = split_plan(True)
    unknown = raw["expenses"][-1]
    raw["adjustments"] = [action(expense_row_id=unknown["id"], current_amount=None, command=command())]
    raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Check source allocation",
        "source_file_ids": [SOURCE], "evidence_note": "Disclosed ledger for preparer review"}]
    current = source_context(raw)
    current.update({key: value for key, value in saved_loan().items() if key in {"plan", "debt"}})
    current["_operating_goal"] = {"status": "set", "amount": 18000, "currency": "USD", "revision": 1}
    raw = assigned(raw, current, [assignment(1000, "existing_cost", expense_row_id=unknown["id"])])
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    assert result["projection"]["proposed"]["noi"] is None
    assert result["projection"]["approved"]["noi"] is None
    assert result["goal_progress"]["draft_noi_before_implementation"] is None
    assert tracking.cash_bridge(raw, current, result)["allocations"][0]["capacity_monthly"] is None
