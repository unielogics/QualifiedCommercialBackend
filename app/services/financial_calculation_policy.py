"""Stable dependencies for current financial math and its saved projections.

Working drafts and hypothetical plans do not establish current figures. The
cockpit already selects source-bound earnings and published debt; projections
consume that position and invalidate when its operands or policy change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CALCULATION_POLICY_VERSION = "business-financial-math-v2"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def financial_authority(cockpit: dict[str, Any]) -> dict[str, Any]:
    from app.services.financial_metric_reporting import metric_brief

    review = cockpit.get("metric_review") or {}
    brief = metric_brief(cockpit) or {}
    primary = brief.get("primary_dscr")
    annual = brief.get("validated_annual")
    service = (cockpit.get("debt") or {}).get("service") or {}
    dependency = {
        "policy_version": CALCULATION_POLICY_VERSION,
        "source_stamp": review.get("source_stamp"),
        "revision": review.get("revision", 0),
        "primary_dscr": primary,
        "validated_annual": annual,
        "debt": {key: service.get(key) for key in (
            "source", "version_id", "monthly", "annual", "schedule_authoritative", "declared_none")},
    }
    return {**dependency, "signature": _digest(dependency)}


def authority_idempotency_key(request_key: str, authority: dict[str, Any]) -> str:
    """A replay is identical only while its calculation dependencies match."""
    return f"financial:{CALCULATION_POLICY_VERSION}:{_digest(request_key)[:24]}:{authority['signature'][:24]}"


def authority_matches(stored: Any, current: dict[str, Any]) -> bool:
    return bool(isinstance(stored, dict)
                and stored.get("policy_version") == CALCULATION_POLICY_VERSION
                and stored.get("signature") == current.get("signature"))
