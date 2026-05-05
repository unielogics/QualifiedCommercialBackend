"""Anthropic SDK client with prompt caching enabled by default."""

from __future__ import annotations

from functools import lru_cache

from anthropic import AsyncAnthropic

from app.config import get_settings


@lru_cache
def get_client() -> AsyncAnthropic:
    settings = get_settings()
    return AsyncAnthropic(api_key=settings.anthropic_api_key or "missing-key-set-ANTHROPIC_API_KEY")


def model_heavy() -> str:
    """Sonnet — for email parsing, doc extraction, underwriting decisions."""
    return get_settings().anthropic_model_heavy


def model_light() -> str:
    """Haiku — for routing, chat, JSON formatting."""
    return get_settings().anthropic_model_light
