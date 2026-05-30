"""Rolling chat-history summary for loan / deal chat.

The chat handlers used to replay the last 20 messages verbatim on every
turn, with no prompt caching, so token cost grew with conversation
length. This helper keeps cost flat: the AI sees only the most recent
`WINDOW` turns verbatim plus a one-paragraph rolling summary of
everything older.

Lazy: the summary re-runs only when at least `ROLLUP_MIN_GROWTH` new
unsummarized turns have accumulated since the last roll-up — so the
Haiku call is amortized over many chat turns.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Number of most-recent turns sent verbatim. Older turns are folded
# into the rolling summary.
WINDOW = 6
# Re-summarize when this many new older turns have accumulated since
# the last roll-up. Tuned to match WINDOW so we summarize at the same
# cadence the window advances.
ROLLUP_MIN_GROWTH = 6


def _text_of(result: dict[str, Any]) -> str:
    return "".join(
        b.get("text", "")
        for b in result.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


async def maybe_rollup(
    older_msgs: list[tuple[str, str]],
    prev_summary: str | None,
    prev_count: int,
    *,
    meta: dict[str, Any] | None = None,
) -> tuple[str | None, int]:
    """Return (summary, summary_count). Caller stores both on the
    parent's JSONB profile.

    When fewer than `ROLLUP_MIN_GROWTH` new unsummarized turns exist,
    returns the previous summary unchanged (no LLM call). Each tuple in
    `older_msgs` is `(role, content)` ordered oldest → newest.
    """
    if not older_msgs:
        return prev_summary, prev_count
    if len(older_msgs) - prev_count < ROLLUP_MIN_GROWTH:
        return prev_summary, prev_count

    transcript = "\n".join(
        f"{role.upper()}: {(content or '').strip()[:1000]}"
        for role, content in older_msgs
    )[:12000]
    try:
        from app.services.ai.orchestrator import run

        res = await run(
            [
                {
                    "role": "user",
                    "content": (
                        "Summarize this prior chat between a real-estate broker "
                        "or borrower and an AI assistant about a single loan "
                        "file. Capture open questions, commitments made, key "
                        "facts agreed upon, and anything pending. Six sentences "
                        "max.\n\n" + transcript
                    ),
                }
            ],
            tier="light",
            max_tokens=350,
            system="You write tight conversation summaries.",
            meta={**(meta or {}), "activity": "chat_rollup"},
        )
        text = _text_of(res)
        if text:
            return text, len(older_msgs)
        return prev_summary, prev_count
    except Exception as exc:  # noqa: BLE001
        log.warning("chat rollup failed: %s", exc)
        return prev_summary, prev_count
