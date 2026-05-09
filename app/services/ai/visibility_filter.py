"""Visibility filter — strip facts the calling role isn't allowed to see.

Every fact carries a `visibility` field (set on the LendingHandoffPacket
+ on `client_ai_plan` per-fact lists). Phase 7 enforces it at
message-render time so a borrower-facing chat turn can NEVER leak
internal_only or agent_visible facts even if a future endpoint forgets
to filter.

Public API:
  - `filter_facts(facts, audience)` — drops disallowed entries.
  - `filter_plan(plan, audience)` — same, applied to `client_ai_plan`.
  - `redact_chat_context(text, plan, audience)` — pass-through; plan
    block render path runs through here.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

Audience = Literal["borrower", "agent", "underwriter", "system"]


# Ordered loosely so a higher-trust audience sees more.
_AUDIENCE_VISIBLE_LABELS: dict[str, set[str]] = {
    "borrower": {"client_visible", "borrower_visible", "shared"},
    "agent": {"agent_visible", "client_visible", "borrower_visible", "shared", "bank_visible"},
    "underwriter": {
        "agent_visible", "client_visible", "borrower_visible",
        "shared", "bank_visible", "internal_only",
    },
    "system": {  # used for cron jobs, internal logs — full access
        "agent_visible", "client_visible", "borrower_visible",
        "shared", "bank_visible", "internal_only",
    },
}


def _labels_for(audience: str) -> set[str]:
    return _AUDIENCE_VISIBLE_LABELS.get(audience, _AUDIENCE_VISIBLE_LABELS["agent"])


def _is_visible(visibility: Any, audience: str) -> bool:
    """Decide whether a single fact's visibility marker is in the
    audience's allowed set.

    `visibility` may be:
      - None / missing → defaults to `agent_visible` (safe default)
      - str → single label
      - list[str] → any-match wins
    """
    allowed = _labels_for(audience)
    if visibility is None:
        return audience != "borrower"  # default: hide from borrower
    if isinstance(visibility, list):
        return any(v in allowed for v in visibility)
    return visibility in allowed


def filter_facts(
    facts: Iterable[dict[str, Any]] | None,
    audience: Audience,
) -> list[dict[str, Any]]:
    """Return only the facts the audience is allowed to see.
    Entries without an explicit visibility default to `agent_visible`
    (safe — borrowers don't see them)."""
    if not facts:
        return []
    out: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if _is_visible(f.get("visibility"), audience):
            out.append(f)
    return out


def filter_plan(plan_dict: dict[str, Any], audience: Audience) -> dict[str, Any]:
    """Apply visibility filtering to a plan-shaped dict. Returns a
    NEW dict — does NOT mutate the input. Drops:
      - required_items entries the audience can't see
      - waived_items entries the audience can't see
      - custom_instructions when audience=borrower (always agent-internal)
    """
    out = dict(plan_dict)
    out["required_items"] = filter_facts(plan_dict.get("required_items"), audience)
    out["waived_items"] = filter_facts(plan_dict.get("waived_items"), audience)
    if audience == "borrower":
        out["custom_instructions"] = None
    return out


def filter_handoff_packet(packet_dict: dict[str, Any], audience: Audience) -> dict[str, Any]:
    """Same shape filter applied to a LendingHandoffPacket dict."""
    out = dict(packet_dict)
    out["extracted_facts"] = filter_facts(packet_dict.get("extracted_facts"), audience)
    if audience == "borrower":
        # Borrowers should never see realtor-side commentary at all —
        # the agent's internal notes about them stay internal.
        rs = packet_dict.get("realtor_summary") or {}
        if "agent_notes" in rs:
            rs = {k: v for k, v in rs.items() if k != "agent_notes"}
        out["realtor_summary"] = rs
    return out
