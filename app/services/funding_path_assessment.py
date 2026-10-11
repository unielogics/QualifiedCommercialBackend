"""Published-policy advisory paths, distinct from approval and readiness scores."""
from __future__ import annotations

from typing import Any

VERSION = "funding_path_v1"
FINANCIAL_REMEDIATION_KEYS = frozenset({
    "gross_margin_pct", "net_margin_pct", "current_dscr", "returned_items_90",
    "books_reconciled", "negative_earnings", "debt_burden", "commingled_funds",
    "liquidity", "average_daily_balance",
})


def assess_funding_paths(candidates: list, *, readiness_blockers: list[dict] | None = None,
                         readiness_state: str = "provisional") -> dict[str, Any]:
    """No inferred thresholds, provider work, file writes or stage transitions.

    Missing facts are not adverse credit findings. Unpublished/out-of-scope
    programs cannot establish a supported path, nor imply a weak borrower.
    """
    rows = []
    for candidate in candidates[:100]:
        row = (candidate.model_dump(mode="json") if hasattr(candidate, "model_dump") else
               dict(candidate) if isinstance(candidate, dict) else vars(candidate).copy())
        if row.get("scope_status") == "outside_scope":
            continue
        rows.append(row)
    eligible = [{"program_key": row["program_key"], "name": row["program_name"],
                 "policy_version": row.get("playbook_version")}
                for row in rows if row.get("eligible") and row.get("criteria_status") == "published"
                and row.get("scope_status") == "matched" and row.get("rule_state") == "pass"]
    configured = [row for row in rows if row.get("criteria_status") == "published"
                  and row.get("rule_state") != "unavailable"]
    gaps = list(dict.fromkeys(str(gap) for row in configured for gap in row.get("evidence_gaps", [])))[:50]
    unresolved = any(row.get("rule_state") == "unknown" or row.get("scope_status") == "unknown" for row in configured)
    review = any(row.get("requires_review") for row in configured)
    remediation = [row for row in (readiness_blockers or []) if isinstance(row, dict)
                   and (row.get("metric_key") or row.get("key")) in FINANCIAL_REMEDIATION_KEYS
                   and row.get("severity") == "financial_finding"
                   and row.get("missing_evidence") is False
                   and row.get("verification_status") == "verified"]
    review = review or any(
        isinstance(row, dict)
        and (row.get("metric_key") or row.get("key")) in FINANCIAL_REMEDIATION_KEYS
        and row.get("severity") == "financial_finding"
        and row.get("missing_evidence") is False
        and row.get("verification_status") != "verified"
        for row in (readiness_blockers or [])
    )
    if eligible:
        status = "potential_program_fit"
        reasons = ["Verified facts satisfy the displayed published screening rules; lender review and the full document package remain required."]
        actions = ["Review the supported program criteria and complete its current document requirements."]
    elif not configured:
        status = "criteria_unavailable"
        reasons = ["No applicable published screening policy is available. This is a configuration gap, not an adverse finding about this client."]
        actions = ["Publish or correct the applicable program criteria, then reassess the same evidence."]
    elif review:
        status = "needs_review"
        reasons = ["Available numbers or conflicting source facts need verification before matching; usable evidence is retained."]
        actions = ["Review the cited source facts, reconcile conflicts, and confirm their association and period."]
    elif unresolved:
        status = "needs_evidence"
        reasons = ["Required facts or routing evidence are missing. Missing evidence does not establish poor financial health or a denial."]
        actions = ["Collect the displayed missing evidence and reassess against the same published policy version."]
    elif remediation:
        status = "capital_readiness_recommended"
        reasons = ["No currently supported program match was established, and known financial findings identify a practical improvement path."]
        actions = ["Review the documented financial blockers with the underwriting team and build a Capital Readiness action plan."]
    else:
        status = "needs_review"
        reasons = ["The known facts do not satisfy the evaluated policies. Review the exact failed rules; this screen is neither approval nor denial."]
        actions = ["Review the observed values and published limits with authorized underwriting staff."]
    return {"version": VERSION, "status": status, "advisory_only": True,
            "readiness_review_status": readiness_state, "eligible_programs": eligible,
            "reasons": reasons, "next_actions": actions, "evidence_gaps": gaps,
            "program_results": rows}
