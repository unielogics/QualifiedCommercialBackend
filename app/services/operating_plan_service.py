"""One versioned operating plan, with server-owned staff review decisions."""

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select

from app.models.file_event import NOTICE_NONE, VISIBILITY_DESK, FileEvent
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormVersion,
)
from app.schemas.earnings_improvement import REVIEW_FIELDS, validate_body
from app.services import earnings_improvement as math

REVIEW_KIND = "operating_plan.adjustments_reviewed"
UPGRADED = {"code": "operating_plan_upgraded", "message": "This plan was moved to the new earnings plan. Reload it before saving."}


def is_v2(body):
    from app.schemas import earnings_plan_v2

    return earnings_plan_v2.is_v2(body)


def linked_file_ids(body):
    """Every supporting file a plan body links, whichever model it is in."""
    if is_v2(body):
        from app.services import earnings_plan_service

        return earnings_plan_service.source_file_ids(body)
    return {str(fid) for block in ("expenses", "adjustments", "historical_corrections")
            for row in (body or {}).get(block, []) for fid in row.get("source_file_ids", [])}


def completion(body, cockpit):
    if is_v2(body):
        from app.services import earnings_plan_service

        return earnings_plan_service.completion(body, cockpit)
    from app.schemas.earnings_improvement import completion as disclosure_completion
    from app.services.released_cash_flow import evaluate

    progress = disclosure_completion(body)
    from app.services.operating_source_review import evaluate as source_review

    source_progress = source_review(validate_body(body), cockpit)
    if source_progress["active"]:
        progress["missing"] = list(dict.fromkeys([*progress["missing"], *source_progress["missing"]]))
    from app.services.operating_plan_commands import itemization_missing

    progress["missing"] = list(dict.fromkeys([*progress["missing"], *itemization_missing(validate_body(body), cockpit)]))
    allocation = evaluate(body, cockpit)
    tracked = math.evaluate(body, cockpit) if (cockpit.get("_operating_goal") or {}).get("status") == "set" else None
    tracking = tracked.get("cash_bridge") if tracked else None
    if tracked:
        allocation = tracked["released_cash_flow"]
    funding_required = tracking.get("funding_required", tracking["required"]) if tracking else allocation["required"]
    funding_missing = tracking.get("funding_missing", tracking["missing"]) if tracking else allocation["missing"]
    missing = [*progress["missing"], *(funding_missing if funding_required else [])]
    return {**progress, "complete": not missing, "missing": missing, "missing_count": len(missing),
            "released_cash_flow": allocation, **({"cash_bridge": tracking} if tracking else {})}


def shareable_package(evaluation, evidence):
    result = math.package_summary(evaluation)
    names = {row["file_id"]: row.get("file_name") for row in (evidence or {}).get("files", [])}
    for action in result["adjustments"]:
        action["source_names"] = [names[fid] for fid in action.get("source_file_ids", []) if names.get(fid)]
    return result


def protect_reviews(incoming, current, *, trusted=False):
    """Ordinary writers may retain an unchanged review, never author one."""
    if is_v2(incoming):
        from app.services import earnings_plan_service

        return earnings_plan_service.protect_reviews(incoming, current, trusted=trusted)
    if trusted:
        return validate_body(incoming)
    if not isinstance(incoming, dict):
        return validate_body(incoming)
    if is_v2(current):
        raise HTTPException(409, UPGRADED)
    old = validate_body(current)
    body = deepcopy(incoming)
    # Removing a row is recoverable; its prior proposal and evidence stay in history.
    for block in ("expenses", "adjustments"):
        rows = body.get(block, []) if isinstance(body, dict) else []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return validate_body(body)
        incoming_ids = {str(row.get("id")) for row in rows}
        rows.extend({**deepcopy(row), "excluded": True} for row in old[block] if row["id"] not in incoming_ids)
        excluded = {str(row.get("id")) for row in rows if row.get("excluded")}
        for row in rows:
            if str(row.get("duplicate_of")) in excluded:
                row.update(excluded=True, duplicate_of=None)
        body[block] = rows
    body = validate_body(body)
    if (old.get("source_group_confirmations") or old.get("source_group_confirmation_stamp")) and not (body.get("source_group_confirmations") or body.get("source_group_confirmation_stamp")):
        body["source_group_confirmations"] = deepcopy(old["source_group_confirmations"])
        body["source_group_confirmation_stamp"] = old["source_group_confirmation_stamp"]
    old_actions = {row["id"]: row for row in old["adjustments"]}
    old_expenses = {row["id"]: row for row in old["expenses"]}
    expenses = {row["id"]: row for row in body["expenses"]}
    defaults = {"status": "proposed", "reviewed_by_user_id": None, "reviewed_at": None,
                "review_reason": "", "review_source_stamp": None, "baseline_treatment": "unreconciled",
                "baseline_reconciliation_note": ""}
    for action in body["adjustments"]:
        prior = old_actions.get(action["id"])
        same = prior is not None and math.proposal_stamp(action, expenses.get(action.get("expense_row_id")), body) == math.proposal_stamp(
            prior, old_expenses.get(prior.get("expense_row_id")), old)
        for key in REVIEW_FIELDS:
            action[key] = deepcopy(prior[key] if same else defaults[key])
    return validate_body(body)


