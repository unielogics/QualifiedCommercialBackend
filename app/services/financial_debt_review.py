"""Compare observed payments with a shared draft; only staff can add a row."""

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException

from app.services.financial_version_selection import (
    active_debt_schedule_rows,
    debt_schedule_completion,
    debt_schedule_monthly,
)


def _key(vendor, account):
    return "observed:" + sha256(f"{vendor}|{account}".encode()).hexdigest()[:32]


def build_candidates(body, analyses, accounts, aliases=None):
    from app.dealer_os.services.vendors import normalize_vendor
    from app.services import financial_cockpit as cockpit

    membership = {member: account["key"] for account in accounts for member in [account["key"], *(account.get("members") or [])]}
    labels = {account["key"]: account["label"] for account in accounts}
    counted = {account["key"]: account.get("counted", True) for account in accounts}
    grouped = {}
    for analysis in cockpit.canonical_bank_analyses(analyses, aliases=aliases, accounts=accounts):
        if "bank_statement" not in cockpit._classes(analysis):
            continue
        key = cockpit.resolve_alias(cockpit.file_account_identity(analysis.get("key_facts") or {})[0], aliases)
        key = membership.get(key, key)
        grouped.setdefault(key, []).append(analysis)
    candidates = []
    for account, sources in grouped.items():
        for payee in cockpit.recurring_payments(sources, limit=None, aliases=aliases)["payees"]:
            key = _key(payee["key"], account)
            decisions = body.get("observed_debt_reviews") or {}
            decision = decisions.get(key)
            if decision is None:
                decision = next((value for value in decisions.values() if value.get("vendor_key") == payee["key"]
                                 and cockpit.resolve_alias(value.get("account_key") or "", aliases) == account), {})
            bound = [row for row in body.get("debts", []) if row.get("observed_candidate_key") == key
                     or row.get("observed_vendor_key") == payee["key"] and cockpit.resolve_alias(row.get("observed_account_key") or "", aliases) == account]
            explicit = next((row for row in body.get("debts", []) if str(row.get("id")) == decision.get("row_id")), None)
            vendor_matches = [row for row in body.get("debts", []) if row.get("vendor_key") == payee["key"] and row.get("status", "active") == "active" and not row.get("duplicate_of_id")]
            possible = [row for row in body.get("debts", []) if row.get("vendor_key") == payee["key"] or normalize_vendor(row.get("lender") or "") == payee["key"]]
            active_possible = [row for row in possible if row.get("status", "active") == "active" and not row.get("duplicate_of_id")]
            row = explicit or (bound[0] if len(bound) == 1 else None) or (vendor_matches[0] if len(vendor_matches) == 1 and len(active_possible) == 1 else None)
            owns_row = row is not None and (row.get("observed_candidate_key") == key or
                        row.get("observed_vendor_key") == payee["key"] and cockpit.resolve_alias(row.get("observed_account_key") or "", aliases) == account)
            shared = row is not None and any(other_key != key and value.get("row_id") == str(row["id"]) and value.get("status") == "included" for other_key, value in decisions.items())
            binding = "existing" if row is not None and (decision.get("binding") == "existing" or shared) else "created" if owns_row else "existing" if row is not None else None
            active = row is not None and row.get("status", "active") == "active" and not row.get("duplicate_of_id")
            status = "disabled" if decision.get("status") == "disabled" else "included" if active else "unreviewed" if decision.get("status") == "unreviewed" else "disabled" if row is not None else "unreviewed"
            refs = [{"file_id": item["file_id"], "content_hash": item.get("content_hash"), "analysis_id": item.get("analysis_id"), "account_key": account}
                    for item in sources if item.get("file_id") in payee.get("file_ids", [])]
            candidates.append({**payee, "key": key, "vendor_key": payee["key"], "account_key": account,
                               "accounts": [labels.get(account, account)], "account_label": labels.get(account, account), "source_refs": refs, "status": status,
                               "row_id": str(row["id"]) if row else None, "binding": binding, "eligible": len(bound) <= 1 and not (row and row.get("duplicate_of_id")),
                               "possible_row_ids": [str(item["id"]) for item in body.get("debts", []) if normalize_vendor(item.get("lender") or "") == payee["key"]],
                               "account_counted": bool(counted.get(account, False)),
                               "related_rows": [{"row_id": str(row["id"]), "lender": row.get("lender"), "status": row.get("status", "active")}
                                                for row in body.get("debts", []) if row.get("vendor_key") == payee["key"] or normalize_vendor(row.get("lender") or "") == payee["key"]],
                               "reason": "Observed payment pattern only, classified as " + str(payee.get("kind_label") or "unknown") + ". Classification does not establish debt. Add only after confirming a separate business obligation; balance and contract terms remain unknown.",
                               "in_debt_service": False})
    return sorted(candidates, key=lambda candidate: (candidate["payee"].casefold(), candidate["account_key"]))


