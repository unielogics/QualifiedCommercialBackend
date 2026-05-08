"""Natural-language doc collection — Haiku-driven nudges.

The borrower-facing chat needs to feel like a human secretary, not
a cron job. This module classifies each outstanding Document into
one of five collection scenarios based on how close it is to its
due date, and composes a unique conversational chat message via
Anthropic Haiku — the AI sees the borrower's first name, the
specific docs in focus, what's already been submitted, and the
loan's close date, then writes a short message in our concierge
voice.

Scenarios (relative to `requested_on + due_offset_days`):
  heads_up   → due in 1-3 days  (gentle "heads up so you can plan")
  due_today  → due today        ("today's the day")
  just_late  → 1-3 days overdue ("running a couple days late")
  week_late  → 4-7 days overdue ("now a week out — let's get past it")
  escalating → 8+ days overdue  ("looped your broker in")

Each (doc, scenario) fires AT MOST ONCE — gated by Activity rows
so a single doc can move through all five scenarios over its
lifecycle without repeating any one of them. When multiple docs
are in the same scenario for the same loan at scan time, they
bundle into one conversational message — referenced by name in the
copy, deep-linked in the message's `actions` array.

Falls back to a templated message if Anthropic is unavailable;
the action CTAs ride independently so functionality is preserved.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.config import get_settings
from app.models.client import Client
from app.models.document import Document
from app.models.loan import Loan
from app.services.ai.anthropic_client import get_client, model_light

log = logging.getLogger(__name__)

Scenario = Literal["heads_up", "due_today", "just_late", "week_late", "escalating"]

SCENARIO_LABELS: dict[Scenario, str] = {
    "heads_up": "due in the next 1-3 days (early heads-up)",
    "due_today": "due today",
    "just_late": "1-3 days overdue (just slipped past due)",
    "week_late": "4-7 days overdue (now blocking the file)",
    "escalating": "8+ days overdue (broker now looped in)",
}

# Per-scenario tone guidance fed to Haiku alongside the scenario
# label. Keeps the system prompt tight while shaping voice per
# stage — without these, the model defaults to "no rush" warmth
# even when the doc is a week overdue.
SCENARIO_TONE: dict[Scenario, str] = {
    "heads_up": (
        "Friendly heads-up. Frame as planning ahead so the borrower "
        "can gather the docs at their own pace. Phrases like 'just "
        "wanted to flag this so you can plan' work well. Do NOT "
        "imply lateness — nothing is late yet."
    ),
    "due_today": (
        "Today is the day. Friendly but mention that today is when "
        "we're hoping to receive it. No urgency, no scolding — just "
        "a check-in that today is the target."
    ),
    "just_late": (
        "A couple days late. Acknowledge that we're past the due "
        "date but stay warm — life happens. Offer help. Do NOT say "
        "'no rush' — it IS late. Phrases like 'running a couple "
        "days behind' or 'noticed we're past the date' work."
    ),
    "week_late": (
        "About a week late. The file is now genuinely blocked. Tone "
        "shifts from friendly to a touch more direct — still kind, "
        "but the borrower needs to act. Say something like 'this is "
        "now blocking the file' or 'we really do need this to keep "
        "moving'. Offer help if they're stuck. Do NOT say 'no rush' "
        "or 'whenever you can' — that contradicts the urgency."
    ),
    "escalating": (
        "Significantly overdue (8+ days). You've already looped in "
        "the borrower's loan team / broker because the silence has "
        "gone on long enough that human follow-up is needed. Tell "
        "the borrower their loan team is now involved, and ask "
        "directly what's going on. Stay warm — there might be a "
        "real reason — but don't pretend everything is fine."
    ),
}

# Activity dedup kinds — `just_late` reuses `doc.reminder.first` so
# the existing kind history (and any AITask side-effects keyed off
# it) keeps working through this transition.
ACTIVITY_KIND: dict[Scenario, str] = {
    "heads_up": "doc.reminder.heads_up",
    "due_today": "doc.reminder.due_today",
    "just_late": "doc.reminder.first",
    "week_late": "doc.reminder.second",
    "escalating": "doc.escalated",
}


def classify(days_until_due: int) -> Scenario | None:
    """Map days-until-due (positive = before due, negative =
    overdue) to a collection scenario, or None when the doc is
    too far out to bother nudging yet (4+ days before due)."""
    if 1 <= days_until_due <= 3:
        return "heads_up"
    if days_until_due == 0:
        return "due_today"
    if -3 <= days_until_due <= -1:
        return "just_late"
    if -7 <= days_until_due <= -4:
        return "week_late"
    if days_until_due <= -8:
        return "escalating"
    return None


@dataclass
class DocCollectionContext:
    scenario: Scenario
    loan: Loan
    focus_docs: list[Document]            # docs entering this scenario today
    already_submitted: list[Document]     # received/verified docs on this loan
    close_date: date | None
    borrower_first_name: str | None


def first_name_of(client: Client | None) -> str | None:
    """Best-effort first name. Falls back to None when we'd be
    guessing — the prompt handles the missing-name case."""
    if client is None:
        return None
    name = (client.name or "").strip()
    if not name:
        return None
    first = name.split()[0]
    if first.lower() in {"the", "client", "borrower", "tbd"}:
        return None
    return first


SYSTEM_PROMPT = """You are the AI Intelligent Underwriter at Qualified Commercial — a borrower-facing concierge helping a borrower complete the documents needed to fund their commercial real estate loan. You are gentle, warm, and conversational. Never demanding or robotic. You're the human-feeling secretary chasing paperwork.

Generate a SHORT chat message asking for documents — 1-3 sentences, max ~60 words.

