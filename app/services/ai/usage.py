"""AI spend ledger + daily budget enforcement."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.app_settings import AppSettings
from app.models.ai_usage_event import AIUsageEvent
from app.schemas.settings import AISpendSettings, AppSettingsData


CHAT_FEATURES = {
    "chat",
    "ai_thread_chat",
    "deal_chat",
    "loan_workspace_chat",
    "doc_collection_chat",
    "orchestrator_chat",
}
SUMMARY_FEATURES = {"loan_summary", "client_summary", "pipeline_digest"}
DOCUMENT_FEATURES = {"document_scan"}
LENDER_FEATURES = {
    "lender_extract",
    "lender_thread_reply",
    "lender_thread_subject",
    "lender_send",
    "handoff_seed",
    "deal_secretary",
}
PROPERTY_FEATURES = {"property_analysis"}


class AIBudgetExceeded(RuntimeError):
    """Raised before a paid provider call when a manual kill switch blocks it."""


def feature_category(feature: str) -> str:
    if feature in CHAT_FEATURES:
        return "chat"
    if feature in SUMMARY_FEATURES:
        return "summary"
    if feature in DOCUMENT_FEATURES:
        return "document_scan"
    if feature in LENDER_FEATURES:
        return "lender_ai"
    if feature in PROPERTY_FEATURES:
        return "property_analysis"
    return "automation"


def _is_heavy_model(model: str) -> bool:
    low = (model or "").lower()
    return "sonnet" in low or "opus" in low


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    settings = get_settings()
    if _is_heavy_model(model):
        in_rate = settings.ai_pricing_heavy_input_per_mtok
        out_rate = settings.ai_pricing_heavy_output_per_mtok
    else:
        in_rate = settings.ai_pricing_light_input_per_mtok
        out_rate = settings.ai_pricing_light_output_per_mtok
    return round((input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate), 6)


def _usage_tokens(resp: Any) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return input_tokens, output_tokens


def _metadata_uuid(metadata: dict[str, Any] | None, key: str) -> UUID | None:
    if not metadata:
        return None
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _spend_today(db: AsyncSession, *, category: str | None = None) -> float:
    stmt = select(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0)).where(
        AIUsageEvent.created_at >= _day_start()
    )
    if category is not None:
        stmt = stmt.where(AIUsageEvent.category == category)
    value = (await db.execute(stmt)).scalar_one()
    return float(value or 0)


async def load_ai_spend_settings(db: AsyncSession) -> AISpendSettings:
    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    if row is None:
        return AISpendSettings()
    return AppSettingsData.model_validate(row.data or {}).ai_spend


async def assert_ai_allowed(
    db: AsyncSession,
    *,
    feature: str,
) -> str:
    settings = await load_ai_spend_settings(db)
    category = feature_category(feature)

    if not settings.master_enabled:
        raise AIBudgetExceeded("AI system is disabled by the master switch")
    if category == "chat" and not settings.chat_enabled:
        raise AIBudgetExceeded("AI chat is disabled in Super Admin AI spend settings")
    if category != "chat" and not settings.automations_enabled:
        raise AIBudgetExceeded("AI automations are disabled in Super Admin AI spend settings")
    if category == "document_scan" and not settings.document_scanning_enabled:
        raise AIBudgetExceeded("AI document scanning is disabled in Super Admin AI spend settings")
    if category == "summary" and not settings.summaries_enabled:
        raise AIBudgetExceeded("AI summaries are disabled in Super Admin AI spend settings")
    if category == "lender_ai" and not settings.lender_ai_enabled:
        raise AIBudgetExceeded("Lender AI is disabled in Super Admin AI spend settings")

    return category


async def assert_ai_allowed_global(*, feature: str) -> str:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        return await assert_ai_allowed(db, feature=feature)


async def record_ai_usage(
    db: AsyncSession,
    *,
    feature: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    user_id: UUID | None = None,
    broker_id: UUID | None = None,
    client_id: UUID | None = None,
    loan_id: UUID | None = None,
    thread_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AIUsageEvent:
    category = feature_category(feature)
    event = AIUsageEvent(
        feature=feature,
        category=category,
        provider="bedrock",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimate_cost_usd(model, input_tokens, output_tokens),
        user_id=user_id,
        broker_id=broker_id,
        client_id=client_id,
        loan_id=loan_id,
        thread_id=thread_id,
        metadata_json=metadata,
    )
    db.add(event)
    await db.flush()
    return event


async def tracked_messages_create(
    db: AsyncSession,
    *,
    feature: str,
    client: Any,
    model: str,
    user_id: UUID | None = None,
    broker_id: UUID | None = None,
    client_id: UUID | None = None,
    loan_id: UUID | None = None,
    thread_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    await assert_ai_allowed(db, feature=feature)
    resp = await client.messages.create(model=model, **kwargs)
    input_tokens, output_tokens = _usage_tokens(resp)
    await record_ai_usage(
        db,
        feature=feature,
        model=getattr(resp, "model", None) or model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        user_id=user_id or _metadata_uuid(metadata, "user_id"),
        broker_id=broker_id or _metadata_uuid(metadata, "broker_id"),
        client_id=client_id or _metadata_uuid(metadata, "client_id"),
        loan_id=loan_id or _metadata_uuid(metadata, "loan_id"),
        thread_id=thread_id or _metadata_uuid(metadata, "thread_id"),
        metadata=metadata,
    )
    return resp
