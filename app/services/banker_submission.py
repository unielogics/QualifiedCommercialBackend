from __future__ import annotations

from typing import Any

from app.models.public_underwriting_intake import PublicUnderwritingIntake


def build_banker_payload(
    intake: PublicUnderwritingIntake,
    *,
    key_metrics: dict[str, Any],
    program_fit: dict[str, Any],
    entity_structure: dict[str, Any],
    owners: list[dict[str, Any]],
    sensitive_identifiers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assembles ONE normalized JSON payload for an admin to hand to the
    banker's intake system. There is no outbound HTTP call yet -- that
    integration's API spec doesn't exist -- so this function is purely the
    boundary/adapter that shapes the document; the caller decides what to do
    with the returned dict (today: return it to the admin for review/
    download).

    This module deliberately has zero knowledge of dealer_ai_intake.py's
    accessor functions or PROGRAM_LABELS -- the caller computes
    key_metrics/program_fit/entity_structure/owners and passes them in, so
    this stays a pure data-assembly function with no risk of a circular
    import back into the router.

    sensitive_identifiers (SSN / personal Tax ID) is included ONLY in the
    dict this function returns, for that one response. This function does
    not persist or log anything -- it has no DB session and never touches
    intake.intake_state -- but the CALLER must also never persist or log the
    returned dict (or the sensitive_identifiers value specifically). This
    mirrors the never-persisted SSN convention already established by
    AdminCreditPullRequest/run_soft_pull elsewhere in this router: the full
    SSN/Tax ID is accepted transiently and forwarded in-memory only, never
    written to a DB column or JSONB field, never logged.
    """
    return {
        "borrower": {
            "full_name": intake.full_name,
            "email": intake.email,
            "phone": intake.phone,
            "business_name": intake.business_name,
        },
        "financing_request": {
            "loan_purpose": intake.loan_purpose,
            "requested_loan_amount": intake.requested_loan_amount,
        },
        "entity_structure": entity_structure,
        "owners": owners,
        "asset_rows": intake.asset_rows or [],
        "key_metrics": key_metrics,
        "program_fit": program_fit,
        "sensitive_identifiers": sensitive_identifiers or {},
    }
