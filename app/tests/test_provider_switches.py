import base64
import hashlib
import hmac
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.dealer_os.services import sms_provider
from app.dealer_os.services.sms_provider import provider_readiness, validate_twilio_signature
from app.routers.analysis import _provider_switch_ready, _public_address_throttle
from app.schemas.analysis import ProviderSettingsRead
from app.services import provider_secrets
from app.services.property_intelligence import _address_from_geoapify_properties


def test_geoapify_address_maps_to_existing_split_address_contract() -> None:
    parts = _address_from_geoapify_properties(
        {
            "address_line1": "100 Main Street",
            "city": "Newark",
            "state_code": "NJ",
            "postcode": "07102",
            "formatted": "100 Main Street, Newark, NJ 07102, United States",
            "lat": 40.7357,
            "lon": -74.1724,
        }
    )

    assert parts.street == "100 Main Street"
    assert parts.city == "Newark"
    assert parts.state == "NJ"
    assert parts.zip == "07102"
    assert parts.latitude == 40.7357
    assert parts.longitude == -74.1724


def test_unchanged_provider_does_not_require_a_configured_key() -> None:
    assert _provider_switch_ready(
        current_provider="google",
        requested_provider="google",
        provider_status={"google_server_configured": False, "geoapify_configured": False},
    )


def test_geoapify_switch_uses_environment_readiness() -> None:
    assert _provider_switch_ready(
        current_provider="google",
        requested_provider="geoapify",
        provider_status={"google_server_configured": False, "geoapify_configured": True},
    )


def test_provider_switch_still_requires_a_configured_target() -> None:
    assert not _provider_switch_ready(
        current_provider="google",
        requested_provider="geoapify",
        provider_status={"google_server_configured": False, "geoapify_configured": False},
    )


@pytest.mark.asyncio
async def test_address_provider_status_never_discloses_environment_keys(monkeypatch) -> None:
    async def fake_runtime_settings(_db):
        return SimpleNamespace(
            rentcast_api_key="rentcast-visible-to-super-admin",
            google_server_api_key="google-backend-secret",
            geoapify_api_key="geoapify-backend-secret",
            address_provider="geoapify",
            property_analysis_ai_enabled=True,
            property_intelligence_cache_ttl_hours=24,
        )

    monkeypatch.setattr(provider_secrets, "runtime_settings", fake_runtime_settings)

    status = await provider_secrets.provider_settings_status(object(), include_secret_values=True)

    assert status["geoapify_configured"] is True
    assert status["google_server_configured"] is True
    assert status["address_provider_ready"] is True
    assert status["address_credentials_source"] == "environment"
    assert "geoapify_api_key" not in status
    assert "google_server_api_key" not in status


def test_provider_settings_read_contract_has_no_address_secret_fields() -> None:
    secret_fields = {
        "geoapify_api_key",
        "google_server_api_key",
        "google_maps_browser_key",
        "google_maps_ios_key",
        "google_maps_android_key",
        "google_maps_mobile_key",
    }

    assert secret_fields.isdisjoint(ProviderSettingsRead.model_fields)


def test_public_address_throttle_is_scoped_by_request_ip() -> None:
    store = {}
    request = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.10"}, client=None)

    _public_address_throttle(store, request, 1)

    with pytest.raises(HTTPException) as error:
        _public_address_throttle(store, request, 1)
    assert error.value.status_code == 429

    other_request = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.11"}, client=None)
    _public_address_throttle(store, other_request, 1)


def test_twilio_readiness_requires_webhook_token_and_sender() -> None:
    incomplete = Settings(
        _env_file=None,
        sms_provider="twilio",
        sms_production=True,
        twilio_account_sid="AC123",
        twilio_api_key_sid="SK123",
        twilio_api_key_secret="secret",
    )
    assert provider_readiness(incomplete)["configured"] is False

    ready = Settings(
        _env_file=None,
        sms_provider="twilio",
        sms_production=True,
        twilio_account_sid="AC123",
        twilio_api_key_sid="SK123",
        twilio_api_key_secret="secret",
        twilio_auth_token="webhook-secret",
        twilio_messaging_service_sid="MG123",
    )
    status = provider_readiness(ready)
    assert status["configured"] is True
    assert status["production"] is True
    assert status["sender"] == "MG123"


def test_sms_provider_selection_never_falls_back() -> None:
    status = provider_readiness(Settings(_env_file=None, sms_provider="unknown", sms_production=True))
    assert status["provider"] == "invalid"
    assert status["configured"] is False


def test_invalid_twilio_signature_flag_fails_closed() -> None:
    settings = Settings(_env_file=None, twilio_validate_signatures="not-a-boolean-secret")

    assert settings.twilio_validate_signatures is True


def test_twilio_signature_validation_uses_public_callback_url_and_sorted_form() -> None:
    url = "https://api.qualifiedcommercial.com/api/v1/webhooks/twilio/sms/inbound"
    form = {"From": "+12015550100", "To": "+18555550100", "Body": "Hello"}
    auth_token = "twilio-auth-token"
    payload = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    signature = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()  # noqa: S324
    ).decode()

    assert validate_twilio_signature(
        url=url,
        form=form,
        signature=signature,
        auth_token=auth_token,
    )
    assert not validate_twilio_signature(
        url=url,
        form={**form, "Body": "Changed"},
        signature=signature,
        auth_token=auth_token,
    )


def test_twilio_outbound_uses_messaging_service_and_status_callback(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        public_api_url="https://api.qualifiedcommercial.com",
        sms_provider="twilio",
        sms_production=True,
        twilio_account_sid="AC123",
        twilio_api_key_sid="SK123",
        twilio_api_key_secret="secret",
        twilio_auth_token="webhook-secret",
        twilio_messaging_service_sid="MG123",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"sid": "SM123", "status": "queued"}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, url: str, *, data: dict[str, str], auth: tuple[str, str]):
            captured.update(url=url, data=data, auth=auth)
            return FakeResponse()

    monkeypatch.setattr(sms_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(sms_provider.httpx, "Client", FakeClient)

    result = sms_provider.send_sms("+12015550100", "Qualified Commercial test")

    assert result.ok is True
    assert result.provider == "twilio"
    assert result.message_id == "SM123"
    assert captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert captured["auth"] == ("SK123", "secret")
    assert captured["data"] == {
        "To": "+12015550100",
        "Body": "Qualified Commercial test",
        "MessagingServiceSid": "MG123",
        "StatusCallback": "https://api.qualifiedcommercial.com/api/v1/webhooks/twilio/sms/status",
    }
