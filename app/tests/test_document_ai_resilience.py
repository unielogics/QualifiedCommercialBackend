from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fitz
import pytest

from app.dealer_os.services.extract import _extract_via_model, _merge_extractions
from app.services.ai.pdf_chunks import split_pdf_pages
from app.services.ai.structured_output import ModelOutputTruncated, parse_json_response


def _pdf(page_count: int) -> bytes:
    document = fitz.open()
    try:
        for page_number in range(1, page_count + 1):
            page = document.new_page()
            page.insert_text((72, 72), f"Statement page {page_number}")
        return document.tobytes()
    finally:
        document.close()


def _response(payload: dict[str, object], *, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


def test_pdf_page_chunks_are_valid_and_cover_every_page() -> None:
    chunks = split_pdf_pages(_pdf(17), pages_per_chunk=8)

    assert [(chunk.first_page, chunk.last_page) for chunk in chunks] == [
        (1, 8),
        (9, 16),
        (17, 17),
    ]
    assert [fitz.open(stream=chunk.raw, filetype="pdf").page_count for chunk in chunks] == [8, 8, 1]


def test_merge_extractions_combines_month_fields_and_negative_dates() -> None:
    merged = _merge_extractions(
        [
            {
                "doc_type": "bank_statement",
                "months": [{"month": "2026-05", "total_deposits": 12000, "negative_balance_dates": []}],
                "transactions": [{"date": "2026-05-01", "amount": 100}],
                "account": {"institution": "Chase", "mask": None},
            },
            {
                "doc_type": "bank_statement",
                "months": [
                    {"month": "2026-05", "ending_balance": 2200, "negative_balance_dates": ["2026-05-12"]},
                    {"month": "2026-06", "total_deposits": 14500},
                ],
                "account": {"mask": "1234"},
            },
        ]
    )

    assert merged["transactions"] == []
    assert merged["account"] == {"institution": "Chase", "mask": "1234"}
    assert merged["months"] == [
        {
            "month": "2026-05",
            "total_deposits": 12000,
            "negative_balance_dates": ["2026-05-12"],
            "ending_balance": 2200,
        },
        {"month": "2026-06", "total_deposits": 14500},
    ]


def test_structured_response_rejects_token_limited_json() -> None:
    response = _response({"months": []}, stop_reason="max_tokens")

    with pytest.raises(ModelOutputTruncated, match="truncated"):
        parse_json_response(response, purpose="extraction")


@pytest.mark.asyncio
async def test_pdf_extraction_retries_a_truncated_chunk_at_smaller_page_ranges() -> None:
    first = _response({"months": []}, stop_reason="max_tokens")
    may = _response(
        {
            "doc_type": "bank_statement",
            "months": [{"month": "2026-05", "total_deposits": 12000}],
            "transactions": [],
        }
    )
    june = _response(
        {
            "doc_type": "bank_statement",
            "months": [{"month": "2026-06", "total_deposits": 14000}],
            "transactions": [],
        }
    )
    call = AsyncMock(side_effect=[first, may, june])
    document = SimpleNamespace(
        id="document-id",
        dealer_id="dealer-id",
        filename="two-month-statement.pdf",
        kind="statement",
    )

    with (
        patch("app.dealer_os.services.extract.get_client", return_value=object()),
        patch("app.dealer_os.services.extract.model_heavy", return_value="model"),
        patch("app.dealer_os.services.extract.tracked_messages_create", call),
    ):
        extracted = await _extract_via_model(object(), document, _pdf(2), "application/pdf")

    assert call.await_count == 3
    assert [month["month"] for month in extracted["months"]] == ["2026-05", "2026-06"]
    assert extracted["transactions"] == []
    assert all(kwargs.kwargs["max_tokens"] == 3000 for kwargs in call.await_args_list)
