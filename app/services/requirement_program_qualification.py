"""Program-specific conditions on shared collected evidence.

A shared checklist row can collect a report once for several programs. Each
program still reads only its own pinned conditions and common file policies.
"""

import re
from types import SimpleNamespace

from app.services import credit_requirement_sync as credit


def _check_ids(checks):
    from app.services.document_qualification_review import check_id

    return {check_id(check) for check in checks}


def for_definitions(state, definitions, *, profile=None, owners=None, pulls=None):
    from app.services import application_programs as programs
    from app.services.requirement_collection_completion import qualification_review

    metadata = state.provenance or {}
    checks = programs._merged_review_checks(definitions)
    original_checks = metadata.get("review_checks") or []
    original = metadata.get("qualification_review") or qualification_review(state)
    if not definitions:
        return {"status": "not_required", "blocking": False, "reasons": [], "checks": []}
    linked_credit = metadata.get("credit_collection")
    if linked_credit:
        # A plain request for owner reports does not acquire another selected
        # program's score floor just because its evidence row is shared.
        constrained = [definition for definition in definitions if (
            programs._merged_review_checks([definition])
            or credit._required_score_model(definition, None) is not None
            or credit._required_minimum(definition, None)[0] is not None
            or any(key in (getattr(definition, "collection_config", None) or {}) for key in (
                "required_score_model", "credit_model", "score_model", "credit_bureau", "bureau",
                "minimum_credit_score", "min_credit_score", "minimum_score", "min_score", "min_fico", "credit_score_min"))
            or re.search(r"\b(?:fico|equifax|experian|transunion)\b", str(getattr(definition, "label", "")), re.I)
        )]
        if not constrained:
            return {"status": "not_required", "blocking": False, "reasons": [], "checks": []}
        results = []
        for definition in constrained:
            if profile is None or owners is None or pulls is None:
                results.append({"status": "pending", "blocking": True,
                                "reasons": ["Review the current owner reports against this program's credit conditions."]})
                continue
            scoped = credit.collection_snapshot(profile, owners, pulls, definition,
                policy_context={"review_checks": programs._merged_review_checks([definition]),
                                "requires_human": programs._requires_human_verification(definition)})
            saved = linked_credit.get("last_review") or {}
            scoped_review = metadata.get("credit_review")
            if saved.get("current") and saved.get("decision") == "met":
                scoped_review = {**saved, "fingerprint": scoped["fingerprint"]}
            projected = SimpleNamespace(status=state.status,
                verification_required=getattr(definition, "verification_required", False),
                verified_by_user_id=getattr(state, "verified_by_user_id", None),
                provenance={**metadata, "credit_collection": scoped,
                            "requires_human": programs._requires_human_verification(definition),
                            "credit_review": scoped_review,
                            "review_checks": programs._merged_review_checks([definition])})
            result = qualification_review(projected)
            # An explicit current adverse review remains authoritative. It
            # cannot disappear just because a scoped snapshot has a new hash.
            if saved.get("current") and saved.get("decision") in {"failed", "incomplete"}:
                result = {**result, "status": "failed" if saved["decision"] == "failed" else "pending",
                          "blocking": True, "reasons": [saved.get("reason") or "Staff program credit review is incomplete", *result.get("reasons", [])]}
            results.append(result)
        return {"status": "failed" if any(row.get("status") == "failed" for row in results) else
                "pending" if any(row.get("blocking") for row in results) else "met",
                "blocking": any(row.get("blocking") for row in results),
                "reasons": list(dict.fromkeys(reason for row in results for reason in row.get("reasons", []))),
                "checks": checks}
    if not checks:
        return {"status": "not_required", "blocking": False, "reasons": [], "checks": []}
    if state.status in {"waived", "not_applicable"} and getattr(state, "verified_by_user_id", None) is not None:
        return {"status": "not_required", "blocking": False, "reasons": [], "checks": checks}
    if _check_ids(checks) == _check_ids(original_checks) or (
        original.get("status") == "met" and _check_ids(checks).issubset(_check_ids(original_checks))
    ):
        return {**original, "checks": checks}
    # A current grounded assessment may contain checks for multiple programs.
    # Its overall failure must not hide another program's individually passed
    # checks; an absent or uncertain result is never promoted to a pass.
    assessment = metadata.get("ai_document_review") or {}
    current_rows = {row.get("id"): row for row in assessment.get("checks", []) if isinstance(row, dict)}
    scoped_rows = [current_rows.get(key) for key in _check_ids(checks)]
    if scoped_rows and all(row and row.get("status") == "pass" and row.get("citations") for row in scoped_rows):
        return {"status": "met", "blocking": False, "reasons": [], "checks": checks}
    failed = [row for row in scoped_rows if row and row.get("status") == "fail"]
    return {"status": "failed" if failed else "pending", "blocking": any(check.get("severity", "block") == "block" for check in checks),
            "reasons": [row.get("reason") or "A program condition is not met" for row in failed] or
                       ["Review this program's financial conditions separately from document collection."],
            "checks": checks}
