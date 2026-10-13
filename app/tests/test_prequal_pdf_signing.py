from __future__ import annotations

import hashlib
import io
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest

from app.models.prequal_request import PrequalRequest
from app.routers import prequal as prequal_router
from app.schemas.prequal import PrequalRequestApprove
from app.services import prequal_pdf


class _FakeS3:
    def __init__(self, signature_bytes: bytes = b"signature-png") -> None:
        self.signature_bytes = signature_bytes
        self.puts: list[dict[str, object]] = []
        self.presign_params: list[dict[str, object]] = []

    def get_object(self, **_kwargs):
        return {"Body": io.BytesIO(self.signature_bytes), "ContentType": "image/png"}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def generate_presigned_url(self, _operation, *, Params, ExpiresIn):
        self.presign_params.append({"Params": Params, "ExpiresIn": ExpiresIn})
        return "https://example.test/prequal.pdf"


def _settings():
    return SimpleNamespace(
        s3_bucket="qc-private",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_region="us-east-1",
    )


def _request(*, status: str = "approved") -> PrequalRequest:
    now = datetime.now(UTC)
    row = PrequalRequest(
        id=uuid4(),
        requester_id=uuid4(),
        loan_id=None,
        client_id=None,
        target_property_address="100 Main Street, Newark, NJ",
        purchase_price=750_000,
        requested_loan_amount=500_000,
        loan_type="bridge",
        borrower_entity="Sample Holdings LLC",
        status=status,
        version_num=1,
        quote_number="Q-1234",
        pdf_s3_key="prequals/example/pre_quals/q-1234.pdf",
    )
    row.created_at = now
    row.updated_at = now
    return row


def test_render_letter_uses_hash_verified_account_signer_and_contact(monkeypatch):
    signature = b"verified-signature-image"
    fake_s3 = _FakeS3(signature)
    monkeypatch.setattr(prequal_pdf, "_s3_client", lambda _settings: fake_s3)

    captured: dict[str, str] = {}
    fake_weasyprint = ModuleType("weasyprint")

    class _HTML:
        def __init__(self, *, string: str):
            captured["html"] = string

        def write_pdf(self):
            return b"%PDF-account-signer"

    fake_weasyprint.HTML = _HTML
    monkeypatch.setitem(sys.modules, "weasyprint", fake_weasyprint)

    signer = prequal_pdf.PrequalSigner(
        name="Alex Underwriter",
        title="Loan Executive",
        email="alex@qualifiedcommercial.com",
        phone="+1 973 555 0100",
        signature_s3_key="stored-signatures/users/alex/signature.png",
        signature_sha256=hashlib.sha256(signature).hexdigest(),
        signature_id=str(uuid4()),
        signature_source="self_adopted",
    )
    request = _request()
    key = prequal_pdf.render_letter(
        request,
        settings=_settings(),
        signer=signer,
    )

    assert key == f"prequals/{request.requester_id}/pre_quals/{request.id}.pdf"
    html = captured["html"]
    assert "Alex Underwriter" in html
    assert "Loan Executive" in html
    assert "alex@qualifiedcommercial.com" in html
    assert "+1 973 555 0100" in html
    assert "data:image/png;base64," in html
    assert fake_s3.puts[0]["Body"] == b"%PDF-account-signer"


def test_signature_hash_mismatch_does_not_embed_unverified_image(monkeypatch):
    fake_s3 = _FakeS3(b"tampered")
    monkeypatch.setattr(prequal_pdf, "_s3_client", lambda _settings: fake_s3)

    result = prequal_pdf._signature_data_uri(
        _settings(),
        "stored-signatures/users/example/signature.png",
        expected_sha256="0" * 64,
    )

    assert result is None


def test_manual_letter_fails_closed_when_saved_signature_cannot_be_verified(monkeypatch):
    fake_s3 = _FakeS3(b"tampered")
    monkeypatch.setattr(prequal_pdf, "_s3_client", lambda _settings: fake_s3)
    fake_weasyprint = ModuleType("weasyprint")
    fake_weasyprint.HTML = object
    monkeypatch.setitem(sys.modules, "weasyprint", fake_weasyprint)
    signer = prequal_pdf.PrequalSigner(
        name="Alex Underwriter",
        signature_s3_key="stored-signatures/users/alex/signature.png",
        signature_sha256="0" * 64,
    )

    with pytest.raises(RuntimeError, match="could not be retrieved and verified"):
        prequal_pdf.render_letter(_request(), settings=_settings(), signer=signer)


