from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationRoomDelivery
from app.models.bucket import BucketRequestedDocument
from app.routers.application_profiles import (
    _require_usable_unlocked_copy_room,
    request_unlocked_application_evidence,
)
from app.routers.buckets import (
    _public_requested_document_read,
    _request_uploaded_file_reads,
    admin_upload_complete,
    admin_upload_init,
    request_link_access,
    request_link_status,
    request_upload_complete,
    request_upload_init,
)
from app.routers.dealer_ai_intake import (
    DealerFileUploadInit,
    DealerUploadComplete,
)
from app.routers.dealer_ai_intake import (
    _complete_upload as _complete_intake_upload,
)
from app.routers.dealer_ai_intake import (
    _requested_document_read as _dealer_requested_document_read,
)
from app.routers.dealer_ai_intake import (
    _start_upload as _start_intake_upload,
)
from app.schemas.application_profile import UnlockedCopyRequestCreate
from app.schemas.bucket import (
    BucketFileUploadInit,
    BucketRequestAccessRequest,
    BucketUploadComplete,
)
from app.services import locked_file_requests


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return [] if self.value is None else [self.value]


class _Rows:
    def __init__(self, values: list[object]):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


def _stamp_added(added: list[object]) -> None:
    timestamp = datetime.now(UTC)
    for row in added:
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        if getattr(row, "created_at", None) is None:
            row.created_at = timestamp


@pytest.mark.asyncio
async def test_request_without_email_still_creates_client_room_todo() -> None:
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    file = SimpleNamespace(
        id=uuid4(),
        file_name="Locked statement.pdf",
        content_hash="a" * 64,
    )
    analysis = SimpleNamespace(content_hash="a" * 64)
    link = SimpleNamespace(token="room-token")
    user = SimpleNamespace(id=uuid4())
    added: list[object] = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(), _Result(), _Result()]),
        add=added.append,
        flush=AsyncMock(side_effect=lambda: _stamp_added(added)),
    )

    with patch.object(locked_file_requests, "send_as_user", AsyncMock()) as send:
        outcome = await locked_file_requests.request_unlocked_copy(
            db,
            profile=profile,
            file=file,
            analysis=analysis,
            link=link,
            recipient=None,
            user=user,
            send_email=False,
        )

    send.assert_not_awaited()
    assert outcome.created_request is True
    assert outcome.deduplicated is False
    assert outcome.room_url.endswith(f"?tab=todo&request={outcome.requested_document.id}")
    requested = next(row for row in added if isinstance(row, BucketRequestedDocument))
    delivery = next(row for row in added if isinstance(row, ApplicationRoomDelivery))
    assert requested.status == "requested"
    assert requested.allow_multiple_files is True
    assert requested.requirement_source["source_file_id"] == str(file.id)
    assert "without a password" in requested.description
    assert delivery.channel == "none"
    assert delivery.status == "created"
    assert delivery.provider_result["source_file_id"] == str(file.id)


@pytest.mark.asyncio
async def test_repeat_request_reuses_existing_delivery_without_resending() -> None:
    requested = SimpleNamespace(id=uuid4(), status="requested")
    existing = SimpleNamespace(status="sent", attempt_number=1)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(requested), _Result(existing)]),
        add=Mock(),
        flush=AsyncMock(),
    )

    before_email = AsyncMock()
    with patch.object(locked_file_requests, "send_as_user", AsyncMock()) as send:
        outcome = await locked_file_requests.request_unlocked_copy(
            db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4()),
            file=SimpleNamespace(id=uuid4(), file_name="locked.pdf", content_hash="b" * 64),
            analysis=SimpleNamespace(content_hash="b" * 64),
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            before_email_delivery=before_email,
        )

    assert outcome.delivery is existing
    assert outcome.deduplicated is True
    send.assert_not_awaited()
    before_email.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_failed_delivery_can_be_retried_without_duplicate_todo() -> None:
    requested = SimpleNamespace(id=uuid4(), status="requested")
    replacement_trigger_id = uuid4()
    failed = SimpleNamespace(
        id=uuid4(),
        idempotency_key="first-key",
        status="failed",
        attempt_number=1,
        provider_result={
            "replacement_trigger_file_id": str(replacement_trigger_id),
            "replacement_trigger_content_hash": "bad-replacement-hash",
        },
    )
    added: list[object] = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(requested),
                _Result(failed),
                _Rows([]),
                _Result(),
            ]
        ),
        add=added.append,
        flush=AsyncMock(side_effect=lambda: _stamp_added(added)),
        commit=AsyncMock(),
    )
    send_result = SimpleNamespace(ok=True, message_id="message-2", detail="sent")

    with patch.object(
        locked_file_requests, "send_as_user", AsyncMock(return_value=send_result)
    ) as send:
        outcome = await locked_file_requests.request_unlocked_copy(
            db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4()),
            file=SimpleNamespace(id=uuid4(), file_name="locked.pdf", content_hash="c" * 64),
            analysis=SimpleNamespace(content_hash="c" * 64),
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
        )

    send.assert_awaited_once()
    assert outcome.requested_document is requested
    assert outcome.created_request is False
    assert outcome.deduplicated is False
    assert outcome.delivery.status == "sent"
    assert outcome.delivery.attempt_number == 2
    assert outcome.delivery.provider_result["replacement_trigger_file_id"] == str(
        replacement_trigger_id
    )
    assert (
        outcome.delivery.provider_result["replacement_trigger_content_hash"]
        == "bad-replacement-hash"
    )
    assert outcome.delivery.created_at is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_state", ["checking", "received"])
async def test_failed_email_is_not_retried_after_replacement_progress(
    review_state: str,
) -> None:
    requested = SimpleNamespace(id=uuid4(), status="uploaded")
    failed = SimpleNamespace(
        id=uuid4(),
        idempotency_key="failed-key",
        status="failed",
        attempt_number=1,
        provider_result={},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(requested), _Result(failed)]),
        add=Mock(),
        flush=AsyncMock(),
    )

    before_email = AsyncMock()
    with (
        patch.object(
            locked_file_requests,
            "_replacement_attempt_state",
            AsyncMock(return_value=locked_file_requests._ReplacementAttemptState(review_state)),
        ),
        patch.object(locked_file_requests, "send_as_user", AsyncMock()) as send,
    ):
        outcome = await locked_file_requests.request_unlocked_copy(
            db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4()),
            file=SimpleNamespace(id=uuid4(), file_name="locked.pdf", content_hash="hash"),
            analysis=SimpleNamespace(content_hash="hash"),
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
            before_email_delivery=before_email,
        )

    assert outcome.deduplicated is True
    assert outcome.delivery is failed
    assert outcome.replacement_review_state == review_state
    send.assert_not_awaited()
    before_email.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_locked_replacement_reuses_parent_task_and_one_retry_key() -> None:
    original_id = uuid4()
    requested = BucketRequestedDocument(
        id=uuid4(),
        bucket_id=uuid4(),
        name="Unlocked copy of original.pdf",
        category="Replacement Documents",
        required=True,
        allow_multiple_files=True,
        status="uploaded",
        is_custom=True,
        requirement_key=f"unlocked_copy:{original_id.hex}:sourcehash",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(original_id),
            "source_content_hash": "sourcehash",
            "source_file_name": "original.pdf",
        },
    )
    prior = SimpleNamespace(
        id=uuid4(),
        idempotency_key="first-key",
        status="sent",
        attempt_number=1,
    )
    replacement = SimpleNamespace(
        id=uuid4(),
        requested_document_id=requested.id,
        file_name="still-locked.pdf",
        content_hash="replacement-hash",
        extraction_reason=None,
        created_at=datetime.now(UTC),
    )
    analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=replacement.id,
        content_hash="replacement-hash",
        analysis_version=3,
        created_at=datetime.now(UTC),
        status="skipped",
        classification="unreadable",
        skip_reason="password_protected",
    )
    source = SimpleNamespace(
        id=original_id,
        content_hash="sourcehash",
        extraction_reason=None,
    )
    source_analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=original_id,
        content_hash="sourcehash",
        analysis_version=3,
        created_at=datetime.now(UTC),
        status="skipped",
        skip_reason="password_protected",
    )
    added: list[object] = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(requested),
                _Result(source),
                _Result(source_analysis),
                _Rows([requested]),
                _Result(prior),
                _Rows([replacement]),
                _Rows([analysis]),
                _Result(),
            ]
        ),
        add=added.append,
        flush=AsyncMock(side_effect=lambda: _stamp_added(added)),
        commit=AsyncMock(),
    )
    send_result = SimpleNamespace(ok=True, message_id="message-2", detail="sent")

    with patch.object(
        locked_file_requests,
        "send_as_user",
        AsyncMock(return_value=send_result),
    ) as send:
        outcome = await locked_file_requests.request_unlocked_copy(
            db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=requested.bucket_id),
            file=replacement,
            analysis=analysis,
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
        )

    send.assert_awaited_once()
    assert outcome.requested_document is requested
    assert outcome.source_file_id == original_id
    assert outcome.created_request is False
    assert not any(isinstance(row, BucketRequestedDocument) for row in added)
    delivery = next(row for row in added if isinstance(row, ApplicationRoomDelivery))
    assert delivery.provider_result["source_file_id"] == str(original_id)
    assert delivery.idempotency_key == locked_file_requests._delivery_key(
        "replacement_retry",
        requested.id,
        replacement.id,
        replacement.content_hash,
    )


