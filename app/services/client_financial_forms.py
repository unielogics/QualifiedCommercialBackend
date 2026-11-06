"""Scoped client corrections and explicit immutable form publication.

The caller proves link/account scope before entering this service. Document
origin describes evidence; it does not stop a client correcting their form.
Only schema-defined financial answers cross this boundary, never staff controls.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.models.financial_form_version import FinancialFormVersion
from app.services import business_statement_schema as bss
from app.services import financial_workspace as workspace
from app.services import pfs_schema, real_estate_schema
from app.services.financial_workspace_legacy import merge_source_debts


async def ensure_snapshot(db, profile, kind: str, *, family_id: UUID | None = None,
                          statement_id: UUID | None = None, actor: Any = None) -> dict:
    await workspace.lock_financial_profile(db, profile)
    if kind == "pfs" and family_id is None and statement_id is None and len(await workspace._families(db, profile.id, kind)) > 1:
        raise HTTPException(409, "Choose the intended owner's personal financial statement before sharing this form")
    family, _draft = await workspace.resolve_family(db, profile, kind=kind, family_id=family_id,
        statement_id=statement_id, actor=actor)
    if kind == "pfs" and statement_id is not None and str((family.identity or {}).get("statement_id") or "") != str(statement_id):
        raise HTTPException(404, "Financial statement not found")
    return await workspace.load_workspace(db, profile, kind=kind, family_id=family.id, actor=actor)


def workspace_metadata(snapshot: dict, *, pdf_base: str | None = None) -> dict:
    def version_read(version):
        result = {key: version.get(key) for key in ("id", "version_number", "created_at", "source", "status")}
        if pdf_base and version.get("pdf_file_id"):
            result["pdf_url"] = f"{pdf_base}/{version['id']}/pdf"
        return result
    versions = [version_read(row) for row in snapshot.get("versions") or []]
    draft = snapshot["draft"]
    return {"family_id": snapshot["family_id"], "revision": draft["revision"],
        "base_version_id": draft.get("base_version_id"), "dirty": draft.get("dirty", False),
        "current_version": next((row for row in versions if row["id"] == snapshot.get("current_version_id")), None),
        "versions": versions}


def client_body(kind: str, body: dict, *, can_edit: bool = True) -> dict:
    """Only the selected form, active obligations, and honest edit capability."""
    visible = workspace.active_body(kind, body)
    if kind == "debt_schedule":
        for row in visible.get("debts") or []:
            row["owner"] = row.get("origin") or row.get("owner") or "admin"
            row["editable"] = can_edit
    return visible


def _copy_fields(current: dict, incoming: dict, fields) -> dict:
    result = copy.deepcopy(current)
    for key in fields:
        if key in incoming:
            result[key] = copy.deepcopy(incoming[key])
    return result


def _merge_rows(current, incoming, fields):
    if not isinstance(incoming, list) or len(incoming) > workspace.MAX_ROWS or any(not isinstance(row, dict) for row in incoming):
        raise HTTPException(422, "Invalid financial schedule rows")
    known = {str(row.get("id")): row for row in current or [] if row.get("id")}
    result, seen = [], set()
    for raw in incoming:
        try:
            row_id = str(UUID(str(raw["id"]))) if raw.get("id") else str(uuid4())
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, "Schedule rows require UUIDs") from None
        if row_id in seen:
            raise HTTPException(422, "Schedule rows require distinct UUIDs")
        seen.add(row_id)
        result.append({**_copy_fields(known.get(row_id, {}), raw, fields), "id": row_id})
    return result


def merge_client_body(kind: str, current: dict, incoming: dict) -> dict:
    """Retain provenance while accepting financial corrections to this form."""
    if not isinstance(incoming, dict):
        raise HTTPException(422, "Send the financial form answers")
    if len(json.dumps(incoming, default=str)) > 2_000_000:
        raise HTTPException(422, "Financial form is too large")
    if kind == "debt_schedule":
        if "debts" in incoming and (not isinstance(incoming["debts"], list) or len(incoming["debts"]) > workspace.MAX_ROWS or any(not isinstance(row, dict) for row in incoming["debts"])):
            raise HTTPException(422, "Invalid financial schedule rows")
        merged = merge_source_debts(current, incoming, allow_source_edits=True)
        if "business_name" in incoming:
            merged["business_name"] = incoming["business_name"]
        return merged
    result = copy.deepcopy(current)
    if kind == "real_estate_schedule":
        result = _copy_fields(result, incoming, [spec["key"] for spec in real_estate_schema.HEADER])
        for block, specs in real_estate_schema.COLLECTIONS.items():
            if block in incoming:
                result[block] = _merge_rows(result.get(block), incoming[block], [spec["key"] for spec in specs])
        return result
    if kind == "pfs":
        template = pfs_schema.empty_body()
        result = _copy_fields(result, incoming, ("as_of", "notes"))
        for block in ("applicant", "assets", "liabilities", "income", "contingent"):
            if block in incoming:
                if not isinstance(incoming[block], dict):
                    raise HTTPException(422, f"Invalid {block}")
                result[block] = _copy_fields(result.get(block) or {}, incoming[block], template[block])
        if "schedules" in incoming:
            if not isinstance(incoming["schedules"], dict):
                raise HTTPException(422, "Invalid personal financial schedules")
            schedules = copy.deepcopy(result.get("schedules") or {})
            for spec in pfs_schema.SCHEDULES:
                if spec.key in incoming["schedules"]:
                    schedules[spec.key] = _merge_rows(schedules.get(spec.key), incoming["schedules"][spec.key], spec.column_keys)
            result["schedules"] = schedules
        return result
    if kind in bss.KINDS:
        template = bss.empty_body(kind)
        if "header" in incoming:
            if not isinstance(incoming["header"], dict):
                raise HTTPException(422, "Invalid statement header")
            result["header"] = _copy_fields(result.get("header") or {}, incoming["header"], template["header"])
        if "sections" in incoming:
            if not isinstance(incoming["sections"], dict):
                raise HTTPException(422, "Invalid statement sections")
            sections = copy.deepcopy(result.get("sections") or {})
            for block, fields in template["sections"].items():
                if block in incoming["sections"]:
                    if not isinstance(incoming["sections"][block], dict):
                        raise HTTPException(422, "Invalid statement section")
                    sections[block] = _copy_fields(sections.get(block) or {}, incoming["sections"][block], fields)
            result["sections"] = sections
        return result
    raise HTTPException(404, "Financial form not found")


def require_revision(snapshot: dict, expected_revision: int | None) -> None:
    if expected_revision is None or expected_revision != snapshot["draft"]["revision"]:
        raise HTTPException(409, {"code": "financial_draft_conflict", "current_revision": snapshot["draft"]["revision"],
            "message": "This form changed or needs to be refreshed. Reload before saving your corrections."})


async def stage_client_draft(db, profile, snapshot: dict, *, body: dict, expected_revision: int | None, actor=None) -> dict:
    require_revision(snapshot, expected_revision)
    merged = merge_client_body(snapshot["kind"], snapshot["body"], body)
    if workspace.body_hash(merged) == workspace.body_hash(snapshot["body"]):
        return snapshot
    return await workspace.save_draft(db, profile, kind=snapshot["kind"], family_id=UUID(snapshot["family_id"]),
        expected_revision=expected_revision, body=merged, actor=actor)


async def save_submission(db, profile, snapshot: dict, *, body: dict, expected_revision: int | None,
                          base_version_id: UUID | None, idempotency_key: str, actor=None) -> dict:
    """Retry before staging; a lost response must not mint another version."""
    family_id, kind = UUID(snapshot["family_id"]), snapshot["kind"]
    prior = (await db.execute(select(FinancialFormVersion).where(FinancialFormVersion.family_id == family_id,
        FinancialFormVersion.idempotency_key == idempotency_key))).scalar_one_or_none()
    if prior is not None:
        candidate = workspace.validate_body(kind, merge_client_body(kind, prior.body, body))
        if workspace.body_hash(candidate) != prior.body_sha256:
            raise HTTPException(409, "This save identifier was already used for different answers. Reload before saving again.")
        return {**snapshot, "saved_version": workspace._version_read(prior)}
    require_revision(snapshot, expected_revision)
    if str(base_version_id or "") != str(snapshot["draft"].get("base_version_id") or ""):
        raise HTTPException(409, "A newer version was saved. Reload before submitting your corrections.")
    staged = await stage_client_draft(db, profile, snapshot, body=body, expected_revision=expected_revision, actor=actor)
    current_id = staged.get("current_version_id")
    current = await db.get(FinancialFormVersion, UUID(current_id)) if current_id else None
    if current and current.status == "filed" and workspace.body_hash(staged["body"]) == current.body_sha256:
        return {**staged, "saved_version": workspace._version_read(current)}
    async def publish():
        return await workspace.publish_version(db, profile, kind=kind, family_id=family_id,
            expected_revision=staged["draft"]["revision"], base_version_id=base_version_id,
            idempotency_key=idempotency_key, actor=actor, submit=True)
    if not callable(getattr(db, "begin_nested", None)):
        return await publish()
    # The draft was validated and flushed before the savepoint. A failed
    # render/storage/project operation rolls back every publication side effect
    # while keeping those accepted working edits. Commit only that draft on
    # the error path, so request cleanup cannot discard the client's answers.
    try:
        async with db.begin_nested():
            return await publish()
    except Exception as error:
        await db.commit()
        if isinstance(error, HTTPException) and error.status_code == 502:
            refresh = staged["draft"]["revision"] != snapshot["draft"]["revision"]
            message = "The PDF could not be generated or stored. Your working edits are saved. " + (
                "Reload the form before retrying Save version." if refresh else "Retry Save version.")
            raise HTTPException(502, {"code": "financial_pdf_unavailable", "message": message,
                "workspace": workspace_metadata(staged)}) from error
        raise


def submission_key(scope_id, key: str | None) -> str:
    if not key or not 8 <= len(key) <= 128:
        raise HTTPException(422, "Include a save identifier before submitting this form")
    return f"client:{scope_id}:{hashlib.sha256(key.encode()).hexdigest()}"
