"""Source-derived debt drafts, with explicit review before publication."""

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select

from app.models.bucket import BucketFileAnalysis
from app.services.debt_terms import DebtTermsError, normalize_terms
from app.services.financial_version_selection import (
    active_debt_schedule_rows,
    debt_schedule_completion,
)

DOCUMENT_CLASSES = {"debt_schedule", "collateral_debt_evidence", "floorplan_mca_inventory", "mca_agreement",
                    "advance_agreement", "loan_agreement", "financing_contract", "payoff_or_mortgage_statement", "payoff_letter"}
PAYOFF_CLASSES = {"payoff_or_mortgage_statement", "payoff_letter"}


def _name(value):
    words = re.sub(r"[^a-z0-9 ]", " ", str(value or "").casefold()).split()
    return " ".join(word for word in words if word not in {"llc", "inc", "incorporated", "corp", "corporation", "ltd"})


def _entity(value):
    # Legal suffixes remain meaningful; Acme LLC is not Acme Inc.
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def blocked_reason(body, accepted=False):
    if body.get("no_debt") is True:
        return "The business has an explicit no-debt declaration. Review that declaration before drafting obligations."
    if accepted:
        return "A completed saved debt schedule is already authoritative. Edit it or map a document to a selected obligation instead."
    if debt_schedule_completion(body)["complete"]:
        return "The working debt schedule is complete. Review and save it, or edit a selected obligation instead."
    return None


def _first(raw, *keys):
    return next((raw[key] for key in keys if raw.get(key) not in (None, "")), None)


def _raw_rows(facts):
    for key in ("debts", "loans", "advances", "obligations", "payoffs"):
        if isinstance(facts.get(key), list) and any(isinstance(row, dict) for row in facts[key]):
            return [row for row in facts[key] if isinstance(row, dict)]
    return [facts] if _first(facts, "lender", "funder", "creditor") else []


def _loan_row(raw):
    from app.services import financial_statements as statements

    fields = {
        "lender": ("lender", "funder", "creditor", "lender_name"),
        "original_amount": ("original_amount", "funded_amount", "purchase_price", "loan_amount", "original_balance"),
        "balance": ("current_balance", "balance", "outstanding_balance"),
        "monthly_payment": ("monthly_payment",), "payment_amount": ("payment_amount", "per_payment_amount", "remittance_amount"),
        "payment_frequency": ("payment_frequency", "frequency", "cadence"),
        "factor_rate": ("factor_rate", "factor"), "rate": ("rate", "rate_pct", "interest_rate", "apr"),
        "payoff_amount": ("payoff_amount",), "payoff_date": ("payoff_date", "good_through_date"),
        "originated_on": ("originated_on", "origination_date", "originated_date", "origin_date"), "maturity_on": ("maturity_on", "maturity_date"),
        "collateral": ("collateral", "secured_by"), "notes": ("notes",),
    }
    row = {field: _first(raw, *keys) for field, keys in fields.items()}
    if "originated_on" in raw:
        row["originated_on"] = raw["originated_on"]
    row["lender"] = str(row.get("lender") or "").strip()[:180]
    if not row["lender"]:
        return None, ["No lender was identified in this source row."]
    row["debt_type"] = statements.normalize_debt_type(_first(raw, "debt_type", "type", "category"))
    warnings = []
    from app.services.debt_schedule_review import cadence_of

    printed_frequency = row["payment_frequency"]
    row["payment_frequency"] = cadence_of(printed_frequency)
    if printed_frequency and row["payment_frequency"] is None:
        warnings.append("The printed payment frequency needs review; no monthly equivalent was invented.")
    for field in ("original_amount", "balance", "payoff_amount", "payment_amount", "monthly_payment"):
        value = row[field]
        row[field] = statements._money_or_none(value)
        if row[field] is not None and row[field] < 0:
            row[field] = None
        if value not in (None, "") and row[field] is None:
            warnings.append(f"The printed {field.replace('_', ' ')} could not be used as a nonnegative debt amount; it remains unknown.")
    for field in ("rate", "factor_rate"):
        try:
            row[field] = normalize_terms({field: row[field]})[field]
        except DebtTermsError:
            row[field] = None
            warnings.append(f"The printed {field.replace('_', ' ')} remains unknown and needs review.")
    for field in ("notes", "collateral"):
        row[field] = str(row[field])[:2000] if isinstance(row[field], (str, int, float)) else None
    for field in ("originated_on", "maturity_on", "payoff_date"):
        value = statements._date(row[field]) if row[field] else None
        row[field] = value.isoformat() if value else None
    try:
        row = normalize_terms(row)
    except DebtTermsError:
        # An unsupported cadence must not silently become a monthly payment.
        row["payment_frequency"] = None
        warnings.append("The printed payment frequency needs review; no monthly equivalent was invented.")
        try:
            row = normalize_terms(row)
        except DebtTermsError:
            return None, ["Some source terms could not be read as financial values. Review the original document."]
    return row, warnings


