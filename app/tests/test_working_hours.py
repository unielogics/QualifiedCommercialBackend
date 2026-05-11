"""Unit tests for app/services/ai/working_hours.py.

Pure, no DB — exercises the decision matrix for the three after-hours
rules and the timezone-aware window check. Behavior we care about:

  • Inside the window: ALWAYS allow.
  • Outside the window, block_all: BLOCK regardless of who initiated.
  • Outside, do_not_initiate_reply_if_client_first:
       initiated=False → BLOCK (AI may not start a thread off-hours)
       initiated=True  → ALLOW (borrower wrote first — be a good citizen)
  • Outside, portal_replies_only:
       initiated=False → BLOCK
       initiated=True + channel=portal → ALLOW
       initiated=True + channel=email/sms → BLOCK
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.ai.working_hours import (
    default_working_hours,
    evaluate,
    is_within_window,
    normalize,
)


def _rules(**overrides):
    base = default_working_hours()
    base.update(overrides)
    return {"working_hours": base}


def test_inside_window_allows_everything():
    # Tue 2025-05-13 14:00 New York — inside Mon-Fri 09:00-18:00.
    now = datetime(2025, 5, 13, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    rules = _rules()
    for ch in ("portal", "email", "sms"):
        for initiated in (True, False):
            d = evaluate(rules, channel=ch, is_client_initiated=initiated, now=now)
            assert d.allow, f"channel={ch} initiated={initiated} got reason={d.reason!r}"


def test_outside_window_block_all_blocks_even_inbound():
    # Sat afternoon — outside default Mon-Fri.
    now = datetime(2025, 5, 17, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    rules = _rules(after_hours_rule="block_all")
    d = evaluate(rules, channel="portal", is_client_initiated=True, now=now)
    assert not d.allow
    assert "block_all" in d.reason


def test_outside_window_do_not_initiate_allows_inbound():
    now = datetime(2025, 5, 17, 14, 0, tzinfo=ZoneInfo("America/New_York"))  # Sat
    rules = _rules(after_hours_rule="do_not_initiate_reply_if_client_first")
    # AI may not start
    d = evaluate(rules, channel="portal", is_client_initiated=False, now=now)
    assert not d.allow
    # but may respond when borrower wrote first
    d2 = evaluate(rules, channel="portal", is_client_initiated=True, now=now)
    assert d2.allow


def test_outside_window_portal_replies_only_restricts_channels():
    now = datetime(2025, 5, 17, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    rules = _rules(after_hours_rule="portal_replies_only")
    # Off-hours portal reply to inbound — allowed.
    d_portal = evaluate(rules, channel="portal", is_client_initiated=True, now=now)
    assert d_portal.allow
    # Off-hours email reply to inbound — blocked.
    d_email = evaluate(rules, channel="email", is_client_initiated=True, now=now)
    assert not d_email.allow
    # Off-hours initiation — blocked regardless of channel.
    d_init = evaluate(rules, channel="portal", is_client_initiated=False, now=now)
    assert not d_init.allow


def test_window_check_respects_timezone():
    # 22:00 in New York is 19:00 in Los Angeles — that's outside an
    # LA-configured 9-18 window but the rules are LA-tz, so we compute
    # local-to-LA, which is 19:00, outside.
    rules = _rules(timezone="America/Los_Angeles")
    moment_ny_22 = datetime(2025, 5, 13, 22, 0, tzinfo=ZoneInfo("America/New_York"))
    assert not is_within_window(rules, now=moment_ny_22)
    # And 09:00 LA local-time IS inside.
    moment_la_morning = datetime(2025, 5, 13, 9, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert is_within_window(rules, now=moment_la_morning)


def test_normalize_fills_defaults_for_missing_fields():
    # Partial config — missing timezone and working_days.
    raw = {"working_hours": {"start_time": "08:00"}}
    out = normalize(raw)
    assert out["start_time"] == "08:00"
    assert out["timezone"] == "America/New_York"
    assert out["working_days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert out["after_hours_rule"] == "do_not_initiate_reply_if_client_first"


def test_normalize_rejects_unknown_after_hours_rule():
    raw = {"working_hours": {"after_hours_rule": "made_up"}}
    out = normalize(raw)
    assert out["after_hours_rule"] == "do_not_initiate_reply_if_client_first"


def test_window_wraps_midnight():
    # Night shift: 22:00 → 06:00 means inside between those hours.
    rules = _rules(start_time="22:00", end_time="06:00")
    inside = datetime(2025, 5, 13, 23, 30, tzinfo=ZoneInfo("America/New_York"))
    outside = datetime(2025, 5, 13, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_within_window(rules, now=inside)
    assert not is_within_window(rules, now=outside)


def test_sending_control_to_outreach_mode_mapping():
    from app.services.ai.agent_settings import sending_control_to_outreach_mode

    assert sending_control_to_outreach_mode("auto_send_portal") == "portal_auto"
    assert sending_control_to_outreach_mode("ask_before_sending") == "draft_first"
    assert sending_control_to_outreach_mode("draft_only") == "draft_first"
    assert sending_control_to_outreach_mode(None) == "draft_first"
    assert sending_control_to_outreach_mode("garbage") == "draft_first"