def compare(snapshot, analyses, accounts, aliases=None, published=None):
    body = snapshot["body"]
    monthly = debt_schedule_monthly(body)
    published_body = published.body if published is not None else None
    published_monthly = debt_schedule_monthly(published_body) if published is not None else None
    return {"workspace": snapshot, "candidates": build_candidates(body, analyses, accounts, aliases),
            "working": {**debt_schedule_completion(body), "monthly": float(monthly) if monthly is not None else None,
                        "active_rows": len(active_debt_schedule_rows(body)), "basis": "working_draft"},
            "published": {"version_id": str(published.id) if published is not None else None,
                          "complete": published is not None, "monthly": float(published_monthly) if published_monthly is not None else None}}


def _action_payload(candidate_key, action, target_row_id=None):
    return {"candidate_key": candidate_key, "action": action, **({"target_row_id": str(target_row_id)} if action == "link_existing" else {})}


def apply_candidate(body, candidate, *, action, family_id, idempotency_key, actor_id=None, target_row_id=None):
    """No lender-name matching: one observed source identity owns at most one row."""
    result = deepcopy(body)
    payload = _action_payload(candidate["key"], action, target_row_id)
    receipts = result.setdefault("observed_debt_action_receipts", {})
    if idempotency_key in receipts:
        if receipts[idempotency_key] != payload:
            raise HTTPException(409, "This request key was already used for another candidate action")
        return result, False
    if action == "include" and not candidate.get("eligible"):
        raise HTTPException(422, "This candidate's row identity needs review")
    rows = result.setdefault("debts", [])
    row = next((row for row in rows if str(row.get("id")) == candidate.get("row_id")), None)
    if action == "link_existing":
        if row is not None and candidate.get("binding") == "created" and row.get("status", "active") == "active" and str(row["id"]) != str(target_row_id):
            raise HTTPException(409, "Exclude or mark the candidate-created row as a duplicate before linking a different obligation")
        row = next((item for item in rows if str(item.get("id")) == str(target_row_id)), None)
        if row is None or row.get("status", "active") != "active" or row.get("duplicate_of_id"):
            raise HTTPException(422, "Choose an active obligation on this working schedule")
    if action == "include":
        if candidate.get("binding") == "existing":
            row = next((item for item in rows if item.get("observed_candidate_key") == candidate["key"]), None)
        if row is not None and row.get("duplicate_of_id"):
            raise HTTPException(409, "This candidate was marked as a duplicate. Restore or review that row in the schedule first.")
        if row is None:
            row = {"id": str(uuid5(NAMESPACE_URL, f"{family_id}:{candidate['key']}")), "lender": candidate["payee"],
                   "origin": "admin", "status": "active", "count_in_dscr": True, "balance": None,
                   "monthly_payment": None, "observed_candidate_key": candidate["key"],
                   "observed_vendor_key": candidate["vendor_key"], "observed_account_key": candidate["account_key"],
                   "evidence_links": [{"file_id": file_id, "role": "supporting", "is_current": False} for file_id in candidate.get("file_ids", [])],
                   "evidence": {"observed_payment_sources": candidate.get("source_refs", []), "observed_monthly_average": candidate.get("monthly_average")}}
            rows.append(row)
        row["status"] = "active"
        result["no_debt"] = False
    elif action == "disable":
        if row is not None and candidate.get("binding") != "existing" and not row.get("duplicate_of_id"):
            row["status"] = "dismissed"
    elif action not in {"restore", "link_existing"}:
        raise HTTPException(422, "Unsupported candidate action")
    decisions = result.setdefault("observed_debt_reviews", {})
    decisions[candidate["key"]] = {"vendor_key": candidate["vendor_key"], "account_key": candidate["account_key"],
                                  "status": "disabled" if action == "disable" else "included" if action in {"include", "link_existing"} else "unreviewed",
                                  "row_id": str(row["id"]) if row else None, "actor_id": actor_id, "at": datetime.now(UTC).isoformat()}
    decisions[candidate["key"]]["binding"] = "existing" if action == "link_existing" else "created" if action == "include" else candidate.get("binding")
    receipts[idempotency_key] = payload
    return result, True