@pytest.mark.asyncio
async def test_source_request_resends_once_for_latest_bad_replacement() -> None:
    timestamp = datetime.now(UTC)
    source_id = uuid4()
    bucket_id = uuid4()
    requested = BucketRequestedDocument(
        id=uuid4(),
        bucket_id=bucket_id,
        name="Unlocked copy of original.pdf",
        category="Replacement Documents",
        required=True,
        allow_multiple_files=True,
        status="uploaded",
        is_custom=True,
        requirement_key=f"unlocked_copy:{source_id.hex}:sourcehash",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "sourcehash",
            "source_file_name": "original.pdf",
        },
    )
    source = SimpleNamespace(
        id=source_id,
        requested_document_id=None,
        parent_zip_file_id=None,
        file_name="original.pdf",
        content_hash="sourcehash",
    )
    source_analysis = SimpleNamespace(content_hash="sourcehash")
    replacement = SimpleNamespace(
        id=uuid4(),
        requested_document_id=requested.id,
        parent_zip_file_id=None,
        file_name="bad-copy.pdf",
        content_hash="bad-hash",
        extraction_reason=None,
        created_at=timestamp,
    )
    replacement_analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=replacement.id,
        content_hash="bad-hash",
        analysis_version=3,
        created_at=timestamp,
        status="completed",
        classification="unreadable",
        skip_reason=None,
    )
    original_delivery = SimpleNamespace(
        id=uuid4(),
        idempotency_key="original-delivery",
        status="sent",
        attempt_number=1,
        provider_result={"accepted": True},
    )
    added: list[object] = []
    first_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(requested),
                _Result(original_delivery),
                _Rows([replacement]),
                _Rows([replacement_analysis]),
                _Result(),
            ]
        ),
        add=added.append,
        flush=AsyncMock(side_effect=lambda: _stamp_added(added)),
        commit=AsyncMock(),
    )
    send_result = SimpleNamespace(ok=True, message_id="reminder", detail="sent")

    with patch.object(
        locked_file_requests,
        "send_as_user",
        AsyncMock(return_value=send_result),
    ) as send:
        first = await locked_file_requests.request_unlocked_copy(
            first_db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=bucket_id),
            file=source,
            analysis=source_analysis,
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
        )

    send.assert_awaited_once()
    assert first.deduplicated is False
    assert first.delivery.provider_result["replacement_trigger_file_id"] == str(replacement.id)

    second_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(requested),
                _Result(first.delivery),
                _Rows([replacement]),
                _Rows([replacement_analysis]),
            ]
        ),
        add=Mock(),
        flush=AsyncMock(),
    )
    with patch.object(locked_file_requests, "send_as_user", AsyncMock()) as resend:
        second = await locked_file_requests.request_unlocked_copy(
            second_db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=bucket_id),
            file=source,
            analysis=source_analysis,
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
        )

    assert second.deduplicated is True
    assert second.delivery is first.delivery
    resend.assert_not_awaited()
    second_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_task_aliases_share_retry_attempts_and_delivery_history() -> None:
    source_id = uuid4()
    bucket_id = uuid4()
    source_hash = "source-hash"
    requirement_key = f"unlocked_copy:{source_id.hex}:{source_hash}"
    canonical = BucketRequestedDocument(
        id=uuid4(),
        bucket_id=bucket_id,
        name="Unlocked copy of original.pdf",
        category="Replacement Documents",
        required=True,
        allow_multiple_files=True,
        status="uploaded",
        is_custom=True,
        requirement_key=requirement_key,
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": source_hash,
            "source_file_name": "original.pdf",
        },
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    duplicate = BucketRequestedDocument(
        id=uuid4(),
        bucket_id=bucket_id,
        name=canonical.name,
        category=canonical.category,
        required=True,
        allow_multiple_files=True,
        status="uploaded",
        is_custom=True,
        requirement_key=requirement_key,
        requirement_source=dict(canonical.requirement_source),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    source = SimpleNamespace(
        id=source_id,
        requested_document_id=None,
        parent_zip_file_id=None,
        file_name="original.pdf",
        content_hash=source_hash,
    )
    source_analysis = SimpleNamespace(content_hash=source_hash)
    replacement = SimpleNamespace(
        id=uuid4(),
        requested_document_id=duplicate.id,
        parent_zip_file_id=None,
        file_name="still-locked.pdf",
        content_hash="replacement-hash",
        extraction_reason=None,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    replacement_analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=replacement.id,
        content_hash=replacement.content_hash,
        analysis_version=1,
        created_at=replacement.created_at,
        status="skipped",
        classification="unreadable",
        skip_reason="password_protected",
    )
    original_delivery = SimpleNamespace(
        id=uuid4(),
        requested_document_id=canonical.id,
        idempotency_key="original",
        status="sent",
        attempt_number=1,
        provider_result={"accepted": True},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    added: list[object] = []
    first_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([canonical, duplicate]),
                _Rows([original_delivery]),
                _Result(original_delivery),
                _Rows([replacement]),
                _Rows([replacement_analysis]),
                _Result(),
            ]
        ),
        add=added.append,
        flush=AsyncMock(side_effect=lambda: _stamp_added(added)),
        commit=AsyncMock(),
    )
    send_result = SimpleNamespace(ok=True, message_id="reminder", detail="sent")

    with patch.object(
        locked_file_requests,
        "send_as_user",
        AsyncMock(return_value=send_result),
    ) as send:
        first = await locked_file_requests.request_unlocked_copy(
            first_db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=bucket_id),
            file=source,
            analysis=source_analysis,
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
        )

    send.assert_awaited_once()
    assert first.requested_document is canonical
    assert first.delivery.requested_document_id == canonical.id
    assert first.delivery.provider_result["replacement_trigger_file_id"] == str(
        replacement.id
    )
    assert (
        first.delivery.provider_result["replacement_trigger_content_hash"]
        == replacement.content_hash
    )

    alias_reminder = SimpleNamespace(
        id=uuid4(),
        requested_document_id=duplicate.id,
        idempotency_key="legacy-alias-reminder",
        status="sent",
        attempt_number=2,
        provider_result={
            "replacement_trigger_file_id": str(replacement.id),
            "replacement_trigger_content_hash": replacement.content_hash,
        },
        created_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    second_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([canonical, duplicate]),
                _Rows([original_delivery, alias_reminder]),
                _Result(alias_reminder),
                _Rows([replacement]),
                _Rows([replacement_analysis]),
            ]
        ),
        add=Mock(),
        flush=AsyncMock(),
    )
    with patch.object(locked_file_requests, "send_as_user", AsyncMock()) as resend:
        second = await locked_file_requests.request_unlocked_copy(
            second_db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=bucket_id),
            file=source,
            analysis=source_analysis,
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            retry_failed=True,
        )

    assert second.deduplicated is True
    assert second.requested_document is canonical
    assert second.delivery is alias_reminder
    resend.assert_not_awaited()
    second_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_zip_child_resolves_parent_task_even_if_transport_was_soft_deleted() -> None:
    bucket_id = uuid4()
    source_id = uuid4()
    document = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        status="requested",
        created_at=datetime.now(UTC),
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "source-hash",
        },
    )
    child = SimpleNamespace(
        id=uuid4(),
        requested_document_id=None,
        parent_zip_file_id=uuid4(),
        content_hash="child-hash",
    )
    source = SimpleNamespace(
        id=source_id,
        content_hash="source-hash",
        extraction_reason=None,
    )
    source_analysis = SimpleNamespace(
        bucket_file_id=source_id,
        content_hash="source-hash",
        status="skipped",
        skip_reason="password_protected",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(document),
                _Result(source),
                _Result(source_analysis),
                _Rows([document]),
            ]
        )
    )

    resolved = await locked_file_requests._existing_request_document(
        db,
        bucket_id=bucket_id,
        file=child,
        analysis=SimpleNamespace(content_hash="child-hash"),
    )

    assert resolved is document
    assert db.execute.await_count == 4
    # ZIP children remain independent evidence rows after a transport archive
    # is soft-deleted, so lineage resolution intentionally does not require
    # the parent transport row itself to remain visible.
    statement = db.execute.await_args_list[0].args[0]
    assert "bucket_files.deleted_at IS NULL" not in str(statement)
    assert "FOR UPDATE" not in str(statement)