def document_proposals(body, sources, family_id, preferred_file_id=None):
    """Prefer the current uploaded schedule; contracts support, never inflate it."""
    usable = [source for source in sources if source["classification"] in DOCUMENT_CLASSES and _raw_rows(source["facts"])]
    schedules = [source for source in usable if source["classification"] == "debt_schedule"]
    preferred = next((source for source in schedules if source["file_id"] == str(preferred_file_id)), None)
    primary = preferred or (max(schedules, key=lambda source: (source.get("created_at") or "", source["file_id"])) if schedules else None)
    selected = [primary] if primary else usable
    proposals = {}
    for source in selected:
        for ordinal, raw in enumerate(_raw_rows(source["facts"])):
            row, warnings = _loan_row(raw)
            if row is None:
                continue
            borrower = _first(raw, "borrower_name", "borrower_entity", "business_name") or _first(source["facts"], "borrower_name", "borrower_entity", "business_name")
            if not borrower and source["classification"] == "debt_schedule" and isinstance(source["facts"].get("entity"), str):
                borrower = source["facts"]["entity"]
            mismatch = bool(borrower and body.get("business_name") and _entity(borrower) != _entity(body["business_name"]))
            if mismatch:
                warnings.append("The document names a different borrower. Confirm its business association before importing these figures.")
            elif not borrower:
                warnings.append("The borrower identity was not established; confirm this source belongs to the business.")
            reference = _first(raw, "loan_reference", "loan_number", "loan_account_number")
            fingerprint = sha256(f"{_entity(row['lender'])}|{_entity(borrower)}|{str(reference).strip().casefold()}".encode()).hexdigest() if reference and borrower else None
            signature = sha256((source["content_hash"] + ":" + str(ordinal)).encode()).hexdigest()
            proposal_id = "document:" + signature[:40]
            row_id = str(uuid5(NAMESPACE_URL, f"{family_id}:{proposal_id}"))
            role = "payoff" if source["classification"] in PAYOFF_CLASSES else "supporting" if primary else "agreement"
            refs = [{"file_id": source["file_id"], "content_hash": source["content_hash"], "analysis_id": source.get("analysis_id"), "row_index": ordinal,
                     **({"page": raw["page"]} if isinstance(raw.get("page"), int) and raw["page"] > 0 else {})}]
            links = [{"file_id": source["file_id"], "role": role, "is_current": False}]
            possible_files = []
            for support in usable:
                if support is source or support["classification"] == "debt_schedule":
                    continue
                for support_ordinal, other in enumerate(_raw_rows(support["facts"])):
                    if _name(_first(other, "lender", "funder", "creditor")) != _name(row["lender"]):
                        continue
                    other_ref = _first(other, "loan_reference", "loan_number", "loan_account_number")
                    other_borrower = _first(other, "borrower_name", "business_name") or _first(support["facts"], "borrower_name", "business_name")
                    exact = bool(reference and other_ref and str(reference).strip().casefold() == str(other_ref).strip().casefold()
                                 and borrower and other_borrower and _entity(borrower) == _entity(other_borrower)
                                 and _entity(_first(other, "lender", "funder", "creditor")) == _entity(row["lender"]))
                    if exact:
                        links.append({"file_id": support["file_id"], "role": "payoff" if support["classification"] in PAYOFF_CLASSES else "agreement", "is_current": False})
                        refs.append({"file_id": support["file_id"], "content_hash": support["content_hash"], "analysis_id": support.get("analysis_id"), "row_index": support_ordinal})
                    else:
                        possible_files.append(support["file_id"])
            if possible_files:
                warnings.append("Other documents name this lender but cannot safely identify the same loan. Link them after reviewing the loan reference.")
            existing = [old for old in body.get("debts", []) if
                        (old.get("debt_intake_source") or {}).get("proposal_id") == proposal_id
                        or (old.get("debt_intake_source") or {}).get("file_id") == source["file_id"] and (old.get("debt_intake_source") or {}).get("row_index") == ordinal
                        or fingerprint and (old.get("debt_intake_source") or {}).get("loan_fingerprint") == fingerprint]
            excluded = [old for old in body.get("debts", []) if old.get("status", "active") != "active" or old.get("duplicate_of_id")]
            def excluded_identity_matches(old, lender=row["lender"], new_fingerprint=fingerprint):
                if _name(old.get("lender")) != _name(lender):
                    return False
                prior = (old.get("debt_intake_source") or {}).get("loan_fingerprint")
                old_reference = _first(old, "loan_reference", "loan_number", "loan_account_number")
                if prior is None and old_reference and body.get("business_name"):
                    prior = sha256(f"{_entity(old['lender'])}|{_entity(body['business_name'])}|{str(old_reference).strip().casefold()}".encode()).hexdigest()
                return not (new_fingerprint and prior and new_fingerprint != prior)
            excluded_match = any(excluded_identity_matches(old) for old in excluded)
            reason = "This source row is already represented in the working schedule." if existing else "A matching excluded obligation needs review before it can be restored." if excluded_match else "Confirm the document's business association first." if mismatch else None
            row.update(id=row_id, status="active", origin="ai_draft", count_in_dscr=True,
                       evidence_links=links, debt_intake_source={"mode": "documents", "proposal_id": proposal_id, "file_id": source["file_id"], "row_index": ordinal,
                                                               "loan_fingerprint": fingerprint, "source_refs": deepcopy(refs)})
            names = {item["file_id"]: item["file_name"] for item in usable}
            proposal = {"id": proposal_id, "lender": row["lender"], "body": row, "file_ids": [link["file_id"] for link in links], "file_names": list(dict.fromkeys(names[link["file_id"]] for link in links)),
                        "warnings": warnings, "eligible": reason is None, "reason": reason, "possible_supporting_file_ids": sorted(set(possible_files)),
                        "source_confidence": source.get("confidence"), "source_refs": refs}
            group_key = fingerprint if fingerprint and primary is None else proposal_id
            previous = proposals.get(group_key)
            if previous is None:
                proposals[group_key] = proposal
            else:
                previous["file_ids"] = sorted(set(previous["file_ids"] + proposal["file_ids"]))
                previous["file_names"] = list(dict.fromkeys(previous["file_names"] + proposal["file_names"]))
                previous["source_refs"].extend(refs)
                previous["body"]["debt_intake_source"]["source_refs"].extend(refs)
                old_links = previous["body"]["evidence_links"]
                old_links.extend(link for link in links if (link["file_id"], link["role"]) not in {(item["file_id"], item["role"]) for item in old_links})
                if fingerprint:
                    for field, value in row.items():
                        if field in {"id", "status", "origin", "count_in_dscr", "evidence_links", "debt_intake_source"}:
                            continue
                        old = previous["body"].get(field)
                        if old in (None, "") and value not in (None, ""):
                            previous["body"][field] = value
                        elif old not in (None, "") and value not in (None, "") and str(old) != str(value):
                            previous["warnings"].append(f"Supporting documents disagree on {field.replace('_', ' ')}. The first source value is retained for review.")
                previous["warnings"] = list(dict.fromkeys(previous["warnings"] + warnings))
                previous["eligible"] = previous["eligible"] and proposal["eligible"]
                previous["reason"] = previous["reason"] or reason
    for proposal in proposals.values():
        source_signature = json.dumps({"refs": sorted(proposal["source_refs"], key=lambda ref: (ref["file_id"], ref["row_index"])),
                                       "values": {key: value for key, value in proposal["body"].items() if key not in {"id", "debt_intake_source"}}}, default=str, sort_keys=True)
        proposal["id"] = "document:" + sha256(source_signature.encode()).hexdigest()[:40]
        proposal["body"]["id"] = str(uuid5(NAMESPACE_URL, f"{family_id}:{proposal['id']}"))
        proposal["body"]["debt_intake_source"]["proposal_id"] = proposal["id"]
    return list(proposals.values())


