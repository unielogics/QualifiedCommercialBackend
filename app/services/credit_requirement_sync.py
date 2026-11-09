"""Project existing owner reports into collection, without deciding credit eligibility.

The bureau pull is the evidence here; a second BucketFile upload is not required.
Only the exact supported requirement is recognized. Consent, owner scope, report
availability and freshness are checked again on every readiness read.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.models.credit_pull import CreditPull
from app.services import credit_links

REQUIREMENT_KEY = "guarantor_credit_review"


def staff_review_only(requirement: Any) -> bool:
    receipt = (getattr(requirement, "provenance", None) or {}).get("credit_collection")
    return bool(
        isinstance(receipt, dict)
        and receipt.get("source") == "linked_owner_credit_pulls"
        and receipt.get("client_action_required") is False
    )


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _percent(owner: Any) -> Decimal | None:
    try:
        value = Decimal(str(owner.ownership_pct))
        return value if value.is_finite() and 0 <= value <= 100 else None
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def owner_review_fingerprint(owner: Any) -> str:
    """Bind a staff confirmation to the displayed owner facts, not the review flag."""
    return _digest({key: getattr(owner, key, None) for key in (
        "id", "profile_id", "dealer_id", "first_name", "last_name", "email", "phone",
        "ownership_pct", "is_primary", "is_guarantor", "dob", "street", "city", "state", "zip",
        "credit_pull_id", "archived_at",
    )})


def owner_review_blockers(owner: Any, owners: list) -> list[str]:
    blockers = []
    if not str(getattr(owner, "first_name", "") or "").strip() or not str(getattr(owner, "last_name", "") or "").strip():
        blockers.append("Enter the owner's first and last name before confirming the imported record")
    if _percent(owner) is None:
        blockers.append("Enter the owner's actual ownership percentage before confirming the imported record")
    percentages = [_percent(row) for row in owners if not getattr(row, "archived_at", None)]
    if not percentages or any(value is None for value in percentages) or abs(sum((value or Decimal(0) for value in percentages), Decimal(0)) - 100) >= Decimal("0.005"):
        blockers.append("Review the current ownership roster; known ownership percentages must total 100.00%")
    return blockers


def _required_minimum(requirement: Any, policy_context: dict | None) -> tuple[int | None, list[str]]:
    """Read explicit score floors from the current published requirement, never a default."""
    config = getattr(requirement, "collection_config", None) or {}
    floors = set()
    for key in ("minimum_credit_score", "min_credit_score", "minimum_score", "min_score", "min_fico", "credit_score_min"):
        if config.get(key) is not None:
            raw = str(config[key])
            if not raw.isascii() or not raw.isdigit() or not 300 <= int(raw) <= 850:
                return None, ["The published minimum credit score needs staff policy review"]
            floors.add(int(raw))
    text = " ".join(str(getattr(requirement, key, None) or "") for key in ("label", "completion_criteria"))
    checks = (policy_context or {}).get("review_checks") or getattr(requirement, "review_checks", None) or []
    unsupported = []
    for raw in checks:
        row = raw.model_dump() if hasattr(raw, "model_dump") else raw if isinstance(raw, dict) else {}
        words = " ".join(str(row.get(key) or "") for key in ("label", "instructions"))
        key = str(row.get("key") or "")
        found = {int(value) for value in re.findall(r"\b([3-8][0-9]{2})\b", words) if 300 <= int(value) <= 850}
        if key == "minimum" and isinstance(row.get("value"), int) and 300 <= row["value"] <= 850:
            found.add(row["value"])
        if not found or key not in {"minimum", "custom_quidity_credit_floor"}:
            unsupported.append("The published credit policy includes additional criteria requiring staff policy review")
        floors.update(found)
    floors.update(int(value) for value in re.findall(r"\b([3-8][0-9]{2})\b", text) if 300 <= int(value) <= 850)
    if len(floors) != 1:
        return None, [*unsupported, "The published minimum credit score is missing or ambiguous; review the selected program's policy"]
    return next(iter(floors)), unsupported


def supports(requirement: Any, profile: Any, *, expected_subjects=None) -> bool:
    key = getattr(requirement, "canonical_key", None) or requirement.requirement_key
    config = getattr(requirement, "collection_config", None) or {}
    anchors = {
        str(value)
        for key in ("id", "dealer_id", "intake_id")
        if (value := getattr(profile, key, None)) is not None
    }
    subject = " ".join(str(config.get("subject") or "").casefold().split())
    allowed_subjects = {
        "",
        "business",
        "company",
        "current business",
        "owner",
        "person",
        "guarantor",
        "all owners",
        "all required owners",
    }
    allowed_subjects.update(
        " ".join(str(value).casefold().split()) for value in (expected_subjects or []) if value
    )
    allowed_subjects.update(anchors)
    allowed_subjects.update(
        str(config[key]) for key in ("owner_id", "person_id") if config.get(key)
    )
    return (
        key == REQUIREMENT_KEY
        and subject in allowed_subjects
        and config.get("period") in (None, "", "current")
        and all(
            not config.get(key) or str(config[key]) in anchors
            for key in ("business_id", "entity_id")
        )
    )


def _score_models(pull: Any) -> list[dict]:
    raw = pull.bureau_response if isinstance(pull.bureau_response, dict) else {}
    parsed = raw.get("parsed_report") if isinstance(raw.get("parsed_report"), dict) else {}
    models = parsed.get("scores") if isinstance(parsed.get("scores"), list) else []
    result = []
    for row in models:
        if not isinstance(row, dict) or not row.get("model"):
            continue
        item = {"model": str(row["model"])[:160], "score": row.get("score")}
        bureau = row.get("bureau") or row.get("credit_bureau")
        if isinstance(bureau, str) and bureau.strip():
            item["bureau"] = bureau.strip()[:80]
        result.append(item)
    # Full-feed fields may retain a bureau omitted by the older report parser.
    feed = raw.get("full_feed") if isinstance(raw.get("full_feed"), dict) else {}
    scores = feed.get("credit_score") if isinstance(feed.get("credit_score"), dict) else {}
    for model, row in scores.items():
        if not isinstance(row, dict):
            continue
        bureau = row.get("bureau") or row.get("credit_bureau")
        if not isinstance(bureau, str) or not bureau.strip():
            continue
        value = str(row.get("score") or "")
        item = {
            "model": str(model)[:160],
            "score": int(value) if value.isascii() and value.isdigit() else None,
            "bureau": bureau.strip()[:80],
        }
        if item not in result:
            result.append(item)
    return result


def _words(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _required_score_model(requirement: Any, policy_context: dict | None) -> str | None:
    config = getattr(requirement, "collection_config", None) or {}
    text = " ".join(
        str(getattr(requirement, key, None) or "")
        for key in ("label", "objective_text", "completion_criteria", "review_checks")
    )
    text += " " + str((policy_context or {}).get("review_checks") or "")
    configured = (
        config.get("required_score_model")
        or config.get("credit_model")
        or config.get("score_model")
    )
    bureau = config.get("credit_bureau") or config.get("bureau")
    if configured:
        text += f" {bureau or ''} {configured}"
    normalized = _words(text)
    if bureau and _words(bureau) != "equifax":
        return None
    if configured and not re.search(r"\bfico\s*(?:score\s*)?9\b", _words(configured)):
        return None
    if re.search(r"\b(?:transunion|trans union|experian)\b", normalized):
        return None
    models = set(re.findall(r"\bfico\s*(?:score\s*)?(\d+)\b", normalized))
    return "Equifax FICO 9" if models == {"9"} and re.search(r"\bequifax\s+fico\s*(?:score\s*)?9\b", normalized) else None


def _documents_required_model(item: dict, required_model: str | None) -> bool:
    if required_model != "Equifax FICO 9":
        return False
    model, bureau = _words(item.get("model")), _words(item.get("bureau"))
    score = item.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 300 <= score <= 850:
        return False
    if not re.fullmatch(r"(?:equifax\s+)?fico\s*(?:score\s*)?9(?:\s+equifax)?", model):
        return False
    # Never infer that an unlabelled model belongs to another reported bureau.
    if bureau and bureau != "equifax":
        return False
    if re.search(r"\b(transunion|experian)\b", model):
        return False
    return bureau == "equifax" or bool(re.search(r"\bequifax\b", model))


def _owner_report(
    owner: Any,
    pull: Any,
    *,
    now: datetime,
    expiration_days: int | None,
    required_score_model: str | None,
) -> dict:
    row = {
        "owner_id": str(owner.id),
        "credit_pull_id": str(owner.credit_pull_id) if owner.credit_pull_id else None,
        "status": "missing",
        "reason": "No credit pull is linked to this owner",
        "report_available": False,
        "report_import_status": None,
        "pulled_at": None,
        "expires_at": None,
        "score": None,
        "score_models": [],
        "required_score_model": required_score_model,
        "required_score_model_present": False,
        "required_score": None,
        "backfill_needs_review": bool(getattr(owner, "backfill_needs_review", False)),
        "owner_review_fingerprint": owner_review_fingerprint(owner),
        "ownership_pct": float(owner.ownership_pct) if _percent(owner) is not None else None,
    }
    if pull is None:
        if owner.credit_pull_id:
            row.update(
                status="needs_review",
                reason="The linked credit pull could not be found; staff must review the link",
            )
        return row
    raw = pull.bureau_response if isinstance(pull.bureau_response, dict) else {}
    pulled, expires = _utc(pull.pulled_at), _utc(pull.expires_at)
    # The provider's expiry always wins when earlier than a published rule.
    if pulled is not None:
        maximum = pulled + timedelta(days=expiration_days or 90)
        expires = min(expires, maximum) if expires else maximum
    models = _score_models(pull)
    required_model = any(_documents_required_model(item, required_score_model) for item in models)
    required_scores = {item["score"] for item in models if _documents_required_model(item, required_score_model)}
    from app.services.credit_pull_core import credit_status_extras

    facts = credit_status_extras(pull)
    row.update(
        report_available=bool(facts["report_available"]),
        report_import_status=raw.get("report_import_status"),
        pulled_at=pulled.isoformat() if pulled else None,
        expires_at=expires.isoformat() if expires else None,
        score=pull.fico,
        score_models=models,
        required_score_model_present=required_model,
        required_score=next(iter(required_scores)) if len(required_scores) == 1 else None,
        score_verified=facts["score_verified"],
        fico_source=facts["fico_source"],
    )
    evidence = credit_links.match_evidence(owner, pull, same_client=False)
    direct = getattr(pull, "application_owner_id", None) == owner.id
    owner_name = credit_links.normalize_name(owner.first_name, owner.last_name)
    pull_name = credit_links.normalize_name(pull.legal_first_name, pull.legal_last_name)
    subject_valid = evidence["dob_match"] is not False and (
        evidence["name_match"] or direct and not (owner_name and pull_name)
    )
    if not subject_valid:
        row.update(
            status="needs_review",
            reason="The linked report's subject does not match this owner; staff must review the link",
        )
    elif not pull.fcra_consent:
        row.update(
            status="needs_review",
            reason="Consent is not recorded on the linked pull; staff must review the existing authorization",
        )
    elif not credit_links.is_completed(pull):
        row.update(
            status="processing" if str(pull.status) == "pending" else "needs_review",
            reason="The existing credit pull has not completed; staff must review it before requesting another pull",
        )
    elif pulled is None or pulled > now + timedelta(minutes=5):
        row.update(
            status="needs_review",
            reason="The linked pull's completion date needs staff verification",
        )
    elif expires is not None and expires <= now:
        row.update(
            status="stale",
            reason="The linked report has expired; staff must review whether an updated report is needed",
        )
    elif raw.get("report_import_status") == "pending" or not facts["report_available"]:
        row.update(
            status="processing",
            reason="The existing report is awaiting import; staff should retry its import, not request a duplicate pull",
        )
    else:
        row.update(
            status="collected",
            reason="The completed, consented owner report is linked; program credit checks await staff verification",
        )
    # Receipt hashes bind manual review to the actual linked report, not just
    # its mutable summary score. No report contents or subject PII are copied.
    row["source_fingerprint"] = _digest(
        {
            "pull_id": pull.id,
            "status": pull.status,
            "consent": pull.fcra_consent,
            "pulled_at": pulled,
            "expires_at": expires,
            "report": raw,
            "fico": pull.fico,
            "notes": pull.notes,
            "subject_valid": subject_valid,
            "owner_subject": [owner.first_name, owner.last_name, getattr(owner, "dob", None)],
            "pull_subject": [
                pull.legal_first_name,
                pull.legal_last_name,
                getattr(pull, "dob", None),
            ],
        }
    )
    return row


def report_collection_rows(
    owners: list, pulls: dict, *, now: datetime | None = None,
    expiration_days: int | None = None, required_score_model: str | None = None,
) -> list[dict]:
    """The shared source check used by ownership and selected-program views.

    A cached owner timestamp is not report evidence. A source must still be
    linked, available, current, consented and associated with that person.
    """
    reports = [_owner_report(row, pulls.get(str(row.credit_pull_id)) if row.credit_pull_id else None,
        now=now or datetime.now(UTC), expiration_days=expiration_days,
        required_score_model=required_score_model) for row in owners]
    linked_ids = [row["credit_pull_id"] for row in reports if row["credit_pull_id"]]
    for row in reports:
        if row["credit_pull_id"] and linked_ids.count(row["credit_pull_id"]) > 1:
            row.update(status="needs_review", reason="The same report is linked to multiple required owners; staff must reconcile the owner records and source link before accepting collection")
    return reports


async def current_owner_collections(db, owners: list) -> dict[str, dict]:
    """Bulk-read current sources without importing, pulling or requesting anything."""
    active = [row for row in owners if not getattr(row, "archived_at", None)]
    ids = {row.credit_pull_id for row in active if getattr(row, "credit_pull_id", None)}
    pulls = list((await db.scalars(select(CreditPull).where(CreditPull.id.in_(ids))
        .execution_options(populate_existing=True))).all()) if ids else []
    by_id = {str(row.id): row for row in pulls}
    required = [row for row in active if (_percent(row) or Decimal(0)) >= 20]
    reports = report_collection_rows(required, by_id)
    for row in active:
        if row not in required:
            reports.extend(report_collection_rows([row], by_id))
    return {row["owner_id"]: row for row in reports}


def collection_snapshot(
    profile: Any,
    owners: list,
    pulls: dict,
    requirement: Any,
    *,
    now: datetime | None = None,
    policy_context: dict | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    config = getattr(requirement, "collection_config", None) or {}
    active = [row for row in owners if not getattr(row, "archived_at", None)]
    scope = config.get("owner_id") or config.get("person_id")
    if scope:
        required = [row for row in active if str(row.id) == str(scope)]
        ownership_complete = bool(required)
    else:
        percentages = [_percent(row) for row in active]
        ownership_complete = (
            bool(active)
            and all(value is not None for value in percentages)
            and abs(sum((value or Decimal(0) for value in percentages), Decimal(0)) - 100)
            < Decimal("0.005")
        )
        required = [row for row in active if (_percent(row) or Decimal(0)) >= 20]
    days = getattr(requirement, "expiration_days", None)
    required_model = _required_score_model(requirement, policy_context)
    minimum, policy_blockers = _required_minimum(requirement, policy_context)
    reports = report_collection_rows(required, pulls, now=now, expiration_days=days, required_score_model=required_model)
    collected = sum(row["status"] == "collected" for row in reports)
    complete = ownership_complete and collected == len(required)
    staff_reasons = []
    if not ownership_complete:
        staff_reasons.append(
            "Confirm the current ownership roster and percentages before accepting every required owner's credit review"
        )
    elif not required:
        staff_reasons.append(
            "No current owner meets the 20% credit collection threshold; staff must confirm the roster and applicability before accepting the requirement"
        )
    if any(row["backfill_needs_review"] for row in reports):
        staff_reasons.append("An imported owner record still needs staff confirmation")
    if any(
        row["status"] == "collected"
        and row["required_score_model"]
        and not row["required_score_model_present"]
        for row in reports
    ):
        staff_reasons.append(
            "The linked report does not document the required Equifax FICO 9 score; a different score or model does not establish the program minimum"
        )
    if reports:
        staff_reasons.append(
            "Staff must verify each required owner's bureau, score model and published minimum; collection is not program qualification"
        )
    staff_reasons.extend(
        row["reason"] for row in reports if row["status"] not in {"collected", "missing"}
    )
    review_blockers = list(policy_blockers)
    if not ownership_complete:
        review_blockers.append("Review the current ownership roster and percentages before completing credit review")
    if not required:
        review_blockers.append("Confirm whether this owner-credit requirement applies; no current owner meets its collection threshold")
    if any(row["backfill_needs_review"] for row in reports):
        review_blockers.append("Confirm the imported owner details before completing credit review")
    if not complete:
        review_blockers.append("Every required owner needs a current, consented report with a matching subject")
    if required_model is None:
        review_blockers.append("The required credit bureau and score model need staff policy review")
    for row in reports:
        if not row.get("score_verified"):
            review_blockers.append("Import and verify the actual linked report score; an identity-check placeholder cannot satisfy the published minimum")
        if not row["required_score_model_present"]:
            review_blockers.append(f"The linked report does not establish {required_model or 'the required bureau and score model'}")
        elif row.get("required_score") is None:
            review_blockers.append("The linked report contains conflicting scores for the required bureau and model")
        elif minimum is not None and row["required_score"] < minimum:
            review_blockers.append(f"A required owner's {required_model} score of {row['required_score']} is below the published minimum of {minimum}")
    snapshot = {
        "source": "linked_owner_credit_pulls",
        "complete": complete,
        "required_owner_count": len(required),
        "collected_owner_count": collected,
        "ownership_complete": ownership_complete,
        "owner_review_required": bool(staff_reasons),
        "client_action_required": any(
            row["status"] == "missing" and not row["credit_pull_id"] for row in reports
        ),
        "staff_review_reasons": list(dict.fromkeys(staff_reasons)),
        "owners": reports,
        "required_score_model": required_model,
        "required_minimum_score": minimum,
        "review_blockers": list(dict.fromkeys(review_blockers)),
        "can_mark_met": not review_blockers,
    }
    snapshot["fingerprint"] = _digest(
        {
            "profile_id": profile.id,
            "owners": [
                {
                    "id": row.id,
                    "pct": _percent(row),
                    "backfill_needs_review": bool(getattr(row, "backfill_needs_review", False)),
                }
                for row in sorted(active, key=lambda row: str(row.id))
            ],
            "snapshot": snapshot,
            "definition_id": getattr(requirement, "id", None),
            "review_checks": getattr(requirement, "review_checks", None),
            "completion_criteria": getattr(requirement, "completion_criteria", None),
            "verification_required": getattr(requirement, "verification_required", None),
            "policy_context": policy_context,
        }
    )
    snapshot["review_fingerprint"] = snapshot["fingerprint"]
    return snapshot


async def materialize(db, profile, requirement, state, requested, *, policy_context=None) -> bool:
    """Return true only when owner-linked evidence is the collection authority.

    Files remain the existing fallback when no required owner has a linked pull.
    A stored owner-credit receipt is removed on unlink, so the generic evidence
    engine can reopen the same request and reuse any remaining uploaded files.
    """

    def clear_projection():
        if not (state.provenance or {}).get("credit_collection"):
            return
        metadata = {
            key: value
            for key, value in state.provenance.items()
            if key not in {"credit_collection", "coverage", "verified_coverage"}
        }
        if metadata.get("source") == "linked_owner_credit_pulls":
            metadata.pop("source")
        state.provenance = metadata
        if state.status not in {"waived", "not_applicable"}:
            state.status = "received_unverified" if state.evidence_file_id else "requested"
            state.verified_at, state.verified_by_user_id = None, None
            state.state_reason = "Owner credit collection no longer applies to this scope; reevaluating the requirement's evidence"

    if not supports(
        requirement, profile, expected_subjects=[(policy_context or {}).get("expected_business")]
    ):
        clear_projection()
        return False
    from app.services.application_profiles import owner_rows

    owners = await owner_rows(db, profile)
    ids = {
        row.credit_pull_id
        for row in owners
        if row.credit_pull_id and not getattr(row, "archived_at", None)
    }
    pulls = (
        list((await db.scalars(select(CreditPull).where(CreditPull.id.in_(ids)))).all())
        if ids
        else []
    )
    snapshot = collection_snapshot(
        profile,
        owners,
        {str(row.id): row for row in pulls},
        requirement,
        policy_context=policy_context,
    )
    previous = (state.provenance or {}).get("credit_collection") or {}
    saved_review = (state.provenance or {}).get("credit_review")
    if isinstance(saved_review, dict):
        snapshot["last_review"] = {
            **saved_review,
            "current": saved_review.get("fingerprint") == snapshot["fingerprint"] and state.status == {
                "met": "verified", "failed": "failed", "incomplete": "received_unverified",
            }.get(saved_review.get("decision")),
        }
    no_required_owners = snapshot["ownership_complete"] and snapshot["required_owner_count"] == 0
    if not no_required_owners and not any(row["credit_pull_id"] for row in snapshot["owners"]):
        clear_projection()
        return False
    retained = bool(
        snapshot["complete"]
        and snapshot["can_mark_met"]
        and state.status == "verified"
        and state.verified_by_user_id
        and previous.get("fingerprint") == snapshot["fingerprint"]
    )
    retained_failure = (
        state.status == "failed" and previous.get("fingerprint") == snapshot["fingerprint"]
    )
    required, collected = snapshot["required_owner_count"], snapshot["collected_owner_count"]
    state.provenance = {
        **(state.provenance or {}),
        "credit_collection": snapshot,
        "source": "linked_owner_credit_pulls",
        "coverage": {
            "current": collected,
            "required": required,
            "unit": "owner reports",
            "complete": snapshot["complete"],
        },
        "verified_coverage": {
            "current": required if retained else 0,
            "required": required,
            "unit": "owner reports",
            "complete": retained,
        },
    }
    if state.status not in {"waived", "not_applicable"} and not retained and not retained_failure:
        state.status = "requested" if snapshot["client_action_required"] else "received_unverified"
        state.verified_at, state.verified_by_user_id = None, None
        state.state_reason = (
            f"{collected} of {required} required owner reports collected. "
            + (
                "Credit is still pending for the remaining owner(s). "
                if snapshot["client_action_required"]
                else "Existing credit evidence is with staff; no duplicate upload or pull is requested. "
            )
            + " ".join(snapshot["staff_review_reasons"])
        )
        if no_required_owners:
            state.state_reason = (
                " ".join(snapshot["staff_review_reasons"])
                + ". No client credit upload is requested."
            )
        completed_dates = [_utc(row.pulled_at) for row in pulls if _utc(row.pulled_at)]
        if completed_dates:
            state.received_at = state.received_at or min(completed_dates)
    if requested is not None and not requested.requires_signature:
        requested.status = "uploaded" if not snapshot["client_action_required"] else "requested"
    return True
