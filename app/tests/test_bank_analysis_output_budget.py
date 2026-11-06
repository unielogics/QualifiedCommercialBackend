from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import bucket_ai


def _response(payload, *, truncated=False):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload) if isinstance(payload, dict) else payload)],
        stop_reason="max_tokens" if truncated else "end_turn",
        model="us.anthropic.claude-sonnet-4-6",
        usage=None,
    )


async def _analyze(previous, responses, *, offer=False):
    raw = b"Document text, read without any storage or model request."
    file = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), file_name="document.txt", content_type="text/plain", content_hash=None, size_bytes=len(raw))
    row = SimpleNamespace(id=uuid4(), bucket_file_id=file.id, status="completed", classification=previous.get("classification"), analysis=previous.get("analysis", {}))
    db = SimpleNamespace(flush=AsyncMock())
    with (
        patch.object(bucket_ai, "_lock_analysis_scope", AsyncMock()),
        patch.object(bucket_ai, "reconcile_uploaded_file", AsyncMock()),
        patch.object(bucket_ai, "_fetch_file", return_value=(raw, "text/plain")),
        patch.object(bucket_ai, "_get_or_create_analysis_row", AsyncMock(return_value=row)),
        patch.object(bucket_ai, "model_heavy", return_value="us.anthropic.claude-sonnet-4-6"),
        patch.object(bucket_ai, "get_client", return_value=object()),
        patch.object(bucket_ai, "tracked_messages_create", AsyncMock(side_effect=responses)) as provider,
        patch.object(bucket_ai, "_reconcile_analysis_consumers", AsyncMock()),
        patch.object(bucket_ai.merchant_processing, "is_offer_document", return_value=offer),
    ):
        result = await bucket_ai.analyze_bucket_file(db, file, force=True)
    return result, provider


@pytest.mark.asyncio
async def test_known_bank_read_has_room_for_forty_individually_cited_transfers():
    transfers = [{
        "date": f"2026-05-{index % 28 + 1:02d}", "description": f"Online transfer reference {index}",
        "amount": index + 1, "direction": "out", "currency": "USD", "page": index // 10 + 1,
        "transfer_reference": f"ref-{index}", "quote": f"Online transfer reference {index} {index + 1}.00",
    } for index in range(40)]
    payload = {"classification": "bank_statement", "confidence": "high", "summary": "May statement.", "key_facts": {"transfer_lines": transfers}}
    result, provider = await _analyze({"classification": "bank_statement"}, [_response(payload)])
    assert result.status == "completed"
    assert result.analysis["key_facts"]["transfer_lines"] == transfers
    provider.assert_awaited_once()
    assert provider.await_args.kwargs["max_tokens"] == 12000
    assert bucket_ai.CURRENT_FILE_ANALYSIS_VERSION == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(("previous", "partial", "offer", "expected"), [
    ({"classification": "bank_statement"}, '{"classification":"bank_statement",', False, [12000, 24000]),
    ({"analysis": {"key_facts": {"months": [{"transfer_lines": []}]}}}, '{"summary":', False, [12000, 24000]),
    ({}, '{"classification":"bank_statement","key_facts":', False, [3000, 24000]),
    ({}, '{"key_facts":{"transfer_lines":[', False, [3000, 24000]),
    ({"classification": "current_p_and_l"}, '{"classification":"current_p_and_l",', False, [3000, 6000]),
    ({"classification": "bank_statement"}, '{"classification":"bank_statement",', True, [3000, 6000]),
])
async def test_only_bank_or_movement_reads_receive_the_larger_bounded_retry(previous, partial, offer, expected):
    final = {"classification": "bank_statement" if not offer else "other", "summary": "Complete read.", "key_facts": {}}
    result, provider = await _analyze(previous, [_response(partial, truncated=True), _response(final)], offer=offer)
    assert result.status == "completed"
    assert [call.kwargs["max_tokens"] for call in provider.await_args_list] == expected
    assert provider.await_args_list[1].kwargs["metadata"]["retry_reason"] == "truncated_output"


@pytest.mark.asyncio
async def test_truncation_at_the_larger_bank_limit_still_fails_without_a_third_model_call():
    partial = _response({"classification": "bank_statement", "key_facts": {"transfer_lines": []}}, truncated=True)
    result, provider = await _analyze({"classification": "bank_statement"}, [partial, partial])
    assert result.status == "failed"
    assert "truncated" in result.error
    assert [call.kwargs["max_tokens"] for call in provider.await_args_list] == [12000, 24000]
    # Even parseable JSON carrying a token-limit stop is incomplete evidence.
    assert result.analysis == {}