async def source_context(db, profile):
    from app.services import application_programs as programs
    from app.services import financial_cockpit as cockpit

    files, latest, _ = await programs._evidence_inventory(db, profile)
    accepted = await programs.accepted_evidence_file_ids(db, profile, files, latest)
    analyses = await cockpit.associated_analysis_rows(db, profile, files, latest, accepted)
    for item in analyses:
        analysis = latest.get(UUID(item["file_id"]))
        item["analysis_id"] = str(analysis.id) if analysis is not None else None
    _, accounts = cockpit.bank_months(analyses, profile.counted_bank_accounts or [], profile.bank_account_aliases or {})
    return analyses, accounts


async def read_review(db, profile, *, family_id=None, actor=None):
    from app.services import financial_workspace as workspace
    from app.services.financial_version_selection import accepted_debt_schedule_snapshot

    snapshot = await workspace.load_workspace(db, profile, kind="debt_schedule", family_id=family_id, actor=actor)
    analyses, accounts = await source_context(db, profile)
    published = await accepted_debt_schedule_snapshot(db, profile.id)
    result = compare(snapshot, analyses, accounts, profile.bank_account_aliases or {}, published)
    if published is not None:
        confirmed_ids = {f"row:{row['id']}" for row in active_debt_schedule_rows(published.body)}
    else:
        from sqlalchemy import select

        from app.dealer_os.models import DealerDebt

        rows = (await db.execute(select(DealerDebt).where(DealerDebt.profile_id == profile.id, DealerDebt.status == "active"))).scalars().all()
        confirmed_ids = {f"row:{row.id}" for row in rows if row.origin != "ai_draft"}
    result["unresolved_previous_selections"] = [str(key) for key in (getattr(profile, "financial_plan", None) or {}).get("retire", [])
                                               if str(key) not in confirmed_ids]
    return result


async def act(db, profile, *, expected_revision, candidate_key, action, idempotency_key, family_id=None, actor=None, target_row_id=None):
    from app.services import financial_workspace as workspace

    review = await read_review(db, profile, family_id=family_id, actor=actor)
    snapshot = review["workspace"]
    receipt = (snapshot["body"].get("observed_debt_action_receipts") or {}).get(idempotency_key)
    if receipt is not None:
        if receipt != _action_payload(candidate_key, action, target_row_id):
            raise HTTPException(409, "This request key was already used for another candidate action")
        return review
    if snapshot["draft"]["revision"] != expected_revision:
        raise HTTPException(409, {"code": "financial_draft_conflict", "message": "This draft changed. Reload before saving.", "current_revision": snapshot["draft"]["revision"]})
    candidate = next((candidate for candidate in review["candidates"] if candidate["key"] == candidate_key), None)
    if candidate is None:
        raise HTTPException(404, "Observed payment candidate not found in this file's current evidence")
    body, changed = apply_candidate(snapshot["body"], candidate, action=action, family_id=snapshot["family_id"], idempotency_key=idempotency_key, actor_id=str(actor.id) if actor else None, target_row_id=target_row_id)
    if changed:
        await workspace.save_draft(db, profile, kind="debt_schedule", family_id=UUID(snapshot["family_id"]), expected_revision=expected_revision, body=body, actor=actor)
    return await read_review(db, profile, family_id=UUID(snapshot["family_id"]), actor=actor)


async def working_snapshot(db, profile):
    """Read an existing draft without creating one or taking publication locks."""
    from app.models.financial_form_version import FinancialFormDraft, FinancialFormVersion
    from app.services import financial_workspace as workspace
    from app.services.financial_version_selection import is_accepted_debt_schedule_version

    family = await workspace.existing_family(db, profile.id, "debt_schedule")
    if family is None:
        return None
    draft = await db.get(FinancialFormDraft, family.id)
    if draft is None:
        return None
    current = await db.get(FinancialFormVersion, family.current_version_id) if family.current_version_id else None
    accepted = current is not None and is_accepted_debt_schedule_version(current)
    return {"kind": "debt_schedule", "family_id": str(family.id), "body": deepcopy(draft.body),
            "completion": debt_schedule_completion(draft.body), "accepted_completion": debt_schedule_completion(current.body) if accepted else None,
            "accepted_version_id": str(current.id) if accepted else None,
            "current_version_id": str(family.current_version_id) if family.current_version_id else None,
            "draft": {"revision": draft.revision, "base_version_id": str(draft.base_version_id) if draft.base_version_id else None,
                      "updated_at": draft.updated_at.isoformat(), "dirty": current is None or workspace.body_hash(draft.body) != current.body_sha256}}