async def prepare_draft(db, profile, *, incoming, current, actor=None):
    from app.services.financial_workspace import filter_current_files, source_files_for_profile

    if is_v2(current) and not is_v2(incoming):
        # An editor still holding the earlier model must not write over the plan it was carried into.
        raise HTTPException(409, UPGRADED)
    body = protect_reviews(incoming, current, trusted=bool(db.info.get("operating_plan_review_write")))
    ids = linked_file_ids(body)
    files = await filter_current_files(db, await source_files_for_profile(db, profile)) if ids else []
    permitted = {str(file.id) for file in files}
    previous_ids = linked_file_ids(current)
    if ids - permitted - previous_ids:
        raise HTTPException(422, "Link only current supporting files belonging to this application")
    return body


async def preview(db, profile, body, *, goal=None):
    from app.services.financial_cockpit import load_cockpit

    cockpit = await load_cockpit(db, profile)
    if goal is not None:
        cockpit = {**cockpit, "_operating_goal": goal}
    evidence = cockpit.get("operating_evidence") or {}
    evaluation = math.evaluate(body, cockpit, source_file_ids=[row["file_id"] for row in evidence.get("files", [])], include_expense_changes=True)
    return {"evaluation": evaluation, "evidence": evidence, "guidance": evidence.get("guidance") or {},
            "goal": cockpit.get("_operating_goal"), "context_stamp": math.context_stamp(cockpit)}


async def client_preview(db, profile, body, *, current):
    """Read-only scenario for the token's form, with server-owned review state."""
    from app.services.operating_plan_goals import reject_client_goal, require_goal
    from app.services.operating_plan_guidance import client_evaluation, client_evidence

    goal = await require_goal(db, profile)
    reject_client_goal(body)
    protected = protect_reviews(body, current)
    result = await preview(db, profile, protected, goal=goal)
    evidence = client_evidence(result["evidence"])
    permitted = {row["file_id"] for row in evidence["files"]}
    retained = linked_file_ids(current)
    supplied = linked_file_ids(protected)
    if supplied - permitted - retained:
        raise HTTPException(422, "Use supporting sources available to this operating form")
    return {"evaluation": client_evaluation(result["evaluation"]), "evidence": evidence,
            "guidance": evidence.get("guidance") or {}, "goal": result.get("goal")}


async def current_body(db, profile, family_id=None):
    """Read the selected saved draft without materializing another workspace."""
    from app.schemas.earnings_improvement import empty_body
    from app.services.financial_workspace import existing_family

    family = await db.get(FinancialFormFamily, family_id) if family_id else await existing_family(db, profile.id, "operating_plan")
    if family is None:
        if family_id:
            raise HTTPException(404, "Operating plan not found")
        return empty_body()
    if family.profile_id != profile.id or family.kind != "operating_plan":
        raise HTTPException(404, "Operating plan not found")
    draft = await db.get(FinancialFormDraft, family.id)
    version = await db.get(FinancialFormVersion, family.current_version_id) if family.current_version_id else None
    return draft.body if draft else version.body if version else empty_body()


