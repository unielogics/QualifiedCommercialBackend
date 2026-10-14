"""Tailored lender snapshots and a persistent, human-approved delivery queue."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import secrets
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic.networks import EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import Role
from app.models.application_profile import ApplicationProfile, ApplicationRequirementState
from app.models.bucket import BucketFile
from app.models.lender import Lender
from app.models.submission_batch import (
    LenderOffering,
    LenderSubmission,
    LenderSubmissionEvent,
    SubmissionBatch,
)
from app.models.user import User
from app.schemas.submission_batch import (
    LenderSubmissionRead,
    SubmissionBatchCreate,
    SubmissionBatchRead,
)
from app.services import application_profiles, application_programs
from app.services.program_rules import evaluate_rules
from app.services.provider_secrets import _decrypt_fernet, _encrypt_fernet

MAX_PACKAGE_BYTES = 200 * 1024 * 1024
COMPLETE = {"verified", "waived", "not_applicable"}
SENT = {"sent", "delivered", "viewed", "downloaded"}


def now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def digest(value: object) -> str:
    def normalize(item):
        if isinstance(item, dict):
            return {str(key): normalize(content) for key, content in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted((normalize(content) for content in item), key=lambda content: json.dumps(content, sort_keys=True, default=str))
        if isinstance(item, (list, tuple)):
            return [normalize(content) for content in item]
        return item
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def require_underwriter(user) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting workspace access is required")


def event(db, row, action: str, user_id=None, **detail) -> None:
    db.add(LenderSubmissionEvent(submission_id=row.id, event=action, actor_user_id=user_id, detail=detail))


def valid_email(value: str | None) -> bool:
    try:
        TypeAdapter(EmailStr).validate_python(value)
        return True
    except ValueError:
        return False


def fit_for_offering(offering, context: dict, selected: set[str]) -> dict:
    if offering is None:
        return {"status": "lender_clarification_required", "reasons": ["Lender checklist has not been configured"]}
    if offering.program_key != "*" and offering.program_key not in selected:
        return {"status": "outside_published_criteria", "reasons": ["This lender offering is not among the selected programs"]}
    if not offering.fit_rules or not offering.fit_rules.get("fit"):
        return {"status": "lender_clarification_required", "reasons": ["No confirmed lender fit criteria are configured"]}
    if "fact_sources" in context:
        context = dict(context)
        for key in application_programs.FINANCIAL_FIT_FIELDS:
            if (context["fact_sources"].get(key) or {}).get("verification_status") != "verified":
                context[key] = None
    result = evaluate_rules(offering.fit_rules, context)
    result_status = "recommended" if result.matched else "needs_information" if result.state == "unknown" else "outside_published_criteria"
    return {"status": result_status, "reasons": result.reasons, "rules": result.results}


async def source_state(db: AsyncSession, profile: ApplicationProfile) -> dict:
    from app.models.client import Client
    from app.models.public_underwriting_intake import PublicUnderwritingIntake
    from app.services.collection_tasks import latest_responses

    readiness = await application_programs.get_program_readiness(db, profile)
    files, analyses, _ = await application_programs._evidence_inventory(db, profile)
    context = await application_programs.profile_fit_context(db, profile)
    states = list((await db.scalars(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id))).all())
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    owners = await application_profiles.owner_rows(db, profile)
    borrower = {"contact_name": getattr(client, "name", None), "business_name": getattr(intake, "business_name", None), "email": getattr(client, "email", None), "phone": getattr(client, "phone", None),
                "owners": [{"name": owner.full_name, "ownership_pct": float(owner.ownership_pct) if owner.ownership_pct is not None else None, "is_guarantor": owner.is_guarantor} for owner in owners]}
    form_lists = list((await db.scalars(select(LenderOffering.official_form_file_ids).where(LenderOffering.status == "active"))).all())
    form_ids = {UUID(key) for values in form_lists for key in values or []}
    official_files = {str(file.id): file for file in (await db.scalars(select(BucketFile).where(BucketFile.id.in_(form_ids)))).all()} if form_ids else {}
    channel_version = None
    if getattr(profile, "funding_category", None) == "arf_external" or any(s.program_key.startswith("arf_") for s in readiness.selections):
        from app.services.arf_channel import get_configuration
        channel_version = (await get_configuration(db)).version
    return {"readiness": readiness, "files": {str(f.id): f for f in files}, "analyses": analyses, "context": context, "states": {s.requirement_key: s for s in states}, "responses": await latest_responses(db, profile.id), "borrower": borrower, "channel_version": channel_version, "official_files": official_files}


def source_fingerprint(profile, source: dict, offering: LenderOffering | None) -> str:
    # Exclude operational timestamps: reading a checklist must not stale its package.
    fields = ("vertical", "funding_category", "entity_type", "industry", "subindustry", "naics_code", "use_of_funds", "use_of_funds_revision", "classification_revision")
    relevant = set()
    if offering:
        relevant.update(lender_requirement_key(offering.lender_id, item["requirement_key"]) for item in offering.requirements or [])
        relevant.add(sharing_requirement(offering.lender_id)["requirement_key"])
        for program in source["readiness"].programs:
            if offering.program_key in {"*", program.program_key}:
                relevant.update(program.requirement_keys)
    states = [state for key, state in source["states"].items() if key in relevant or not key.startswith(("lr_", "lender_sharing_"))]
    return digest({
        "profile": {key: getattr(profile, key, None) for key in fields},
        "context": source["context"],
        "borrower": source.get("borrower"),
        "channel_version": source.get("channel_version"),
        "selections": [{"key": s.program_key, "version": getattr(s, "playbook_version", None)} for s in source["readiness"].selections],
        "requirements": sorted([{
            "key": s.requirement_key, "status": s.status, "provenance": {key: value for key, value in (s.provenance or {}).items() if not key.startswith("collection_")},
            "evidence": str(s.evidence_file_id) if s.evidence_file_id else None,
        } for s in states], key=lambda item: item["key"]),
        "files": sorted([{"id": str(f.id), "hash": getattr(f, "content_hash", None), "version": getattr(f, "s3_version_id", None), "updated": getattr(f, "updated_at", None)} for f in source["files"].values()], key=lambda item: item["id"]),
        "offering": {"id": str(offering.id), "status": offering.status, "requirements": offering.requirements, "forms": offering.official_form_file_ids} if offering else None,
        "official_files": [{"id": key, "hash": getattr(file, "content_hash", None), "version": getattr(file, "s3_version_id", None), "deleted": getattr(file, "deleted_at", None), "status": file.status} for key in sorted(offering.official_form_file_ids or []) if (file := source.get("official_files", {}).get(key)) is not None] if offering else [],
    })


async def active_offerings(db, lender_id, selected: set[str]) -> list[LenderOffering]:
    return list((await db.scalars(select(LenderOffering).where(
        LenderOffering.lender_id == lender_id, LenderOffering.status == "active",
    ).order_by(LenderOffering.version.desc()))).all())


async def selected_offering(db, target, selected: set[str]) -> LenderOffering | None:
    if target.offering_id:
        offering = await db.get(LenderOffering, target.offering_id)
        if offering is None or offering.lender_id != target.lender_id or offering.status != "active":
            raise HTTPException(422, "Offering is not active for the selected lender")
        return offering
    offerings = await active_offerings(db, target.lender_id, selected)
    return next((o for o in offerings if o.program_key in selected), next((o for o in offerings if o.program_key == "*"), None))


def sharing_requirement(lender_id) -> dict:
    key = f"lender_sharing_{str(lender_id).replace('-', '')}"
    return {
        "requirement_key": key,
        "canonical_key": key,
        "label": "Authorize sharing this file with the selected institution",
        "requirement_kind": "consent", "required_level": "required", "visibility": "client",
        "collection_config": {
            "subject": str(lender_id), "lender_ids": [str(lender_id)], "scope": {"lender_ids": [str(lender_id)]},
            "disclosure_version": "lender-sharing-v1",
            "disclosure_text": "I authorize Qualified Commercial to share my application information and the documents selected for this submission with the institution identified in this request for a financing review. This authorization does not authorize a credit pull or guarantee financing.",
        },
    }


async def materialize_lender_requirements(db, profile, offering, lender) -> None:
    from app.services.collection_tasks import materialize_external_requirements
    definitions = list(offering.requirements or []) if offering else []
    definitions = [{**definition, "requirement_key": lender_requirement_key(lender.id, definition["requirement_key"]), "canonical_key": definition.get("canonical_key") or definition["requirement_key"], "visibility": ["borrower", "underwriter"] if definition.get("visibility", "client") == "client" else ["underwriter"], "can_underwriter_waive": definition.get("waivable", False), "version": offering.version} for definition in definitions]
    consent = sharing_requirement(lender.id)
    consent["visibility"] = ["borrower", "underwriter"]
    consent["label"] = f"Authorize sharing with {lender.name}"
    consent["collection_config"]["disclosure_text"] += f" Institution: {lender.name}."
    await materialize_external_requirements(db, profile, requirements=[consent], source_key=f"lender:{lender.id}:sharing", approved=True)
    if definitions:
        await materialize_external_requirements(db, profile, requirements=definitions, source_key=f"lender:{lender.id}:{offering.id}", approved=offering.checklist_confirmed)


def lender_requirement_key(lender_id, key: str) -> str:
    # Independent policy/evidence rows, shared canonical collection task.
    return f"lr_{str(lender_id).replace('-', '')}_{key[:65]}_{digest(key)[:12]}"


async def sharing_is_authorized(db, source, lender_id) -> bool:
    from app.models.application_profile import ApplicationRequirementResponse
    state = source["states"].get(sharing_requirement(lender_id)["requirement_key"])
    if state is None or state.status != "verified":
        return False
    response = (await db.scalars(select(ApplicationRequirementResponse).where(
        ApplicationRequirementResponse.requirement_state_id == state.id,
    ).order_by(ApplicationRequirementResponse.created_at.desc()).limit(1))).first()
    value = response.value.get("answer", response.value) if response and isinstance(response.value, dict) else {}
    return bool(response and response.review_state == "accepted" and isinstance(value, dict) and value.get("granted") is True)


async def package_material(db, profile, lender, offering, source, extra_ids: list[UUID]) -> dict:
    selected = {s.program_key for s in source["readiness"].selections}
    fit = fit_for_offering(offering, source["context"], selected)
    blockers = []
    def block(code, message):
        blockers.append({"code": code, "message": message})
    if not lender.is_active or not valid_email(lender.submission_email):
        block("invalid_recipient", "An active lender and valid submission email are required")
    if offering is None or not offering.checklist_confirmed:
        block("configuration_incomplete", "The official lender checklist must be confirmed before delivery")
    if offering and offering.status != "active":
        block("offering_retired", "The offering version has been replaced; prepare a new batch")
    if not await sharing_is_authorized(db, source, lender.id):
        block("sharing_consent_required", f"Borrower authorization to share with {lender.name} is required")
    if (offering and offering.program_key.startswith("arf_")) or getattr(profile, "funding_category", None) == "arf_external" or any(key.startswith("arf_") for key in selected):
        from app.services.arf_channel import require_submission_permission
        try:
            await require_submission_permission(db)
        except HTTPException:
            block("arf_submission_permission", "ARF's authorized intake and submission route must be confirmed")
    required = [{**definition, "canonical_key": definition.get("canonical_key") or definition["requirement_key"], "requirement_key": lender_requirement_key(lender.id, definition["requirement_key"])} for definition in (offering.requirements or [])] if offering else []
    applicable_keys = {item["requirement_key"] for item in required}
    if offering:
        for program in source["readiness"].programs:
            if offering.program_key in {"*", program.program_key}:
                applicable_keys.update(program.requirement_keys)
    by_key = {r.requirement_key: r for r in source["readiness"].requirements}
    snapshots = []
    structured_responses = []
    file_ids = {str(i) for i in extra_ids}
    missing_unknown = file_ids - source["files"].keys()
    if missing_unknown:
        raise HTTPException(422, "A selected document is outside this application's evidence inventory")
    for definition in required:
        state = source["states"].get(definition["requirement_key"])
        required_level = definition.get("required_level", "required")
        satisfied = bool(state and (state.status in {"verified", "not_applicable"} or (state.status == "waived" and definition.get("waivable", False))))
        snapshots.append({"key": definition["requirement_key"], "label": definition.get("label"), "kind": definition.get("requirement_kind", "document"), "status": state.status if state else "missing", "required": required_level == "required", "waivable": definition.get("waivable", False)})
        if required_level == "required" and not satisfied:
            block("missing_lender_requirement", definition.get("label", definition["requirement_key"]))
    applicable_keys.add(sharing_requirement(lender.id)["requirement_key"])
    for key in applicable_keys:
        requirement = by_key.get(key)
        if requirement:
            file_ids.update(str(f.file_id) for f in requirement.evidence_files if f.verified or f.ai_decision == "accepted")
        state = source["states"].get(key)
        metadata = (state.provenance or {}) if state else {}
        response = source.get("responses", {}).get(getattr(state, "id", None)) if state else None
        if response and response.review_state == "accepted":
            structured_responses.append({"key": metadata.get("canonical_key") or key, "label": state.label,
                "kind": metadata.get("requirement_kind", "field"), "value": response.value.get("answer"),
                "provenance": {"source": response.source, "source_version": response.source_version, "received_at": response.created_at.isoformat(),
                               **({name: (response.provenance or {}).get(name) for name in ("disclosure_version", "disclosure_text", "disclosure_hash", "scope")} if metadata.get("requirement_kind") == "consent" else {})}})
        origin = (metadata.get("collection_config") or {}).get("origin") or metadata.get("origin")
        if key not in {item["requirement_key"] for item in required} and state and origin == "lender_explicit":
            snapshots.append({"key": key, "label": state.label, "kind": metadata.get("requirement_kind", "document"), "status": state.status, "required": state.required_level == "required", "waivable": False})
            if state.required_level == "required" and state.status not in {"verified", "not_applicable"}:
                block("missing_lender_requirement", state.label)
        if state and state.evidence_file_id and state.status == "verified":
            file_ids.add(str(state.evidence_file_id))
    files = [source["files"][key] for key in sorted(file_ids) if key in source["files"]]
    official_files = []
    if offering:
        for key in offering.official_form_file_ids or []:
            file = await db.get(BucketFile, UUID(key))
            if file and file.deleted_at is None and file.status == "uploaded":
                official_files.append(file)
            else:
                block("official_form_unavailable", "A configured official form is unavailable")
        if offering.official_forms_required and not official_files:
            block("official_form_required", "The official lender forms must be supplied")
    files = list({str(file.id): file for file in files + official_files}.values())
    for file in files:
        analysis = source["analyses"].get(file.id)
        if file.status != "uploaded" or getattr(analysis, "usability_status", None) == "unsafe":
            block("unsafe_evidence", f"A selected document cannot be shared: {file.file_name}")
        if not getattr(file, "content_hash", None) and getattr(file, "s3_key", None):
            try:
                raw = await asyncio.to_thread(_read_file, file, {"s3_version_id": getattr(file, "s3_version_id", None)})
                file.content_hash = hashlib.sha256(raw).hexdigest()
            except Exception:
                block("document_snapshot_unavailable", f"A stable document snapshot could not be prepared: {file.file_name}")
    summary_keys = ("requested_amount", "annual_revenue", "business_age_years", "industry", "entity_type", "funding_category")
    facts = {key: source["context"].get(key) for key in summary_keys if source["context"].get(key) is not None}
    manifest = {
        "lender": lender.name, "application_id": str(profile.id), "facts": facts,
        "borrower": source.get("borrower", {}), "responses": structured_responses,
        "program_key": offering.program_key if offering else None,
        "offering_version": offering.version if offering else None,
        "summary": "Financing review request. Figures are application facts and are subject to institutional verification and approval.",
        "files": [{"id": str(f.id), "name": f.file_name, "content_type": f.content_type, "size_bytes": f.size_bytes, "content_hash": getattr(f, "content_hash", None), "s3_version_id": getattr(f, "s3_version_id", None)} for f in files],
        "requirements": snapshots,
        "configuration_incomplete": any(b["code"] in {"configuration_incomplete", "official_form_required"} for b in blockers),
    }
    return {"manifest": manifest, "requirements": snapshots, "blockers": blockers, "fit": fit, "fingerprint": source_fingerprint(profile, source, offering)}


async def prepare_batch(db, profile, user, payload: SubmissionBatchCreate) -> SubmissionBatch:
    require_underwriter(user)
    request_hash = digest(payload.model_dump(mode="json"))
    # Lock the profile to serialize same-key requests, including the first insert.
    await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update())
    existing = (await db.scalars(select(SubmissionBatch).where(SubmissionBatch.profile_id == profile.id, SubmissionBatch.idempotency_key == payload.idempotency_key))).first()
    if existing:
        if existing.request_fingerprint != request_hash:
            raise HTTPException(409, "This idempotency key was used for a different batch")
        return existing
    source = await source_state(db, profile)
    selected = {s.program_key for s in source["readiness"].selections}
    targets = []
    for target in payload.targets:
        lender = await db.get(Lender, target.lender_id)
        if lender is None:
            raise HTTPException(422, "A selected lender does not exist")
        offering = await selected_offering(db, target, selected)
        await materialize_lender_requirements(db, profile, offering, lender)
        targets.append((target, lender, offering))
    source = await source_state(db, profile)
    batch = SubmissionBatch(profile_id=profile.id, idempotency_key=payload.idempotency_key, request_fingerprint=request_hash, created_by_user_id=user.id, created_at=now())
    db.add(batch)
    await db.flush()
    for target, lender, offering in targets:
        material = await package_material(db, profile, lender, offering, source, target.additional_file_ids)
        token = secrets.token_urlsafe(40)
        previous_version = (await db.scalar(select(func.max(LenderSubmission.version)).join(SubmissionBatch, SubmissionBatch.id == LenderSubmission.batch_id).where(SubmissionBatch.profile_id == profile.id, LenderSubmission.lender_id == lender.id))) or 0
        row = LenderSubmission(
            batch_id=batch.id, lender_id=lender.id, lender_name=lender.name,
            offering_id=offering.id if offering else None, recipient_email=lender.submission_email,
            status="prepared", version=previous_version + 1, source_fingerprint=material["fingerprint"],
            fit_status=material["fit"]["status"], warning_acknowledged=target.warning_acknowledged,
            blockers=material["blockers"], requirements_snapshot=material["requirements"], manifest=material["manifest"],
            token_hash=hashlib.sha256(token.encode()).hexdigest(), encrypted_token=_encrypt_fernet(token),
            expires_at=now() + timedelta(days=payload.expires_in_days),
        )
        db.add(row)
        await db.flush()
        event(db, row, "prepared", user.id, document_count=len(material["manifest"]["files"]))
    return batch


async def read_batch(db, batch, *, refresh=True) -> SubmissionBatchRead:
    rows = list((await db.scalars(select(LenderSubmission).where(LenderSubmission.batch_id == batch.id).order_by(LenderSubmission.lender_name))).all())
    if refresh:
        profile = await db.get(ApplicationProfile, batch.profile_id)
        source = await source_state(db, profile)
        for row in rows:
            if row.status in {"prepared", "failed"}:
                offering = await db.get(LenderOffering, row.offering_id) if row.offering_id else None
                if source_fingerprint(profile, source, offering) != row.source_fingerprint:
                    row.status = "package_stale"
    statuses = {r.status for r in rows}
    if statuses and statuses <= SENT:
        batch.status = "sent"
    elif statuses & SENT and statuses & {"failed", "delivery_unknown", "package_stale"}:
        batch.status = "partial_success"
    elif statuses & {"queued", "sending"}:
        batch.status = "queued"
    elif statuses & {"failed", "delivery_unknown"}:
        batch.status = "failed"
    elif statuses == {"revoked"}:
        batch.status = "revoked"
    return SubmissionBatchRead(id=batch.id, profile_id=batch.profile_id, status=batch.status, created_at=batch.created_at, approved_at=batch.approved_at, submissions=[LenderSubmissionRead.model_validate(row) for row in rows])


async def approve_batch(db, profile, batch, user, submission_ids=None) -> SubmissionBatchRead:
    require_underwriter(user)
    await db.execute(select(SubmissionBatch.id).where(SubmissionBatch.id == batch.id).with_for_update())
    rows = list((await db.scalars(select(LenderSubmission).where(LenderSubmission.batch_id == batch.id))).all())
    ids = set(submission_ids) if submission_ids is not None else {r.id for r in rows}
    if ids - {r.id for r in rows}:
        raise HTTPException(422, "A selected submission does not belong to this batch")
    source = await source_state(db, profile)
    for row in rows:
        if row.id not in ids or row.status in SENT | {"queued", "sending"}:
            continue
        if row.status not in {"prepared", "failed"} or row.revoked_at or aware(row.expires_at) <= now():
            continue
        lender = await db.get(Lender, row.lender_id)
        offering = await db.get(LenderOffering, row.offering_id) if row.offering_id else None
        if source_fingerprint(profile, source, offering) != row.source_fingerprint:
            row.status = "package_stale"
            continue
        extras = [UUID(item["id"]) for item in row.manifest.get("files", []) if item["id"] in source["files"]]
        material = await package_material(db, profile, lender, offering, source, extras)
        row.blockers = material["blockers"]
        if row.fit_status != "recommended" and not row.warning_acknowledged:
            row.blockers = [*row.blockers, {"code": "fit_warning_acknowledgement", "message": "Acknowledge the lender fit warning before delivery"}]
        if row.blockers:
            continue
        row.status = "queued"
        row.queued_at = now()
        row.last_error = None
        event(db, row, "queued", user.id)
    batch.approved_at = batch.approved_at or now()
    batch.approved_by_user_id = batch.approved_by_user_id or user.id
    await db.flush()
    return await read_batch(db, batch, refresh=False)


async def assert_current(db, row, *, require_sent=False):
    if row.revoked_at or aware(row.expires_at) <= now():
        raise HTTPException(410, "This lender package has expired or been revoked")
    if require_sent and row.status not in SENT:
        raise HTTPException(404, "Lender package is not available")
    batch = await db.get(SubmissionBatch, row.batch_id)
    profile = await db.get(ApplicationProfile, batch.profile_id)
    source = await source_state(db, profile)
    offering = await db.get(LenderOffering, row.offering_id) if row.offering_id else None
    lender = await db.get(Lender, row.lender_id)
    if lender is None or not lender.is_active or lender.submission_email != row.recipient_email:
        raise HTTPException(410, "This institution's package access has changed")
    if source_fingerprint(profile, source, offering) != row.source_fingerprint:
        raise HTTPException(409, "This package is stale. Underwriting must prepare a new version.")
    for item in row.manifest.get("files", []):
        file = await db.get(BucketFile, UUID(item["id"]))
        if not file or file.deleted_at or file.status != "uploaded" or item.get("content_hash") != getattr(file, "content_hash", None) or item.get("s3_version_id") != getattr(file, "s3_version_id", None):
            raise HTTPException(409, "A package document changed. Underwriting must prepare a new version.")
    return source


async def public_submission(db, token: str):
    row = (await db.scalars(select(LenderSubmission).where(LenderSubmission.token_hash == hashlib.sha256(token.encode()).hexdigest()))).first()
    if row is None:
        raise HTTPException(404, "Lender package not found")
    await assert_current(db, row, require_sent=True)
    return row


def _safe_name(name: str, file_id: str) -> str:
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).lstrip(". ") or "document"
    return f"{file_id[:8]}-{name}"


def render_cover(manifest: dict, *, preview=False) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    lines = ["Qualified Commercial", "Financing review package", manifest["lender"], ""]
    if preview:
        lines += ["INTERNAL PREVIEW — CONFIGURATION INCOMPLETE", ""]
    lines += [manifest["summary"], ""]
    lines += [f"{key.replace('_', ' ').title()}: {value}" for key, value in manifest.get("facts", {}).items()]
    lines += ["", "Documents included:"] + [item["name"] for item in manifest.get("files", [])]
    # Use bounded pages instead of silently overflowing a fixed textbox.
    for index in range(0, len(lines), 36):
        if index:
            page = doc.new_page()
        page.insert_textbox(fitz.Rect(48, 48, 547, 790), "\n".join(lines[index:index + 36]), fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def _read_file(file, snapshot: dict) -> bytes:
    from app.routers.buckets import _bucket_storage_config, _s3_client
    bucket, _, _ = _bucket_storage_config()
    args = {"Bucket": bucket, "Key": file.s3_key}
    if snapshot.get("s3_version_id"):
        args["VersionId"] = snapshot["s3_version_id"]
    body = _s3_client().get_object(**args)["Body"]
    try:
        raw = body.read(MAX_PACKAGE_BYTES + 1)
    finally:
        body.close()
    if len(raw) > MAX_PACKAGE_BYTES:
        raise HTTPException(413, "The package exceeds the 200 MB export limit")
    if snapshot.get("content_hash") and hashlib.sha256(raw).hexdigest() != snapshot["content_hash"]:
        raise HTTPException(409, "Document bytes changed; prepare a new package")
    return raw


async def archive_bytes(db, row, *, preview=False) -> bytes:
    await assert_current(db, row)
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = {**row.manifest, "internal_preview": preview, "package_version": row.version}
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        archive.writestr("cover-and-summary.pdf", render_cover(manifest, preview=preview))
        for item in row.manifest.get("files", []):
            file = await db.get(BucketFile, UUID(item["id"]))
            if not file or file.deleted_at is not None or file.status != "uploaded":
                raise HTTPException(409, "A packaged document is no longer available")
            raw = await asyncio.to_thread(_read_file, file, item)
            total += len(raw)
            if total > MAX_PACKAGE_BYTES:
                raise HTTPException(413, "The package exceeds the 200 MB export limit")
            archive.writestr(_safe_name(item["name"], item["id"]), raw)
    return buf.getvalue()


async def deliver_one(submission_id: UUID) -> str:
    from app.db import SessionLocal
    from app.services.email.user_mailer import send_as_user
    async with SessionLocal() as db:
        row = (await db.scalars(select(LenderSubmission).where(LenderSubmission.id == submission_id).with_for_update(skip_locked=True))).first()
        if row is None or row.status != "queued":
            return "skipped"
        batch = await db.get(SubmissionBatch, row.batch_id)
        profile = await db.get(ApplicationProfile, batch.profile_id)
        approver = await db.get(User, batch.approved_by_user_id) if batch.approved_by_user_id else None
        try:
            if approver is None or approver.deleted_at is not None or approver.account_status != "active":
                raise HTTPException(403, "No active underwriting approval")
            require_underwriter(approver)
            await application_profiles.load_profile(db, profile.id, approver)
            source = await assert_current(db, row)
            lender = await db.get(Lender, row.lender_id)
            offering = await db.get(LenderOffering, row.offering_id) if row.offering_id else None
            extras = [UUID(item["id"]) for item in row.manifest.get("files", []) if item["id"] in source["files"]]
            material = await package_material(db, profile, lender, offering, source, extras)
            if material["blockers"] or lender.submission_email != row.recipient_email:
                raise HTTPException(409, "Readiness or lender recipient changed; prepare a new batch")
        except HTTPException as exc:
            row.status = "package_stale" if exc.status_code == 409 else "failed"
            row.last_error = str(exc.detail)
            event(db, row, row.status)
            await db.commit()
            return row.status
        row.status = "sending"
        row.sending_at = now()
        row.attempt_count += 1
        event(db, row, "sending")
        # Commit the claim before provider I/O. A crash here becomes unknown,
        # never an automatic duplicate email on worker restart.
        await db.commit()
        try:
            token = _decrypt_fernet(row.encrypted_token)
            url = f"{get_settings().frontend_app_url.rstrip('/')}/lender-submissions/{token}"
            result = await send_as_user(db, batch.approved_by_user_id, to_emails=[row.recipient_email],
                subject="Qualified Commercial — financing review package",
                body_text=f"Hello {row.lender_name},\n\nPlease review the application and supporting documents in your secure package:\n{url}\n\nThe package expires {row.expires_at.date().isoformat()}. A ZIP download is available inside.\n\nQualified Commercial",
                headers={"Message-ID": f"<qc-submission-{row.id}@qualifiedcommercial.com>"})
            row.status = "sent" if result.ok else "failed" if result.detail in {"not_configured", "bad recipients"} else "delivery_unknown"
            row.provider_message_id = result.message_id
            row.sent_at = now() if result.ok else None
            row.last_error = None if result.ok else str(result.detail)[:1000] if row.status == "failed" else "Provider outcome is unknown; reconcile delivery before retrying."
        except Exception:
            row.status = "delivery_unknown"
            row.last_error = "Provider outcome is unknown; reconcile delivery before retrying."
        event(db, row, row.status, provider_message_id=row.provider_message_id)
        await db.commit()
        return row.status


async def drain_delivery_queue() -> dict:
    from app.db import SessionLocal
    async with SessionLocal() as db:
        abandoned = list((await db.scalars(select(LenderSubmission).where(LenderSubmission.status == "sending", LenderSubmission.sending_at < now() - timedelta(minutes=15)).with_for_update(skip_locked=True))).all())
        for row in abandoned:
            row.status = "delivery_unknown"
            row.last_error = "Delivery worker stopped before recording the provider result. Reconcile before retrying."
            event(db, row, "delivery_unknown")
        ids = list((await db.scalars(select(LenderSubmission.id).where(LenderSubmission.status == "queued").order_by(LenderSubmission.queued_at).limit(10))).all())
        await db.commit()
    results = await asyncio.gather(*(deliver_one(key) for key in ids), return_exceptions=True)
    return {"processed": len(ids), "sent": sum(result == "sent" for result in results), "unknown": len(abandoned)}


async def refresh_pending_previews() -> dict:
    """Refresh previously selected institutions after new answers/evidence.

    Preparation never selects a new institution or inherits send approval.
    The new version always waits for an underwriter's explicit approval.
    """
    from app.db import SessionLocal
    from app.schemas.submission_batch import SubmissionTarget

    refreshed = 0
    async with SessionLocal() as db:
        batches = list((await db.scalars(select(SubmissionBatch).order_by(SubmissionBatch.created_at.desc()).limit(300))).all())
        visited = set()
        for batch in batches:
            if batch.profile_id in visited:
                continue
            visited.add(batch.profile_id)
            if batch.status not in {"prepared", "partial_success", "failed"}:
                continue
            rows = list((await db.scalars(select(LenderSubmission).where(LenderSubmission.batch_id == batch.id, LenderSubmission.status.in_(["prepared", "package_stale", "failed"])))).all())
            if not rows:
                continue
            user = await db.get(User, batch.created_by_user_id) if batch.created_by_user_id else None
            if not user or user.deleted_at or user.account_status != "active" or user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
                continue
            profile = await application_profiles.load_profile(db, batch.profile_id, user)
            source = await source_state(db, profile)
            fingerprints = []
            targets = []
            for row in rows:
                offering = await db.get(LenderOffering, row.offering_id) if row.offering_id else None
                fingerprints.append(source_fingerprint(profile, source, offering))
                targets.append(SubmissionTarget(lender_id=row.lender_id, offering_id=row.offering_id if offering and offering.status == "active" else None,
                    warning_acknowledged=row.warning_acknowledged,
                    additional_file_ids=[UUID(item["id"]) for item in row.manifest.get("files", []) if item["id"] in source["files"]]))
            if all(row.source_fingerprint == fingerprint for row, fingerprint in zip(rows, fingerprints, strict=True)):
                continue
            try:
                async with db.begin_nested():
                    payload = SubmissionBatchCreate(idempotency_key=f"refresh:{batch.id}:{digest(fingerprints)}", targets=targets)
                    refreshed_batch = await prepare_batch(db, profile, user, payload)
                    for row in rows:
                        row.status = "superseded"
                        event(db, row, "preview_refreshed", replacement_batch_id=str(refreshed_batch.id))
                    if len(rows) == len(list((await db.scalars(select(LenderSubmission.id).where(LenderSubmission.batch_id == batch.id))).all())):
                        batch.status = "superseded"
                    refreshed += 1
            except HTTPException:
                continue
        await db.commit()
    return {"refreshed": refreshed}
