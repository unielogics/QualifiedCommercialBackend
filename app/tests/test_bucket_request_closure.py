"""A requested document can be closed, and archiving is what closes it.

Requests only ever knew `requested` and `uploaded`, so 142 asks sat open on
archived buckets in production. These tests pin the closure semantics, that
every "what is outstanding" view ignores a closed ask, and that the archive
and restore handlers actually call the service rather than reinventing it.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.routers import buckets as buckets_router
from app.schemas.bucket import BucketRequestedDocumentRead
from app.services import bucket_requests


def _doc(status: str = "requested", closed_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, closed_at=None, closed_reason=closed_reason)


def test_only_an_open_ask_is_outstanding() -> None:
    assert bucket_requests.is_open_request(_doc("requested")) is True
    assert bucket_requests.is_open_request(_doc("uploaded")) is False
    assert bucket_requests.is_open_request(_doc("closed")) is False
    assert bucket_requests.is_open_request(_doc("not_applicable")) is False


def test_archiving_closes_open_asks_and_leaves_uploaded_ones_alone() -> None:
    at = datetime(2026, 10, 30, 12, tzinfo=UTC)
    docs = [_doc("requested"), _doc("uploaded"), _doc("requested")]
    closed = bucket_requests.close_open_requests(docs, reason=bucket_requests.ARCHIVE_REASON, at=at)
    assert closed == 2
    assert [doc.status for doc in docs] == ["closed", "uploaded", "closed"]
    assert docs[0].closed_at == at and docs[0].closed_reason == "bucket_archived"
    assert docs[1].closed_at is None


def test_restoring_reopens_only_what_the_archive_closed() -> None:
    docs = [
        _doc("closed", "bucket_archived"),
        _doc("closed", "client_withdrew"),
        _doc("uploaded"),
    ]
    reopened = bucket_requests.reopen_requests_closed_for(docs, reason=bucket_requests.ARCHIVE_REASON)
    assert reopened == 1
    assert docs[0].status == "requested" and docs[0].closed_reason is None
    assert docs[1].status == "closed" and docs[1].closed_reason == "client_withdrew"
    assert docs[2].status == "uploaded"


def test_an_ask_names_where_it_came_from() -> None:
    assert bucket_requests.source_kind({"kind": "program_readiness", "policy_keys": ["business_baseline"]}) == "rule"
    assert bucket_requests.source_kind({"kind": "system_supporting", "client_visible": True}) == "system"
    assert bucket_requests.source_kind(None) == "manual"
    assert bucket_requests.source_kind({}) == "manual"


def test_the_read_schema_exposes_the_source_kind_and_closure() -> None:
    row = BucketRequestedDocumentRead.model_validate(
        SimpleNamespace(
            id=uuid4(), bucket_id=uuid4(), template_id=None, name="Bank statements",
            category=None, description=None, required=True, allow_multiple_files=True,
            status="closed", is_custom=False, requires_signature=False, signature_kind=None,
            template_file_id=None, signature_document_text=None, requirement_key=None,
            requirement_source={"kind": "program_readiness"}, request_kind=None,
            source_file_id=None, replacement_review_state=None, template_download_url=None,
            closed_at=datetime(2026, 10, 30, tzinfo=UTC), closed_reason="bucket_archived",
        )
    )
    dumped = row.model_dump()
    assert dumped["source_kind"] == "rule"
    assert dumped["closed_reason"] == "bucket_archived"


def _calls_in(func: object) -> set[str]:
    tree = ast.parse(inspect.getsource(func))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def test_the_archive_and_restore_handlers_use_the_service() -> None:
    # The handlers are the only place archive semantics live; if someone
    # rewrites them without the service, open asks go back to never closing.
    assert "close_open_requests" in _calls_in(buckets_router.delete_bucket)
    assert "reopen_requests_closed_for" in _calls_in(buckets_router.restore_bucket)


def test_the_cross_bucket_action_item_route_registers_before_the_uuid_path() -> None:
    paths = [getattr(route, "path", "") for route in buckets_router.router.routes]
    static = paths.index("/buckets/admin/ai-action-items")
    dynamic = paths.index("/buckets/admin/{bucket_id}")
    assert static < dynamic, "a UUID path parameter would otherwise claim the static path"