@pytest.mark.asyncio
async def test_stale_replacement_resolves_current_source_task_in_source_first_lock_order() -> None:
    bucket_id, source_id = uuid4(), uuid4()
    old_document = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="requested",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "h1",
        },
    )
    current_document = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="requested",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "h2",
        },
    )
    replacement = SimpleNamespace(
        id=uuid4(),
        requested_document_id=old_document.id,
        parent_zip_file_id=None,
        content_hash="replacement-hash",
    )
    current_source = SimpleNamespace(
        id=source_id,
        content_hash="h2",
        extraction_reason=None,
    )
    current_analysis = SimpleNamespace(
        bucket_file_id=source_id,
        content_hash="h2",
        status="skipped",
        skip_reason="password_protected",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(old_document),
                _Result(current_source),
                _Result(current_analysis),
                _Rows([current_document]),
            ]
        )
    )

    resolved = await locked_file_requests._existing_request_document(
        db,
        bucket_id=bucket_id,
        file=replacement,
        analysis=SimpleNamespace(content_hash="replacement-hash"),
        for_update=True,
    )

    assert resolved is current_document
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert "FOR UPDATE" not in statements[0]
    assert "FOR UPDATE" in statements[1]
    assert "FOR UPDATE" in statements[3]


@pytest.mark.asyncio
async def test_delivery_marker_is_durable_before_external_email_send() -> None:
    requested = SimpleNamespace(id=uuid4(), status="requested")
    added: list[object] = []
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    async def send(*_args, **_kwargs):
        order.append("send")
        raise RuntimeError("worker stopped after provider boundary")

    async def authorize_email() -> None:
        order.append("guard")

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(requested), _Result(), _Result()]),
        add=added.append,
        flush=AsyncMock(side_effect=lambda: _stamp_added(added)),
        commit=AsyncMock(side_effect=commit),
    )

    with (
        patch.object(locked_file_requests, "send_as_user", AsyncMock(side_effect=send)),
        pytest.raises(RuntimeError),
    ):
        await locked_file_requests.request_unlocked_copy(
            db,
            profile=SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4()),
            file=SimpleNamespace(id=uuid4(), file_name="locked.pdf", content_hash="e" * 64),
            analysis=SimpleNamespace(content_hash="e" * 64),
            link=SimpleNamespace(token="room-token"),
            recipient="client@example.com",
            user=SimpleNamespace(id=uuid4()),
            before_email_delivery=authorize_email,
        )

    delivery = next(row for row in added if isinstance(row, ApplicationRoomDelivery))
    assert order == ["guard", "commit", "send"]
    assert delivery.status == "sending"
    assert delivery.channel == "email"
    assert delivery.recipient_email == "client@example.com"
    assert delivery.idempotency_key


def test_password_protection_ignores_stale_analysis_and_includes_encrypted_zip() -> None:
    stale = SimpleNamespace(
        status="skipped",
        skip_reason="password_protected",
        content_hash="old-hash",
    )
    replaced = SimpleNamespace(content_hash="new-hash", extraction_reason=None)
    encrypted_zip = SimpleNamespace(
        content_hash="zip-hash",
        extraction_reason='[{"entry":"locked.pdf","reason":"zip_entry_encrypted"}]',
    )

    assert locked_file_requests.is_password_protected_file(replaced, stale) is False
    assert locked_file_requests.is_password_protected_file(encrypted_zip, None) is True


@pytest.mark.asyncio
async def test_bad_replacement_inherits_parent_request_and_review_state() -> None:
    timestamp = datetime.now(UTC)
    source_id, replacement_id, requested_document_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    document = SimpleNamespace(
        id=requested_document_id,
        created_at=timestamp,
        status="uploaded",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "source-hash",
        },
    )
    replacement = SimpleNamespace(
        id=replacement_id,
        requested_document_id=requested_document_id,
        parent_zip_file_id=None,
        content_hash="replacement-hash",
        extraction_reason=None,
        created_at=timestamp,
    )
    delivery = SimpleNamespace(
        id=uuid4(),
        requested_document_id=requested_document_id,
        status="sent",
        created_at=timestamp,
    )
    analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=replacement_id,
        content_hash="replacement-hash",
        analysis_version=3,
        created_at=timestamp,
        status="skipped",
        classification="unreadable",
        skip_reason="password_protected",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([document]),
                _Rows([replacement]),
                _Rows([delivery]),
                _Rows([analysis]),
            ]
        )
    )

    states = await locked_file_requests.request_states_for_bucket(
        db,
        bucket_id=uuid4(),
        file_ids={source_id, replacement_id},
    )

    assert states[source_id] is states[replacement_id]
    assert states[replacement_id].source_file_id == source_id
    assert states[replacement_id].replacement_review_state == "needs_another_copy"
    # The parent's source hash must not invalidate an attached replacement.
    assert (
        locked_file_requests.current_request_state(replacement, analysis, states[replacement_id])
        is states[replacement_id]
    )