def test_letter_signer_snapshot_preserves_contact_and_signature_evidence():
    user_id = uuid4()
    signature_id = str(uuid4())
    signed_at = datetime.now(UTC)
    signer = prequal_pdf.PrequalSigner(
        name="Alex Underwriter",
        title="Loan Executive",
        email="alex@qualifiedcommercial.com",
        phone="+1 973 555 0100",
        signature_sha256="a" * 64,
        signature_id=signature_id,
        signature_source="self_adopted",
    )

    snapshot = prequal_router._letter_signer_snapshot(
        signer,
        actor_user_id=user_id,
        signed_at=signed_at,
    )

    assert snapshot == {
        "source": "account_signature",
        "authorization": "explicit_per_issuance",
        "user_id": str(user_id),
        "name": "Alex Underwriter",
        "title": "Loan Executive",
        "email": "alex@qualifiedcommercial.com",
        "phone": "+1 973 555 0100",
        "signature_id": signature_id,
        "signature_source": "self_adopted",
        "signature_sha256": "a" * 64,
        "signed_at": signed_at.isoformat(),
    }


def test_manual_approval_contract_requires_explicit_signature_consent():
    payload = PrequalRequestApprove(
        approved_purchase_price=750_000,
        approved_loan_amount=500_000,
    )

    assert payload.signature_consent is False


def test_presign_get_can_force_a_safe_pdf_download(monkeypatch):
    fake_s3 = _FakeS3()
    monkeypatch.setattr(prequal_pdf, "_s3_client", lambda _settings: fake_s3)

    url = prequal_pdf.presign_get(
        "prequals/example/pre_quals/q-1234.pdf",
        settings=_settings(),
        download_filename="QC Letter Q/1234.pdf",
    )

    assert url == "https://example.test/prequal.pdf"
    params = fake_s3.presign_params[0]["Params"]
    assert params["ResponseContentDisposition"] == 'attachment; filename="QC_Letter_Q_1234.pdf"'
    assert params["ResponseContentType"] == "application/pdf"


def test_manual_approval_signer_comes_from_user_account():
    user_id = uuid4()
    signature_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        name="Taylor Morgan",
        title="Senior Loan Executive",
        email="taylor@qualifiedcommercial.com",
        phone="+1 201 555 0199",
    )
    signature = SimpleNamespace(
        id=signature_id,
        typed_name="Taylor M. Morgan",
        title="Loan Executive",
        signature_s3_key="stored-signatures/users/taylor/signature.png",
        signature_sha256="a" * 64,
        source="self_adopted",
    )

    resolved = prequal_router._signer_from_account(user, signature)

    assert resolved is not None
    assert resolved.name == "Taylor M. Morgan"
    assert resolved.title == "Loan Executive"
    assert resolved.email == user.email
    assert resolved.phone == user.phone
    assert resolved.signature_id == str(signature_id)
    assert resolved.signature_source == "self_adopted"


def test_automatic_approval_uses_firm_identity_and_support_contact(monkeypatch):
    fake_s3 = _FakeS3()
    monkeypatch.setattr(prequal_pdf, "_s3_client", lambda _settings: fake_s3)

    captured: dict[str, str] = {}
    fake_weasyprint = ModuleType("weasyprint")

    class _HTML:
        def __init__(self, *, string: str):
            captured["html"] = string

        def write_pdf(self):
            return b"%PDF-firm-signer"

    fake_weasyprint.HTML = _HTML
    monkeypatch.setitem(sys.modules, "weasyprint", fake_weasyprint)

    settings = _settings()
    settings.prospect_reply_to_email = "support@qualifiedcommercial.com"
    prequal_pdf.render_letter(_request(), settings=settings, signer=None)

    html = captured["html"]
    assert "Franco Pellegrino" in html
    assert "Managing Director | Qualified Commercial LLC" in html
    assert "support@qualifiedcommercial.com" in html


@pytest.mark.parametrize("status", ["approved", "offer_accepted"])
def test_read_shape_exposes_inline_and_attachment_urls(monkeypatch, status):
    monkeypatch.setattr(prequal_router, "get_settings", _settings)

    def _presign(_key, *, settings, ttl_seconds=prequal_pdf.PRESIGN_TTL_SECONDS, download_filename=None):
        assert settings.s3_bucket == "qc-private"
        return "download-url" if download_filename else "inline-url"

    monkeypatch.setattr(prequal_router.prequal_pdf, "presign_get", _presign)

    result = prequal_router._to_read(_request(status=status))

    assert result.pdf_url == "inline-url"
    assert result.pdf_download_url == "download-url"
