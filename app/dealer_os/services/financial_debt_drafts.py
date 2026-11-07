"""Legacy dealer editors/extraction stage managed debt; publication stays explicit."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models.application_profile import ApplicationProfile
from app.services import (
    financial_statements,
    financial_version_selection,
    financial_workspace,
    financial_workspace_legacy,
)
from app.services.debt_terms import normalize_terms
from app.services.loan_progress import originated_on

from . import refinance
from .vendors import normalize_vendor


@dataclass
class ManagedDebtDraft:
    profile: Any
    snapshot: dict


async def managed_context(db: Any, dealer_id: UUID, kind: str = "debt_schedule") -> ManagedDebtDraft | None:
    if not isinstance(getattr(db, "info", None), dict) or db.info.get("financial_workspace_publishing"):
        return None
    profile = (await db.execute(select(ApplicationProfile).where(
        ApplicationProfile.dealer_id == dealer_id,
    ))).scalar_one_or_none()
    if profile is None:
        return None
    snapshot = await financial_workspace_legacy.managed_snapshot(db, profile, kind)
    return ManagedDebtDraft(profile, snapshot) if snapshot is not None else None


def inactive(row: dict) -> bool:
    return row.get("status", "active") != "active" or bool(row.get("duplicate_of_id"))


def read_row(row: dict, snapshot: dict) -> dict:
    return {
        **row, "category": row.get("debt_type") or row.get("category") or "loan",
        **{key: financial_statements._blank_or_amount(row.get(key)) for key in
           ("original_amount", "monthly_payment", "balance", "rate", "payment_amount", "factor_rate", "payoff_amount")},
        "originated_on": originated_on(row),
        "maturity_on": row.get("maturity_on") or None, "term_months": row.get("term_months") or None,
        "origin": row.get("origin") or row.get("owner") or "ai_draft",
        "status": row.get("status", "active"), "count_in_dscr": row.get("count_in_dscr", True),
        "draft_pending": bool(snapshot["draft"]["dirty"]),
        "draft_family_id": snapshot["family_id"], "draft_revision": snapshot["draft"]["revision"],
    }


def monthly(row: dict) -> float:
    return refinance.monthly_equivalent(SimpleNamespace(**{
        "monthly_payment": financial_statements._blank_or_amount(row.get("monthly_payment")),
        "payment_amount": financial_statements._blank_or_amount(row.get("payment_amount")),
        "payment_frequency": row.get("payment_frequency") or None,
    }))


def accepted_debt_views(published_rows: list, accepted_rows: list[dict]) -> list:
    """Read immutable financial terms, retaining current banking annotations."""
    live = {str(row.id): row for row in published_rows}
    result = []
    for raw in accepted_rows:
        if inactive(raw) or not str(raw.get("lender") or "").strip():
            continue
        raw = normalize_terms(raw)
        cleaned = (financial_statements.debt_rows_from_body({"debts": [raw]}) or [{}])[0]
        row = live.get(str(raw.get("id")))
        values = {**raw, **cleaned, "id": UUID(str(raw["id"])),
                  "category": raw.get("debt_type") or raw.get("category") or "loan",
                  "origin": raw.get("origin") or raw.get("owner") or "client_form",
                  "status": "active", "count_in_dscr": raw.get("count_in_dscr", True),
                  "evidence": raw.get("evidence") or {},
                  "document_id": raw.get("document_id") or None,
                  "vendor_key": row.vendor_key if row is not None else raw.get("vendor_key")}
        values.update({key: financial_statements._blank_or_amount(raw.get(key)) for key in (
            "monthly_payment", "balance", "rate", "payment_amount", "factor_rate", "payoff_amount",
        )})
        if row is not None and isinstance(getattr(row, "evidence", None), dict) and row.evidence.get("payee_link"):
            values["evidence"] = {**(values.get("evidence") or {}), "payee_link": row.evidence["payee_link"]}
        result.append(SimpleNamespace(**values))
    return result


async def save_body(db: Any, context: ManagedDebtDraft, body: dict, *, actor: Any = None) -> dict:
    result = await financial_workspace.save_draft(
        db, context.profile, kind="debt_schedule", family_id=UUID(context.snapshot["family_id"]),
        expected_revision=context.snapshot["draft"]["revision"], body=body, actor=actor,
    )
    context.snapshot = result
    return result


async def edit_row(db: Any, context: ManagedDebtDraft, *, op: str, fields: dict | None = None,
                   row_id: UUID | None = None, actor: Any = None) -> dict:
    body = deepcopy(context.snapshot["body"])
    rows = body.setdefault("debts", [])
    row = next((item for item in rows if str(item["id"]) == str(row_id)), None)
    if op == "create":
        row = {"id": str(uuid4()), "origin": "admin", "status": "active", "count_in_dscr": True, "evidence_links": []}
        rows.append(row)
    elif row is None:
        raise HTTPException(404, "Debt row not found for this client")
    elif inactive(row):
        raise HTTPException(409, "Restore this deleted or duplicate obligation in the financial workspace first")
    if op == "delete":
        row["status"] = "dismissed"
    else:
        patch = dict(fields or {})
        if "lender" in patch and not str(patch["lender"] or "").strip():
            raise HTTPException(422, "A lender name is required")
        if "status" in patch and patch["status"] not in {"active", "dismissed"}:
            raise HTTPException(422, "Use the financial workspace to mark a duplicate or restore a debt")
        if "category" in patch:
            patch["debt_type"] = patch.pop("category")
        row.update(patch)
        if row.get("monthly_payment") in (None, "") and row.get("payment_amount") and row.get("payment_frequency"):
            row["monthly_payment"] = round(float(row["payment_amount"]) * refinance.FREQUENCY_MONTHLY_MULT.get(row["payment_frequency"], 1.0), 2)
    row["origin"] = "admin"
    if row.get("status") == "dismissed":
        from app.services.financial_debt_actions import apply_debt_action
        body = apply_debt_action(body, {"op": "delete", "row_id": row["id"]}, actor_id=str(getattr(actor, "id", "") or "") or None)
    snapshot = await save_body(db, context, body, actor=actor)
    accepted = next(item for item in snapshot["body"]["debts"] if item["id"] == row["id"])
    return read_row(accepted, snapshot)


async def stage_proposals(db: Any, context: ManagedDebtDraft, proposals: list[dict], *, fields: tuple[str, ...],
                          document_id: UUID | None = None, actor: Any = None,
                          source: str = "document_extraction") -> dict:
    accepted = await financial_version_selection.accepted_debt_schedule_snapshot(db, context.profile.id)
    if accepted is not None:
        # The extractor/ledger still retains its observations, but it cannot
        # add obligations or overwrite reviewed terms after a complete Save.
        # A staff-selected document import remains an explicit review path.
        active = [read_row(row, context.snapshot) for row in financial_version_selection.active_debt_schedule_rows(accepted.body)]
        return {"created": 0, "updated": 0, "skipped_admin": 0,
                "total_monthly": round(sum(monthly(row) for row in active if row.get("count_in_dscr", True)), 2),
                "debts": active, "schedule_authoritative": True,
                "suggestions": [{**deepcopy(proposal), "source": source,
                                 "document_id": str(document_id) if document_id else None} for proposal in proposals],
                "draft_pending": bool(context.snapshot["draft"]["dirty"]),
                "draft_family_id": context.snapshot["family_id"], "draft_revision": context.snapshot["draft"]["revision"]}
    body = deepcopy(context.snapshot["body"])
    rows = body.setdefault("debts", [])
    created = updated = skipped = 0
    for proposal in proposals:
        lender = str(proposal.get("lender") or "").strip()
        if not lender:
            continue
        key = proposal.get("vendor_key") or normalize_vendor(lender) or None
        matches = [row for row in rows if str(row.get("lender", "")).strip().casefold() == lender.casefold()]
        if not matches and key:
            matches = [row for row in rows if row.get("vendor_key") and refinance.key_matches(row["vendor_key"], key)]
        # Reconciliation decisions apply across all aliases, not merely the
        # first active row encountered in the array.
        if any(inactive(row) for row in matches):
            skipped += 1
            continue
        if len(matches) > 1:
            # Multiple legitimate contracts at one lender require explicit
            # row association; extraction cannot choose an arbitrary loan.
            skipped += 1
            continue
        row = matches[0] if matches else None
        if row is not None and (row.get("origin", row.get("owner")) == "admin" or row.get("client_reviewed") is True):
            skipped += 1
            continue
        fresh = row is None
        if fresh:
            row = {"id": str(uuid4()), "lender": lender[:180], "debt_type": proposal.get("category") or "loan",
                   "origin": "ai_draft", "status": "active", "vendor_key": key,
                   "evidence_links": [], "count_in_dscr": proposal.get("category") != "credit_card",
                   "evidence": {"source": source}}
            rows.append(row)
        before = deepcopy(row)
        row.update({field: proposal[field] for field in fields if proposal.get(field) is not None})
        if document_id is not None:
            row["document_id"] = str(document_id)
        if source == "vendor_rollup":
            row["debt_type"] = proposal.get("category") or row.get("debt_type") or "loan"
            row["evidence"] = {name: proposal.get(name) for name in ("observed_months", "observed_count", "cadence", "amount_stable", "rationale", "last_seen")}
        if fresh:
            created += 1
        elif row != before:
            updated += 1
    if created or updated:
        await save_body(db, context, body, actor=actor)
    active = [read_row(row, context.snapshot) for row in context.snapshot["body"].get("debts", []) if not inactive(row)]
    return {"created": created, "updated": updated, "skipped_admin": skipped,
            "total_monthly": round(sum(monthly(row) for row in active), 2), "debts": active,
            "draft_pending": bool(context.snapshot["draft"]["dirty"]),
            "draft_family_id": context.snapshot["family_id"], "draft_revision": context.snapshot["draft"]["revision"]}


async def retire_document_rows(db: Any, dealer_id: UUID, document_ids: list[UUID], *, actor: Any = None) -> bool:
    context = await managed_context(db, dealer_id)
    if context is None:
        return False
    body = deepcopy(context.snapshot["body"])
    touched = False
    retired_ids = []
    targets = {str(value) for value in document_ids}
    for row in body.get("debts", []):
        if str(row.get("document_id")) not in targets:
            continue
        # The version snapshot retains the originating id even after the
        # source is removed from the active document room.
        row["evidence"] = {**(row.get("evidence") or {}), "source_document_removed": True}
        if row.get("origin", row.get("owner")) != "admin" and not inactive(row):
            row["status"] = "dismissed"
            retired_ids.append(row["id"])
        touched = True
    if touched:
        from app.services.financial_debt_actions import apply_debt_action
        for row_id in retired_ids:
            body = apply_debt_action(body, {"op": "delete", "row_id": row_id}, actor_id=str(getattr(actor, "id", "") or "") or None)
        await save_body(db, context, body, actor=actor)
    return True


async def version_file_ids(db: Any, file_ids: list[UUID]) -> set[UUID]:
    """Source removal may not remove an immutable version's original/PDF."""
    if not file_ids or not isinstance(getattr(db, "info", None), dict):
        return set()
    from app.models.financial_form_version import FinancialFormVersion

    rows = list((await db.execute(select(FinancialFormVersion).where(or_(
        FinancialFormVersion.source_file_id.in_(file_ids), FinancialFormVersion.pdf_file_id.in_(file_ids),
    )))).scalars().all())
    return {value for row in rows for value in (row.source_file_id, row.pdf_file_id) if value in file_ids}