@pytest.mark.asyncio
async def test_staff_state_prefers_current_request_after_source_hash_change() -> None:
    source_id = uuid4()
    old_document = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="requested",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "h1",
        },
    )
    current_document = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="requested",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "h2",
        },
    )
    # The query contract is newest first. A set-based rebuild used to scramble
    # this order and could leave A with stale D1, which hash filtering then hid.
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([current_document, old_document]),
                _Rows([]),
                _Rows([]),
            ]
        )
    )

    states = await locked_file_requests.request_states_for_bucket(
        db,
        bucket_id=uuid4(),
        file_ids={source_id},
        file_fingerprints={source_id: "h2"},
    )

    assert states[source_id].requested_document_id == current_document.id
    assert states[source_id].source_content_hash == "h2"

    rollback_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([current_document, old_document]),
                _Rows([]),
                _Rows([]),
            ]
        )
    )
    rollback_states = await locked_file_requests.request_states_for_bucket(
        rollback_db,
        bucket_id=uuid4(),
        file_ids={source_id},
        file_fingerprints={source_id: "h1"},
    )
    assert rollback_states[source_id].requested_document_id == old_document.id
    assert rollback_states[source_id].source_content_hash == "h1"


@pytest.mark.asyncio
async def test_exact_duplicate_uses_delivered_canonical_task_everywhere() -> None:
    source_id = uuid4()
    bucket_id = uuid4()
    old_document = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="requested",
        requirement_key=f"unlocked_copy:{source_id.hex}:samehash",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "samehash",
        },
    )
    duplicate_document = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="requested",
        requirement_key=old_document.requirement_key,
        requirement_source=dict(old_document.requirement_source),
    )
    delivery = SimpleNamespace(
        id=uuid4(),
        requested_document_id=old_document.id,
        status="sent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer_duplicate_delivery = SimpleNamespace(
        id=uuid4(),
        requested_document_id=duplicate_document.id,
        status="failed",
        created_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    replacement = SimpleNamespace(
        id=uuid4(),
        requested_document_id=duplicate_document.id,
        parent_zip_file_id=None,
        content_hash="replacement-hash",
        extraction_reason=None,
        deleted_at=None,
    )
    replacement_analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=replacement.id,
        content_hash="replacement-hash",
        analysis_version=1,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
        status="completed",
        classification="bank_statement",
        skip_reason=None,
    )

    staff_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([duplicate_document, old_document]),
                _Rows([replacement]),
                _Rows([newer_duplicate_delivery, delivery]),
                _Rows([replacement_analysis]),
            ]
        )
    )
    states = await locked_file_requests.request_states_for_bucket(
        staff_db,
        bucket_id=bucket_id,
        file_ids={source_id, replacement.id},
    )
    assert states[source_id].requested_document_id == old_document.id
    assert states[source_id].delivery_id == newer_duplicate_delivery.id
    assert states[source_id].delivery_status == "failed"
    assert states[replacement.id].requested_document_id == old_document.id
    assert states[replacement.id].replacement_review_state == "received"

    source = SimpleNamespace(
        id=source_id,
        content_hash="samehash",
        extraction_reason=None,
    )
    analysis = SimpleNamespace(
        bucket_file_id=source_id,
        content_hash="samehash",
        status="skipped",
        skip_reason="password_protected",
    )
    public_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([source]),
                _Rows([analysis]),
                _Rows([newer_duplicate_delivery, delivery]),
            ]
        )
    )
    visible = await locked_file_requests.current_public_request_documents(
        public_db,
        [duplicate_document, old_document],
    )
    assert visible == [old_document]

    replacement_read = SimpleNamespace(
        id=replacement.id,
        requested_document_id=duplicate_document.id,
        parent_zip_file_id=None,
        analysis_review_state="received",
        unlocked_copy_request=SimpleNamespace(
            requested_document_id=states[replacement.id].requested_document_id
        ),
    )
    assert locked_file_requests.public_request_metadata(
        old_document, [replacement_read]
    )["replacement_review_state"] == "received"

    service_db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([old_document, duplicate_document]),
                _Rows([newer_duplicate_delivery, delivery]),
            ]
        )
    )
    canonical = await locked_file_requests._canonical_request_document(
        service_db,
        bucket_id=bucket_id,
        requirement_key=old_document.requirement_key,
        for_update=True,
    )
    assert canonical is old_document


@pytest.mark.asyncio
async def test_not_applicable_duplicate_cannot_hide_active_task() -> None:
    source_id = uuid4()
    bucket_id = uuid4()
    inactive = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="not_applicable",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "samehash",
        },
    )
    active = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="requested",
        requirement_source=dict(inactive.requirement_source),
    )
    source = SimpleNamespace(
        id=source_id,
        bucket_id=bucket_id,
        content_hash="samehash",
        extraction_reason=None,
    )
    analysis = SimpleNamespace(
        bucket_file_id=source_id,
        content_hash="samehash",
        status="skipped",
        skip_reason="password_protected",
    )
    stale_delivery = SimpleNamespace(requested_document_id=inactive.id)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([source]),
                _Rows([analysis]),
                _Rows([stale_delivery]),
            ]
        )
    )

    visible = await locked_file_requests.current_public_request_documents(
        db, [inactive, active]
    )

    assert visible == [active]


@pytest.mark.asyncio
async def test_surviving_zip_child_keeps_state_after_parent_transport_delete() -> None:
    timestamp = datetime.now(UTC)
    source_id, zip_id, child_id, requested_document_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    document = SimpleNamespace(
        id=requested_document_id,
        created_at=timestamp,
        status="uploaded",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "source-hash",
        },
    )
    deleted_parent = SimpleNamespace(
        id=zip_id,
        requested_document_id=requested_document_id,
        parent_zip_file_id=None,
        content_hash="zip-hash",
        extraction_reason=None,
        deleted_at=timestamp,
    )
    child = SimpleNamespace(
        id=child_id,
        requested_document_id=None,
        parent_zip_file_id=zip_id,
        content_hash="child-hash",
        extraction_reason=None,
        deleted_at=None,
    )
    delivery = SimpleNamespace(
        id=uuid4(),
        requested_document_id=requested_document_id,
        status="sent",
        created_at=timestamp,
    )
    analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=child_id,
        content_hash="child-hash",
        analysis_version=3,
        created_at=timestamp,
        status="completed",
        classification="bank_statement",
        skip_reason=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([document]),
                _Rows([deleted_parent, child]),
                _Rows([delivery]),
                _Rows([analysis]),
            ]
        )
    )

    states = await locked_file_requests.request_states_for_bucket(
        db,
        bucket_id=uuid4(),
        file_ids={child_id},
    )

    assert states[child_id].requested_document_id == requested_document_id
    assert states[child_id].replacement_review_state == "received"


@pytest.mark.asyncio
async def test_deleted_direct_replacement_does_not_satisfy_request() -> None:
    timestamp = datetime.now(UTC)
    source_id, replacement_id, requested_document_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    document = SimpleNamespace(
        id=requested_document_id,
        created_at=timestamp,
        status="uploaded",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_id),
            "source_content_hash": "source-hash",
        },
    )
    deleted_replacement = SimpleNamespace(
        id=replacement_id,
        requested_document_id=requested_document_id,
        parent_zip_file_id=None,
        content_hash="readable-hash",
        extraction_reason=None,
        deleted_at=timestamp,
    )
    delivery = SimpleNamespace(
        id=uuid4(),
        requested_document_id=requested_document_id,
        status="sent",
        created_at=timestamp,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Rows([document]),
                _Rows([deleted_replacement]),
                _Rows([delivery]),
            ]
        )
    )

    states = await locked_file_requests.request_states_for_bucket(
        db,
        bucket_id=uuid4(),
        file_ids={source_id},
    )

    assert states[source_id].replacement_review_state == "requested"
    # No analysis query is made because deleted attempts are lineage-only.
    assert db.execute.await_count == 3


