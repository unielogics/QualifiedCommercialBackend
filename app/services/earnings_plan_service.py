"""Rules for a v2 earnings plan body: reviews, the one save gate, what a client may see.

The v1 plan and the v2 plan share one form kind (`operating_plan`), one draft
and one version history. `app/services/operating_plan_service.py` and the
financial workspace call in here whenever the body in hand is v2.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.schemas import earnings_plan_v2 as schema
from app.services import earnings_plan_envelope as envelope

GATE_CODE = "earnings_plan_incomplete"
PRIVATE_REVIEW_FIELDS = tuple(key for key in schema.REVIEW_FIELDS if key != "status")


def source_file_ids(body: dict) -> set[str]:
    return {str(file_id) for _block, row in schema.items(body) for file_id in row.get("source_file_ids") or []}


def protect_reviews(incoming: Any, current: Any, *, trusted: bool = False) -> dict:
    """An ordinary writer keeps a review only on a change it left as it was; it never writes one."""
    body = schema.validate_body(incoming)
    if trusted:
        return body
    old = schema.validate_body(current) if schema.is_v2(current) else schema.empty_body()
    prior = {str(row["id"]): (block, row) for block, row in schema.items(old)}
    for block, row in schema.items(body):
        before = prior.get(str(row["id"]))
        same = (before is not None and before[0] == block
                and envelope.proposal_stamp(block, row, body) == envelope.proposal_stamp(block, before[1], old))
        for key in schema.REVIEW_FIELDS:
            row[key] = deepcopy(before[1][key]) if same else deepcopy(schema.REVIEW_DEFAULTS[key])
    return schema.validate_body(body)


def completion(body: dict, cockpit: dict) -> dict:
    """What is left before a version can be saved: the plan's own to-finish list, nothing else."""
    evaluation = envelope.evaluate(body, cockpit)
    return _completion(body, evaluation)


def _completion(body: dict, evaluation: dict) -> dict:
    items = evaluation["plan"]["to_finish"]
    return {"complete": not items, "missing": [row["label"] for row in items], "missing_count": len(items), "to_finish": items,
            "active_expenses": len(body.get("lines") or []),
            "active_adjustments": sum(row.get("included") is not False for _block, row in schema.items(body)),
            "released_cash_flow": evaluation["released_cash_flow"], "warnings": evaluation["plan"]["warnings"]}


def require_complete(body: dict, evaluation: dict) -> None:
    """The only reason a v2 plan cannot be saved as a version. The draft is always kept."""
    items = evaluation["plan"]["to_finish"]
    if items:
        raise HTTPException(422, {"code": GATE_CODE, "to_finish": items, "missing": [row["label"] for row in items],
                                  "message": f"{len(items)} item{'s' if len(items) != 1 else ''} to finish before saving a version. Your working draft is retained."})


def review(raw: Any, cockpit: dict, item_ids, decision: str, reason: str, actor, *, baseline_treatment: str = "unreconciled",
           baseline_reconciliation_note: str = "", now: datetime | None = None) -> dict:
    """Approve, reject or reopen changes. An approval is given to the figures as they stand and to the source they rest on."""
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required to review plan changes")
    if decision not in {"approved", "rejected", "proposed"} or not reason.strip() or len(reason) > 2000:
        raise HTTPException(422, "Choose a valid review decision and provide a review reason")
    if baseline_treatment not in {"unreconciled", "included_in_baseline", "incremental_to_baseline", "outside_operating_earnings"}:
        raise HTTPException(422, "Choose how the change reconciles to the current baseline")
    body = schema.validate_body(raw)
    ids = [str(UUID(str(value))) for value in item_ids]
    known = {str(row["id"]): (block, row) for block, row in schema.items(body)}
    if not ids or len(ids) != len(set(ids)) or set(ids) - set(known):
        raise HTTPException(422, "Choose each existing change once")
    baseline = envelope.engine_baseline(cockpit)
    stamp_time = now or datetime.now(UTC)
    for identifier in ids:
        block, row = known[identifier]
        if row.get("included") is False:
            raise HTTPException(422, "Switch a change back on before reviewing it")
        if decision == "approved":
            if not baseline["available"]:
                raise HTTPException(422, "Select an earnings source for the file before approving plan changes")
            if not str(row.get("note") or "").strip():
                raise HTTPException(422, {"message": "Each change needs its explanation before it can be approved",
                                          "items": [{"id": identifier, "issues": ["Explain why this change is reasonable."]}]})
            if baseline_treatment not in {"included_in_baseline", "incremental_to_baseline"} or not baseline_reconciliation_note.strip():
                raise HTTPException(422, "Say how the change reconciles to the current baseline before approving it")
        if decision == "proposed":
            row.update(deepcopy(schema.REVIEW_DEFAULTS))
            continue
        row.update(status=decision, reviewed_by_user_id=str(actor.id), reviewed_at=stamp_time.isoformat(), review_reason=reason.strip(),
                   baseline_treatment=baseline_treatment, baseline_reconciliation_note=baseline_reconciliation_note.strip())
        row["review_stamp"] = envelope.review_stamp(block, row, body, baseline)
    return schema.validate_body(body)