Style rules:
- Use the borrower's first name when given (no honorifics, no last name).
- No bullets, numbered lists, or markdown headers. Bold a doc name with **double asterisks** sparingly — at most one per message.
- Write the actual document names — never placeholders like {doc_name}.
- Don't lecture or scold. If a doc is overdue, acknowledge it gently ("a couple days late", "running a little behind", "been a minute").
- For early heads-up, frame as planning ahead, not pressure.
- For the broker-loop scenario (escalating), tell the borrower you've involved their loan team but stay friendly.
- Mention the close date only when it's tight (under 21 days away) AND it adds urgency.
- If the borrower has already submitted other docs, give them ONE small piece of credit ("you're already most of the way through") — don't enumerate.
- End by inviting action ("tap below", "drop them in here", "send them over"). The frontend renders upload buttons after your text — don't simulate them.

Output ONLY the message body. No preamble, no quotes, no signoff, no surrounding markdown."""


async def compose_collection_nudge(ctx: DocCollectionContext) -> str:
    """Generate a natural-sounding chat message for this scenario.
    Falls back to a templated message if Anthropic is unavailable
    or returns an empty reply."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return _fallback_template(ctx)

    first_name = ctx.borrower_first_name or "there"
    focus_lines = "\n".join(f"- {d.name}" for d in ctx.focus_docs)
    submitted_count = len(ctx.already_submitted)
    submitted_summary = (
        f"{submitted_count} doc(s) received so far"
        if submitted_count > 0 else "none yet"
    )
    days_to_close = (
        (ctx.close_date - date.today()).days if ctx.close_date else None
    )
    close_line = (
        f"Close date: {ctx.close_date.isoformat()} "
        f"({days_to_close} days away)"
        if ctx.close_date is not None
        else "Close date: not set yet"
    )

    user_prompt = textwrap.dedent(
        f"""\
        SCENARIO: {ctx.scenario} — {SCENARIO_LABELS[ctx.scenario]}

        TONE GUIDANCE FOR THIS SCENARIO:
        {SCENARIO_TONE[ctx.scenario]}

        BORROWER FIRST NAME: {first_name}
        LOAN: {ctx.loan.deal_id} ({ctx.loan.address})
        {close_line}
        SUBMITTED HISTORY: {submitted_summary}

        DOCS NEEDED IN THIS MESSAGE (focus on these — there are {len(ctx.focus_docs)}):
        {focus_lines}

        Compose the message body."""
    ).strip()

    try:
        client = get_client()
        result = await client.messages.create(
            model=model_light(),
            max_tokens=220,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(
            b.text for b in result.content
            if getattr(b, "type", None) == "text"
        ).strip()
        # Strip leading/trailing quotes the model occasionally adds.
        text = text.strip().strip('"').strip("'").strip()
        if not text:
            log.warning(
                "compose_collection_nudge: empty Haiku reply, falling back. scenario=%s loan=%s",
                ctx.scenario, ctx.loan.deal_id,
            )
            return _fallback_template(ctx)
        return text
    except Exception:  # noqa: BLE001
        log.warning(
            "compose_collection_nudge: Haiku failed, falling back. scenario=%s",
            ctx.scenario, exc_info=True,
        )
        return _fallback_template(ctx)


def _fallback_template(ctx: DocCollectionContext) -> str:
    """Deterministic fallback when Haiku is unavailable. Less
    natural than the AI version, but keeps the system functional
    when Anthropic is down."""
    name = ctx.borrower_first_name or "there"
    if len(ctx.focus_docs) == 1:
        nm = ctx.focus_docs[0].name
        if ctx.scenario == "heads_up":
            return (
                f"Hey {name} — heads up that **{nm}** is coming due in the "
                f"next couple of days. Tap below whenever it's ready."
            )
        if ctx.scenario == "due_today":
            return (
                f"Hey {name} — **{nm}** is due today. Drop it in here "
                f"whenever you've got it."
            )
        if ctx.scenario == "just_late":
            return (
                f"Hey {name} — still waiting on **{nm}** (running a couple "
                f"days behind). Let me know if you're hitting any snags."
            )
        if ctx.scenario == "week_late":
            return (
                f"Hey {name} — **{nm}** has been outstanding for about a "
                f"week now and we can't move forward without it. Need a hand?"
            )
        if ctx.scenario == "escalating":
            return (
                f"Hey {name} — I've looped your loan team in on **{nm}** "
                f"since it's been a while. Let's get past this together."
            )
    else:
        names = ", ".join(d.name for d in ctx.focus_docs[:3])
        rest = (
            f" (+ {len(ctx.focus_docs) - 3} more)"
            if len(ctx.focus_docs) > 3 else ""
        )
        if ctx.scenario == "heads_up":
            return (
                f"Hey {name} — a few items are coming due in the next few "
                f"days: {names}{rest}. Tap below when you're ready."
            )
        if ctx.scenario == "due_today":
            return (
                f"Hey {name} — these are due today: {names}{rest}. Drop "
                f"them in here whenever they're ready."
            )
        if ctx.scenario == "just_late":
            return (
                f"Hey {name} — still waiting on a few items: {names}{rest}. "
                f"Any of these giving you trouble?"
            )
        if ctx.scenario == "week_late":
            return (
                f"Hey {name} — these have been outstanding for about a "
                f"week: {names}{rest}. Let's get them squared away."
            )
        if ctx.scenario == "escalating":
            return (
                f"Hey {name} — I've looped your loan team in on these: "
                f"{names}{rest}. Let's get past it together."
            )
    return f"Hey {name} — a few docs need your attention. Tap below to upload."
