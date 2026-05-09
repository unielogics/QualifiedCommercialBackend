"""Firm-wide AI identity + global rules loader.

Stored as a JSONB block on a funding-owned cadence playbook with
product_key='communication' (same row the lending-admin
/communication-rules endpoint patches). Schema (all optional):

  {
    "identity": {
      "ai_name": "Quinn",
      "greeting_style": "friendly" | "formal" | "concise",
      "voice_summary": "Direct, knowledgeable...",
      "brand_signature": "— Quinn, Qualified Commercial",
      "global_rules": ["Never quote rates", ...],
      "forbidden_topics": ["exact rate quotes", "legal advice", ...],
      "redirect_template": "That's something the funding team..."
    }
  }

Falls back to safe defaults when nothing is configured. Result is
cached at module level for a request-cycle (recomputed cheaply on
every chat turn since it's a single SELECT).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_playbook import AIPlaybookTemplate

log = logging.getLogger(__name__)


_DEFAULT_IDENTITY: dict[str, Any] = {
    "ai_name": "Quinn",
    "greeting_style": "friendly",
    "voice_summary": "",
    "brand_signature": "",
    "global_rules": [],
    "forbidden_topics": [],
    "redirect_template": "",
}


async def load_firm_identity(db: AsyncSession) -> dict[str, Any]:
    """Read the firm-wide AI identity + global rules. Returns a dict
    with the safe defaults filled in for any unset key. NEVER raises —
    falls back to defaults on DB error so a misconfigured row can't
    break the AI."""
    try:
        rows = (await db.execute(
            select(AIPlaybookTemplate).where(
                AIPlaybookTemplate.owner_type == "funding",
                AIPlaybookTemplate.playbook_type == "cadence",
                AIPlaybookTemplate.product_key == "communication",
                AIPlaybookTemplate.is_active.is_(True),
            )
        )).scalars().all()
    except Exception:
        log.exception("firm_identity: load failed; using defaults")
        return dict(_DEFAULT_IDENTITY)

    # Multiple funding admins could each have their own row. Pick the
    # one with the most-recently set identity block — first non-empty wins.
    for r in rows:
        ident = (r.rules or {}).get("identity") if isinstance(r.rules, dict) else None
        if isinstance(ident, dict) and any(ident.get(k) for k in ("ai_name", "global_rules", "voice_summary")):
            merged = dict(_DEFAULT_IDENTITY)
            merged.update({k: v for k, v in ident.items() if v not in (None, "")})
            return merged
    return dict(_DEFAULT_IDENTITY)


def render_identity_prefix(identity: dict[str, Any]) -> str:
    """Render the identity dict as a system-prompt prefix block.
    Sits at the TOP of every Realtor + Lending AI system prompt so
    these rules + the AI's name override anything below. Empty-safe:
    returns "" if there's nothing meaningful to say."""
    if not identity:
        return ""
    name = (identity.get("ai_name") or "").strip()
    voice = (identity.get("voice_summary") or "").strip()
    greeting = (identity.get("greeting_style") or "").strip()
    signature = (identity.get("brand_signature") or "").strip()
    rules = [r for r in (identity.get("global_rules") or []) if isinstance(r, str) and r.strip()]
    forbidden = [t for t in (identity.get("forbidden_topics") or []) if isinstance(t, str) and t.strip()]
    redirect = (identity.get("redirect_template") or "").strip()

    parts: list[str] = []
    if name or voice or greeting or signature:
        parts.append("[FIRM AI IDENTITY]")
        if name:
            parts.append(
                f"Your name is {name}. Introduce yourself by name on the first turn of any "
                f"new conversation. After the first turn, do not re-introduce."
            )
        if greeting:
            tone_map = {
                "formal": "Address the user with formal courtesy. Use full sentences. No slang.",
                "friendly": "Write in a warm, approachable voice. First-name basis. Light contractions.",
                "concise": "Keep messages tight. One short paragraph max unless detail is needed.",
            }
            if greeting in tone_map:
                parts.append(tone_map[greeting])
        if voice:
            parts.append(f"Voice: {voice}")
        if signature:
            parts.append(f"Sign emails / longer messages with: {signature}")

    if rules:
        parts.append("")
        parts.append("[GLOBAL RULES — these always apply, regardless of any per-client override below]")
        for r in rules:
            parts.append(f"- {r}")

    if forbidden:
        parts.append("")
        parts.append("[OFF-LIMITS TOPICS]")
        parts.append(f"You are not allowed to engage on: {', '.join(forbidden)}.")
        if redirect:
            parts.append(f"When asked, redirect with: \"{redirect}\"")
        else:
            parts.append("Politely defer and offer to flag it for the human team.")

    if not parts:
        return ""
    return "\n".join(parts) + "\n\n"
