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

from typing import Any, Literal

from anthropic.types import MessageParam

from app.services.ai.anthropic_client import get_client, model_heavy, model_light
from app.services.ai.tools import TOOL_SCHEMAS, TOOLS

DEFAULT_SYSTEM = (
    "You are the Qualified Commercial AI assistant — an institutional underwriting "
    "co-pilot for commercial real estate brokers. Be precise with numbers, cite "
    "DSCR / LTV / LTC limits when relevant, and prefer using tools over guessing."
)

Tier = Literal["heavy", "light"]


def _model_for(tier: Tier) -> str:
    return model_heavy() if tier == "heavy" else model_light()


async def run(
    messages: list[MessageParam],
    *,
    tier: Tier = "light",
    system: str = DEFAULT_SYSTEM,
    max_tokens: int = 1024,
    enable_tools: bool = False,
    cache_system: bool = True,
) -> dict[str, Any]:
    """Single-shot completion. If `enable_tools=True`, runs the tool loop until
    the model returns a `text`-terminated stop reason."""
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
        resp = await client.messages.create(**kwargs)

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
) -> dict[str, Any]:
    """Convenience for a single-turn chat (used by AIRail + IntakeScreen)."""
    return await run(
        [{"role": "user", "content": user_message}],
        tier=tier,
        enable_tools=enable_tools,
    )
