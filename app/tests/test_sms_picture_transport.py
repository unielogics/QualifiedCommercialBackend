"""Picture transport uses the chosen provider without losing media or text."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.dealer_os.services import sms_provider as sms


def settings(provider="twilio", **changes):
    return SimpleNamespace(**{
        "sms_provider": provider, "sms_production": True, "sms_origination_number": "+15555550100",
        "twilio_account_sid": "account", "twilio_auth_token": "auth", "twilio_api_key_sid": "",
        "twilio_api_key_secret": "", "twilio_messaging_service_sid": "service", "twilio_from_number": "",
        "public_api_url": "https://api.example.test", "relay_sms_url": "https://relay.example.test",
        "relay_auth_token": "relay", **changes,
    })


@pytest.mark.parametrize("provider,mode", [("twilio", "mms"), ("android", "secure_link"), ("aws", "secure_link"), ("unknown", "unavailable")])
def test_mode_respects_selected_transport(provider, mode):
    assert sms.image_delivery_mode(settings(provider)) == mode


def test_twilio_encodes_all_images_with_image_only_message(monkeypatch):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(201, json={"sid": "message", "status": "queued"})

    original = httpx.Client
    monkeypatch.setattr(sms.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(sms, "get_settings", lambda: settings())
    result = sms.send_sms("+15555550101", "", media_urls=["https://images.example.test/a", "https://images.example.test/b"])
    assert result.ok and result.provider == "twilio" and result.message_id == "message"
    from urllib.parse import parse_qs

    fields = parse_qs(captured[0].content.decode(), keep_blank_values=True)
    assert fields["MediaUrl"] == ["https://images.example.test/a", "https://images.example.test/b"]
    assert fields["Body"] == [""] and fields["MessagingServiceSid"] == ["service"]
    assert fields["StatusCallback"] == ["https://api.example.test/api/v1/webhooks/twilio/sms/status"]


@pytest.mark.parametrize("provider", ["android", "aws"])
def test_text_only_transport_never_silently_drops_pictures(monkeypatch, provider):
    monkeypatch.setattr(sms, "get_settings", lambda: settings(provider))
    transport = Mock()
    monkeypatch.setattr(sms, f"_send_{provider}", transport)
    result = sms.send_sms("+15555550101", "caption", media_urls=["https://images.example.test/a"])
    assert not result.ok and "private image links" in result.detail
    transport.assert_not_called()


@pytest.mark.parametrize("urls", [["http://images.example.test/a"], ["https://images.example.test/a"] * 11])
def test_invalid_media_does_not_reach_transport(monkeypatch, urls):
    monkeypatch.setattr(sms, "get_settings", lambda: settings())
    transport = Mock()
    monkeypatch.setattr(sms, "_send_twilio", transport)
    assert not sms.send_sms("+15555550101", "caption", media_urls=urls).ok
    transport.assert_not_called()


def test_plain_sms_transport_contract_is_unchanged(monkeypatch):
    monkeypatch.setattr(sms, "get_settings", lambda: settings())
    transport = Mock(return_value=sms.SmsSendResult(True, "twilio", "accepted"))
    monkeypatch.setattr(sms, "_send_twilio", transport)
    assert sms.send_sms("+15555550101", "text").ok
    transport.assert_called_once_with("+15555550101", "text", sms.get_settings())