async def source_documents(db, profile):
    from app.services import financial_workspace as workspace
    from app.services.document_version_selection import filter_document_files

    result = []
    sources = await filter_document_files(db, await workspace.source_files_for_profile(db, profile), usable=False)
    for file in sources:
        if getattr(file, "status", "uploaded") != "uploaded":
            continue
        if getattr(file, "source_entity_type", None) in {"financial_form_version", "financial_version"}:
            continue
        query = select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id == file.id, BucketFileAnalysis.status == "completed")
        if file.content_hash:
            query = query.where(BucketFileAnalysis.content_hash == file.content_hash)
        analysis = (await db.execute(query.order_by(BucketFileAnalysis.created_at.desc()).limit(1))).scalar_one_or_none()
        if analysis is None or analysis.provider == "drafted_form" or not file.content_hash or analysis.content_hash != file.content_hash:
            continue
        facts = (analysis.analysis or {}).get("key_facts") or {}
        if not isinstance(facts, dict):
            continue
        result.append({"file_id": str(file.id), "file_name": file.file_name, "content_hash": analysis.content_hash,
                       "analysis_id": str(analysis.id), "classification": analysis.classification, "confidence": analysis.confidence,
                       "facts": facts, "created_at": file.created_at.isoformat() if file.created_at else ""})
    return result


