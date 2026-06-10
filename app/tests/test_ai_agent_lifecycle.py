from __future__ import annotations

from types import SimpleNamespace

from app.services.ai.ai_agent import _reply_decision_from_text


def test_reply_decision_hands_off_negative_reply():
    status, reason = _reply_decision_from_text(
        body="Please unsubscribe me from these emails."
    )

    assert status == "handed_off"
    assert reason == "opt_out_or_negative_reply"


def test_reply_decision_hands_off_actionable_reply():
    status, reason = _reply_decision_from_text(body="Can we schedule a call tomorrow?")

    assert status == "handed_off"
    assert reason == "qualified_or_actionable_reply"


def test_reply_decision_hands_off_configured_trigger():
    goal = SimpleNamespace(handoff_triggers=["handoff phrase alpha"])

    status, reason = _reply_decision_from_text(
        body="Please note handoff phrase alpha on this conversation.",
        goal=goal,
    )

    assert status == "handed_off"
    assert reason == "matched_configured_handoff_trigger"


def test_reply_decision_keeps_generic_reply_for_review():
    status, reason = _reply_decision_from_text(body="Thanks for the update.")

    assert status == "replied"
    assert reason == "reply_needs_agent_review"