def test_unlocked_copy_request_requires_a_publicly_usable_room() -> None:
    valid = {
        "status": "active",
        "expires_at": None,
        "completed_at": None,
        "allow_multiple_sessions": True,
        "passcode_hash": "hash",
        "encrypted_passcode": "ciphertext",
    }
    with patch(
        "app.routers.application_profiles.client_room.read_passcode",
        return_value="123456",
    ):
        _require_usable_unlocked_copy_room(SimpleNamespace(**valid))

    for override, expected_code in [
        ({"passcode_hash": None}, "application_room_pin_required"),
        ({"encrypted_passcode": None}, "application_room_pin_required"),
        ({"status": "revoked"}, "application_room_unavailable"),
        (
            {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
            "application_room_unavailable",
        ),
        (
            {"completed_at": datetime.now(UTC), "allow_multiple_sessions": False},
            "application_room_unavailable",
        ),
    ]:
        with (
            patch(
                "app.routers.application_profiles.client_room.read_passcode",
                return_value="123456",
            ),
            pytest.raises(HTTPException) as error,
        ):
            _require_usable_unlocked_copy_room(SimpleNamespace(**{**valid, **override}))
        assert error.value.status_code == 409
        assert error.value.detail["code"] == expected_code

    for invalid_recovered_pin in (None, "Welcome123!", "12345", "１２３４５６"):
        with (
            patch(
                "app.routers.application_profiles.client_room.read_passcode",
                return_value=invalid_recovered_pin,
            ),
            pytest.raises(HTTPException) as error,
        ):
            _require_usable_unlocked_copy_room(SimpleNamespace(**valid))
        assert error.value.detail["code"] == "application_room_pin_required"


@pytest.mark.asyncio
async def test_public_uploaded_file_read_projects_lock_and_request_state() -> None:
    timestamp = datetime.now(UTC)
    file_id = uuid4()
    requested_document_id = uuid4()
    file = SimpleNamespace(
        id=file_id,
        bucket_id=uuid4(),
        requested_document_id=None,
        parent_zip_file_id=None,
        zip_entry_path=None,
        extraction_status=None,
        extraction_reason=None,
        file_name="locked.pdf",
        content_type="application/pdf",
        size_bytes=100,
        uploaded_by_name="Client",
        uploaded_by_email="client@example.com",
        source_kind="client_room",
        source_detail=None,
        source_label="Uploaded by the client",
        status="uploaded",
        created_at=timestamp,
        updated_at=timestamp,
    )
    state = locked_file_requests.UnlockedCopyRequestState(
        source_file_id=file_id,
        source_content_hash="d" * 64,
        requested_document_id=requested_document_id,
        request_status="requested",
        delivery_id=uuid4(),
        delivery_status="sent",
        requested_at=timestamp,
        last_delivery_at=timestamp,
        replacement_review_state="requested",
    )
    analysis_state = locked_file_requests.PublicFileAnalysisState(
        analysis_status="skipped",
        analysis_reason_code="password_protected",
        analysis_classification="unreadable",
        analysis_review_state="needs_another_copy",
    )

    with patch(
        "app.routers.buckets.locked_file_requests.password_protection_for_files",
        AsyncMock(return_value=({file_id}, {file_id: state}, {file_id: analysis_state})),
    ):
        reads = await _request_uploaded_file_reads(SimpleNamespace(), file.bucket_id, [file])

    assert reads[0].is_password_protected is True
    assert reads[0].unlocked_copy_request is not None
    assert reads[0].unlocked_copy_request.requested_document_id == requested_document_id
    assert reads[0].unlocked_copy_request.delivery_status == "sent"
    assert reads[0].analysis_status == "skipped"
    assert reads[0].analysis_reason_code == "password_protected"
    assert reads[0].analysis_classification == "unreadable"
    assert reads[0].analysis_review_state == "needs_another_copy"


@pytest.mark.asyncio
async def test_public_analysis_lifecycle_is_batched_and_current_hash_filtered() -> None:
    bucket_id = uuid4()
    pending_id, completed_id, locked_id, skipped_id, unreadable_id, failed_id = (
        uuid4() for _ in range(6)
    )

    def file(file_id, content_hash):
        return SimpleNamespace(
            id=file_id,
            content_hash=content_hash,
            extraction_reason=None,
        )

    files = [
        file(pending_id, "pending-current"),
        file(completed_id, "completed-current"),
        file(locked_id, "locked-current"),
        file(skipped_id, "skipped-current"),
        file(unreadable_id, "unreadable-current"),
        file(failed_id, "failed-current"),
    ]

    def analysis(file_id, content_hash, status, classification=None, skip_reason=None):
        return SimpleNamespace(
            id=uuid4(),
            bucket_file_id=file_id,
            content_hash=content_hash,
            analysis_version=3,
            created_at=datetime.now(UTC),
            status=status,
            classification=classification,
            skip_reason=skip_reason,
        )

    analyses = [
        analysis(
            completed_id,
            "stale-bytes",
            "skipped",
            "unreadable",
            "password_protected",
        ),
        analysis(pending_id, "pending-current", "pending"),
        analysis(completed_id, "completed-current", "completed", "bank_statement"),
        analysis(
            locked_id,
            "locked-current",
            "skipped",
            "unreadable",
            "password_protected",
        ),
        analysis(
            skipped_id,
            "skipped-current",
            "skipped",
            "unreadable",
            "unsupported_content_type",
        ),
        analysis(
            unreadable_id,
            "unreadable-current",
            "completed",
            "unreadable",
        ),
        analysis(failed_id, "failed-current", "failed"),
    ]
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_Rows(analyses), _Rows([])]))

    protected, requests, states = await locked_file_requests.password_protection_for_files(
        db,
        bucket_id=bucket_id,
        files=files,
    )

    assert protected == {locked_id}
    assert requests == {}
    assert states[pending_id] == locked_file_requests.PublicFileAnalysisState(
        "pending", "analysis_pending", None, "checking"
    )
    assert states[completed_id] == locked_file_requests.PublicFileAnalysisState(
        "completed", None, "bank_statement", "received"
    )
    assert states[locked_id] == locked_file_requests.PublicFileAnalysisState(
        "skipped", "password_protected", "unreadable", "needs_another_copy"
    )
    assert states[skipped_id] == locked_file_requests.PublicFileAnalysisState(
        "skipped", "unreadable", "unreadable", "needs_another_copy"
    )
    assert states[unreadable_id] == locked_file_requests.PublicFileAnalysisState(
        "completed", "unreadable", "unreadable", "needs_another_copy"
    )
    assert states[failed_id] == locked_file_requests.PublicFileAnalysisState(
        "failed", "analysis_failed", None, "checking"
    )
    # One analysis query + one replacement-document query, never one per file.
    assert db.execute.await_count == 2