async def suggest(db, profile, body, *, current, expense_row_id, actor_id=None,
                  client=False, suggestion_id=None, context_stamp=None):
    """A scoped proposed-body response; applying never saves or approves it."""
    from app.services import operating_plan_suggestions
    from app.services.financial_cockpit import load_cockpit
    from app.services.operating_plan_goals import reject_client_goal, require_goal
    from app.services.operating_plan_guidance import client_evaluation, client_evidence

    if is_v2(body) or is_v2(current):
        raise HTTPException(409, {"code": "not_available_for_this_plan", "message": "Suggestions belong to the earlier plan editor. Reload the plan."})
    goal = await require_goal(db, profile)
    if client:
        reject_client_goal(body)
    protected = protect_reviews(body, current)
    cockpit = await load_cockpit(db, profile)
    cockpit = {**cockpit, "_operating_goal": goal}
    if client:
        # Public links retain the same source boundary as their preview. A
        # private quote or offer must not become a new recommendation channel.
        evidence = client_evidence(cockpit.get("operating_evidence"))
        allowed = {row["file_id"] for row in evidence["files"]}
        retained = {str(fid) for block in ("expenses", "adjustments", "historical_corrections")
                    for row in (current or {}).get(block, []) for fid in row.get("source_file_ids", [])}
        supplied = {str(fid) for block in ("expenses", "adjustments", "historical_corrections")
                    for row in protected[block] for fid in row.get("source_file_ids", [])}
        if supplied - allowed - retained:
            raise HTTPException(422, "Use supporting sources available to this operating form")
        original = cockpit.get("operating_evidence") or {}
        visible = {**original, **evidence, "files": [row for row in original.get("files", []) if row["file_id"] in allowed],
                   "cost_quotes": [row for row in original.get("cost_quotes", []) if str(row.get("file_id")) in allowed]}
        cockpit["operating_evidence"] = visible
    if suggestion_id is None:
        return await operating_plan_suggestions.prepare(db, body=protected, cockpit=cockpit, goal=goal,
            expense_row_id=expense_row_id, actor_id=actor_id)
    result = operating_plan_suggestions.apply(protected, cockpit, goal, expense_row_id, suggestion_id, context_stamp)
    if client:
        from app.services.client_financial_forms import client_body

        result = {**result, "body": client_body("operating_plan", result["body"]),
                  "evaluation": client_evaluation(result["evaluation"])}
    return result


async def summary(db, profile, *, family_id=None):
    """Inspect the saved draft without creating a family, request or publication."""
    from app.schemas.earnings_improvement import empty_body
    from app.services.financial_cockpit import load_cockpit
    from app.services.financial_workspace import existing_family

    family = await db.get(FinancialFormFamily, family_id) if family_id else await existing_family(db, profile.id, "operating_plan")
    if family is not None and (family.profile_id != profile.id or family.kind != "operating_plan"):
        raise HTTPException(404, "Operating plan not found")
    if family_id and family is None:
        raise HTTPException(404, "Operating plan not found")
    from app.services import operating_plan_schema

    draft = await db.get(FinancialFormDraft, family.id) if family else None
    version = await db.get(FinancialFormVersion, family.current_version_id) if family and family.current_version_id else None
    body = operating_plan_schema.validate(draft.body if draft else version.body if version else empty_body())
    cockpit = await load_cockpit(db, profile)
    evaluation = math.evaluate(body, cockpit, include_expense_changes=True)
    progress = completion(body, cockpit)
    disclosure = evaluation["disclosure"]
    stamp = draft.updated_at if draft else version.created_at if version else None
    return {"has_plan": bool(family), "model": body["schema_version"],
        "state": "working_draft" if draft and (version is None or body != operating_plan_schema.validate(version.body)) else "published" if version else "not_started",
        "family_id": str(family.id) if family else None,
        "expense_count": progress["active_expenses"], "action_count": progress["active_adjustments"],
        "reviewed_action_count": sum(row["review_state"] in {"approved", "rejected"} for row in evaluation["actions"] if not row["excluded"]),
        "missing_count": progress["missing_count"], "complete": progress["complete"],
        "cost_mix": disclosure["cost_mix"], "disclosure": disclosure, "evaluation": evaluation,
        "goal": cockpit.get("_operating_goal"),
        "version_number": version.version_number if version else None,
        "version_id": str(version.id) if version else None,
        "pdf_file_id": str(version.pdf_file_id) if version and version.pdf_file_id else None,
        "updated_at": stamp.isoformat() if stamp else None,
        "recommendation": evaluation["recommendation"]}


