"""Staff reconciliation changes drafts; publication alone changes the ledger."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial_debt_evidence import FinancialDebtEvidence


def active_debt(row: Any) -> bool:
    if isinstance(row, dict):
        return row.get("status", "active") == "active" and not row.get("duplicate_of_id")
    return getattr(row, "status", "active") == "active" and not getattr(row, "duplicate_of_id", None)


def apply_debt_action(body: dict, action: dict, *, actor_id: str | None = None) -> dict:
    """Pure, lossless transition. UUIDs, amounts, provenance and aliases survive."""
    result = deepcopy(body)
    rows = result.get("debts") or []
    by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict) and row.get("id")}
    row_id = str(action["row_id"])
    row = by_id.get(row_id)
    if row is None:
        raise HTTPException(404, "The obligation is not on this financial draft")
    op = action["op"]
    if op == "delete":
        row["status"] = "dismissed"
        # Removing the surviving obligation removes its excluded aliases from
        # the live group too; all source rows and links remain recoverable.
        for alias in rows:
            if str(alias.get("duplicate_of_id") or "") == row_id:
                alias["previous_duplicate_of_id"] = row_id
                alias["duplicate_of_id"] = None
                alias["status"] = "dismissed"
    elif op == "restore":
        row["status"] = "active"
        row["duplicate_of_id"] = None
    elif op == "duplicate":
        target_id = str(action.get("target_row_id") or "")
        target = by_id.get(target_id)
        if target_id == row_id or target is None or not active_debt(target):
            raise HTTPException(422, "Choose a different active obligation on this file")
        # Retarget dependent aliases as a unit so there are no duplicate chains.
        for other in rows:
            if str(other.get("duplicate_of_id") or "") == row_id:
                other["duplicate_of_id"] = target_id
        row["status"] = "duplicate"
        row["duplicate_of_id"] = target_id
    else:
        file_id = str(action.get("file_id") or "")
        if not file_id:
            raise HTTPException(422, "Choose a document")
        links = row.setdefault("evidence_links", [])
        role = action.get("role") or ("payoff" if op == "select_payoff" else "supporting")
        link = next((item for item in links if str(item.get("file_id")) == file_id and item.get("role") == role), None)
        if op == "link_document":
            if link is None:
                link = {"file_id": file_id, "role": role, "is_current": False}
                links.append(link)
        elif op == "unlink_document":
            row["evidence_links"] = [item for item in links if not (str(item.get("file_id")) == file_id and item.get("role") == role)]
            if link and link.get("is_current"):
                row["payoff_amount"] = None
                row["payoff_date"] = None
        elif op == "select_payoff":
            if link is None or role != "payoff":
                raise HTTPException(422, "Link this document as a payoff letter first")
            for item in links:
                item["is_current"] = item is link
            for key in ("payoff_amount", "payoff_date"):
                if key in action:
                    link[key] = str(action[key]) if action[key] is not None else None
                row[key] = link.get(key)
        else:
            raise HTTPException(422, "Unsupported debt action")
        if link is not None and op == "link_document":
            for key in ("payoff_amount", "payoff_date"):
                if action.get(key) is not None:
                    link[key] = str(action[key])
    audit = list(result.get("reconciliation_history") or [])
    audit.append({"op": op, "row_id": row_id, "target_row_id": str(action.get("target_row_id") or "") or None,
                  "file_id": str(action.get("file_id") or "") or None,
                  "actor_id": actor_id, "at": datetime.now(UTC).isoformat()})
    result["reconciliation_history"] = audit
    return result


async def sync_debt_evidence_links(db: AsyncSession, profile: Any, row: Any, links: list[dict]) -> None:
    """Replace the published projection from the frozen version snapshot."""
    await db.execute(delete(FinancialDebtEvidence).where(FinancialDebtEvidence.debt_id == row.id,
                                                        FinancialDebtEvidence.profile_id == profile.id))
    await db.flush()
    seen = set()
    current = False
    for item in links or []:
        role = item.get("role")
        key = (str(item.get("file_id")), role)
        if key in seen or role not in {"agreement", "payoff", "supporting"}:
            continue
        seen.add(key)
        is_current = role == "payoff" and bool(item.get("is_current")) and not current
        current = current or is_current
        db.add(FinancialDebtEvidence(
            profile_id=profile.id, debt_id=row.id, file_id=UUID(key[0]), role=role,
            is_current=is_current,
            payoff_amount=Decimal(str(item["payoff_amount"])) if item.get("payoff_amount") is not None else None,
            payoff_date=date.fromisoformat(str(item["payoff_date"])) if item.get("payoff_date") else None,
        ))
    await db.flush()


async def debt_evidence_by_row(db: AsyncSession, profile_id: UUID) -> dict[str, list[dict]]:
    links = (await db.execute(select(FinancialDebtEvidence).where(FinancialDebtEvidence.profile_id == profile_id))).scalars().all()
    result: dict[str, list[dict]] = {}
    for link in links:
        result.setdefault(str(link.debt_id), []).append({
            "file_id": str(link.file_id), "role": link.role, "is_current": link.is_current,
            "payoff_amount": str(link.payoff_amount) if link.payoff_amount is not None else None,
            "payoff_date": link.payoff_date.isoformat() if link.payoff_date else None,
        })
    return result

