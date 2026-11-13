"""Client acknowledgement of exact selected source facts, never source approval."""
import hashlib
import json
from copy import deepcopy


def evaluate(body, cockpit):
    evidence = cockpit.get("operating_evidence") or {}
    guidance = evidence.get("guidance") or {}
    reporting = deepcopy(guidance.get("reporting") or {})
    groups = sorted(guidance.get("expense_groups") or [], key=lambda row: row["key"])
    source_ids = {str(fid) for group in groups for fid in group.get("source_file_ids") or []}
    if reporting.get("source_file_id"):
        source_ids.add(str(reporting["source_file_id"]))
    files = sorted([{key: row.get(key) for key in ("file_id", "analysis_id", "content_hash", "association_stamp", "eligible_for_review")}
                    for row in evidence.get("files") or [] if str(row.get("file_id")) in source_ids], key=lambda row: str(row["file_id"]))
    facts = [{key: row.get(key) for key in ("key", "category", "source_amount", "baseline_annual_amount", "year", "period_start", "period_end", "currency", "basis", "source_file_ids", "source_fact_keys")} for row in groups]
    source_stamp = hashlib.sha256(json.dumps({"business": evidence.get("business_name"), "reporting": reporting, "facts": facts, "files": files}, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest() if groups else None
    decisions = body.get("source_group_confirmations") or {}
    supplied_stamp = body.get("source_group_confirmation_stamp")
    active = bool(decisions or supplied_stamp)
    stale = bool(active and supplied_stamp != source_stamp)
    corrections = {row["source_group_key"]: row for row in body.get("historical_corrections") or []}
    rows, missing = [], []
    if active and not groups:
        missing.append("source_group_confirmations.selected_source")
    if stale:
        missing.append("source_group_confirmation_stamp")
    if active:
        missing.extend(key for key in ("period_start", "period_end", "currency", "basis")
                       if reporting.get(key) is not None and body.get(key) != reporting[key])
    for group in groups:
        key, decision = group["key"], decisions.get(group["key"])
        status = "stale" if stale else decision or "unreviewed"
        if active and not stale and decision is None:
            missing.append("source_group_confirmations." + key)
        if decision == "needs_correction" and len(corrections.get(key, {}).get("reason", "").strip()) < 3:
            missing.append("historical_corrections." + key + ".reason")
        rows.append({key: deepcopy(group.get(key)) for key in ("key", "category", "label", "source_amount", "baseline_annual_amount")}
                    | {"decision": decision, "status": status, "correction_pending": decision == "needs_correction"})
    known_keys = {row["key"] for row in groups}
    missing.extend("source_group_confirmations." + key for key in decisions if key not in known_keys)
    return {"source_fingerprint": source_stamp, "reporting": reporting, "groups": rows,
            "active": active, "complete": bool(active and groups and not missing), "stale": stale,
            "missing": list(dict.fromkeys(missing)), "fix_fields": list(dict.fromkeys(missing)),
            "note": "Confirming means the source figures were reviewed. A reported correction remains pending; this does not change or accept the tax/P&L source or current NOI."}
