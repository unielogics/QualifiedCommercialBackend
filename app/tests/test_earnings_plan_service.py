"""Rules around a v2 plan body: who may keep a review, the one save gate, the client's view, saving a version."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.financial_form_version import FinancialFormDraft
from app.schemas import earnings_improvement as v1_schema
from app.schemas import earnings_plan_v2 as schema
from app.services import client_financial_forms, earnings_plan_pdf, operating_plan_schema
from app.services import earnings_improvement as v1
from app.services import earnings_plan_service as plan_service
from app.services import financial_workspace as ws
from app.services import operating_plan_service as service
from app.tests.earnings_plan_fixture import REVIEWED_IN_DESIGN, file_body, file_cockpit, item_id
from app.tests.test_financial_workspace import Memory, context

STAFF = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Desk")
RENT = item_id("l:rent")


def approve(body, cockpit, ids=REVIEWED_IN_DESIGN, **overrides):
    options = {"baseline_treatment": "incremental_to_baseline", "baseline_reconciliation_note": "Not in the 2025 return.", **overrides}
    return plan_service.review(body, cockpit, [item_id(key) for key in ids], "approved", "Lease and price list checked.", STAFF, **options)


def change(body, design_id):
    return next(row for block in schema.ITEM_BLOCKS for row in body[block] if row["id"] == item_id(design_id))


def finished(body):
    """Everything on the to-finish list done."""
    body = deepcopy(body)
    body["source"]["confirmations"].update({"utilities": "confirmed", "repairs": "confirmed", "other_deductions": "confirmed"})
    body["category_answers"] = {"payroll_taxes": "included", "transportation": "not_applicable"}
    for block in schema.ITEM_BLOCKS:
        for row in body[block]:
            row["note"] = row["note"] or "Contract signed; copy on file."
    body["attestation"] = True
    return body


# ---------------------------------------------------------------------------
# What a well-formed plan is
# ---------------------------------------------------------------------------

def test_a_body_is_read_by_the_model_it_names():
    assert operating_plan_schema.validate(file_body())["schema_version"] == "operating-plan-v2"
    assert operating_plan_schema.validate(v1_schema.empty_body())["schema_version"] == "operating-plan-v1"
    facts = operating_plan_schema.key_facts(file_body(keep_design_statuses=True))
    assert facts["document_type"] == "operating_expenses_and_earnings_plan" and facts["operating_plan"]["schema_version"] == "operating-plan-v2"
    saved = facts["operating_plan"]["changes"][0]
    assert saved["status"] == "approved" and not {"reviewed_by_user_id", "review_reason", "review_stamp", "baseline_reconciliation_note"} & set(saved)
    assert operating_plan_schema.totals(file_body()) == {"disclosed_expense_rows": 14, "adjustment_rows": 11}


@pytest.mark.parametrize(("edit", "message"), [
    (lambda body: body["changes"].append({**deepcopy(body["changes"][4]), "id": str(uuid4())}), "Only one change can apply"),
    (lambda body: body["changes"][3]["target"].update(line_id="missing"), "one of this plan's cost lines"),
    (lambda body: body["changes"][0]["target"].update(factor_id="nope"), "a metric of one of this plan's revenue streams"),
    (lambda body: body["growth"][0].update(factor_id="f1"), "must feed a volume metric"),
    (lambda body: body["lines"][0].update(link="s9"), "must follow one of this plan's revenue streams"),
    (lambda body: body["streams"].append(deepcopy(body["streams"][0])), "Each revenue stream needs its own ID"),
    (lambda body: body["changes"][0].update(value=True), "not a boolean"),
    (lambda body: body.update(plan_start="2027-13"), "String should match pattern"),
])
def test_an_incoherent_plan_is_refused_with_its_reason(edit, message):
    body = file_body()
    edit(body)
    with pytest.raises(HTTPException) as error:
        schema.validate_body(body)
    assert error.value.status_code == 422 and any(message in row["msg"] for row in error.value.detail["errors"])


def test_a_cleared_amount_is_unknown_and_a_withdrawn_correction_unconfirms_the_line():
    body = file_body()
    body["changes"][0]["value"] = ""
    body["source"]["confirmations"]["rent"] = "corrected"
    cleaned = schema.validate_body(body)
    assert cleaned["changes"][0]["value"] is None and "rent" not in cleaned["source"]["confirmations"]
    body["source"]["corrections"] = {"rent": {"amount": 190000, "reason": "One month was prepaid in 2024."}}
    assert schema.validate_body(body)["source"]["confirmations"]["rent"] == "corrected"


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

def test_an_ordinary_writer_cannot_author_a_review_and_keeps_one_only_on_untouched_figures():
    cockpit = file_cockpit()
    forged = file_body(keep_design_statuses=True)
    change(forged, "l:rent").update(reviewed_by_user_id=str(uuid4()), reviewed_at=datetime.now(UTC).isoformat(), review_reason="self-approved")
    protected = plan_service.protect_reviews(forged, file_body())
    assert change(protected, "l:rent")["status"] == "proposed" and change(protected, "l:rent")["reviewed_by_user_id"] is None

    current = approve(file_body(), cockpit)
    untouched = plan_service.protect_reviews(deepcopy(current), current)
    assert change(untouched, "l:rent")["status"] == "approved" and change(untouched, "l:rent")["review_reason"] == "Lease and price list checked."

    words = deepcopy(current)
    change(words, "l:rent").update(note="Lease amendment signed Oct 14.", owner="Dana")
    assert change(plan_service.protect_reviews(words, current), "l:rent")["status"] == "approved"

    figure = deepcopy(current)
    change(figure, "l:rent")["value"] = 30000
    edited = plan_service.protect_reviews(figure, current)
    assert change(edited, "l:rent")["status"] == "proposed" and change(edited, "l:rent")["review_stamp"] is None
    # The other approvals rest on the same model and were not touched.
    assert change(edited, "l:sw")["status"] == "approved"

    model = deepcopy(current)
    model["streams"][0]["factors"][0]["value"] = 320
    assert all(row["status"] == "proposed" for block in schema.ITEM_BLOCKS for row in plan_service.protect_reviews(model, current)[block])
    # Staff's own review write is trusted as it stands.
    assert change(plan_service.protect_reviews(current, file_body(), trusted=True), "l:rent")["status"] == "approved"


def test_review_needs_the_role_a_reason_an_explanation_and_the_baseline_treatment():
    cockpit = file_cockpit()
    with pytest.raises(HTTPException) as error:
        plan_service.review(file_body(), cockpit, [RENT], "approved", "ok", SimpleNamespace(id=uuid4(), role=Role.CLIENT))
    assert error.value.status_code == 403
    for options, expected in (({"reason": " "}, "review reason"), ({"ids": ["f:s2:0"]}, "explanation"), ({"baseline_treatment": "unreconciled"}, "reconciles"),
                              ({"baseline_reconciliation_note": ""}, "reconciles"), ({"ids": ["l:rent", "l:rent"]}, "once")):
        reason = options.pop("reason", "Lease checked.")
        ids = options.pop("ids", ["l:rent"])
        with pytest.raises(HTTPException) as error:
            plan_service.review(file_body(), cockpit, [item_id(key) for key in ids], "approved", reason, STAFF,
                                **{"baseline_treatment": "incremental_to_baseline", "baseline_reconciliation_note": "Not in the return.", **options})
        assert error.value.status_code == 422 and expected in str(error.value.detail)
    off = file_body()
    change(off, "l:rent")["included"] = False
    with pytest.raises(HTTPException) as error:
        approve(off, cockpit, ["l:rent"])
    assert "back on" in str(error.value.detail)
    with pytest.raises(HTTPException) as error:
        approve(file_body(), file_cockpit(analyses=[]), ["l:rent"])
    assert "earnings source" in str(error.value.detail)


def test_reject_and_reopen_through_the_shared_review_entry_point():
    cockpit = file_cockpit()
    approved = approve(file_body(), cockpit)
    row = change(approved, "l:rent")
    assert (row["status"], row["reviewed_by_user_id"], row["baseline_treatment"]) == ("approved", str(STAFF.id), "incremental_to_baseline") and len(row["review_stamp"]) == 64
    # The same route the earlier plan uses: `review_actions` hands a v2 body to its own rules.
    rejected = v1.review_actions(approved, cockpit, [RENT], "rejected", "Amendment not countersigned.", STAFF)
    assert change(rejected, "l:rent")["status"] == "rejected"
    result = v1.evaluate(rejected, cockpit)
    assert next(item for item in result["actions"] if item["id"] == RENT)["review_state"] == "rejected"
    assert result["totals"]["proposed"]["count"] == 10 and result["totals"]["approved"]["count"] == 2
    reopened = v1.review_actions(rejected, cockpit, [RENT], "proposed", "Countersigned copy received.", STAFF)
    assert change(reopened, "l:rent")["status"] == "proposed" and change(reopened, "l:rent")["review_reason"] == ""


# ---------------------------------------------------------------------------
# The one save gate
# ---------------------------------------------------------------------------

def test_the_gate_is_one_list_and_opens_when_the_list_is_done():
    cockpit = file_cockpit()
    with pytest.raises(HTTPException) as error:
        plan_service.require_complete(file_body(), v1.evaluate(file_body(), cockpit))
    detail = error.value.detail
    assert error.value.status_code == 422 and detail["code"] == "earnings_plan_incomplete" and len(detail["to_finish"]) == 8
    assert detail["message"] == "8 items to finish before saving a version. Your working draft is retained."
    assert {row["step"] for row in detail["to_finish"]} == {0, 1, 2, 3}
    done = finished(file_body())
    plan_service.require_complete(done, v1.evaluate(done, cockpit))
    # Assigning too much released cash, or a source that changed, warns without closing the gate.
    done["allocations"][2]["monthly_amount"] = 90_000
    evaluation = v1.evaluate(done, cockpit)
    assert evaluation["plan"]["warnings"] and plan_service.require_complete(done, evaluation) is None


# ---------------------------------------------------------------------------
# The plan service and the client link
# ---------------------------------------------------------------------------

def test_the_plan_service_hands_a_v2_body_to_its_own_rules_and_refuses_a_downgrade():
    body = file_body(keep_design_statuses=True)
    # Published context and the package protect a client submission against an empty v1 plan.
    assert all(row["status"] == "proposed" for row in service.protect_reviews(body, v1_schema.empty_body())["changes"])
    with pytest.raises(HTTPException) as error:
        service.protect_reviews(v1_schema.empty_body(), body)
    assert error.value.status_code == 409 and error.value.detail["code"] == "operating_plan_upgraded"
    linked = file_body()
    file_id = str(uuid4())
    linked["changes"][0]["source_file_ids"] = [file_id]
    assert service.linked_file_ids(linked) == {file_id} and service.linked_file_ids(v1_schema.empty_body()) == set()


@pytest.mark.asyncio
async def test_drafts_check_linked_files_and_never_overwrite_an_upgraded_plan():
    body = file_body()
    body["changes"][0]["source_file_ids"] = [str(uuid4())]
    with patch.object(ws, "source_files_for_profile", AsyncMock(return_value=[])), patch.object(ws, "filter_current_files", AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as error:
            await service.prepare_draft(Memory(), SimpleNamespace(), incoming=body, current=file_body())
        assert error.value.status_code == 422
        assert await service.prepare_draft(Memory(), SimpleNamespace(), incoming=body, current=body)
        assert (await service.prepare_draft(Memory(), SimpleNamespace(), incoming=file_body(), current=v1_schema.empty_body()))["schema_version"] == "operating-plan-v2"
        with pytest.raises(HTTPException) as error:
            await service.prepare_draft(Memory(), SimpleNamespace(), incoming=v1_schema.empty_body(), current=file_body())
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await service.suggest(Memory(), SimpleNamespace(), file_body(), current=file_body(), expense_row_id=uuid4())
    assert error.value.status_code == 409 and error.value.detail["code"] == "not_available_for_this_plan"


def test_the_client_sees_statuses_only_and_cannot_write_a_review():
    cockpit = file_cockpit()
    current = approve(file_body(), cockpit)
    seen = client_financial_forms.client_body("operating_plan", current)
    row = change(seen, "l:rent")
    assert row["status"] == "approved" and not {"reviewed_by_user_id", "reviewed_at", "review_reason", "review_stamp", "baseline_reconciliation_note"} & set(row)
    # The client sends the plan back with a new activity and a hand-written approval on it.
    edited = deepcopy(seen)
    edited["activities"].append({"id": str(uuid4()), "name": "Second detailer", "monthly_amount": 2400, "start_month": 6, "note": "Summer demand.",
                                 "status": "approved", "review_reason": "client wrote this"})
    merged = client_financial_forms.merge_client_body("operating_plan", current, edited)
    assert merged["activities"][-1]["status"] == "proposed" and merged["activities"][-1]["review_reason"] == ""
    # What staff approved and the client left alone stays approved, reviewer and reason intact.
    assert change(merged, "l:rent")["status"] == "approved" and change(merged, "l:rent")["reviewed_by_user_id"] == str(STAFF.id)
    with pytest.raises(HTTPException) as error:
        client_financial_forms.merge_client_body("operating_plan", current, {**v1_schema.empty_body(), "business_name": "x"})
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        client_financial_forms.merge_client_body("operating_plan", current, {**edited, "goal": {"amount": 1}})


# ---------------------------------------------------------------------------
# Saving a version
# ---------------------------------------------------------------------------

def publishing(db, family, draft, cockpit, **extra):
    slot = SimpleNamespace(id=uuid4(), status="requested")
    patches = [patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})),
               patch.object(ws, "_write_pdf"), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)),
               patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)),
               patch("app.services.financial_publication.after_publication", AsyncMock()),
               patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)), patch.object(ws, "_project", AsyncMock())]
    return patches


@pytest.mark.asyncio
async def test_saving_a_version_is_held_only_by_the_to_finish_list_and_keeps_the_plans_figures():
    db = Memory()
    profile, family, draft = context("operating_plan")
    cockpit = file_cockpit()
    draft.body = schema.validate_body(file_body())
    actor = SimpleNamespace(id=uuid4(), name="Desk", role=Role.LOAN_EXEC)
    patches = publishing(db, family, draft, cockpit)
    for item in patches:
        item.start()
    try:
        with pytest.raises(HTTPException) as error:
            await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="plan-v2-1", actor=actor)
        assert error.value.status_code == 422 and error.value.detail["code"] == "earnings_plan_incomplete" and not db.versions
        draft.body = schema.validate_body(finished(file_body()))
        result = await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="plan-v2-2", actor=actor)
    finally:
        for item in patches:
            item.stop()
    version = db.versions[0]
    assert result["saved_version"]["version_number"] == 1 and version.status == "published"
    facts = version.key_facts
    assert facts["plan_model"] == "earnings-plan-v2" and facts["proposal_status"] == "staff_saved"
    assert facts["plan_result"]["proposed"]["ebitda"] == 182896.64 and facts["plan_result"]["metrics"]["proposed"]["with_terms"]["dscr"] == pytest.approx(1.029823)
    assert facts["improvement_plan"]["projection"]["proposed"]["earnings"] == 182896.64 and len(facts["improvement_plan"]["adjustments"]) == 11
    assert facts["operating_review"]["working"]["earnings"] == 182896.64 and facts["operating_review"]["version_number"] == 1
    assert facts["operating_plan"]["schema_version"] == "operating-plan-v2" and version.body["schema_version"] == "operating-plan-v2"
    assert "annual_ebitda" not in facts and family.current_version_id == draft.base_version_id == version.id


@pytest.mark.asyncio
async def test_a_client_submission_carries_no_approval_into_the_saved_version():
    from app.models.file_event import FileEvent

    db = Memory()
    profile, family, draft = context("operating_plan")
    cockpit = file_cockpit(goal=215_000)
    draft.body = schema.validate_body(finished(approve(file_body(), cockpit)))
    db.add(FileEvent(profile_id=profile.id, kind="operating_plan.goal_set", meta={"amount": "215000", "currency": "USD", "revision": 1}))
    patches = publishing(db, family, draft, cockpit)
    for item in patches:
        item.start()
    try:
        await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="client-v2-1",
                                 actor=SimpleNamespace(id=None, name="Client", role=Role.CLIENT), submit=True)
    finally:
        for item in patches:
            item.stop()
    version = db.versions[0]
    assert version.status == "client_submitted" and version.key_facts["proposal_status"] == "client_submitted"
    assert all(row["status"] == "proposed" for block in schema.ITEM_BLOCKS for row in version.body[block])
    assert version.key_facts["improvement_plan"]["totals"]["approved"]["count"] == 0
    assert version.key_facts["goal_progress"]["target"] == 215000 and version.key_facts["operating_goal"]["amount"] == 215000


def test_the_plan_document_draws_only_what_the_version_stored():
    cockpit = file_cockpit(goal=215_000)
    body = schema.validate_body(finished(approve(file_body(), cockpit)))
    body["carried_over"] = [{"label": "Google Ads", "detail": "Re-enter it as a growth activity.", "annual_amount": 12000, "kind": "change"}]
    body = schema.validate_body(body)
    evaluation = v1.evaluate(body, cockpit)
    facts = plan_service.version_facts(operating_plan_schema.key_facts(body), body, cockpit, evaluation, client_submission=False, goal=cockpit["_operating_goal"])
    document = earnings_plan_pdf.build_html(body=body, financial_totals=facts)
    for text in ("Harbor Auto Detail LLC", "IRS 1120-S · 2025", "Plan starts Jan 2027", "-$22,377", "$182,897", "1.03x", "$177,600",
                 "Still to find for 1.25x coverage: <b>$39,103</b>", "Revenue drivers", "+$89,616", "Lease amendment on bay 2 signed in October 2026.",
                 "Reviewed", "Feb 2027", "First-year EBITDA $145,663", "Goal $215,000", "release $50,000 a month", "Carried over from the earlier plan", "Google Ads",
                 "The business confirmed these disclosures and proposals."):
        assert text in document, text
    for private in ("Lease and price list checked.", str(STAFF.id), "Not in the 2025 return."):
        assert private not in document
    pdf = earnings_plan_pdf.render_pdf(body=body, financial_totals=facts)
    assert pdf.startswith(b"%PDF") and len(pdf) > 4000
    # A version saved without figures (nothing to draw from) still renders its body.
    assert "No changes proposed." in earnings_plan_pdf.build_html(body=schema.empty_body(), financial_totals={})


# ---------------------------------------------------------------------------
# What the plan screens load
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reading_the_plan_never_writes_and_carries_a_v1_draft_over_in_memory():
    from app.tests.test_earnings_plan_migrate import old_plan

    cockpit = file_cockpit()
    profile, family, draft = context("operating_plan")
    with patch.object(ws, "existing_family", AsyncMock(return_value=None)), patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)):
        db = Memory()
        fresh = await plan_service.workspace(db, profile)
    assert (fresh["origin"], fresh["family_id"], fresh["draft"], fresh["versions"]) == ("seeded", None, None, [])
    assert fresh["model"] == "operating-plan-v2" and len(fresh["body"]["lines"]) == 10 and db.added == []
    assert fresh["completion"]["missing_count"] == 13 and fresh["evaluation"]["comparison"]["baseline"]["earnings"] == -22377
    assert "units" in fresh["stream_templates"]

    db = Memory()
    draft.body = old_plan()
    db.objects[(FinancialFormDraft, family.id)] = draft
    with patch.object(ws, "existing_family", AsyncMock(return_value=family)), patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)):
        carried = await plan_service.workspace(db, profile)
        assert (carried["origin"], carried["draft"]["revision"], carried["draft"]["stored_model"]) == ("carried_over", 4, "operating-plan-v1")
        assert len(carried["body"]["changes"]) == 3 and len(carried["body"]["carried_over"]) == 5
        # The stored draft is still the earlier plan until someone saves.
        assert draft.body["schema_version"] == "operating-plan-v1" and db.added == []
        draft.body = schema.validate_body(file_body())
        saved = await plan_service.workspace(db, profile)
    assert saved["origin"] == "saved" and saved["evaluation"]["comparison"]["proposed"]["earnings"] == 182896.64
    assert saved["completion"]["to_finish"][0]["label"] == "Confirm Utilities source amount"
    other = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    from app.models.financial_form_version import FinancialFormFamily

    db.objects[(FinancialFormFamily, family.id)] = family
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)), pytest.raises(HTTPException) as error:
        await plan_service.workspace(db, other, family_id=family.id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_summary_reads_a_v2_draft_for_the_file_page():
    db = Memory()
    profile, family, draft = context("operating_plan")
    draft.body = schema.validate_body(file_body())
    db.objects[(FinancialFormDraft, family.id)] = draft
    with patch.object(ws, "existing_family", AsyncMock(return_value=family)), patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=file_cockpit())):
        result = await service.summary(db, profile)
    assert (result["has_plan"], result["model"], result["state"]) == (True, "operating-plan-v2", "working_draft")
    assert (result["expense_count"], result["action_count"], result["reviewed_action_count"], result["missing_count"]) == (14, 11, 0, 8)
    # The file page's strip and plan card read these.
    assert result["evaluation"]["comparison"]["proposed"]["earnings"] == 182896.64 and result["evaluation"]["comparison"]["proposed"]["dscr"] == 1.03
    assert {row["category"] for row in result["cost_mix"]} >= {"cogs", "rent", "salaries_and_wages"}
    assert db.added == db.versions == []


@pytest.mark.asyncio
async def test_plan_routes_are_for_staff_take_only_the_v2_model_and_never_trust_posted_reviews():
    from app.routers import operating_plan as routes

    profile_id, actor = uuid4(), SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    profile = SimpleNamespace(id=profile_id)
    cockpit = file_cockpit()
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), \
         patch.object(service, "current_body", AsyncMock(return_value=v1_schema.empty_body())), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)):
        result = await routes.plan_evaluate(profile_id, routes.PlanBody(body=file_body(keep_design_statuses=True)), actor, Memory())
        assert result["evaluation"]["comparison"]["proposed"]["earnings"] == 182896.64 and result["completion"]["missing_count"] == 8
        assert all(row["status"] == "proposed" for row in result["body"]["changes"])
        with pytest.raises(HTTPException) as error:
            await routes.plan_evaluate(profile_id, routes.PlanBody(body=v1_schema.empty_body()), actor, Memory())
        assert error.value.status_code == 422
        synced = await routes.plan_resync(profile_id, routes.PlanBody(body={**file_body(), "source": {"stamp": "0" * 64, "confirmations": {"rent": "confirmed"}, "corrections": {}}}), actor, Memory())
        assert synced["body"]["source"]["confirmations"] == {} and synced["evaluation"]["plan"]["source_changed"] is False
    for call in (routes.plan_workspace(profile_id, SimpleNamespace(role=Role.CLIENT), Memory()),
                 routes.plan_evaluate(profile_id, routes.PlanBody(body=file_body()), SimpleNamespace(role=Role.CLIENT), Memory()),
                 routes.plan_save_draft(profile_id, routes.PlanDraft(body=file_body()), SimpleNamespace(role=Role.LENDER), Memory())):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_the_first_save_creates_the_plan_and_a_stale_tab_is_told_to_reload():
    from app.routers import operating_plan as routes

    profile, family, draft = context("operating_plan")
    draft.revision = 0
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = Memory()
    db.commit = AsyncMock()
    staged = AsyncMock()
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(ws, "stage_draft", staged), patch.object(plan_service, "workspace", AsyncMock(return_value={"origin": "saved"})) as read:
        assert await routes.plan_save_draft(profile.id, routes.PlanDraft(body=file_body()), actor, db) == {"origin": "saved"}
        assert staged.await_args.args[4]["schema_version"] == "operating-plan-v2" and read.await_args.kwargs["family_id"] == family.id
        draft.revision = 3
        with pytest.raises(HTTPException) as error:
            await routes.plan_save_draft(profile.id, routes.PlanDraft(body=file_body()), actor, db)
        assert error.value.status_code == 409 and error.value.detail["current_revision"] == 3
        with pytest.raises(HTTPException) as error:
            await routes.plan_save_draft(profile.id, routes.PlanDraft(body=file_body(), expected_revision=2), actor, db)
        assert error.value.status_code == 409
        await routes.plan_save_draft(profile.id, routes.PlanDraft(body=file_body(), expected_revision=3), actor, db)
    assert staged.await_count == 2


# ---------------------------------------------------------------------------
# The client link: the same plan, without staff review detail
# ---------------------------------------------------------------------------

PRIVATE = {"reviewed_by_user_id", "reviewed_at", "review_reason", "review_stamp", "baseline_treatment", "baseline_reconciliation_note"}


@pytest.mark.asyncio
async def test_a_client_link_loads_the_plan_and_its_baseline_without_who_reviewed_or_why():
    cockpit = file_cockpit(goal=215_000)
    profile, family, draft = context("operating_plan")
    draft.body = approve(file_body(), cockpit)
    db = Memory()
    db.objects[(FinancialFormDraft, family.id)] = draft
    from app.models.financial_form_version import FinancialFormFamily, FinancialFormVersion

    db.objects[(FinancialFormFamily, family.id)] = family
    with_pdf = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=2, schema_version="operating-plan-v2", body=draft.body, key_facts={},
                                    body_sha256="b", pdf_sha256="p", source="form", status="published", pdf_file_id=uuid4(), actor_name="Desk", created_at=ws.now())
    original = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=1, schema_version="operating-plan-v1", body={}, key_facts={},
                                    body_sha256="b", pdf_sha256="p", source="original", status="original", pdf_file_id=None, actor_name="Client upload", created_at=ws.now())
    db.versions = [original, with_pdf]
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)):
        seen = await plan_service.client_workspace(db, profile, family_id=family.id, pdf_base="/api/v1/x/versions", can_edit=True, completed=False)
    assert set(seen) == {"kind", "model", "origin", "family_id", "goal", "context_stamp", "stream_templates", "draft", "current_version_id",
                         "body", "evaluation", "versions", "can_edit", "completed"}
    rent = change(seen["body"], "l:rent")
    assert rent["status"] == "approved" and not PRIVATE & set(rent)
    # Only what the page cannot work out itself: the baseline to compute from, and what the server alone knows.
    assert set(seen["evaluation"]) == {"context_stamp", "plan"} and set(seen["evaluation"]["plan"]) == set(plan_service.CLIENT_PLAN_KEYS)
    assert seen["evaluation"]["plan"]["baseline"]["ebitda"] == -22377 and seen["evaluation"]["plan"]["baseline"]["debt_service_with_terms"] == 177600
    assert [row["key"] for row in seen["evaluation"]["plan"]["questions"]] == ["payroll_taxes", "transportation"]
    assert str(STAFF.id) not in str(seen) and "Lease and price list checked." not in str(seen)
    assert (seen["goal"]["amount"], seen["draft"]["revision"], seen["can_edit"], seen["completed"]) == (215_000, 4, True, False)
    assert [(row["version_number"], row.get("pdf_url"), "actor_name" in row) for row in seen["versions"]] == [
        (2, f"/api/v1/x/versions/{with_pdf.id}/pdf", False), (1, None, False)]


@pytest.mark.asyncio
async def test_a_client_draft_checks_the_revision_and_never_writes_a_review():
    from app.tests.test_earnings_plan_migrate import old_plan

    profile, family, draft = context("operating_plan")
    draft.body = old_plan()
    staged = AsyncMock()
    posted = file_body(keep_design_statuses=True)
    change(posted, "l:rent")["review_reason"] = "the client wrote this"
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "stage_draft", staged):
        for revision in (None, 3):
            with pytest.raises(HTTPException) as error:
                await plan_service.stage_client_draft(Memory(), profile, family_id=family.id, incoming=posted, expected_revision=revision)
            assert error.value.status_code == 409 and error.value.detail == {
                "code": "financial_draft_conflict", "current_revision": 4, "message": "This plan changed or needs to be refreshed. Reload before saving your changes."}
        with pytest.raises(HTTPException) as error:
            await plan_service.stage_client_draft(Memory(), profile, family_id=family.id, incoming=v1_schema.empty_body(), expected_revision=4)
        assert error.value.status_code == 422
        with pytest.raises(HTTPException) as error:
            await plan_service.stage_client_draft(Memory(), profile, family_id=family.id, incoming={**posted, "goal": {"amount": 1}}, expected_revision=4)
        assert error.value.status_code == 422 and error.value.detail["code"] == "operating_goal_read_only"
        assert staged.await_count == 0
        # The first save from the new screens replaces the earlier model; nothing the page says about reviews is kept.
        assert await plan_service.stage_client_draft(Memory(), profile, family_id=family.id, incoming=posted, expected_revision=4) is family
        saved = staged.await_args.args[4]
        assert saved["schema_version"] == "operating-plan-v2" and all(row["status"] == "proposed" and row["review_reason"] == "" for row in saved["changes"])
        # Saving what is already stored writes nothing, so the revision does not move under another tab.
        draft.body = saved
        await plan_service.stage_client_draft(Memory(), profile, family_id=family.id, incoming=plan_service.client_body(saved), expected_revision=4)
    assert staged.await_count == 1


@pytest.mark.asyncio
async def test_a_client_can_bring_the_plan_onto_changed_source_figures():
    from app.tests.test_financial_baseline import filed_return

    current = approve(file_body(), file_cockpit())
    amended = file_cockpit(analyses=[filed_return(rent=200000, total_deductions=668086, ordinary_business_income=-25572)])
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=amended)):
        result = await plan_service.client_resync(Memory(), SimpleNamespace(id=uuid4()), plan_service.client_body(current), current=current)
        with pytest.raises(HTTPException) as error:
            await plan_service.client_resync(Memory(), SimpleNamespace(id=uuid4()), v1_schema.empty_body(), current=current)
    assert error.value.status_code == 422
    assert set(result) == {"body", "evaluation", "context_stamp"} and result["evaluation"]["plan"]["source_changed"] is False
    assert next(line["base"] for line in result["body"]["lines"] if line["id"] == "rent") == 200000
    assert result["body"]["source"]["confirmations"] == {}
    # The lines moved, so no earlier approval stands; and the client still sees no review detail.
    assert all(row["status"] == "proposed" and not PRIVATE & set(row) for row in result["body"]["changes"])


@pytest.mark.asyncio
async def test_client_plan_routes_open_only_an_earnings_plan_link_and_respect_read_only():
    from app.routers import application_profiles as routes

    profile, family, draft = context("operating_plan")
    link = SimpleNamespace(kind="operating_plan", permission="edit", profile_id=profile.id, financial_family_id=family.id, completed_at=None,
                           invitee_email="owner@example.test", last_used_at=None)
    db = Memory()
    db.commit = AsyncMock()
    from app.models.application_profile import ApplicationProfile

    db.objects[(ApplicationProfile, profile.id)] = profile
    opened = AsyncMock(return_value={"origin": "saved"})
    staged = AsyncMock(return_value=family)
    with patch.object(routes, "_open_link_or_404", AsyncMock(return_value=link)), patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(plan_service, "client_workspace", opened), patch.object(plan_service, "stage_client_draft", staged):
        assert await routes.public_earnings_plan("tok", db) == {"origin": "saved"}
        assert opened.await_args.kwargs == {"family_id": family.id, "pdf_base": "/api/v1/application-profiles/public/financial-forms/tok/versions",
                                            "can_edit": True, "completed": False}
        assert link.last_used_at is not None and db.commit.await_count == 1
        await routes.save_public_earnings_plan_draft("tok", routes.PublicEarningsPlanDraft(body=file_body(), expected_revision=4), db)
        assert staged.await_args.kwargs["expected_revision"] == 4 and staged.await_args.kwargs["actor"].id is None
        assert staged.await_args.kwargs["actor"].name == "owner@example.test"
        link.permission = "view"
        assert (await routes.public_earnings_plan("tok", db)) == {"origin": "saved"} and opened.await_args.kwargs["can_edit"] is False
        with pytest.raises(HTTPException) as error:
            await routes.save_public_earnings_plan_draft("tok", routes.PublicEarningsPlanDraft(body=file_body(), expected_revision=4), db)
        assert error.value.status_code == 403 and staged.await_count == 1
        link.kind = "debt_schedule"
        for call in (routes.public_earnings_plan("tok", db),
                     routes.save_public_earnings_plan_draft("tok", routes.PublicEarningsPlanDraft(body=file_body(), expected_revision=4), db)):
            with pytest.raises(HTTPException) as error:
                await call
            assert error.value.status_code == 404
