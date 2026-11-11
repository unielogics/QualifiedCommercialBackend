"""The implemented marketplace access rules and their shareable description.

Platform approval and self-confirmation are not independent verification of
accreditation or a determination that a securities exemption is available.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.enums import Role

from .. import enums as E

POLICY_VERSION = "marketplace-membership-v1"
CONFIRMED_ISSUER_NAME = "Qualified Commercial LLC"
MEMBER_ROLE = Role.CAPITAL_PARTNER
PREVIEW_ROLES = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC})
APPROVED_STATUS = E.MemberStatus.APPROVED
REQUIRE_PPM_ACK = True
REQUIRE_ACCREDITATION_ACK = True


def is_cleared(member: Any, *, now: datetime | None = None) -> bool:
    if member is None or member.status != APPROVED_STATUS:
        return False
    end = member.waiting_period_ends_at
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return end is None or end <= (now or datetime.now(UTC))


def acknowledgements_satisfied(*, ack_ppm: bool, ack_accredited: bool) -> bool:
    return (not REQUIRE_PPM_ACK or bool(ack_ppm)) and (not REQUIRE_ACCREDITATION_ACK or bool(ack_accredited))


def subscription_context_allowed(*, preview: bool) -> bool:
    return not preview


def describe(settings: Any = None) -> dict[str, Any]:
    from ..models import CpIssuerSettings

    configured = getattr(settings, "default_waiting_period_days", None)
    days = int(configured if configured is not None else CpIssuerSettings.__table__.c.default_waiting_period_days.default.arg)
    requirements = [
        {"key": "membership", "label": "Capital Partners membership",
         "detail": "Investors need staff-approved Capital Partners membership."},
        {"key": "waiting_period", "label": "Recorded relationship waiting period",
         "detail": f"Any recorded relationship waiting period must be complete. The current default is {days} calendar days; a member's approved period may differ."},
        {"key": "acknowledgements", "label": "Subscription acknowledgements",
         "detail": "Investors acknowledge the PPM and provide their accredited-investor self-confirmation when requesting a subscription."},
        {"key": "offering_review", "label": "Offering-specific checks",
         "detail": "Each request is subject to offering-specific review, a current PPM, stated minimums and available capital. Final documents, signing, funding and issuer acceptance remain required."},
        {"key": "preview", "label": "Operator preview",
         "detail": "Authorized operators can preview offerings; subscriptions are made by members."},
    ]
    return {"policy_version": POLICY_VERSION, "label": "Current marketplace membership policy",
        "summary": "Marketplace access requires staff-approved membership and completion of any recorded relationship waiting period.",
        "requirements": requirements, "waiting_period_days": days,
        "limitations": ["Investor eligibility and subscription acceptance are confirmed for each offering."],
        "approved_status": str(APPROVED_STATUS), "waiting_end_must_have_passed_if_set": True,
        "requires_accreditation_acknowledgement": REQUIRE_ACCREDITATION_ACK,
        "requires_ppm_acknowledgement": REQUIRE_PPM_ACK,
        "independent_accreditation_verification": False,
        "operator_subscriptions_allowed": subscription_context_allowed(preview=True)}


def investor_disclosure(policy: dict[str, Any]) -> str:
    return " ".join([*[row["detail"] for row in policy["requirements"]],
        "Investor eligibility and subscription acceptance are confirmed for each offering."])
