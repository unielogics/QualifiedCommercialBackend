from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.routers.operator_files import get_attention_queue
from app.schemas.operator_file import AttentionItem, AttentionQueue
from app.services import attention_queue as aq

NOW = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
LOADERS = ("_intakes_without_files", "_buckets_without_client_entry", "_overdue_requests", "_stale_action_items")


def item(number, *, kind="intake_no_files", title="Example Company", detail="Waiting for documents", count=1):
    return AttentionItem(kind=kind, severity="warn", title=title, detail=detail, age_days=20, count=count,
        bucket_id=UUID(int=1000 + number), intake_id=UUID(int=number), profile_id=UUID(int=2000 + number), href=f"/file/{number}")


def source(monkeypatch, items):
    for index, name in enumerate(LOADERS):
        monkeypatch.setattr(aq, name, AsyncMock(return_value=items if index == 0 else []))


async def test_default_limit_remains_sixty_but_every_row_beyond_two_hundred_is_reachable(monkeypatch):
    rows = [item(number) for number in range(205, 0, -1)]
    source(monkeypatch, rows)
    first = await aq.load_attention_queue(None, now=NOW)
    assert first.total == first.filtered_total == 205
    assert first.limit == 60 and first.offset == 0 and len(first.items) == 60
    assert first.counts == {"intake_no_files": 205}
    pages = [await aq.load_attention_queue(None, now=NOW, limit=50, offset=offset) for offset in range(0, 205, 50)]
    identifiers = [row.intake_id for page in pages for row in page.items]
    assert identifiers == [UUID(int=number) for number in range(1, 206)]
    assert len(set(identifiers)) == 205 and len(pages[-1].items) == 5


async def test_filters_apply_before_slice_and_global_counts_count_issues_not_document_totals(monkeypatch):
    rows = [item(1, title="Alpha   Group"), item(2, title="Alpha Group"),
            item(3, kind="request_overdue", title="Alpha Group", count=37),
            item(4, kind="action_items_stale", title="Other Company", count=19)]
    source(monkeypatch, rows)
    queue = await aq.load_attention_queue(None, now=NOW, limit=1, offset=1, kind="intake_no_files", q="  ALPHA\tGROUP  ")
    assert queue.total == 4 and queue.filtered_total == 2
    assert queue.counts == {"intake_no_files": 2, "request_overdue": 1, "action_items_stale": 1}
    # Same name means separate records here, never deduplicated or grouped.
    assert len(queue.items) == 1 and queue.items[0].intake_id == UUID(int=2)
    assert queue.offset == 1 and queue.limit == 1


async def test_search_covers_details_and_unicode_casefold_without_filtering_global_counts(monkeypatch):
    rows = [item(1, detail="Waiting for Straße LLC statement"), item(2, detail="No client entry")]
    source(monkeypatch, rows)
    queue = await aq.load_attention_queue(None, now=NOW, q="STRASSE llc")
    assert queue.total == 2 and queue.filtered_total == 1
    assert queue.items[0].intake_id == UUID(int=1)
    assert queue.counts == {"intake_no_files": 2}


@pytest.mark.parametrize("query,offset", [("not present", 0), (None, 500)])
async def test_empty_page_keeps_global_and_filtered_totals_distinct(monkeypatch, query, offset):
    source(monkeypatch, [item(1), item(2)])
    queue = await aq.load_attention_queue(None, now=NOW, q=query, offset=offset, limit=5)
    assert queue.items == [] and queue.total == 2 and queue.offset == offset
    assert queue.filtered_total == (0 if query else 2)
    assert queue.counts == {"intake_no_files": 2}


def test_equal_priority_rows_have_stable_id_order_when_database_order_changes():
    rows = [item(3), item(1), item(2)]
    # Same bucket still allows different intake/profile records and stable keys.
    for row in rows:
        row.bucket_id = UUID(int=500)
    assert aq.sort_items(rows) == aq.sort_items(list(reversed(rows)))
    assert [row.intake_id for row in aq.sort_items(rows)] == [UUID(int=1), UUID(int=2), UUID(int=3)]


def api_app(monkeypatch, role=Role.LOAN_EXEC):
    app = FastAPI()
    app.add_api_route("/operator-files/attention", get_attention_queue, response_model=AttentionQueue)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=role)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    loader = AsyncMock(return_value=AttentionQueue(generated_at=NOW, items=[], counts={}, total=0, filtered_total=0))
    monkeypatch.setattr(aq, "load_attention_queue", loader)
    return app, loader


async def test_api_default_and_filter_parameters_are_forwarded_with_new_response_metadata(monkeypatch):
    app, loader = api_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/operator-files/attention")
        assert response.status_code == 200
        assert response.json() | {"generated_at": "ignored"} == {
            "generated_at": "ignored", "items": [], "counts": {}, "total": 0, "filtered_total": 0, "limit": 60, "offset": 0}
        assert loader.await_args.kwargs == {"limit": 60, "offset": 0, "kind": None, "q": None}
        response = await client.get("/operator-files/attention", params={"limit": 5, "offset": 10, "kind": "request_overdue", "q": " Alpha "})
    assert response.status_code == 200
    assert loader.await_args.kwargs == {"limit": 5, "offset": 10, "kind": "request_overdue", "q": " Alpha "}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"kind": "unknown"}, {"q": "x" * 201}])
async def test_api_rejects_invalid_or_unbounded_pagination_parameters(monkeypatch, params):
    app, loader = api_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/operator-files/attention", params=params)
    assert response.status_code == 422
    loader.assert_not_awaited()


@pytest.mark.parametrize("role", [Role.BROKER, Role.REGIONAL_MANAGER, Role.FIELD_REP, Role.CLIENT, Role.DEALER_PARTNER])
async def test_pagination_does_not_widen_desk_queue_authorization(monkeypatch, role):
    app, loader = api_app(monkeypatch, role)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/operator-files/attention")
    assert response.status_code == 403
    loader.assert_not_awaited()
