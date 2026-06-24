"""Amazon Bedrock Claude client with an Anthropic-compatible surface."""

from __future__ import annotations

import asyncio
import json
import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from app.config import get_settings


class BedrockUsage:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or {}
        self.input_tokens = int(self._payload.get("input_tokens") or 0)
        self.output_tokens = int(self._payload.get("output_tokens") or 0)
        self.cache_read_input_tokens = int(self._payload.get("cache_read_input_tokens") or 0)
        self.cache_creation_input_tokens = int(self._payload.get("cache_creation_input_tokens") or 0)

    def model_dump(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


class BedrockContentBlock:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        for key, value in payload.items():
            setattr(self, key, value)

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class BedrockMessage:
    def __init__(self, payload: dict[str, Any], *, requested_model: str) -> None:
        self._payload = payload
        self.id = payload.get("id")
        self.type = payload.get("type", "message")
        self.role = payload.get("role", "assistant")
        self.model = payload.get("model") or requested_model
        self.stop_reason = payload.get("stop_reason")
        self.stop_sequence = payload.get("stop_sequence")
        self.content = [BedrockContentBlock(block) for block in payload.get("content", [])]
        self.usage = BedrockUsage(payload.get("usage"))

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "role": self.role,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "stop_sequence": self.stop_sequence,
            "content": [block.model_dump() for block in self.content],
            "usage": self.usage.model_dump(),
        }


class BedrockMessages:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def create(self, *, model: str, **kwargs: Any) -> BedrockMessage:
        body = {"anthropic_version": "bedrock-2023-05-31", **kwargs}
        body.setdefault("max_tokens", 1024)

        def _invoke() -> dict[str, Any]:
            response = self._runtime.invoke_model(
                modelId=model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            return json.loads(response["body"].read())

        payload = await asyncio.to_thread(_invoke)
        return BedrockMessage(payload, requested_model=model)


class BedrockClaudeClient:
    def __init__(self, runtime: Any) -> None:
        self.messages = BedrockMessages(runtime)


@lru_cache
def get_client() -> BedrockClaudeClient:
    settings = get_settings()
    if settings.aws_bearer_token_bedrock:
        os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", settings.aws_bearer_token_bedrock)
    runtime = boto3.client(
        "bedrock-runtime",
        region_name=settings.bedrock_runtime_region,
        config=Config(read_timeout=3600, connect_timeout=10, retries={"max_attempts": 2}),
    )
    return BedrockClaudeClient(runtime)


def model_heavy() -> str:
    """Higher-capability model for document analysis and complex reasoning."""
    return get_settings().bedrock_model_heavy


def model_light() -> str:
    """Lower-cost model for chat, summaries, routing, and formatting."""
    return get_settings().bedrock_model_light
