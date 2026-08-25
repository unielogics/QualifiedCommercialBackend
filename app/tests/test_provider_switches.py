import base64
import hashlib
import hmac

from app.config import Settings
from app.dealer_os.services import sms_provider
from app.dealer_os.services.sms_provider import provider_readiness, validate_twilio_signature
from app.routers.analysis import _provider_switch_ready
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


def test_saving_geoapify_key_does_not_revalidate_unchanged_google_provider() -> None:
    assert _provider_switch_ready(
        current_provider="google",
        requested_provider="google",
        provider_status={"google_server_configured": False, "geoapify_configured": False},
        supplied_secrets={"geoapify_api_key"},
    )


def test_geoapify_key_can_be_saved_and_activated_atomically() -> None:
    assert _provider_switch_ready(
        current_provider="google",
        requested_provider="geoapify",
        provider_status={"google_server_configured": False, "geoapify_configured": False},
        supplied_secrets={"geoapify_api_key"},
    )


def test_provider_switch_still_requires_a_configured_target() -> None:
    assert not _provider_switch_ready(
        current_provider="google",
        requested_provider="geoapify",
        provider_status={"google_server_configured": False, "geoapify_configured": False},
        supplied_secrets=set(),
    )


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
