"""Shared safeguards for structured model responses.

Document ingestion must never treat a token-limited JSON response as a
successful extraction.  Keep that check central so every PDF/vision pipeline
uses the provider's stop metadata consistently.
"""

from __future__ import annotations

import json
import re
from typing import Any


class ModelOutputTruncated(ValueError):
    """Raised when the provider stopped before completing structured output."""


def response_text(response: Any) -> str:
    return "".join(
        str(getattr(block, "text", "") or "")
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
    )


def require_complete_response(response: Any, *, purpose: str = "structured output") -> None:
    if is_truncated_response(response):
        raise ModelOutputTruncated(
            f"Model {purpose} was truncated before its JSON response completed"
        )


def is_truncated_response(response: Any) -> bool:
    stop_reason = str(getattr(response, "stop_reason", "") or "").lower()
    return stop_reason in {"max_tokens", "length", "model_context_window_exceeded"}


def parse_json_object(text: str, *, purpose: str = "structured output") -> dict[str, Any]:
    """Parse a JSON object while tolerating markdown fences and leading prose."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    candidates = (cleaned, cleaned[start : end + 1] if start >= 0 and end >= start else "")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"Model did not return parseable {purpose} JSON")


def parse_json_response(response: Any, *, purpose: str = "structured output") -> dict[str, Any]:
    require_complete_response(response, purpose=purpose)
    return parse_json_object(response_text(response), purpose=purpose)
