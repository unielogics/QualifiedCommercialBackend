from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers.dealer_ai_intake import (
    _require_super_admin,
    admin_router,
    client_router,
    funding_router,
    mca_router,
    router,
)
from app.services import bucket_ai, operator_file_links


def _route_contract(router_) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in router_.routes
        for method in getattr(route, "methods", set())
    }


def test_all_client_and_admin_intake_conversation_routes_remain_registered() -> None:
    assert ("/public/dealer-ai-intake/{token}/chat", "POST") in _route_contract(router)
    assert ("/public/funding-review/{token}/chat", "POST") in _route_contract(funding_router)
    assert ("/public/mca-refinance/{token}/chat", "POST") in _route_contract(mca_router)
    assert ("/buckets/client/intakes/{intake_id}/chat", "POST") in _route_contract(client_router)
    assert ("/admin/ai-underwriter-leads/{intake_id}/chat", "POST") in _route_contract(admin_router)
    assert ("/admin/ai-underwriter-leads/{intake_id}/client-thread", "GET") in _route_contract(admin_router)


def test_client_transcript_access_stays_super_admin_only() -> None:
    _require_super_admin(SimpleNamespace(role=Role.SUPER_ADMIN))
    with pytest.raises(HTTPException) as exc:
        _require_super_admin(SimpleNamespace(role=Role.CLIENT))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_private_underwriter_context_includes_selected_linked_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = SimpleNamespace(
        id=uuid4(),
        file_name="primary-bank-statement.pdf",
        content_type="application/pdf",
        size_bytes=100,
        requested_document_id=None,
        uploaded_by_name="Client",
        status="uploaded",
        deleted_at=None,
    )
    linked = SimpleNamespace(
        id=uuid4(),
        file_name="linked-tax-return.pdf",
        content_type="application/pdf",
        size_bytes=200,
        requested_document_id=None,
        uploaded_by_name="Operator",
        status="uploaded",
        deleted_at=None,
    )
    bucket = SimpleNamespace(
        id=uuid4(),
        name="Primary intake room",
        client_name="Fixture Client",
        purpose="Underwriting",
        bucket_type="underwriting",
        ai_context={},
        files=[primary],
        requested_documents=[],
        notes=[],
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )
    )
    monkeypatch.setattr(bucket_ai, "latest_review", AsyncMock(return_value=None))
    monkeypatch.setattr(bucket_ai, "visible_action_items", AsyncMock(return_value=[]))
    selected = AsyncMock(return_value=[linked])
    monkeypatch.setattr(operator_file_links, "selected_files_for_intake", selected)
    intake_id = uuid4()

    context = await bucket_ai._chat_context(
        db,
        bucket=bucket,
        audience="admin",
        upload_link=None,
        share=None,
        vendor_access=None,
        intake_id=intake_id,
    )

    assert {item["id"] for item in context["files"]} == {str(primary.id), str(linked.id)}
    assert [item["id"] for item in context["linked_evidence_files"]] == [str(linked.id)]
    selected.assert_awaited_once_with(db, intake_id)
