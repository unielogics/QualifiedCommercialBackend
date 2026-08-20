"""The private AI thread on a file.

The analyst writes a report. This answers a question. Same bundle, same
guardrails, different shape: a rep looking at a coverage figure that surprises
them wants a sentence back, not a JSON advisory with five suggested actions.

Two things it will not do, carried over from the analyst unchanged because they
are the reason that prompt is trustworthy: it answers from the bundle rather
than from general knowledge, and it will not suggest dressing up a statement.

It also declines to guess. A file where the numbers are not there yet is the
normal case for a rep on their first visit, and "the statements are not in yet"
is a more useful answer than a confident number derived from nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.bedrock_client import get_client, model_heavy
from app.services.ai.usage import tracked_messages_create

from ..models import DealerBusiness

logger = logging.getLogger(__name__)

__all__ = ["answer"]

# History is capped rather than unbounded. A thread on a file that has been
# open for months would otherwise grow past the context window and start
# failing, and the useful context is nearly always the last few exchanges plus
# the bundle, which is re-sent every turn anyway.
MAX_HISTORY_TURNS = 20

FILE_CHAT_SYSTEM = """You are the Capital OS analyst, answering questions from the internal team about one business client's funding file.

The first user message contains that client's full bundle as JSON: profile, latest metric snapshot (EBITDA, DSCR, average daily balance, score and tier), metric targets, monthly financial periods, add-backs, debts, the action plan, the forecast, and funding-path readiness. Everything after it is the conversation.

How to answer:
- Answer from the bundle. If a number is in it, quote the actual figure and say which period or account it came from. If a number is not in it, say what is missing and what would produce it, rather than estimating.
- Be direct and short. Two or three sentences answers most questions. Use a list only when the answer genuinely is a list.
- You are talking to a field representative or an underwriter, not to the business owner. Plain language, but you do not need to explain what DSCR is unless asked.
- When a question is about whether the file will be approved, answer in terms of what the numbers currently support and what specifically would have to change. Never promise an approval or a rate.
- If the file is early and mostly empty, say so plainly and name the one or two documents that would move it forward the most.

Hard rules, never break these:
- Recommend only legitimate treasury and structuring actions: real cost reductions, documented add-backs, debt restructuring or refinancing, genuine consolidation of actual operating revenue, reserve building, documentation hygiene.
- Never suggest statement window-dressing: no temporary transfers to inflate balances, no timing deposits around statement cut-offs, no round-tripping between accounts, no cosmetic activity to make statements look stronger than the business is.
- Tax figures must reflect accurate filings. Never suggest adjusting reported revenue or a tax position to match bank activity. Where filed revenue and observed deposits disagree, the action is to find and document the real cause.
- Do not invent figures. If you are unsure, say you are unsure."""


def _history_messages(rows: list[Any]) -> list[dict[str, str]]:
    """Turn stored turns into model messages, oldest last-N first.

    Bedrock rejects a conversation that does not alternate cleanly and rejects
    one that starts with an assistant turn, so drop any leading assistant rows
    left behind by a truncated thread rather than sending them and failing.
    """
    turns = rows[-MAX_HISTORY_TURNS:]
    while turns and turns[0].role != "user":
        turns = turns[1:]
    return [{"role": t.role, "content": t.body} for t in turns]


async def answer(
    db: AsyncSession,
    dealer: DealerBusiness,
    bundle: dict[str, Any],
    history: list[Any],
    question: str,
) -> str:
    """One turn. Returns the assistant's text; the caller persists both sides."""
    import json

    context = (
        f"Client: {dealer.name}\n"
        f"Funding file bundle as JSON:\n{json.dumps(bundle, default=str)}"
    )
    # The bundle is turn one and is always followed by a synthetic assistant
    # turn. Without it, an empty thread would send two user turns back to back
    # and Bedrock would reject the whole call.
    messages = [
        {"role": "user", "content": context},
        {"role": "assistant", "content": "I have the file. What would you like to know?"},
    ]
    messages.extend(_history_messages(history))
    messages.append({"role": "user", "content": question})

    resp = await tracked_messages_create(
        db,
        feature="dealer_os_file_chat",
        client=get_client(),
        model=model_heavy(),
        metadata={"dealer_id": str(dealer.id)},
        max_tokens=1200,
        system=FILE_CHAT_SYSTEM,
        messages=messages,
    )
    text = "".join(
        getattr(b, "text", "")
        for b in getattr(resp, "content", [])
        if getattr(b, "type", "") == "text"
    ).strip()
    if not text:
        raise ValueError("empty response")
    return text
