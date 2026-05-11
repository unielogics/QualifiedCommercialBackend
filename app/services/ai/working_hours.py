"""Agent working-hours policy — the gate around when the AI may
initiate outreach or respond to inbound borrower messages.

Configured per agent under `AIPlaybookTemplate.rules["working_hours"]`
on the agent's "cadence" playbook (the same JSONB that already
holds follow-up + voice/style). The five fields:

    timezone           IANA, e.g. "America/New_York"
    start_time         "HH:MM" 24h, local-to-timezone
    end_time           "HH:MM" 24h, local-to-timezone
    working_days       list of 3-letter day codes: ["Mon","Tue",...]
    after_hours_rule   one of:
        "do_not_initiate_reply_if_client_first"  (default — AI may
            respond when the borrower wrote first, but never starts
            a conversation off-hours)
        "block_all"     hard off — no outbound, no auto-reply
        "portal_replies_only"   off-hours: only portal channel may
            respond to inbound; email/SMS still blocked

`evaluate()` returns ALLOW / BLOCK + a reason string the dispatcher
records onto the AIOutreachEvent row so the audit trail is legible.

This module is intentionally small and pure — no DB I/O. The caller
hands in the rules dict (loaded from the agent's playbook) and the
context (channel + is_client_initiated). Keeps testing trivial."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.enums import OutreachChannel

# Default policy when an agent hasn't configured working hours yet:
# Mon–Fri 09:00–18:00 America/New_York, polite after-hours behavior.
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_START = "09:00"
DEFAULT_END = "18:00"
DEFAULT_WORKING_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
DEFAULT_AFTER_HOURS_RULE = "do_not_initiate_reply_if_client_first"

VALID_AFTER_HOURS_RULES = {
    "do_not_initiate_reply_if_client_first",
    "block_all",
    "portal_replies_only",
}

_WEEKDAY_CODES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Decision:
    """Result of the working-hours check. `allow=True` means the
    caller may proceed. `reason` is short and audit-friendly — it
    becomes the AIOutreachEvent.error string when blocked."""
    allow: bool
    reason: str = ""


def default_working_hours() -> dict[str, Any]:
    return {
        "timezone": DEFAULT_TIMEZONE,
        "start_time": DEFAULT_START,
        "end_time": DEFAULT_END,
        "working_days": list(DEFAULT_WORKING_DAYS),
        "after_hours_rule": DEFAULT_AFTER_HOURS_RULE,
    }


def normalize(rules: dict[str, Any] | None) -> dict[str, Any]:
    """Defensive coerce — agents who haven't visited the settings
    page yet have no working_hours block; legacy rows may have
    partial blocks. Always return all five fields with safe defaults
    so callers can index without try/except."""
    src = dict((rules or {}).get("working_hours") or {})
    out = default_working_hours()
    if isinstance(src.get("timezone"), str) and src["timezone"]:
        out["timezone"] = src["timezone"]
    for key in ("start_time", "end_time"):
        v = src.get(key)
        if isinstance(v, str) and len(v) == 5 and v[2] == ":":
            out[key] = v
    days = src.get("working_days")
    if isinstance(days, list) and days:
        clean = [d for d in days if isinstance(d, str) and d in _WEEKDAY_CODES]
        if clean:
            out["working_days"] = clean
    rule = src.get("after_hours_rule")
    if isinstance(rule, str) and rule in VALID_AFTER_HOURS_RULES:
        out["after_hours_rule"] = rule
    return out


def is_within_window(rules: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """True when `now` (or current time) falls inside the configured
    timezone-aware working window. Lenient on bad timezone strings —
    falls back to UTC rather than throwing, since this is in the
    dispatch hot path."""
    cfg = normalize(rules)
    try:
        tz = ZoneInfo(cfg["timezone"])
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    moment = (now or datetime.now(tz)).astimezone(tz)
    weekday = _WEEKDAY_CODES[moment.weekday()]
    if weekday not in cfg["working_days"]:
        return False
    hhmm = moment.strftime("%H:%M")
    start = cfg["start_time"]
    end = cfg["end_time"]
    if start <= end:
        return start <= hhmm < end
    # Wraps midnight (start=22:00, end=06:00).
    return hhmm >= start or hhmm < end


def evaluate(
    rules: dict[str, Any] | None,
    *,
    channel: str,
    is_client_initiated: bool,
    now: datetime | None = None,
) -> Decision:
    """The single decision point used by the cadence engine and the
    outreach dispatcher. `channel` is an OutreachChannel value; we
    accept it as a plain string to avoid an import cycle if callers
    pass it pre-stringified."""
    if is_within_window(rules, now=now):
        return Decision(allow=True)

    cfg = normalize(rules)
    rule = cfg["after_hours_rule"]
    if rule == "block_all":
        return Decision(allow=False, reason="outside working hours (block_all)")

    if rule == "do_not_initiate_reply_if_client_first":
        if is_client_initiated:
            return Decision(allow=True)
        return Decision(allow=False, reason="outside working hours — AI may not initiate")

    if rule == "portal_replies_only":
        if not is_client_initiated:
            return Decision(allow=False, reason="outside working hours — AI may not initiate")
        if channel == OutreachChannel.PORTAL.value:
            return Decision(allow=True)
        return Decision(
            allow=False,
            reason=f"outside working hours — channel {channel!r} blocked, portal only",
        )

    # Defensive: unknown rule → behave like the strict default.
    return Decision(allow=False, reason=f"outside working hours (unknown rule {rule!r})")