def client_body(body: dict) -> dict:
    """The plan as the business sees it: its own proposals and their status, never who reviewed or why."""
    safe = deepcopy(body)
    for _block, row in schema.items(safe):
        for key in PRIVATE_REVIEW_FIELDS:
            row.pop(key, None)
    return safe


def merge_client_body(current: dict, incoming: Any) -> dict:
    """A client writes the whole plan except staff review fields, which `protect_reviews` settles."""
    if not isinstance(incoming, dict):
        raise HTTPException(422, "Review the earnings plan fields")
    merged = deepcopy(incoming)
    merged["schema_version"] = schema.SCHEMA_VERSION
    for _block, row in schema.items(merged):
        if isinstance(row, dict):
            for key in PRIVATE_REVIEW_FIELDS:
                row.pop(key, None)
    return protect_reviews(merged, current)


def version_facts(facts: dict, body: dict, cockpit: dict, evaluation: dict, *, client_submission: bool, goal: dict | None) -> dict:
    """What a saved v2 version records beside its body, for the package and the file's readers."""
    from app.services import operating_plan_service

    facts["improvement_plan"] = operating_plan_service.shareable_package(evaluation, cockpit.get("operating_evidence"))
    facts["evidence_sources"] = [{key: row.get(key) for key in ("file_id", "file_name", "classification")}
                                 for row in (cockpit.get("operating_evidence") or {}).get("files", [])]
    facts["proposal_status"] = "client_submitted" if client_submission else "staff_saved"
    facts["plan_model"] = envelope.RESULT_VERSION
    facts["plan_result"] = plan_result(evaluation)
    if (goal or {}).get("status") == "set":
        facts["operating_goal"] = goal
        facts["goal_progress"] = evaluation.get("goal_progress")
        facts["cash_bridge"] = evaluation.get("cash_bridge")
    return facts


def plan_result(evaluation: dict) -> dict:
    """The engine's figures a saved version keeps: enough to redraw its document without recomputing."""
    plan = evaluation["plan"]
    return {key: deepcopy(plan[key]) for key in ("model", "rates", "behaviors", "items", "proposed", "reviewed", "months", "first_year", "effects",
                                                 "sections", "bridge", "metrics", "debt_service", "target", "goal", "released", "baseline",
                                                 "questions", "labels", "source_stamp")}


async def workspace(db, profile, *, family_id=None, actor=None) -> dict:
    """What the plan screens load: the plan in the v2 model, its figures, and where the draft stands.

    Reading never writes. A file whose saved plan is still v1 gets it carried
    over in memory; it becomes v2 on the first save.
    """
    from app.models.financial_form_version import (
        FinancialFormDraft,
        FinancialFormFamily,
        FinancialFormVersion,
    )
    from app.services import earnings_plan_migrate as migrate
    from app.services.financial_cockpit import load_cockpit
    from app.services.financial_workspace import _version_read, body_hash, existing_family

    family = await db.get(FinancialFormFamily, family_id) if family_id else await existing_family(db, profile.id, "operating_plan")
    if family is not None and (family.profile_id != profile.id or family.kind != "operating_plan"):
        raise HTTPException(404, "Operating plan not found")
    if family_id and family is None:
        raise HTTPException(404, "Operating plan not found")
    draft = await db.get(FinancialFormDraft, family.id) if family else None
    current = await db.get(FinancialFormVersion, family.current_version_id) if family and family.current_version_id else None
    stored = draft.body if draft else current.body if current else None
    cockpit = await load_cockpit(db, profile)
    if stored is None:
        body, origin = migrate.seed(cockpit), "seeded"
    elif schema.is_v2(stored):
        body, origin = schema.validate_body(stored), "saved"
    else:
        body, origin = migrate.v1_to_v2(stored, cockpit), "carried_over"
    evaluation = envelope.evaluate(body, cockpit)
    versions = []
    if family is not None:
        rows = (await db.execute(select(FinancialFormVersion).where(FinancialFormVersion.family_id == family.id))).scalars().all()
        versions = [_version_read(row) for row in sorted(rows, key=lambda row: row.version_number, reverse=True)]
    return {
        "kind": "operating_plan", "model": schema.SCHEMA_VERSION, "origin": origin,
        "family_id": str(family.id) if family else None, "body": body, "evaluation": evaluation,
        "completion": _completion(body, evaluation), "goal": cockpit.get("_operating_goal"),
        "context_stamp": evaluation["context_stamp"], "stream_templates": schema.STREAM_TEMPLATES,
        "draft": {"revision": draft.revision, "base_version_id": str(draft.base_version_id) if draft.base_version_id else None,
                  "updated_at": draft.updated_at.isoformat(), "stored_model": stored.get("schema_version") if isinstance(stored, dict) else None,
                  "dirty": current is None or body_hash(draft.body) != current.body_sha256} if draft else None,
        "current_version_id": str(family.current_version_id) if family and family.current_version_id else None,
        "versions": versions,
    }