async def register_managed_form_source(db: Any, document: Any, kind: str, notes: list[str]) -> bool:
    """Reuse the PDF mapping pipeline without publishing dealer extraction.

    The bucket analyzer owns durable financial key facts/citations. If it is
    still processing, its existing completion hook will register the mapping;
    a dealer summary must not replace that richer source analysis.
    """
    context = await managed_context(db, document.dealer_id, kind)
    if context is None:
        return False
    from app.models.bucket import BucketFile, BucketFileAnalysis

    file_id = getattr(document, "bucket_file_id", None)
    if file_id is None:
        notes.append("Financial source awaits a linked PDF before field mapping; published figures are unchanged.")
        return True
    file = await db.get(BucketFile, file_id)
    if (file is None or file.bucket_id != context.profile.primary_bucket_id or file.deleted_at is not None
            or (file.content_type != "application/pdf" and not getattr(file, "file_name", "").lower().endswith(".pdf"))):
        notes.append("Financial source retained for review; field mapping requires the original linked PDF.")
        return True
    query = select(BucketFileAnalysis).where(
        BucketFileAnalysis.bucket_file_id == file.id, BucketFileAnalysis.status == "completed",
        BucketFileAnalysis.classification == financial_workspace.CLASSIFICATIONS[kind],
    )
    if getattr(file, "content_hash", None):
        query = query.where(BucketFileAnalysis.content_hash == file.content_hash)
    analysis = (await db.execute(query.order_by(BucketFileAnalysis.created_at.desc()).limit(1))).scalar_one_or_none()
    if analysis is None:
        notes.append("Financial PDF analysis is pending; its completed mapping will be available for review without changing published figures.")
        return True
    if getattr(analysis, "provider", None) == "drafted_form" or (getattr(analysis, "analysis", None) or {}).get("financial_version"):
        notes.append("This PDF is an existing generated financial version; no import was created.")
        return True
    await financial_workspace.propose_import(db, context.profile, kind=kind, file_id=file.id,
                                             analysis=analysis, extract_full=False)
    notes.append("Financial PDF mapping was staged for review; apply it to the draft and save a version to update financial figures.")
    return True
