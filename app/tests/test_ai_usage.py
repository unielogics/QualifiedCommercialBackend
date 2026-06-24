from __future__ import annotations

import pytest

from app.schemas.settings import AISpendSettings
from app.services.ai.usage import AIBudgetExceeded, assert_ai_allowed, estimate_cost_usd, feature_category


def test_feature_categories_split_chat_and_automation():
    assert feature_category("chat") == "chat"
    assert feature_category("ai_thread_chat") == "chat"
    assert feature_category("document_scan") == "document_scan"
    assert feature_category("loan_summary") == "summary"
    assert feature_category("client_summary") == "summary"
    assert feature_category("lender_extract") == "lender_ai"
    assert feature_category("cadence_unknown") == "automation"


def test_cost_estimate_uses_model_tier_pricing():
    # Defaults: light 0.80/4.00 per MTok, heavy 3.00/15.00 per MTok.
    assert estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 4.8
    assert estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0


@pytest.mark.asyncio
async def test_master_switch_blocks_all_ai(monkeypatch):
    async def fake_load_ai_spend_settings(_db):
        return AISpendSettings(master_enabled=False)

    monkeypatch.setattr("app.services.ai.usage.load_ai_spend_settings", fake_load_ai_spend_settings)

    with pytest.raises(AIBudgetExceeded, match="master switch"):
        await assert_ai_allowed(object(), feature="chat")  # type: ignore[arg-type]
