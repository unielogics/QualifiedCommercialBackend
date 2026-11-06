"""Durable, source-bound review of several debt documents; never publication."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.db import SessionLocal
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.financial_form_version import FinancialFormFamily, FinancialFormImport
from app.models.user import User
from app.services import financial_debt_document_imports as documents
from app.services import financial_workspace as workspace
from app.services.debt_terms import FREQUENCY_MONTHLY_MULT, monthly_equivalent, normalize_terms
from app.services.financial_source_images import validated_image_media

log = logging.getLogger(__name__)
SCOPE = "debt_batch"
STAFF_ROLES = {Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.BROKER, Role.LOAN_EXEC, Role.FIELD_REP}
LEASE_SECONDS = 900
INSTRUCTIONS = documents.CONTRACT_INSTRUCTIONS + (
    " For every source obligation r1, r2, ... also return references['rN.loan_reference'] with value and quote "
    "ONLY when an explicitly labeled loan/account reference is visibly printed for that obligation. "
    "Preserve masking and leading zeroes. Never use a deposit account number, routing number, SSN, borrower name, "
    "amount or lender name as its loan reference. Do not fabricate a reference. Keep separately identified SBA "
    "accounts as separate rows even with the same lender. No identity/amount-only merging."
)


def _staff(actor):
    if getattr(actor, "role", None) not in STAFF_ROLES:
        raise HTTPException(403, "Debt batch review requires an internal staff account")


def _text(value):
    return " ".join(str(value or "").split()).casefold()


def _reference(value):
    text = str(value or "").strip()
    if not text or len(text) > 80 or re.search(r"\b\d{3}-\d{2}-\d{4}\b|\bssn\b", text, re.I):
        return None
    if not re.fullmatch(r"[A-Za-z0-9#*/ ._-]+", text):
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", text.upper())
    return text if len(normalized) >= 4 and any(char.isdigit() for char in normalized) else None


def _reference_key(value):
    reference = _reference(value)
    return re.sub(r"[^A-Z0-9]", "", reference.upper()) if reference else None


def _full_reference(value):
    reference = _reference(value)
    key = _reference_key(reference)
    return bool(key and len(key) >= 6 and not re.search(r"\*|#{2,}|x{2,}", reference, re.I))


def _row_reference(row):
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return _reference(row.get("loan_reference") or evidence.get("loan_reference"))


def _legacy_reference_match(row, reference):
    key = _reference_key(reference)
    if not _full_reference(reference):
        return False
    tokens = re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*", str(row.get("lender") or ""))
    return any(_full_reference(token) and _reference_key(token) == key for token in tokens)


def _similar_lender(left, right):
    ignored = {"llc", "inc", "corp", "corporation", "company", "ltd", "the"}
    def words(value):
        return {word for word in re.findall(r"[a-z]+", _text(value)) if word not in ignored and len(word) > 2}
    a, b = words(left), words(right)
    return bool(a and b and (a <= b or b <= a))


def _compatible_reference(prior, source):
    if not prior or not source:
        return True
    left, right = _reference_key(prior), _reference_key(source)
    if _full_reference(prior) and _full_reference(source):
        return left == right
    # A masked/last-four source may confirm a manually chosen full reference,
    # but can neither group loans automatically nor replace that full identity.
    left_digits, right_digits = re.sub(r"\D", "", prior), re.sub(r"\D", "", source)
    return bool(len(left_digits) >= 4 and len(right_digits) >= 4 and left_digits[-4:] == right_digits[-4:])


def _body(imported):
    body = imported.proposed_body or {}
    if body.get("import_scope") != SCOPE:
        raise HTTPException(404, "Debt batch review not found")
    return body


async def _batches(db, family_id):
    return list((await db.execute(select(FinancialFormImport).where(
        FinancialFormImport.family_id == family_id,
        FinancialFormImport.proposed_body["import_scope"].astext == SCOPE,
    ).order_by(FinancialFormImport.created_at.desc()))).scalars().all())


async def _load(db, profile, batch_id, *, lock=False):
    query = select(FinancialFormImport).where(FinancialFormImport.id == batch_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    imported = (await db.execute(query)).scalar_one_or_none()
    family = await db.get(FinancialFormFamily, imported.family_id) if imported else None
    if family is None or family.profile_id != profile.id:
        raise HTTPException(404, "Debt batch review not found")
    _body(imported)
    return imported, family


def _rebuild(body, batch_id):
    groups = {}
    existing = body.get("base_body", {}).get("debts") or []
    for file in body["files"]:
        for row in file.get("rows") or []:
            reference = _reference(row.get("loan_reference"))
            lender = row["values"].get("lender")
            key = f"ref:{_text(lender)}:{_reference_key(reference)}" if _full_reference(reference) and lender else f"source:{row['source_id']}"
            group = groups.setdefault(key, {"id": str(uuid5(NAMESPACE_URL, f"debt-batch:{body['family_id']}:{key}")),
                "lender": lender or None, "loan_reference": reference, "reference_strength": "full" if _full_reference(reference) else "partial" if reference else "missing", "source_file_ids": [], "mappings": [], "warnings": []})
            if file["file_id"] not in group["source_file_ids"]:
                group["source_file_ids"].append(file["file_id"])
            group["warnings"] = list(dict.fromkeys([*group["warnings"], *(file.get("warnings") or [])]))
            for mapping in row["mappings"]:
                if any(item["field"] == mapping["field"] and item["value"] == mapping["value"] for item in group["mappings"]):
                    continue
                item = copy.deepcopy(mapping)
                item["key"] = f"{group['id']}:{item['field']}:{file['file_id']}:{row['ordinal']}"
                group["mappings"].append(item)
    candidates = []
    for group in groups.values():
        exact = [row for row in existing if _full_reference(group["loan_reference"]) and _full_reference(_row_reference(row)) and _reference_key(_row_reference(row)) == _reference_key(group["loan_reference"])
                 and _text(row.get("lender")) == _text(group["lender"]) and row.get("status", "active") == "active" and not row.get("duplicate_of_id")]
        legacy = [row for row in existing if _legacy_reference_match(row, group["loan_reference"])]
        possible = [row for row in existing if _similar_lender(row.get("lender"), group["lender"]) or row in legacy
                    or group["loan_reference"] and _reference_key(_row_reference(row)) == _reference_key(group["loan_reference"])]
        group["possible_duplicate_row_ids"] = [str(row["id"]) for row in possible]
        group["requires_separate_loan_confirmation"] = bool(possible)
        suggested = exact[0] if len(exact) == 1 else legacy[0] if len(legacy) == 1 and legacy[0].get("status", "active") == "active" and not legacy[0].get("duplicate_of_id") else None
        group["suggested_target_row_id"] = str(suggested["id"]) if suggested else None
        group["suggested_action"] = "update" if len(exact) == 1 else "review" if possible or not group["loan_reference"] else "create"
        if not group["loan_reference"]:
            group["warnings"].append("No reliable printed loan reference was read. Confirm a new or existing obligation explicitly; lender and amounts never establish identity.")
        if possible:
            group["warnings"].append("Possible existing or excluded obligation: review its identity. No row will be merged, removed, restored or marked duplicate automatically.")
        target = suggested or {}
        for mapping in group["mappings"]:
            mapping["before"] = target.get(mapping["field"])
            mapping["conflict"] = documents._conflict(mapping["before"], mapping["value"])
            if sum(item["field"] == mapping["field"] for item in group["mappings"]) > 1:
                mapping["conflict"] = True
        group["warnings"] = list(dict.fromkeys(group["warnings"]))
        candidates.append(group)
    body["candidates"] = candidates
    for file in body["files"]:
        file["candidate_ids"] = [row["id"] for row in candidates if file["file_id"] in row["source_file_ids"]]


def read(imported, current_revision=None):
    body = _body(imported)
    files = [{key: value for key, value in file.items() if key not in {"rows", "in_scope"}} for file in body["files"]]
    return {"id": str(imported.id), "family_id": str(imported.family_id), "base_revision": imported.base_revision,
            "status": imported.status, "idempotency_key": body["request_key"], "role": body["role"], "files": files,
            "candidates": copy.deepcopy(body.get("candidates") or []), "current_revision": current_revision,
            "stale": current_revision is not None and current_revision != imported.base_revision,
            "progress": {"total": len(files), "done": sum(file["status"] not in {"queued", "processing"} for file in files)}}


async def create(db, profile, *, file_ids, expected_revision, idempotency_key, family_id=None, role="supporting", actor=None):
    _staff(actor)
    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family_id, actor=actor)
    ids = list(dict.fromkeys(str(value) for value in file_ids))
    if not 1 <= len(ids) <= 20 or role not in documents.DOCUMENT_ROLES:
        raise HTTPException(422, "Choose one to twenty source files and a supported document role")
    fingerprint = workspace.body_hash({"files": sorted(ids), "revision": expected_revision, "role": role})
    prior = next((item for item in await _batches(db, family.id) if _body(item).get("request_key") == str(idempotency_key)), None)
    if prior:
        if _body(prior)["request_fingerprint"] != fingerprint:
            raise HTTPException(409, "This request key already belongs to a different batch")
        return read(prior, draft.revision)
    workspace._check_revision(draft, expected_revision)
    files, primary = [], None
    for value in ids:
        state = {"file_id": value, "file_name": "Unavailable file", "status": "queued", "candidate_ids": []}
        try:
            file = await workspace.source_file_for_profile(db, profile, UUID(value))
            primary = primary or file.id
            state.update(file_name=file.file_name, in_scope=True)
        except HTTPException:
            state.update(status="unavailable", error="This source is not available in this application file")
        files.append(state)
    if primary is None:
        raise HTTPException(404, "No selected sources are available on this file")
    body = {"import_scope": SCOPE, "profile_id": str(profile.id), "request_key": str(idempotency_key),
            "request_fingerprint": fingerprint, "role": role, "files": files, "candidates": [],
            "base_body": copy.deepcopy(draft.body), "family_id": str(family.id)}
    imported = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=primary, content_hash=fingerprint,
        base_revision=draft.revision, proposed_body=body, mappings=[], warnings=[], status="queued",
        created_at=workspace.now(), created_by_user_id=getattr(actor, "id", None))
    db.add(imported)
    await db.flush()
    return read(imported, draft.revision)


async def get(db, profile, batch_id):
    imported, family = await _load(db, profile, batch_id)
    _family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family.id)
    return read(imported, draft.revision)


async def list_reviews(db, profile, *, family_id=None):
    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family_id)
    return {"items": [read(item, draft.revision) for item in (await _batches(db, family.id))[:10]]}


async def retry(db, profile, batch_id, *, expected_revision, file_ids=None, actor=None):
    _staff(actor)
    imported, family = await _load(db, profile, batch_id)
    _family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family.id, actor=actor)
    imported, family = await _load(db, profile, batch_id, lock=True)
    workspace._check_revision(draft, expected_revision)
    if imported.status != "proposed" or imported.base_revision != expected_revision:
        raise HTTPException(409, {"code": "debt_batch_stale", "message": "Review these retained sources against the current draft in a new batch before retrying."})
    body = copy.deepcopy(_body(imported))
    requested = {str(value) for value in file_ids} if file_ids is not None else None
    if requested is not None and requested - {file["file_id"] for file in body["files"]}:
        raise HTTPException(422, "Unknown batch source file")
    for file in body["files"]:
        if file["status"] in {"unreadable", "unavailable"} and (requested is None or file["file_id"] in requested):
            file.update(status="queued")
            file.pop("error", None)
    if any(file["status"] == "queued" for file in body["files"]):
        imported.status = "queued"
    imported.proposed_body = body
    await db.flush()
    return read(imported, draft.revision)


async def refresh(db, profile, batch_id, *, expected_revision, idempotency_key, actor=None):
    imported, family = await _load(db, profile, batch_id)
    return await create(db, profile, file_ids=[file["file_id"] for file in _body(imported)["files"]],
        expected_revision=expected_revision, idempotency_key=idempotency_key, family_id=family.id,
        role=_body(imported)["role"], actor=actor)


async def _source(db, profile, file_id, *, expected_hash=None):
    file = await workspace.source_file_for_profile(db, profile, UUID(str(file_id)))
    from app.services.document_version_selection import document_file_ids

    if getattr(file, "source_kind", None) == "drafted_form" or str(getattr(file, "source_detail", "") or "").startswith("financial_version:") or getattr(file, "source_entity_type", None) in {"financial_form_version", "financial_version"}:
        raise HTTPException(409, "Choose uploaded loan evidence rather than a generated financial version")
    if file.id not in await document_file_ids(db, [file.id], usable=False):
        raise HTTPException(409, "This source has been superseded or is unavailable. Review its current document version.")
    raw = await asyncio.to_thread(workspace._read_file, file)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_hash and digest != expected_hash or file.content_hash and digest != file.content_hash:
        raise HTTPException(409, "The source bytes changed. Review the current source again.")
    return file, raw, digest


async def extract_file(db, profile, file_id, *, cache=()):
    file, raw, digest = await _source(db, profile, file_id)
    image_media = await asyncio.to_thread(validated_image_media, raw)
    if image_media is None and b"%PDF" not in raw[:1024]:
        raise HTTPException(422, "Choose a readable PDF or supported still image")
    cached = next((item for item in cache if item.get("content_hash") == digest and item.get("rows")), None)
    if cached:
        result = copy.deepcopy(cached)
        old = result["file_id"]
        result.update(file_id=str(file.id), file_name=file.file_name, status="duplicate", duplicate_of_file_id=old)
        for row in result["rows"]:
            for mapping in row["mappings"]:
                mapping["source"] = _remap_source(mapping["source"], str(file.id))
        return result
    body, references, warnings, pages = await workspace._extract_full_form(db, "debt_schedule", file, raw, extra_instructions=INSTRUCTIONS)
    if len(body.get("debts") or []) > 50:
        raise HTTPException(422, "This file has too many loan rows for one batch review. Split it into smaller sources.")
    rows = []
    for ordinal, raw_row in enumerate(body.get("debts") or [], 1):
        values = workspace._json(normalize_terms(raw_row))
        ref = references.get(f"r{ordinal}.loan_reference") or references.get(f"debts.r{ordinal}.loan_reference") or {}
        reference = _reference(ref.get("value")) if isinstance(ref, dict) else None
        if reference and _reference_key(reference) not in re.sub(r"[^A-Z0-9]", "", str(ref.get("quote") or "").upper()):
            reference = None
            warnings.append("A loan reference lacked a matching printed source excerpt and was left unknown for review.")
        if not any(values.get(key) not in (None, "") for key in workspace.financial_statements.DEBT_COLUMNS if key not in {"lender", "notes", "debt_type"}) and not (reference and values.get("lender")):
            continue
        equivalent = monthly_equivalent(values.get("payment_amount"), values.get("payment_frequency"))
        if equivalent is not None:
            if values.get("monthly_payment_source") == "stated" and Decimal(values["monthly_payment"]) != equivalent:
                warnings.append("The printed monthly payment differs from its calculated native-payment equivalent. Review both source terms.")
            values["monthly_payment"], values["monthly_payment_source"] = str(equivalent), "native_terms"
        row_references = {}
        for key, reference_value in references.items():
            for prefix in (f"r{ordinal}.", f"debts.r{ordinal}.", f"ds.r{ordinal}."):
                if str(key).startswith(prefix):
                    row_references["r1." + str(key)[len(prefix):]] = reference_value
                    break
        mappings = workspace._mapping_records("debt_schedule", {"debts": [values]}, file, None, digest, row_references, pages)
        for mapping in mappings:
            mapping["field"] = mapping.pop("key").rpartition(".")[2]
            if image_media:
                mapping["source"].pop("page", None)
                mapping["source"].update(kind="image", content_type=image_media)
                mapping["confidence"] = "needs_review"
        native = {item["field"]: item for item in mappings}
        if equivalent is not None and "monthly_payment" in native:
            native["monthly_payment"].update(derived=True)
            native["monthly_payment"]["source"] = {"file_id": str(file.id), "content_hash": digest, "kind": "calculation",
                "calculation": "payment_amount × monthly multiplier", "multiplier": str(FREQUENCY_MONTHLY_MULT[values["payment_frequency"]]),
                "derived_from": {key: copy.deepcopy(native[key]["source"]) for key in ("payment_amount", "payment_frequency") if key in native}}
        rows.append({"ordinal": ordinal, "source_id": f"{digest}:{ordinal}", "loan_reference": reference,
                     "values": {key: value for key, value in values.items() if key in workspace.financial_statements.DEBT_COLUMNS}, "mappings": mappings})
    if not rows:
        raise HTTPException(422, "No readable printed loan terms were found. The original is retained for manual review.")
    return {"file_id": str(file.id), "file_name": file.file_name, "content_hash": digest,
            "status": "ambiguous" if len(rows) > 1 and any(not row["loan_reference"] for row in rows) else "ready",
            "warnings": warnings, "rows": rows}


def _remap_source(source, file_id):
    result = copy.deepcopy(source)
    if "file_id" in result:
        result["file_id"] = file_id
    if isinstance(result.get("derived_from"), dict):
        result["derived_from"] = {key: _remap_source(value, file_id) for key, value in result["derived_from"].items()}
    return result


async def run_batch(batch_id):
    """One leased worker, sequential per-file sessions, progress committed durably."""
    token = str(uuid4())
    async with SessionLocal() as db:
        imported = (await db.execute(select(FinancialFormImport).where(FinancialFormImport.id == batch_id).with_for_update(skip_locked=True))).scalar_one_or_none()
        if imported is None or imported.status not in {"queued", "processing"}:
            return False
        body = copy.deepcopy(_body(imported))
        if imported.status == "processing" and body.get("lease_until", "") > workspace.now().isoformat():
            return False
        body.update(worker_token=token, lease_until=(workspace.now() + timedelta(seconds=LEASE_SECONDS)).isoformat())
        imported.proposed_body, imported.status = body, "processing"
        await db.commit()
        pending = [file["file_id"] for file in body["files"] if file["status"] in {"queued", "processing"}]
        profile_id, actor_id = UUID(body["profile_id"]), imported.created_by_user_id
    for file_id in pending:
        result = None
        async with SessionLocal() as db:
            imported = await db.get(FinancialFormImport, batch_id)
            if imported is None or _body(imported).get("worker_token") != token:
                return False
            profile = await db.get(ApplicationProfile, profile_id)
            actor = await db.get(User, actor_id) if actor_id else None
            try:
                progress = copy.deepcopy(_body(imported))
                for file in progress["files"]:
                    if file["file_id"] == file_id:
                        file["status"] = "processing"
                imported.proposed_body = progress
                await db.commit()
                _staff(actor)
                from app.services import application_profiles

                await application_profiles.load_profile(db, profile_id, actor)
                cache = [file for prior in await _batches(db, imported.family_id) for file in _body(prior)["files"] if file.get("rows")]
                result = await extract_file(db, profile, file_id, cache=cache)
                await db.commit()
            except Exception as error:
                await db.rollback()
                detail = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else "This file could not be read. Retry or review its original manually."
                result = {"file_id": file_id, "status": "unavailable" if isinstance(error, HTTPException) and error.status_code in {403, 404, 409} else "unreadable", "error": detail[:500]}
                log.info("Debt batch source review failed batch=%s file=%s kind=%s", batch_id, file_id, type(error).__name__)
        async with SessionLocal() as db:
            imported = (await db.execute(select(FinancialFormImport).where(FinancialFormImport.id == batch_id).with_for_update())).scalar_one_or_none()
            if imported is None or _body(imported).get("worker_token") != token:
                return False
            body = copy.deepcopy(_body(imported))
            for file in body["files"]:
                if file["file_id"] == file_id:
                    file.update(result)
            _rebuild(body, imported.id)
            body["lease_until"] = (workspace.now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
            imported.proposed_body = body
            await db.commit()
    async with SessionLocal() as db:
        imported = (await db.execute(select(FinancialFormImport).where(FinancialFormImport.id == batch_id).with_for_update())).scalar_one_or_none()
        if imported is not None and _body(imported).get("worker_token") == token:
            imported.status = "proposed"
            await db.commit()
    return True


async def drain_pending(limit=2):
    async with SessionLocal() as db:
        ids = list((await db.execute(select(FinancialFormImport.id).where(
            FinancialFormImport.proposed_body["import_scope"].astext == SCOPE,
            or_(FinancialFormImport.status == "queued", (FinancialFormImport.status == "processing") &
                (FinancialFormImport.proposed_body["lease_until"].astext < workspace.now().isoformat())),
        ).order_by(FinancialFormImport.created_at).limit(limit))).scalars().all())
    for batch_id in ids:
        await run_batch(batch_id)
    return len(ids)


async def apply(db, profile, batch_id, *, expected_revision, idempotency_key, decisions, actor=None):
    _staff(actor)
    imported, family = await _load(db, profile, batch_id)
    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family.id, actor=actor)
    imported, _ = await _load(db, profile, batch_id, lock=True)
    body = copy.deepcopy(_body(imported))
    payload = workspace._json([item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in decisions])
    fingerprint = workspace.body_hash(sorted(payload, key=lambda item: item["candidate_id"]))
    receipt = body.get("apply_receipt")
    if receipt:
        if receipt["key"] != str(idempotency_key) or receipt["fingerprint"] != fingerprint:
            raise HTTPException(409, "This batch was already applied with a different review decision")
        return await workspace.load_workspace(db, profile, kind="debt_schedule", family_id=family.id, actor=actor)
    workspace._check_revision(draft, expected_revision)
    if imported.base_revision != draft.revision:
        raise HTTPException(409, {"code": "debt_batch_stale", "message": "The shared draft changed. Review the retained sources against the current draft before applying."})
    if imported.status != "proposed":
        raise HTTPException(409, {"code": "debt_batch_processing", "message": "Wait for all files to finish reading before applying this review."})
    candidates = {item["id"]: item for item in body.get("candidates") or []}
    if len(payload) != len(candidates) or {item["candidate_id"] for item in payload} != set(candidates):
        raise HTTPException(422, "Review every detected candidate exactly once, including any you skip")
    chosen = [item for item in payload if item["action"] != "skip"]
    next_body = copy.deepcopy(draft.body)
    row_by_id = {str(row["id"]): row for row in next_body.get("debts") or []}
    row_fields = {}
    needed = set()
    for decision in chosen:
        if decision.get("confirmed") is not True:
            raise HTTPException(422, "Confirm each loan/source association before applying")
        candidate = candidates[decision["candidate_id"]]
        selected = set(decision.get("selected_keys") or [])
        available = {item["key"]: item for item in candidate["mappings"]}
        if selected - available.keys():
            raise HTTPException(422, "Unknown field mapping selected")
        mappings = [available[key] for key in selected]
        if len({item["field"] for item in mappings}) != len(mappings):
            raise HTTPException(422, "Choose only one source value for each field")
        if decision["action"] == "create":
            if candidate.get("requires_separate_loan_confirmation") and decision.get("confirm_separate_loan") is not True:
                raise HTTPException(422, "Confirm this is a separate loan despite the possible existing or excluded match, or review/restore the existing obligation first")
            if decision.get("target_row_id") or not any(item["field"] == "lender" and str(item["value"] or "").strip() for item in mappings):
                raise HTTPException(422, "Select a printed lender value when creating a new obligation")
            row_id = str(uuid5(NAMESPACE_URL, f"debt-batch-row:{batch_id}:{candidate['id']}"))
            if row_id in row_by_id:
                raise HTTPException(409, "This candidate row already exists")
            row = {"id": row_id, "origin": "admin", "status": "active", "evidence_links": []}
            next_body.setdefault("debts", []).append(row)
            row_by_id[row_id] = row
        else:
            row_id = str(decision.get("target_row_id") or "")
            row = row_by_id.get(row_id)
            if row is None or row.get("status", "active") != "active" or row.get("duplicate_of_id"):
                raise HTTPException(422, "Choose an active obligation on this working schedule")
            if decision["action"] == "link" and selected or decision["action"] == "update" and not selected:
                raise HTTPException(422, "Update requires selected fields; evidence-only linking must not select fields")
        prior_ref, source_ref = _row_reference(row), candidate.get("loan_reference")
        if not _compatible_reference(prior_ref, source_ref):
            raise HTTPException(409, "The printed loan reference conflicts with the selected existing obligation")
        desired = {f"{row_id}.{item['field']}": item["value"] for item in mappings}
        prior = row_fields.setdefault(row_id, {})
        for key, value in desired.items():
            if key in prior and documents._conflict(prior[key], value):
                raise HTTPException(422, "Two candidates propose contradictory values for the same loan field")
            prior[key] = value
        temporary = SimpleNamespace(proposed_body={"target_debt_id": row_id, "debts": [{"id": row_id}],
            "import_context": {"kind": "debt_document", "target_row_id": row_id, "role": body["role"]}})
        documents.validate_targeted_import(temporary, kind="debt_schedule", current_body=next_body, mappings=desired)
        for item in mappings:
            row[item["field"]] = copy.deepcopy(item["value"])
            next_body.setdefault("field_sources", {})[f"{row_id}.{item['field']}"] = copy.deepcopy(item["source"])
        if source_ref and not prior_ref:
            row["loan_reference"] = source_ref
        row["evidence_links"] = workspace._merge_import_links(row.get("evidence_links") or [], [
            {"file_id": source_id, "role": body["role"], "is_current": False} for source_id in candidate["source_file_ids"]])
        needed.update(candidate["source_file_ids"])
    for file in body["files"]:
        if file["file_id"] in needed:
            await _source(db, profile, file["file_id"], expected_hash=file.get("content_hash"))
    if chosen:
        next_body["no_debt"] = False
        await workspace.stage_draft(db, profile, family, draft, next_body, actor=actor)
    body["apply_receipt"] = {"key": str(idempotency_key), "fingerprint": fingerprint, "actor_id": str(actor.id), "at": workspace.now().isoformat()}
    imported.proposed_body, imported.status = body, "applied"
    await db.flush()
    return await workspace.load_workspace(db, profile, kind="debt_schedule", family_id=family.id, actor=actor)