async def proposals(db, profile, *, mode, expected_revision, family_id=None, actor=None):
    from app.models.financial_form_version import FinancialFormVersion
    from app.services import financial_workspace as workspace
    from app.services.financial_version_selection import accepted_debt_schedule_snapshot

    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family_id, actor=actor)
    workspace._check_revision(draft, expected_revision)
    accepted = await accepted_debt_schedule_snapshot(db, profile.id)
    reason = blocked_reason(draft.body, accepted is not None)
    rows, warnings, count = [], [], 0
    if reason is None and mode == "documents":
        documents = await source_documents(db, profile)
        current = await db.get(FinancialFormVersion, family.current_version_id) if family.current_version_id else None
        rows = document_proposals(draft.body, documents, family.id, getattr(current, "source_file_id", None))
        count = len({file_id for row in rows for file_id in row["file_ids"]})
    elif reason is None and mode == "banking":
        from app.services import financial_debt_review

        analyses, accounts = await financial_debt_review.source_context(db, profile)
        for candidate in financial_debt_review.build_candidates(draft.body, analyses, accounts, profile.bank_account_aliases or {}):
            if not (candidate.get("is_debt") or candidate.get("is_card")):
                continue
            bank_signature = json.dumps({"candidate_key": candidate["key"], "payee": candidate["payee"],
                                         "sources": candidate["source_refs"], "observed_monthly_average": candidate["monthly_average"]}, default=str, sort_keys=True)
            proposal_id = "banking:" + sha256(bank_signature.encode()).hexdigest()[:40]
            row = {"id": str(uuid5(NAMESPACE_URL, f"{family.id}:{proposal_id}")), "lender": candidate["payee"], "origin": "ai_draft", "status": "active",
                   "balance": None, "monthly_payment": None, "debt_type": None, "count_in_dscr": True,
                   "evidence_links": [{"file_id": file_id, "role": "supporting", "is_current": False} for file_id in candidate["file_ids"]],
                   "debt_intake_source": {"mode": "banking", "proposal_id": proposal_id, "source_refs": candidate["source_refs"], "candidate_key": candidate["key"]},
                   "evidence": {"observed_monthly_average": candidate["monthly_average"]}}
            represented = any((old.get("debt_intake_source") or {}).get("proposal_id") == proposal_id
                              or (old.get("debt_intake_source") or {}).get("candidate_key") == candidate["key"] for old in draft.body.get("debts", []))
            excluded_match = any(_name(old.get("lender")) == _name(row["lender"]) and (old.get("status", "active") != "active" or old.get("duplicate_of_id")) for old in draft.body.get("debts", []))
            blocked = candidate["status"] != "unreviewed" or represented or excluded_match
            rows.append({"id": proposal_id, "lender": row["lender"], "body": row, "file_ids": candidate["file_ids"], "file_names": [],
                         "warnings": ["Bank payments suggest an obligation but do not establish its current balance or contract terms. Review the source before entering those fields."],
                         "eligible": not blocked, "reason": "This payment is already linked, disabled or represented. Review its existing row instead." if blocked else None,
                         "source_refs": candidate["source_refs"]})
        count = len({file_id for row in rows for file_id in row["file_ids"]})
    elif mode not in {"documents", "banking"}:
        raise HTTPException(422, "Choose documents or banking activity")
    if not rows and reason is None:
        warnings.append("No current extracted loan rows were found. Re-read the uploaded agreements or enter an obligation manually; missing figures have not been invented.")
    return {"family_id": str(family.id), "revision": draft.revision, "mode": mode, "blocked_reason": reason,
            "proposals": rows, "source_count": count, "warnings": warnings}


