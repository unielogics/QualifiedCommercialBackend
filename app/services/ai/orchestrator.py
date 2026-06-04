"""Native Anthropic tool-use orchestration.

Routes simple tasks to Haiku, complex ones to Sonnet, runs the tool-use loop
inline (no LangChain). Prompt caching is on by default.

For loan-scoped runs, callers should pre-assemble loan context via
`app.services.ai.context.assemble_loan_context(...)` and concatenate it into
the `system` argument here. That's how operator instructions, AI-modify
corrections, and recent thumbs-down feedback influence the orchestrator's
behavior on the same loan over time.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from anthropic.types import MessageParam
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.anthropic_client import get_client, model_heavy, model_light
from app.services.ai.tools import TOOL_SCHEMAS, TOOLS
from app.services.ai.usage import tracked_messages_create

log = logging.getLogger(__name__)

DEFAULT_SYSTEM = (
    "You are the Qualified Commercial AI assistant — an institutional underwriting "
    "co-pilot for commercial real estate brokers. Be precise with numbers, cite "
    "DSCR / LTV / LTC limits when relevant, and prefer using tools over guessing."
)

Tier = Literal["heavy", "light"]


def _model_for(tier: Tier) -> str:
    return model_heavy() if tier == "heavy" else model_light()


async def record_usage(model: str, usage: Any, meta: dict[str, Any] | None) -> None:
    """Best-effort token-ledger write. Never raises into the caller — a
    failed insert must not break an AI response."""
    if usage is None:
        return
    try:
        from app.constants_pricing import compute_cost
        from app.db import SessionLocal
        from app.models.ai_token_usage import AITokenUsage

        u = usage if isinstance(usage, dict) else usage.model_dump()
        inp = int(u.get("input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        cread = int(u.get("cache_read_input_tokens") or 0)
        cwrite = int(u.get("cache_creation_input_tokens") or 0)
        m = meta or {}

        def _uid(key: str):
            v = m.get(key)
            return str(v) if v else None

        cost = compute_cost(
            model,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cread,
            cache_creation_tokens=cwrite,
        )
        async with SessionLocal() as db:
            db.add(
                AITokenUsage(
                    model=model,
                    activity=str(m.get("activity") or "other")[:48],
                    loan_id=_uid("loan_id"),
                    deal_id=_uid("deal_id"),
                    client_id=_uid("client_id"),
                    ai_agent_id=_uid("ai_agent_id"),
                    broker_id=_uid("broker_id"),
                    input_tokens=inp,
                    cache_read_tokens=cread,
                    cache_creation_tokens=cwrite,
                    output_tokens=out,
                    cost_usd=cost,
                )
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("token-usage ledger write failed: %s", exc)


async def run(
    messages: list[MessageParam],
    *,
    tier: Tier = "light",
    system: str = DEFAULT_SYSTEM,
    max_tokens: int = 1024,
    enable_tools: bool = False,
    cache_system: bool = True,
    meta: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
    feature: str = "orchestrator_chat",
) -> dict[str, Any]:
    """Single-shot completion. If `enable_tools=True`, runs the tool loop until
    the model returns a `text`-terminated stop reason.

    `meta` tags the call for the token-usage ledger:
    {activity, loan_id, deal_id, client_id, ai_agent_id, broker_id}."""
    client = get_client()
    model = _model_for(tier)
    sys_block = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if cache_system
        else system
    )

    convo: list[MessageParam] = list(messages)
    while True:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": sys_block,
            "messages": convo,
        }
        if enable_tools:
            kwargs["tools"] = TOOL_SCHEMAS
        if db is not None:
            resp = await tracked_messages_create(
                db,
                feature=feature,
                client=client,
                metadata=meta,
                **kwargs,
            )
        else:
            resp = await client.messages.create(**kwargs)
        await record_usage(model, resp.usage, meta)

        if resp.stop_reason != "tool_use" or not enable_tools:
            return {
                "stop_reason": resp.stop_reason,
                "content": [b.model_dump() for b in resp.content],
                "usage": resp.usage.model_dump(),
            }

        # Run all requested tools, append results, loop.
        convo.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                fn = TOOLS.get(block.name)
                if fn is None:
                    result_value = {"error": f"unknown tool {block.name}"}
                else:
                    try:
                        result_value = await fn(**(block.input or {}))
                    except Exception as exc:  # surface tool errors back to the model
                        result_value = {"error": str(exc)}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result_value),
                    }
                )
        convo.append({"role": "user", "content": tool_results})


async def chat(
    user_message: str,
    *,
    tier: Tier = "light",
    enable_tools: bool = True,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Convenience for a single-turn chat (used by AIRail + IntakeScreen)."""
    return await run(
        [{"role": "user", "content": user_message}],
        tier=tier,
        enable_tools=enable_tools,
        db=db,
    )