def test_public_unlocked_request_metadata_is_explicit_and_redacted() -> None:
    source_file_id = uuid4()
    requested_document_id = uuid4()
    document = BucketRequestedDocument(
        id=requested_document_id,
        bucket_id=uuid4(),
        template_id=None,
        name="Unlocked copy of statement.pdf",
        category="Replacement Documents",
        description="Upload without a password",
        required=True,
        allow_multiple_files=True,
        status="uploaded",
        is_custom=True,
        requires_signature=False,
        signature_kind=None,
        template_file_id=None,
        signature_document_text=None,
        requirement_key=f"unlocked_copy:{source_file_id.hex}:secret-hash",
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_file_id),
            "source_content_hash": "secret-hash",
            "source_file_name": "statement.pdf",
        },
    )
    zip_parent_id = uuid4()
    attempts = [
        SimpleNamespace(
            id=zip_parent_id,
            requested_document_id=requested_document_id,
            # A later ZIP parent/bad retry must not reopen a request when one
            # extracted child or earlier attempt is already readable.
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
            parent_zip_file_id=None,
            analysis_review_state="needs_another_copy",
        ),
        SimpleNamespace(
            id=uuid4(),
            requested_document_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            parent_zip_file_id=zip_parent_id,
            analysis_review_state="received",
        ),
    ]

    result = _public_requested_document_read(document, attempts)

    assert result.request_kind == "unlocked_copy"
    assert result.source_file_id == source_file_id
    assert result.replacement_review_state == "received"
    assert result.requirement_source is None
    assert result.requirement_key is None
    assert _public_requested_document_read(document, []).replacement_review_state == "requested"
    surviving_child = SimpleNamespace(
        id=uuid4(),
        requested_document_id=None,
        parent_zip_file_id=uuid4(),
        analysis_review_state="received",
        unlocked_copy_request=SimpleNamespace(requested_document_id=requested_document_id),
    )
    assert (
        _public_requested_document_read(document, [surviving_child]).replacement_review_state
        == "received"
    )
    dealer_result = _dealer_requested_document_read(document, attempts)
    assert dealer_result.request_kind == "unlocked_copy"
    assert dealer_result.replacement_review_state == "received"
    assert dealer_result.requirement_source is None


@pytest.mark.asyncio
async def test_public_projection_hides_stale_duplicate_and_deleted_source_requests() -> None:
    source_file_id = uuid4()
    bucket_id = uuid4()
    old_document = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_file_id),
            "source_content_hash": "h1",
        },
    )
    current_document = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        requirement_source={
            "kind": locked_file_requests.REQUEST_KIND,
            "source_file_id": str(source_file_id),
            "source_content_hash": "h2",
        },
    )
    normal_document = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
        requirement_source={"kind": "program_readiness"},
    )
    source = SimpleNamespace(
        id=source_file_id,
        bucket_id=bucket_id,
        status="uploaded",
        deleted_at=None,
        content_hash="h2",
        extraction_reason=None,
    )
    analysis = SimpleNamespace(
        id=uuid4(),
        bucket_file_id=source_file_id,
        content_hash="h2",
        analysis_version=3,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        status="skipped",
        skip_reason="password_protected",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[_Rows([source]), _Rows([analysis]), _Rows([])]
        )
    )

    visible = await locked_file_requests.current_public_request_documents(
        db,
        [old_document, current_document, normal_document],
    )

    assert [document.id for document in visible] == [
        current_document.id,
        normal_document.id,
    ]
    # Source lookup is deliberately global rather than constrained to the
    # room's bucket, because linked evidence can originate in another bucket.
    assert source.bucket_id == bucket_id
    # Removing/deleting the active source removes both unlocked-copy tasks,
    # while ordinary requested documents remain available.
    deleted_db = SimpleNamespace(execute=AsyncMock(return_value=_Rows([])))
    assert await locked_file_requests.current_public_request_documents(
        deleted_db,
        [old_document, current_document, normal_document],
    ) == [normal_document]
    assert deleted_db.execute.await_count == 1


def test_unlocked_copy_room_url_uses_canonical_production_origin() -> None:
    requested_document_id = uuid4()
    with patch.object(
        locked_file_requests,
        "canonical_room_url",
        return_value="https://app.qualifiedcommercial.com/buckets/request/token",
    ) as canonical:
        value = locked_file_requests._room_url(
            SimpleNamespace(token="token"), requested_document_id
        )

    canonical.assert_called_once_with("token")
    assert value == (
        "https://app.qualifiedcommercial.com/buckets/request/token"
        f"?tab=todo&request={requested_document_id}"
    )


@pytest.mark.asyncio
async def test_public_status_refresh_has_no_success_audit_write() -> None:
    link = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        passcode_hash="hash",
        recipient_name="Client",
        recipient_email="client@example.com",
    )
    expected = object()
    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch(
            "app.routers.buckets._load_upload_link_or_404",
            AsyncMock(return_value=link),
        ),
        patch("app.routers.buckets._client_ip", return_value="203.0.113.4"),
        patch("app.routers.buckets._verify_passcode", return_value=True),
        patch("app.routers.buckets._log", AsyncMock()) as audit,
        patch(
            "app.routers.buckets._request_access_read",
            AsyncMock(return_value=expected),
        ) as read,
    ):
        result = await request_link_status(
            "token",
            BucketRequestAccessRequest(passcode="123456"),
            SimpleNamespace(),
            db,
        )

    assert result is expected
    read.assert_awaited_once_with(db, link)
    audit.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_status_refresh_audits_failed_pin() -> None:
    link = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        passcode_hash="hash",
        recipient_name="Client",
        recipient_email="client@example.com",
    )
    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch(
            "app.routers.buckets._load_upload_link_or_404",
            AsyncMock(return_value=link),
        ),
        patch("app.routers.buckets._client_ip", return_value="203.0.113.5"),
        patch("app.routers.buckets._verify_passcode", return_value=False),
        patch("app.routers.buckets._log", AsyncMock()) as audit,
        patch("app.routers.buckets._request_access_read", AsyncMock()) as read,
        pytest.raises(HTTPException) as error,
    ):
        await request_link_status(
            "token",
            BucketRequestAccessRequest(passcode="000000"),
            SimpleNamespace(),
            db,
        )

    assert error.value.status_code == 403
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "upload_passcode_failed"
    db.commit.assert_awaited_once()
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_public_access_remains_audited() -> None:
    link = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        passcode_hash="hash",
        recipient_name="Client",
        recipient_email="client@example.com",
    )
    expected = object()
    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch(
            "app.routers.buckets._load_upload_link_or_404",
            AsyncMock(return_value=link),
        ),
        patch("app.routers.buckets._client_ip", return_value="203.0.113.6"),
        patch("app.routers.buckets._verify_passcode", return_value=True),
        patch("app.routers.buckets._log", AsyncMock()) as audit,
        patch(
            "app.routers.buckets._request_access_read",
            AsyncMock(return_value=expected),
        ),
    ):
        result = await request_link_access(
            "token",
            BucketRequestAccessRequest(passcode="123456"),
            SimpleNamespace(),
            db,
        )

    assert result is expected
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "upload_link_accessed"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_init_rejects_an_obsolete_unlocked_copy_target() -> None:
    bucket_id = uuid4()
    link = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        passcode_hash="hash",
        recipient_name="Client",
        recipient_email="client@example.com",
    )
    document = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, status="requested")
    db = SimpleNamespace(get=AsyncMock(return_value=document), commit=AsyncMock())
    stale = locked_file_requests.StaleUnlockedCopyRequest(
        "This upload request was replaced by a newer task."
    )

    with (
        patch(
            "app.routers.buckets._load_upload_link_or_404",
            AsyncMock(return_value=link),
        ),
        patch("app.routers.buckets._client_ip", return_value="203.0.113.7"),
        patch("app.routers.buckets._verify_passcode", return_value=True),
        patch("app.routers.buckets._log", AsyncMock()) as audit,
        patch(
            "app.routers.buckets.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(side_effect=stale),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await request_upload_init(
            "token",
            BucketFileUploadInit(
                requested_document_id=document.id,
                file_name="unlocked.pdf",
                content_type="application/pdf",
                size_bytes=100,
                uploader_name="Client",
                passcode="123456",
            ),
            SimpleNamespace(),
            db,
        )

    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "stale_requested_document",
        "message": str(stale),
    }
    require_current.assert_awaited_once_with(db, document, for_update=True)
    audit.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_init_treats_an_inactive_normal_request_as_stale() -> None:
    bucket_id = uuid4()
    link = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        passcode_hash="hash",
        recipient_name="Client",
        recipient_email="client@example.com",
    )
    document = SimpleNamespace(
        id=uuid4(), bucket_id=bucket_id, status="not_applicable"
    )
    db = SimpleNamespace(get=AsyncMock(return_value=document), commit=AsyncMock())

    with (
        patch(
            "app.routers.buckets._load_upload_link_or_404",
            AsyncMock(return_value=link),
        ),
        patch("app.routers.buckets._client_ip", return_value="203.0.113.8"),
        patch("app.routers.buckets._verify_passcode", return_value=True),
        patch("app.routers.buckets._log", AsyncMock()),
        patch(
            "app.routers.buckets.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await request_upload_init(
            "token",
            BucketFileUploadInit(
                requested_document_id=document.id,
                file_name="evidence.pdf",
                content_type="application/pdf",
                size_bytes=100,
                uploader_name="Client",
                passcode="123456",
            ),
            SimpleNamespace(),
            db,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "stale_requested_document"
    require_current.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_complete_rejects_an_obsolete_unlocked_copy_target() -> None:
    bucket_id = uuid4()
    link_id = uuid4()
    document_id = uuid4()
    link = SimpleNamespace(
        id=link_id,
        bucket_id=bucket_id,
        recipient_name="Client",
        recipient_email="client@example.com",
    )
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        upload_link_id=link_id,
        requested_document_id=document_id,
        deleted_at=None,
        status="uploading",
        uploaded_by_name="Client",
        uploaded_by_email="client@example.com",
    )
    document = SimpleNamespace(id=document_id, bucket_id=bucket_id, status="requested")
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[file, document]),
        commit=AsyncMock(),
    )
    stale = locked_file_requests.StaleUnlockedCopyRequest(
        "This upload request was replaced by a newer task."
    )

    with (
        patch(
            "app.routers.buckets._load_upload_link_or_404",
            AsyncMock(return_value=link),
        ),
        patch("app.routers.buckets._log", AsyncMock()) as audit,
        patch(
            "app.routers.buckets.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(side_effect=stale),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await request_upload_complete(
            "token",
            BucketUploadComplete(file_id=file.id),
            SimpleNamespace(add_task=Mock()),
            SimpleNamespace(),
            db,
        )

    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "stale_requested_document",
        "message": str(stale),
    }
    require_current.assert_awaited_once_with(db, document, for_update=True)
    audit.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert file.status == "uploading"
    assert document.status == "requested"


