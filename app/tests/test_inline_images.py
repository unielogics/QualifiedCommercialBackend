"""Pasting an image into a note: the validation, the binding, the read.

The transport (a presigned PUT) is boto3's business. What is worth pinning down
is everything around it — which files are allowed in, and the rule that an image
id from somewhere else cannot be pulled into your note.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.inline_image import InlineImage
from app.services import inline_images


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _FakeSession:
    """Enough session for the service: add/flush, get, and one canned select."""

    def __init__(self, rows=None):
        self.added = []
        self.rows = rows or []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def get(self, _model, key):
        return next((row for row in self.rows if row.id == key), None)

    async def execute(self, _statement):
        # The service filters in SQL; the fake returns what a matching query
        # would have, so these tests are about the Python-side rules only.
        return _FakeResult(
            [
                row
                for row in self.rows
                if row.subject_id is None and row.status == "ready"
            ]
        )


def _image(**overrides) -> InlineImage:
    row = InlineImage(
        id=uuid.uuid4(),
        subject_kind="dealer_message",
        subject_id=None,
        s3_key="inline-images/dealer_message/x.png",
        filename="screenshot.png",
        mime_type="image/png",
        size_bytes=1024,
        uploaded_by_user_id=uuid.uuid4(),
        status="ready",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


@pytest.mark.asyncio
async def test_upload_refuses_a_type_that_is_not_an_image() -> None:
    with pytest.raises(inline_images.InlineImageError):
        await inline_images.start_upload(
            _FakeSession(),
            subject_kind="dealer_message",
            filename="payload.svg",
            mime_type="image/svg+xml",  # a script carrier, rendered inline
            size_bytes=200,
            user_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_upload_refuses_an_unknown_subject_kind() -> None:
    with pytest.raises(inline_images.InlineImageError):
        await inline_images.start_upload(
            _FakeSession(),
            subject_kind="loan_note",
            filename="a.png",
            mime_type="image/png",
            size_bytes=200,
            user_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_upload_refuses_something_too_large() -> None:
    with pytest.raises(inline_images.InlineImageError):
        await inline_images.start_upload(
            _FakeSession(),
            subject_kind="dealer_message",
            filename="huge.png",
            mime_type="image/png",
            size_bytes=inline_images.MAX_IMAGE_BYTES + 1,
            user_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_upload_accepts_a_content_type_carrying_a_charset() -> None:
    """Some browsers put "image/png; charset=binary" on the clipboard."""
    session = _FakeSession()
    ticket = await inline_images.start_upload(
        session,
        subject_kind="dealer_message",
        filename="shot.png",
        mime_type="image/PNG; charset=binary",
        size_bytes=4096,
        user_id=uuid.uuid4(),
    )
    assert ticket["mime_type"] == "image/png"
    assert session.added and session.added[0].status == "staged"


@pytest.mark.asyncio
async def test_a_filename_cannot_escape_its_prefix() -> None:
    session = _FakeSession()
    await inline_images.start_upload(
        session,
        subject_kind="bucket_note",
        filename="../../etc/passwd.png",
        mime_type="image/png",
        size_bytes=64,
        user_id=uuid.uuid4(),
    )
    key = session.added[0].s3_key
    # The property that matters is that the name contributes exactly one
    # segment: separators are gone, so it cannot walk out of the prefix.
    assert key.startswith("inline-images/bucket_note/")
    assert "/" not in key[len("inline-images/bucket_note/") :]


@pytest.mark.asyncio
async def test_attach_binds_only_the_authors_own_uploads() -> None:
    """An id guessed from elsewhere must not be pulled into your note."""
    author = uuid.uuid4()
    mine = _image(uploaded_by_user_id=author)
    theirs = _image(uploaded_by_user_id=uuid.uuid4())
    session = _FakeSession([mine, theirs])

    attached = await inline_images.attach(
        session,
        image_ids=[mine.id, theirs.id],
        subject_kind="dealer_message",
        subject_id="message-1",
        user_id=author,
    )

    assert [row.id for row in attached] == [mine.id]
    assert mine.subject_id == "message-1" and mine.attached_at is not None
    assert theirs.subject_id is None


@pytest.mark.asyncio
async def test_attach_is_a_no_op_without_ids() -> None:
    assert await inline_images.attach(
        _FakeSession(), image_ids=[], subject_kind="dealer_message",
        subject_id="m", user_id=uuid.uuid4(),
    ) == []


# --- the sweeper -----------------------------------------------------------


class _SweepSession(_FakeSession):
    def __init__(self, rows):
        super().__init__(rows)
        self.deleted = []

    async def execute(self, _statement):
        # Stands in for "unattached and older than the grace period".
        return _FakeResult([r for r in self.rows if r.subject_id is None])

    async def delete(self, row):
        self.deleted.append(row)


@pytest.mark.asyncio
async def test_the_sweeper_removes_an_upload_that_never_joined_anything(monkeypatch):
    """Uploads happen on send, so this only catches the narrow case where the
    upload landed and the send that followed it did not."""
    orphan = _image(subject_id=None)
    session = _SweepSession([orphan])

    deleted_keys = []

    class _Client:
        def delete_object(self, **kwargs):
            deleted_keys.append(kwargs["Key"])

    monkeypatch.setattr(inline_images, "_s3_client", lambda: _Client())
    removed = await inline_images.sweep_orphans(session)

    assert removed == 1
    assert session.deleted == [orphan]
    assert deleted_keys == [orphan.s3_key]


@pytest.mark.asyncio
async def test_a_row_survives_when_its_object_cannot_be_deleted(monkeypatch):
    """Dropping the row anyway would strand the object with nothing pointing at
    it. Leaving it means the next sweep tries again."""
    orphan = _image(subject_id=None)
    session = _SweepSession([orphan])

    class _Failing:
        def delete_object(self, **kwargs):
            raise RuntimeError("s3 is unhappy")

    monkeypatch.setattr(inline_images, "_s3_client", lambda: _Failing())
    removed = await inline_images.sweep_orphans(session)

    assert removed == 0
    assert session.deleted == []


@pytest.mark.asyncio
async def test_the_sweeper_leaves_attached_images_alone(monkeypatch):
    attached = _image(subject_id="message-1")
    session = _SweepSession([attached])
    monkeypatch.setattr(inline_images, "_s3_client", lambda: None)

    assert await inline_images.sweep_orphans(session) == 0
    assert session.deleted == []


def test_the_grace_period_is_long_enough_to_cover_a_retried_send():
    """A row created seconds ago may belong to a send still in flight; deleting
    it would break the very message it was uploaded for."""
    assert inline_images.ORPHAN_GRACE_HOURS >= 1
