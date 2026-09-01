from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.dealer_os.schemas import DealerCreate
from app.dealer_os.services import client_room, consent_delivery
from app.dealer_os.services.client_room import _generate_passcode as _generate_field_room_passcode
from app.dealer_os.services.client_room import _hash_passcode as _hash_field_room_passcode
from app.dealer_os.services.client_room import verify_passcode as verify_field_room_passcode
from app.models.application_profile import ApplicationRoomDelivery
from app.models.bucket import BucketUploadLink
from app.routers.application_profiles import _delivery_overall, _masked_recipient
from app.routers.buckets import (
    _PASSCODE_ATTEMPTS,
    _PASSCODE_MAX_ATTEMPTS,
    _generate_passcode,
    _hash_passcode,
    _verify_passcode,
)
from app.routers.dealer_ai_intake import AdminLeadCreate
from app.schemas.application_profile import RoomPinRotateRequest


def test_new_room_passcodes_are_six_numeric_digits() -> None:
    assert re.fullmatch(r"\d{6}", _generate_passcode())


def test_field_desk_file_requires_one_six_digit_initial_room_pin() -> None:
    data = {
        "name": "Example LLC",
        "entity_type": "Limited liability company",
        "funding_goal": 250_000,
        "funding_purpose": "working_capital",
        "use_of_proceeds_note": "Purchase inventory.",
    }
    with pytest.raises(ValidationError):
        DealerCreate.model_validate(data)
    for invalid in ("12345", "1234567", "12A456", "١٢٣٤٥٦"):
        with pytest.raises(ValidationError):
            DealerCreate.model_validate({**data, "secure_room_pin": invalid})
    assert DealerCreate.model_validate({**data, "secure_room_pin": "000014"}).secure_room_pin == "000014"


def test_generated_replacement_pin_invalidates_the_initial_pin() -> None:
    initial_pin = "000014"
    initial_hash = _hash_field_room_passcode(initial_pin)
    assert verify_field_room_passcode(initial_pin, initial_hash)

    replacement_pin = _generate_field_room_passcode()
    replacement_hash = _hash_field_room_passcode(replacement_pin)
    assert re.fullmatch(r"\d{6}", replacement_pin)
    assert not verify_field_room_passcode(initial_pin, replacement_hash)
    assert verify_field_room_passcode(replacement_pin, replacement_hash)


def test_current_room_pin_is_recoverable_only_from_encrypted_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_room,
        "get_settings",
        lambda: SimpleNamespace(provider_secrets_kms_key_id=""),
    )
    monkeypatch.setattr(client_room, "_encrypt_fernet", lambda value: f"sealed:{value[::-1]}")
    monkeypatch.setattr(client_room, "_decrypt_fernet", lambda value: value.removeprefix("sealed:")[::-1])
    link = SimpleNamespace(
        id="room-link",
        passcode_hash=None,
        encrypted_passcode=None,
        passcode_encryption_provider=None,
    )

    client_room._store_passcode(link, "000014")

    assert link.passcode_hash != "000014"
    assert link.encrypted_passcode == "sealed:410000"
    assert link.passcode_encryption_provider == "fernet"
    assert client_room.read_passcode(link) == "000014"


def test_bucket_room_schema_has_no_plaintext_pin_column() -> None:
    columns = set(BucketUploadLink.__table__.columns.keys())
    assert "encrypted_passcode" in columns
    assert "passcode_encryption_provider" in columns
    assert "passcode" not in columns
    assert "pin" not in columns


def test_legacy_room_passcodes_remain_compatible() -> None:
    passcode_hash = _hash_passcode("QC-104293")
    assert _verify_passcode("QC-104293", passcode_hash, attempt_scope="198.51.100.4")


def test_room_pin_lockout_is_scoped_to_room_and_ip() -> None:
    passcode_hash = _hash_passcode("104293")
    _PASSCODE_ATTEMPTS.clear()
    try:
        for _ in range(_PASSCODE_MAX_ATTEMPTS):
            assert not _verify_passcode("000000", passcode_hash, attempt_scope="198.51.100.4")
        assert not _verify_passcode("104293", passcode_hash, attempt_scope="198.51.100.4")
        assert _verify_passcode("104293", passcode_hash, attempt_scope="198.51.100.5")
    finally:
        _PASSCODE_ATTEMPTS.clear()


def test_admin_intake_requires_a_matching_shape_room_pin() -> None:
    row = AdminLeadCreate(
        full_name="Jane Owner",
        email="jane@example.com",
        business_name="Example LLC",
        secure_room_pin="104293",
    )
    assert row.secure_room_pin == "104293"
    with pytest.raises(ValidationError):
        AdminLeadCreate(
            full_name="Jane Owner",
            email="jane@example.com",
            business_name="Example LLC",
            secure_room_pin="QC-104293",
        )


def test_pin_rotation_accepts_only_six_numeric_digits() -> None:
    assert RoomPinRotateRequest(secure_room_pin="000014").secure_room_pin == "000014"
    for invalid in ("12345", "1234567", "12A456"):
        with pytest.raises(ValidationError):
            RoomPinRotateRequest(secure_room_pin=invalid)


def test_delivery_receipt_schema_has_no_room_secret_columns() -> None:
    columns = set(ApplicationRoomDelivery.__table__.columns.keys())
    assert "token" not in columns
    assert "pin" not in columns
    assert "passcode" not in columns


def test_delivery_rollup_distinguishes_created_partial_and_failure() -> None:
    assert _delivery_overall([SimpleNamespace(channel="none", status="created")]) == "created"
    assert _delivery_overall([SimpleNamespace(channel="email", status="sent")]) == "success"
    assert _delivery_overall([
        SimpleNamespace(channel="email", status="sent"),
        SimpleNamespace(channel="sms", status="failed"),
    ]) == "partial"
    assert _delivery_overall([SimpleNamespace(channel="email", status="failed")]) == "failed"


def test_delivery_receipts_mask_destinations() -> None:
    assert _masked_recipient("email", "owner@example.com", None) == "ow***@example.com"
    assert _masked_recipient("sms", None, "+1 (555) 222-9911") == "***-***-9911"


def test_room_delivery_uses_the_canonical_app_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_email(to_email: str, subject: str, body: str) -> consent_delivery.DeliveryResult:
        captured.update(to=to_email, subject=subject, body=body)
        return consent_delivery.DeliveryResult(True, "email", "accepted")

    monkeypatch.setattr(consent_delivery, "_send_email", fake_email)
    result = consent_delivery.deliver_link(
        channel="email",
        to_email="owner@example.com",
        to_phone=None,
        business_name="Example LLC",
        purpose="open the secure application room",
        path="/buckets/request/opaque-token",
        sms_consent_ok=False,
        origin="https://app.qualifiedcommercial.com",
    )
    assert result.ok
    assert "https://app.qualifiedcommercial.com/buckets/request/opaque-token" in captured["body"]
    assert "PIN" not in captured["body"]