def apply_selected(body, proposal_rows, selected_ids, *, idempotency_key, mode):
    result = deepcopy(body)
    receipt = {"mode": mode, "selected_ids": sorted(set(selected_ids))}
    prior = (result.get("debt_intake_receipts") or {}).get(idempotency_key)
    if prior is not None:
        if prior != receipt:
            raise HTTPException(409, "This request key was used for another draft intake")
        return result, False
    available = {row["id"]: row for row in proposal_rows}
    if not selected_ids or any(key not in available or not available[key]["eligible"] for key in selected_ids):
        raise HTTPException(409, "The selected source proposals changed or need review. Refresh before applying them.")
    rows = result.setdefault("debts", [])
    seen = {str(row["id"]) for row in rows}
    for key in dict.fromkeys(selected_ids):
        proposed = deepcopy(available[key]["body"])
        if str(proposed["id"]) in seen:
            raise HTTPException(409, "This obligation is already represented; no row was restored or duplicated")
        rows.append(proposed)
        seen.add(str(proposed["id"]))
    result.setdefault("debt_intake_receipts", {})[idempotency_key] = receipt
    result["debt_intake_review"] = {"status": "pending_review", "mode": mode, "proposal_count": len(selected_ids), "at": datetime.now(UTC).isoformat()}
    return result, True


async def apply(db, profile, *, mode, expected_revision, selected_ids, idempotency_key, family_id=None, actor=None):
    from app.services import financial_workspace as workspace

    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family_id, actor=actor)
    prior = (draft.body.get("debt_intake_receipts") or {}).get(idempotency_key)
    if prior is not None:
        apply_selected(draft.body, [], selected_ids, idempotency_key=idempotency_key, mode=mode)
        return await workspace.load_workspace(db, profile, kind="debt_schedule", family_id=family.id, actor=actor)
    preview = await proposals(db, profile, mode=mode, expected_revision=expected_revision, family_id=family.id, actor=actor)
    if preview["blocked_reason"]:
        raise HTTPException(409, preview["blocked_reason"])
    body, _ = apply_selected(draft.body, preview["proposals"], selected_ids, idempotency_key=idempotency_key, mode=mode)
    await workspace.stage_draft(db, profile, family, draft, body, actor=actor)
    return await workspace.load_workspace(db, profile, kind="debt_schedule", family_id=family.id, actor=actor)


async def initial_seed(db, profile, family, draft):
    """Safe automation entry point: virgin drafts only, no publication or emails."""
    from app.models.financial_form_version import FinancialFormVersion
    from app.services import financial_workspace as workspace
    from app.services.financial_version_selection import accepted_debt_schedule_snapshot

    if draft.revision != 0 or draft.body.get("debts") or draft.body.get("no_debt") is True or draft.body.get("reconciliation_history") or draft.body.get("debt_intake_review"):
        return {"seeded": False, "reason": "The working draft has already been edited or reviewed."}
    if await accepted_debt_schedule_snapshot(db, profile.id) is not None:
        return {"seeded": False, "reason": "A complete saved debt schedule is already current."}
    sources = await source_documents(db, profile)
    current = await db.get(FinancialFormVersion, family.current_version_id) if family.current_version_id else None
    proposed = [row for row in document_proposals(draft.body, sources, family.id, getattr(current, "source_file_id", None)) if row["eligible"] and row.get("source_confidence") == "high"]
    if not proposed:
        return {"seeded": False, "reason": "No current high-confidence source rows are available for an initial draft."}
    body, _ = apply_selected(draft.body, proposed, [row["id"] for row in proposed], idempotency_key="initial-document-seed", mode="documents")
    await workspace.stage_draft(db, profile, family, draft, body, actor=None)
    return {"seeded": True, "proposal_count": len(proposed), "revision": draft.revision}


async def seed_initial_for_profile(db, profile, *, family_id=None):
    """Maintenance entry point using the same locks and guards as first open.

    The caller owns commit/rollback. The result contains counts only, with no
    financial values, publication, PDF generation, storage writes or email.
    """
    from app.services import financial_workspace as workspace

    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family_id, actor=None)
    before_pointer = family.current_version_id
    before = len(draft.body.get("debts") or [])
    result = await initial_seed(db, profile, family, draft)
    return {**result, "family_id": str(family.id), "revision": draft.revision,
            "rows_before": before, "rows_after": len(draft.body.get("debts") or []),
            "active_rows": len(active_debt_schedule_rows(draft.body)),
            "original_pointer_unchanged": family.current_version_id == before_pointer,
            "all_rows_ai_draft": all(row.get("origin") == "ai_draft" for row in draft.body.get("debts") or []),
            "published": False, "emails_sent": 0}
