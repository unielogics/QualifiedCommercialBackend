"""The client signs exactly the words the server certifies, and the PIN room's
credit authorization pulls on the room's own file."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.dealer_ai_intake as core
from app.models.bucket import BucketRequestedDocument
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers.buckets import _public_requested_document_read
from app.services import document_signature


def _document(**overrides) -> BucketRequestedDocument:
    values = dict(
        id=uuid4(), bucket_id=uuid4(), template_id=None, name="Credit authorization",
        category="Credit", description=None, required=True, allow_multiple_files=False,
        status="requested", is_custom=False, requires_signature=True,
        signature_kind="credit_authorization", template_file_id=None, signature_document_text=None,
        requirement_key="signed_credit_authorization", requirement_source=None,
    )
    values.update(overrides)
    return BucketRequestedDocument(**values)


def test_the_room_shows_and_hashes_the_text_the_server_certifies() -> None:
    read = _public_requested_document_read(_document(), [])
    certified = document_signature.credit_authorization_document_text()
    assert read.signature_document_text == certified
    assert read.signature_document_sha256 == document_signature.document_hash(certified)

    custom = _public_requested_document_read(_document(signature_kind="custom", signature_document_text="Sign this."), [])
    assert custom.signature_document_text == "Sign this."
    assert custom.signature_document_sha256 == document_signature.document_hash("Sign this.")

    unsigned = _public_requested_document_read(_document(requires_signature=False, signature_kind=None), [])
    assert unsigned.signature_document_text is None
    assert unsigned.signature_document_sha256 is None


def _db_returning(requested: BucketRequestedDocument) -> SimpleNamespace:
    result = MagicMock()
    result.scalar_one_or_none.return_value = requested
    return SimpleNamespace(
        execute=AsyncMock(return_value=result), add=MagicMock(), flush=AsyncMock(),
        commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock(), get=AsyncMock(return_value=None),
    )


def _payload(requested: BucketRequestedDocument, **overrides) -> core.DealerDocumentSignRequest:
    values = dict(
        requested_document_id=requested.id, typed_name="Jordan Sample", esign_consent=True,
        signature_data_url="data:image/png;base64,iVBORw0KGgo=",
        applicant_legal_first_name="Jordan", applicant_legal_last_name="Sample", applicant_dob="1980-01-15",
        applicant_street="100 Example St", applicant_city="Charlotte", applicant_state="NC", applicant_zip="28202",
    )
    values.update(overrides)
    return core.DealerDocumentSignRequest(**values)


REQUEST = SimpleNamespace(headers={"user-agent": "pytest"}, client=SimpleNamespace(host="127.0.0.1"))


async def test_a_signature_on_text_that_changed_is_refused() -> None:
    requested = _document()
    stand_in = SimpleNamespace(bucket_id=requested.bucket_id, bucket_upload_link_id=uuid4())
    with pytest.raises(HTTPException) as refused:
        await core._sign_requested_document(
            _db_returning(requested), stand_in, _payload(requested, document_sha256="0" * 64), REQUEST,
            actor_name="Jordan Sample", actor_email="jordan@example.com",
        )
    assert refused.value.status_code == 409
    assert "changed" in refused.value.detail


@pytest.fixture
def signing_io(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(core, "_bucket_storage_config", lambda: ("bucket", "prefix", None))
    monkeypatch.setattr(core, "_put_bucket_object", lambda *args, **kwargs: "v1")
    monkeypatch.setattr(core, "_log", AsyncMock())
    monkeypatch.setattr(core, "_send_signed_document_copy_email", lambda **kwargs: None)
    monkeypatch.setattr(document_signature, "render_signature_certificate_pdf", lambda **kwargs: b"%PDF-1.4 test")
    monkeypatch.setattr(document_signature, "decode_signature_data_url", lambda url: (b"png", "a" * 64, "image/png"))
    pull = AsyncMock(return_value={"status": "completed"})
    with patch("app.services.credit_pull_core.auto_pull_after_signature", pull):
        yield pull


async def test_the_pin_room_pulls_on_its_own_file_and_matching_text_signs(signing_io: AsyncMock) -> None:
    requested = _document()
    stand_in = SimpleNamespace(bucket_id=requested.bucket_id, bucket_upload_link_id=uuid4())
    room_file = PublicUnderwritingIntake(id=uuid4(), bucket_id=requested.bucket_id, intake_state={})
    certified = document_signature.document_hash(document_signature.credit_authorization_document_text())
    result = await core._sign_requested_document(
        _db_returning(requested), stand_in, _payload(requested, document_sha256=certified), REQUEST,
        actor_name="Jordan Sample", actor_email="jordan@example.com", pull_intake=room_file,
    )
    assert result.source_kind == "generated" and result.source_detail == "signed_document"
    assert requested.status == "uploaded"
    signing_io.assert_awaited_once()
    assert signing_io.await_args.args[1] is room_file


async def test_a_stand_in_with_no_file_signs_without_attempting_a_pull(signing_io: AsyncMock) -> None:
    requested = _document()
    stand_in = SimpleNamespace(bucket_id=requested.bucket_id, bucket_upload_link_id=uuid4())
    result = await core._sign_requested_document(
        _db_returning(requested), stand_in, _payload(requested), REQUEST,
        actor_name="Jordan Sample", actor_email="jordan@example.com",
    )
    assert result.source_detail == "signed_document"
    signing_io.assert_not_awaited()