@pytest.mark.asyncio
async def test_shared_intake_upload_init_rejects_a_hidden_duplicate_target() -> None:
    bucket_id = uuid4()
    document = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, status="requested")
    intake = SimpleNamespace(bucket_id=bucket_id, bucket_upload_link_id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=document))
    stale = locked_file_requests.StaleUnlockedCopyRequest(
        "This upload request was replaced by a newer task."
    )

    with (
        patch(
            "app.routers.dealer_ai_intake.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(side_effect=stale),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await _start_intake_upload(
            db,
            intake,
            DealerFileUploadInit(
                requested_document_id=document.id,
                file_name="unlocked.pdf",
                content_type="application/pdf",
                size_bytes=100,
            ),
            SimpleNamespace(),
            actor_name="Client",
            actor_email="client@example.com",
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "stale_requested_document"
    require_current.assert_awaited_once_with(db, document, for_update=True)


@pytest.mark.asyncio
async def test_shared_intake_upload_complete_rejects_a_hidden_duplicate_target() -> None:
    bucket_id = uuid4()
    upload_link_id = uuid4()
    document = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, status="requested")
    intake = SimpleNamespace(
        bucket_id=bucket_id,
        bucket_upload_link_id=upload_link_id,
    )
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        upload_link_id=upload_link_id,
        requested_document_id=document.id,
        deleted_at=None,
        status="uploading",
    )
    db = SimpleNamespace(get=AsyncMock(side_effect=[file, document]))
    stale = locked_file_requests.StaleUnlockedCopyRequest(
        "This upload request was replaced by a newer task."
    )

    with (
        patch(
            "app.routers.dealer_ai_intake.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(side_effect=stale),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await _complete_intake_upload(
            db,
            intake,
            DealerUploadComplete(file_id=file.id),
            SimpleNamespace(),
            actor_name="Client",
            actor_email="client@example.com",
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "stale_requested_document"
    require_current.assert_awaited_once_with(db, document, for_update=True)
    assert file.status == "uploading"


@pytest.mark.asyncio
async def test_admin_bucket_upload_init_rejects_a_hidden_duplicate_target() -> None:
    bucket_id = uuid4()
    document = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, status="requested")
    db = SimpleNamespace(get=AsyncMock(return_value=document), commit=AsyncMock())
    stale = locked_file_requests.StaleUnlockedCopyRequest(
        "This upload request was replaced by a newer task."
    )

    with (
        patch("app.routers.buckets._load_bucket_or_404", AsyncMock()),
        patch("app.routers.buckets._log", AsyncMock()),
        patch(
            "app.routers.buckets.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(side_effect=stale),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await admin_upload_init(
            bucket_id,
            BucketFileUploadInit(
                requested_document_id=document.id,
                file_name="unlocked.pdf",
                content_type="application/pdf",
                size_bytes=100,
                uploader_name="Underwriter",
            ),
            SimpleNamespace(),
            SimpleNamespace(id=uuid4()),
            db,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "stale_requested_document"
    require_current.assert_awaited_once_with(db, document, for_update=True)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_bucket_upload_complete_rejects_a_hidden_duplicate_target() -> None:
    bucket_id = uuid4()
    document = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, status="requested")
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        upload_link_id=None,
        requested_document_id=document.id,
        deleted_at=None,
        status="uploading",
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[file, document]),
        commit=AsyncMock(),
    )
    stale = locked_file_requests.StaleUnlockedCopyRequest(
        "This upload request was replaced by a newer task."
    )

    with (
        patch(
            "app.routers.buckets._load_bucket_or_404",
            AsyncMock(return_value=SimpleNamespace()),
        ),
        patch("app.routers.buckets._log", AsyncMock()),
        patch(
            "app.routers.buckets.locked_file_requests.require_current_unlocked_copy_upload_target",
            AsyncMock(side_effect=stale),
        ) as require_current,
        pytest.raises(HTTPException) as error,
    ):
        await admin_upload_complete(
            bucket_id,
            BucketUploadComplete(file_id=file.id),
            SimpleNamespace(add_task=Mock()),
            SimpleNamespace(),
            SimpleNamespace(id=uuid4()),
            db,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "stale_requested_document"
    require_current.assert_awaited_once_with(db, document, for_update=True)
    assert file.status == "uploading"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_zip_encrypted_entry_can_create_unlocked_copy_request_without_analysis() -> None:
    profile_id = uuid4()
    file_id = uuid4()
    timestamp = datetime.now(UTC)
    profile = SimpleNamespace(
        id=profile_id,
        primary_bucket_id=uuid4(),
        client_id=None,
        dealer_id=None,
    )
    file = SimpleNamespace(
        id=file_id,
        file_name="statements.zip",
        status="uploaded",
        deleted_at=None,
        content_hash="z" * 64,
        extraction_reason='[{"entry":"locked.pdf","reason":"zip_entry_encrypted"}]',
    )
    requested = SimpleNamespace(id=uuid4(), status="requested", created_at=timestamp)
    delivery = SimpleNamespace(
        id=uuid4(),
        status="created",
        channel="none",
        detail="Created without sending",
        attempt_number=1,
        provider_result={"accepted": False},
        created_at=timestamp,
    )
    outcome = locked_file_requests.UnlockedCopyRequestOutcome(
        requested_document=requested,
        delivery=delivery,
        room_url="https://app.example/buckets/request/token?tab=todo",
        deduplicated=False,
        created_request=True,
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(file)]),
        commit=AsyncMock(),
    )
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Underwriter")

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)])),
        ),
        patch(
            "app.routers.application_profiles._profile_room_link",
            AsyncMock(
                return_value=SimpleNamespace(
                    token="token",
                    passcode_hash="hash",
                    encrypted_passcode="ciphertext",
                )
            ),
        ),
        patch(
            "app.routers.application_profiles.missing_item_automation.optional_recipient_for_profile",
            AsyncMock(return_value="client@example.com"),
        ),
        patch(
            "app.routers.application_profiles.locked_file_requests.request_unlocked_copy",
            AsyncMock(return_value=outcome),
        ) as request_copy,
        patch(
            "app.routers.application_profiles.bucket_ai.analyze_bucket_file",
            AsyncMock(return_value=None),
        ) as analyze,
        patch(
            "app.routers.application_profiles.profiles.log_profile_action", AsyncMock()
        ) as audit_log,
        patch("app.routers.application_profiles.file_events.emit", AsyncMock()) as event,
        patch(
            "app.routers.application_profiles._require_training_live_action", AsyncMock()
        ) as training_guard,
        patch(
            "app.routers.application_profiles.client_room.read_passcode",
            return_value="123456",
        ),
    ):
        result = await request_unlocked_application_evidence(
            profile_id,
            file_id,
            UnlockedCopyRequestCreate(delivery_mode="room_link_only"),
            SimpleNamespace(headers={}),
            user,
            db,
        )

    assert result.source_file_id == file_id
    assert result.delivery_status == "created"
    assert result.provider_accepted is False
    assert request_copy.await_args.kwargs["analysis"] is None
    assert request_copy.await_args.kwargs["send_email"] is False
    assert request_copy.await_args.kwargs["before_email_delivery"] is None
    analyze.assert_awaited_once_with(db, file, force=False)
    training_guard.assert_not_awaited()
    audit_log.assert_awaited_once()
    assert audit_log.await_args.args[3] == "evidence.unlocked_copy_requested"
    event.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_locked_pdf_without_prior_analysis_is_revalidated_and_requested() -> None:
    profile_id, bucket_id, file_id = uuid4(), uuid4(), uuid4()
    timestamp = datetime.now(UTC)
    profile = SimpleNamespace(
        id=profile_id,
        primary_bucket_id=bucket_id,
        client_id=None,
        dealer_id=None,
    )
    file = SimpleNamespace(
        id=file_id,
        file_name="direct-upload.pdf",
        status="uploaded",
        deleted_at=None,
        content_hash=None,
        extraction_reason=None,
    )
    analysis = SimpleNamespace(
        id=uuid4(),
        status="skipped",
        skip_reason="password_protected",
        content_hash="new-current-hash",
    )

    async def analyze_current(*_args, **_kwargs):
        # Canonical analysis records the freshly computed current fingerprint.
        file.content_hash = analysis.content_hash
        return analysis

    requested = SimpleNamespace(id=uuid4(), status="requested", created_at=timestamp)
    delivery = SimpleNamespace(
        id=uuid4(),
        status="created",
        channel="none",
        recipient_email=None,
        detail="Created without sending",
        attempt_number=1,
        provider_result={"accepted": False},
        created_at=timestamp,
    )
    outcome = locked_file_requests.UnlockedCopyRequestOutcome(
        requested_document=requested,
        delivery=delivery,
        room_url="https://app.example/buckets/request/token?tab=todo",
        deduplicated=False,
        created_request=True,
    )

    async def request_after_final_decision(*_args, **kwargs):
        # The service invokes authorization only after its final idempotency /
        # replacement-state decision says a new email will be created.
        await kwargs["before_email_delivery"]()
        return outcome

    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(file)), commit=AsyncMock())

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)])),
        ),
        patch(
            "app.routers.application_profiles.bucket_ai.analyze_bucket_file",
            AsyncMock(side_effect=analyze_current),
        ) as analyze,
        patch(
            "app.routers.application_profiles._profile_room_link",
            AsyncMock(
                return_value=SimpleNamespace(
                    token="token",
                    passcode_hash="hash",
                    encrypted_passcode="ciphertext",
                )
            ),
        ),
        patch(
            "app.routers.application_profiles.missing_item_automation.optional_recipient_for_profile",
            AsyncMock(return_value="client@example.com"),
        ),
        patch(
            "app.routers.application_profiles._require_training_live_action",
            AsyncMock(),
        ) as training_guard,
        patch(
            "app.routers.application_profiles.locked_file_requests.request_unlocked_copy",
            AsyncMock(side_effect=request_after_final_decision),
        ) as request_copy,
        patch("app.routers.application_profiles.profiles.log_profile_action", AsyncMock()),
        patch("app.routers.application_profiles.file_events.emit", AsyncMock()),
        patch(
            "app.routers.application_profiles.client_room.read_passcode",
            return_value="123456",
        ),
    ):
        result = await request_unlocked_application_evidence(
            profile_id,
            file_id,
            UnlockedCopyRequestCreate(),
            SimpleNamespace(headers={}),
            SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Underwriter"),
            db,
        )

    assert result.source_file_id == file_id
    analyze.assert_awaited_once_with(db, file, force=False)
    assert request_copy.await_args.kwargs["analysis"] is analysis
    assert request_copy.await_args.kwargs["send_email"] is True
    assert callable(request_copy.await_args.kwargs["before_email_delivery"])
    training_guard.assert_awaited_once()