async def review(db, profile, actor, *, family_id, expected_revision, action_ids, decision, reason,
                 context_stamp, idempotency_key, baseline_treatment="unreconciled", baseline_reconciliation_note=""):
    from app.services import financial_workspace as workspace
    from app.services.financial_cockpit import load_cockpit

    family, draft = await workspace.resolve_family(db, profile, kind="operating_plan", family_id=family_id, actor=actor)
    payload_stamp = workspace.body_hash({"family_id": str(family.id), "revision": expected_revision,
        "action_ids": sorted(str(value) for value in action_ids), "decision": decision, "reason": reason,
        "context_stamp": context_stamp, "baseline_treatment": baseline_treatment,
        "baseline_reconciliation_note": baseline_reconciliation_note})
    events = (await db.execute(select(FileEvent).where(FileEvent.profile_id == profile.id,
        FileEvent.kind == REVIEW_KIND, FileEvent.target_id == str(family.id)))).scalars().all()
    previous = next((event for event in events if (event.meta or {}).get("idempotency_key") == idempotency_key), None)
    if previous:
        if previous.meta.get("request_stamp") != payload_stamp:
            raise HTTPException(409, "This review retry key belongs to another request")
        result = await workspace.load_workspace(db, profile, kind="operating_plan", family_id=family.id, actor=actor)
        return {**result, **await preview(db, profile, draft.body), "replayed": True}
    workspace._check_revision(draft, expected_revision)
    cockpit = await load_cockpit(db, profile)
    if math.context_stamp(cockpit) != context_stamp:
        raise HTTPException(409, "Financial evidence or loan inputs changed. Refresh the preview before reviewing.")
    body = math.review_actions(draft.body, cockpit, action_ids, decision, reason, actor,
        baseline_treatment=baseline_treatment, baseline_reconciliation_note=baseline_reconciliation_note,
        source_file_ids=[row["file_id"] for row in (cockpit.get("operating_evidence") or {}).get("files", [])])
    previous_trust = db.info.get("operating_plan_review_write")
    db.info["operating_plan_review_write"] = True
    try:
        await workspace.stage_draft(db, profile, family, draft, body, actor=actor)
    finally:
        if previous_trust is None:
            db.info.pop("operating_plan_review_write", None)
        else:
            db.info["operating_plan_review_write"] = previous_trust
    db.add(FileEvent(profile_id=profile.id, kind=REVIEW_KIND, visibility=VISIBILITY_DESK, actor_user_id=actor.id,
        title="Operating adjustments reviewed", target_type="financial_form_family", target_id=str(family.id),
        meta={"idempotency_key": idempotency_key, "request_stamp": payload_stamp, "decision": decision,
              "action_ids": [str(value) for value in action_ids], "draft_revision": draft.revision}, notice_status=NOTICE_NONE))
    await db.flush()
    result = await workspace.load_workspace(db, profile, kind="operating_plan", family_id=family.id, actor=actor)
    return {**result, "evaluation": math.evaluate(body, cockpit), "evidence": cockpit.get("operating_evidence"),
            "context_stamp": math.context_stamp(cockpit), "replayed": False}


async def published_context(db, profile, cockpit):
    """Read only: reading the cockpit must not create a plan or promote a proposal."""
    from app.services.financial_workspace import existing_family

    family = await existing_family(db, profile.id, "operating_plan")
    version = await db.get(FinancialFormVersion, family.current_version_id) if family and family.current_version_id else None
    if version is None or not version.body:
        from app.schemas.earnings_improvement import empty_body

        evaluation = math.evaluate(empty_body(), cockpit)
        return {"available": False, "state": "not_started", "requirement_key": "operating_expenses_and_earnings_plan",
            "evaluation": evaluation, "package": shareable_package(evaluation, cockpit.get("operating_evidence"))}
    body = version.body
    if version.status == "client_submitted":
        from app.schemas.earnings_improvement import empty_body
        body = protect_reviews(body, empty_body())
    evaluation = math.evaluate(body, cockpit)
    return {"available": True, "state": "client_submitted" if version.status == "client_submitted" else "published",
        "family_id": str(family.id), "version_id": str(version.id), "version_number": version.version_number,
        "saved_at": version.created_at.isoformat(), "pdf_file_id": str(version.pdf_file_id) if version.pdf_file_id else None,
        "evaluation": evaluation, "package": shareable_package(evaluation, cockpit.get("operating_evidence"))}
