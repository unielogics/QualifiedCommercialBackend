"""Per-task AI training config — loader + prompt renderer.

The "train the AI" interface lets a super-admin configure, per AI task
type, the instructions + tone the AI uses. Config is stored on the same
funding 'communication' cadence playbook `rules` JSONB that
`firm_identity` reads, under a `task_training` key:

  rules.task_training = {
    "doc_collection_nudge": {instructions, tone, dos[], donts[],
                             examples[], updated_by, updated_at},
    "nurture_chat":         {...},
    "reengagement":         {...},
  }

Each trainable AI task composes its system prompt as:

  <base structural prompt — kept in code: JSON schema, hard rules>
  + render_training_block(config)        <- the operator-configured layer

When nothing is configured for a task, `load_task_config` returns an
empty config and `render_training_block` returns "" — so the caller
falls back to its hardcoded default and behavior is byte-identical to
today. Training is purely additive.

Never raises — a misconfigured row falls back to empty (no training).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_playbook import AIPlaybookTemplate

log = logging.getLogger(__name__)

# The borrower-facing task types exposed for training in v1.
TASK_KEYS: tuple[str, ...] = (
    "doc_collection_nudge",
    "nurture_chat",
    "reengagement",
)

TASK_LABELS: dict[str, str] = {
    "doc_collection_nudge": "Document-collection nudges",
    "nurture_chat": "Nurture AI chat replies",
    "reengagement": "Re-engagement messages",
}

_EMPTY: dict[str, Any] = {
    "instructions": "",
    "tone": "",
    "dos": [],
    "donts": [],
    "examples": [],
}


def _empty() -> dict[str, Any]:
    return {"instructions": "", "tone": "", "dos": [], "donts": [], "examples": []}


async def _communication_playbook(db: AsyncSession) -> AIPlaybookTemplate | None:
    """The funding 'communication' cadence playbook row — the same row
    firm_identity reads. Returns the first active one, or None."""
    try:
        rows = (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.owner_type == "funding",
                    AIPlaybookTemplate.playbook_type == "cadence",
                    AIPlaybookTemplate.product_key == "communication",
                    AIPlaybookTemplate.is_active.is_(True),
                )
            )
        ).scalars().all()
    except Exception:
        log.exception("task_training: playbook load failed")
        return None
    return rows[0] if rows else None


async def load_task_config(db: AsyncSession, task_key: str) -> dict[str, Any]:
    """Configured training for one task type, with empty defaults filled
    in. Empty config => the caller uses its hardcoded default prompt."""
    pb = await _communication_playbook(db)
    if pb is None or not isinstance(pb.rules, dict):
        return _empty()
    training = pb.rules.get("task_training")
    if not isinstance(training, dict):
        return _empty()
    cfg = training.get(task_key)
    if not isinstance(cfg, dict):
        return _empty()
    merged = _empty()
    for k in ("instructions", "tone"):
        v = cfg.get(k)
        if isinstance(v, str):
            merged[k] = v
    for k in ("dos", "donts", "examples"):
        v = cfg.get(k)
        if isinstance(v, list):
            merged[k] = [str(x) for x in v if x]
    return merged


async def load_all_task_configs(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """All trainable task configs — for the admin training UI."""
    pb = await _communication_playbook(db)
    training: dict[str, Any] = {}
    if pb is not None and isinstance(pb.rules, dict):
        t = pb.rules.get("task_training")
        if isinstance(t, dict):
            training = t
    out: dict[str, dict[str, Any]] = {}
    for key in TASK_KEYS:
        cfg = training.get(key) if isinstance(training.get(key), dict) else {}
        merged = _empty()
        for k in ("instructions", "tone"):
            if isinstance(cfg.get(k), str):
                merged[k] = cfg[k]
        for k in ("dos", "donts", "examples"):
            if isinstance(cfg.get(k), list):
                merged[k] = [str(x) for x in cfg[k] if x]
        out[key] = merged
    return out


def render_training_block(cfg: dict[str, Any]) -> str:
    """Render a configured task config into a system-prompt fragment.

    Returns "" when the config is empty — so an untrained task adds
    nothing and the caller's hardcoded prompt stands unchanged."""
    if not isinstance(cfg, dict):
        return ""
    instructions = (cfg.get("instructions") or "").strip()
    tone = (cfg.get("tone") or "").strip()
    dos = [str(x).strip() for x in (cfg.get("dos") or []) if str(x).strip()]
    donts = [str(x).strip() for x in (cfg.get("donts") or []) if str(x).strip()]
    examples = [str(x).strip() for x in (cfg.get("examples") or []) if str(x).strip()]
    if not (instructions or tone or dos or donts or examples):
        return ""

    parts: list[str] = ["", "--- OPERATOR TRAINING (configured for this task) ---"]
    if instructions:
        parts.append(f"Instructions: {instructions}")
    if tone:
        parts.append(f"Tone / voice: {tone}")
    if dos:
        parts.append("Do:\n" + "\n".join(f"  - {d}" for d in dos))
    if donts:
        parts.append("Don't:\n" + "\n".join(f"  - {d}" for d in donts))
    if examples:
        parts.append(
            "Example phrasings to emulate:\n"
            + "\n".join(f"  - {e}" for e in examples)
        )
    return "\n".join(parts)
