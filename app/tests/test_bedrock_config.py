from __future__ import annotations

from app.config import Settings


def test_bedrock_enabled_allows_iam_without_bearer_token() -> None:
    settings = Settings(bedrock_enabled=True, aws_bearer_token_bedrock="")

    assert settings.ai_provider_enabled is True


def test_bedrock_bearer_token_still_enables_provider() -> None:
    settings = Settings(bedrock_enabled=False, aws_bearer_token_bedrock="bedrock-token")

    assert settings.ai_provider_enabled is True


def test_bedrock_disabled_without_iam_flag_or_token() -> None:
    settings = Settings(bedrock_enabled=False, aws_bearer_token_bedrock="")

    assert settings.ai_provider_enabled is False


def test_default_light_model_uses_bedrock_inference_profile() -> None:
    settings = Settings()

    assert settings.bedrock_model_light == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
