"""AI composer for re-engagement messages.

Generates a channel-appropriate message that pulls a quiet borrower
back into the app — email gets a subject + fuller body, SMS / WhatsApp
get a short single paragraph. Every message ends with a deep link into
the borrower's file.

This is a *trainable* task type: the system prompt is
  <base prompt — kept here> + <operator training layer from task_training>
so a super-admin can tune the tone/instructions via the AI training
interface. With nothing configured, the base prompt alone is used.

Never raises — on any failure (no API key, model error) it returns a
deterministic fallback message so the re-engagement engine always has
something to send.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.client import Client
from app.models.loan import Loan
from app.services.ai.task_training import load_task_config, render_training_block

log = logging.getLogger(__name__)

APP_BASE_URL = "https://app.qualifiedcommercial.com"

_BASE_PROMPT = """You write short re-engagement messages for a commercial real estate borrower who has gone quiet in the Qualified Commercial app while their loan file still needs them.

Goal: warmly and concretely get them to come back and continue — not a guilt trip, not marketing fluff. Reference the specific thing the file is waiting on. Make the next step feel small and easy.

Hard rules:
  - Never quote interest rates, APRs, payments, or "market" numbers.
  - Never promise approval or guarantee terms.
  - Do not invent documents, deadlines, or facts beyond what's given.
  - Always point them to the app link provided — that's the next step.

Output ONLY the message. No preamble, no sign-off block beyond a brief friendly closer.
"""

_CHANNEL_GUIDANCE = {
    "email": "Channel: EMAIL. 2-3 short paragraphs. Also produce a subject line. Warm, professional.",
    "sms": "Channel: SMS. ONE short sentence or two, under ~300 characters. Plain, friendly, no subject.",
    "whatsapp": "Channel: WHATSAPP. One short friendly paragraph, under ~400 characters, no subject.",
}


def _first_name(full: str | None) -> str:
    if not full:
        return "there"
    return full.strip().split()[0] or "there"


def _deep_link(loan: Loan) -> str:
    return f"{APP_BASE_URL}/pipeline?file={loan.id}&tab=documents"


def _fallback(loan: Loan, client: Client, channel: str, reason: str) -> dict[str, str]:
    """Deterministic message used when the model is unavailable."""
    name = _first_name(client.name)
    link = _deep_link(loan)
    addr = loan.address or "your property file"
    if channel == "email":
        body = (
            f"Hi {name},\n\n"
            f"Your file on {addr} is still moving — we just need {reason} "
            f"from you to keep it on track.\n\n"
            f"It only takes a minute: {link}\n\n"
            f"Reply here any time if you have questions.\n\n"
            f"— Qualified Commercial"
        )
        return {"subject": f"Quick step to keep your file moving — {addr}", "body": body}
    body = (
        f"Hi {name} — your {addr} file is waiting on {reason}. "
        f"Pick up where you left off here: {link}"
    )
    return {"subject": "", "body": body}


async def compose_reengagement_message(
    db: AsyncSession,
    *,
    loan: Loan,
    client: Client,
    channel: str,
    reason: str,
) -> dict[str, str]:
    """Return {"subject", "body"} for a re-engagement message on the
    given channel. Subject is "" for SMS / WhatsApp."""
    settings = get_settings()
    link = _deep_link(loan)
    if not settings.ai_provider_enabled:
        return _fallback(loan, client, channel, reason)

    lp = loan.living_profile if isinstance(loan.living_profile, dict) else {}
    current_status = lp.get("current_status") if isinstance(lp, dict) else None

    training = await load_task_config(db, "reengagement")
    training_block = render_training_block(training)

    system = _BASE_PROMPT + (training_block or "")
    channel_line = _CHANNEL_GUIDANCE.get(channel, _CHANNEL_GUIDANCE["sms"])

    context = (
        f"Borrower first name: {_first_name(client.name)}\n"
        f"Property: {loan.address or '(address on file)'}\n"
        f"What the file is waiting on: {reason}\n"
        f"File status (internal AI summary, paraphrase — do not quote verbatim): "
        f"{current_status or '(none)'}\n"
        f"App link to include: {link}\n\n"
        f"{channel_line}\n\n"
        f'Respond as JSON: {{"subject": "...", "body": "..."}} — subject "" for non-email.'
    )

    try:
        from app.services.ai.bedrock_client import get_client, model_light
        from app.services.ai.usage import tracked_messages_create

        result = await tracked_messages_create(
            db,
            feature="reengagement",
            client=get_client(),
            model=model_light(),
            loan_id=loan.id,
            broker_id=loan.broker_id,
            client_id=loan.client_id,
            metadata={"activity": "reengagement", "channel": channel},
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": context}],
        )
        from app.services.ai.orchestrator import record_usage

        await record_usage(
            model_light(),
            result.usage,
            {"activity": "reengagement", "loan_id": loan.id, "broker_id": loan.broker_id},
        )
        text = "".join(
            b.text for b in result.content if getattr(b, "type", None) == "text"
        ).strip()
        import json

        # Tolerate a fenced or bare JSON object.
        if "{" in text and "}" in text:
            blob = text[text.index("{") : text.rindex("}") + 1]
            parsed = json.loads(blob)
            body = str(parsed.get("body") or "").strip()
            subject = str(parsed.get("subject") or "").strip()
            if body:
                if channel == "email" and not subject:
                    subject = f"Quick step to keep your file moving — {loan.address or 'your file'}"
                # Safety net: guarantee the deep link is present.
                if link not in body:
                    body = f"{body}\n\n{link}" if channel == "email" else f"{body} {link}"
                return {"subject": subject if channel == "email" else "", "body": body}
    except Exception as exc:  # noqa: BLE001
        log.warning("reengagement_composer: model call failed: %s", exc)

    return _fallback(loan, client, channel, reason)
