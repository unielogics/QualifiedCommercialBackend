"""Compatibility adapters for editors predating financial draft/version APIs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

EDITABLE_DEBT_FIELDS = frozenset({"lender", "debt_type", "original_amount", "balance", "rate", "monthly_payment", "factor_rate", "payment_amount", "payment_frequency", "originated_on", "maturity_on", "secured", "payment_status", "collateral", "mca_modified", "mca_modification_note", "notes"})


def merge_source_debts(current: dict, incoming: dict, *, origin: str = "client_form", allow_source_edits: bool = False) -> dict:
    """Merge scoped financial answers without accepting provenance/desk controls.

    An authorized client form can correct imported figures. Source origin is
    retained as evidence, rather than used as an access-control boundary.
    Other legacy callers keep their existing source-owned behavior.
    """
    body = deepcopy(current)
    if "no_debt" in incoming:
        # Validation rejects non-boolean or contradictory declarations. All
        # obligations owned by another source remain in this merged body.
        body["no_debt"] = incoming["no_debt"]
    if "debts" not in incoming:
        return body
    known = {str(row["id"]): row for row in body.get("debts", []) if row.get("id")}
    seen = set()
    for raw in incoming.get("debts") or []:
        row_id = str(raw.get("id") or "")
        row = known.get(row_id)
        if row is not None:
            if (not allow_source_edits and row.get("origin", row.get("owner")) != origin) or row.get("status", "active") != "active":
                continue
        else:
            # Old borrowers may send no ids. Match a unique own row before
            # minting identity; never treat a lender name as a global loan id.
            matches = [r for key, r in known.items() if not row_id and key not in seen and (allow_source_edits or r.get("origin", r.get("owner")) == origin)
                       and r.get("status", "active") == "active" and str(r.get("lender", "")).strip().casefold() == str(raw.get("lender", "")).strip().casefold()]
            if len(matches) == 1:
                row = matches[0]
            else:
                try:
                    fresh_id = str(UUID(row_id)) if row_id else str(uuid4())
                except (ValueError, TypeError):
                    raise HTTPException(422, "Schedule rows require UUIDs") from None
                row = {"id": fresh_id, "origin": origin, "status": "active", "count_in_dscr": True, "evidence_links": []}
                body.setdefault("debts", []).append(row)
        if str(row["id"]) in seen:
            raise HTTPException(422, "Schedule rows require distinct UUIDs")
        patch = {key: value for key, value in raw.items() if key in EDITABLE_DEBT_FIELDS}
        changed = [key for key, value in patch.items() if row.get(key) != value]
        row.update(patch)
        if allow_source_edits and changed:
            row["client_reviewed"] = True
            row["client_edited_fields"] = sorted(set(row.get("client_edited_fields") or []) | set(changed))
        seen.add(str(row["id"]))
    for row in body.get("debts", []):
        if (allow_source_edits or row.get("origin", row.get("owner")) == origin) and str(row.get("id")) not in seen and row.get("status", "active") == "active":
            row["status"] = "dismissed"
    if "no_debt" not in incoming and any(
        row.get("status", "active") == "active" and (allow_source_edits or row.get("origin", row.get("owner")) == origin)
        for row in body.get("debts", [])
    ):
        body["no_debt"] = False
    return body


async def managed_snapshot(db: Any, profile: Any, kind: str, *, statement_id: UUID | None = None, family_id: UUID | None = None) -> dict | None:
    # Real async sessions expose transaction-local info; lightweight legacy
    # adapters without it cannot hold a managed draft transaction.
    if not isinstance(getattr(db, "info", None), dict) or db.info.get("financial_workspace_publishing"):
        return None
    from sqlalchemy import select

    from app.models.financial_form_version import FinancialFormFamily
    from app.services import financial_workspace as workspace

    family = None
    if family_id:
        family = await db.get(FinancialFormFamily, family_id)
        if family is None or family.profile_id != profile.id or family.kind != kind:
            raise HTTPException(404, "Financial form family not found")
    elif statement_id:
        family = (await db.execute(select(FinancialFormFamily).where(
            FinancialFormFamily.profile_id == profile.id, FinancialFormFamily.kind == kind,
            FinancialFormFamily.identity["statement_id"].astext == str(statement_id),
        ))).scalar_one_or_none()
        if family is None:
            return None
    else:
        family = await workspace.existing_family(db, profile.id, kind)
    if family is None:
        return None
    return await workspace.load_workspace(db, profile, kind=kind, family_id=family.id)


async def stage_legacy_body(db: Any, profile: Any, snapshot: dict, body: dict, *, actor: Any = None,
                            public: bool = False, submit: bool = False) -> dict:
    from app.services import financial_workspace as workspace

    if public and snapshot["kind"] == "debt_schedule":
        body = merge_source_debts(snapshot["body"], body)
    elif snapshot["kind"] == "debt_schedule":
        # Bulk old forms omit reconciliation/provenance fields. Retain them.
        known = {str(row.get("id")): row for row in snapshot["body"].get("debts", [])}
        body = {**deepcopy(snapshot["body"]), **body, "debts": [
            {**known.get(str(row.get("id")), {}), **row} for row in body.get("debts", [])
        ]}
    result = await workspace.save_draft(db, profile, kind=snapshot["kind"], family_id=UUID(snapshot["family_id"]),
                                        expected_revision=snapshot["draft"]["revision"], body=body, actor=actor)
    if submit:
        result = await workspace.publish_version(db, profile, kind=snapshot["kind"], family_id=UUID(snapshot["family_id"]),
            expected_revision=result["draft"]["revision"], base_version_id=UUID(result["draft"]["base_version_id"]) if result["draft"]["base_version_id"] else None,
            idempotency_key=f"legacy:{snapshot['family_id']}:{result['draft']['revision']}:submit", actor=actor, submit=True)
    return result


async def protect_projection(db: Any, profile: Any, kind: str) -> None:
    """The legacy persistence functions are internal published projections."""
    if not isinstance(getattr(db, "info", None), dict) or db.info.get("financial_workspace_publishing"):
        return
    from app.services.financial_workspace import has_managed_family
    if await has_managed_family(db, profile, kind):
        raise HTTPException(409, {"code": "financial_workspace_required", "message": "This form has a versioned working draft. Save it through the financial workspace."})