@pytest.mark.asyncio
async def test_unlocked_copy_request_denies_non_underwriting_role_before_lookup() -> None:
    db = SimpleNamespace(execute=AsyncMock())
    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile", AsyncMock()
        ) as load_profile,
        pytest.raises(HTTPException) as error,
    ):
        await request_unlocked_application_evidence(
            uuid4(),
            uuid4(),
            UnlockedCopyRequestCreate(),
            SimpleNamespace(headers={}),
            SimpleNamespace(id=uuid4(), role=Role.FIELD_REP),
            db,
        )

    assert error.value.status_code == 403
    load_profile.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unlocked_copy_request_rejects_a_readable_file() -> None:
    profile_id = uuid4()
    file_id = uuid4()
    profile = SimpleNamespace(id=profile_id)
    file = SimpleNamespace(
        id=file_id,
        status="uploaded",
        deleted_at=None,
        content_hash="readable-hash",
        extraction_reason=None,
    )
    analysis = SimpleNamespace(
        status="completed",
        skip_reason=None,
        content_hash="readable-hash",
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(file)]),
        commit=AsyncMock(),
    )

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "app.routers.application_profiles.profiles.evidence_state",
            AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)])),
        ),
        patch("app.routers.application_profiles._profile_room_link", AsyncMock()) as room_link,
        patch(
            "app.routers.application_profiles.bucket_ai.analyze_bucket_file",
            AsyncMock(return_value=analysis),
        ) as analyze,
        pytest.raises(HTTPException) as error,
    ):
        await request_unlocked_application_evidence(
            profile_id,
            file_id,
            UnlockedCopyRequestCreate(),
            SimpleNamespace(headers={}),
            SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC),
            db,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "evidence_not_password_protected"
    analyze.assert_awaited_once_with(db, file, force=False)
    db.commit.assert_awaited_once()
    room_link.assert_not_awaited()
