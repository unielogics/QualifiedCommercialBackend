from types import SimpleNamespace

import pytest

from app.routers.dealer_ai_intake import PublicIntakeAttribution, _public_intake_attribution
from app.services.intake_source import safe_source_url, source_for_intake


def test_source_retains_campaign_but_never_access_credentials():
    url = "https://app.qualifiedcommercial.com/mca-refinance-intake?token=private&source=public_site&page=%2Fmca-refinance&email=private%40example.com#secret"
    payload = PublicIntakeAttribution(entry_url=url, page="/mca-refinance")
    saved = _public_intake_attribution(payload)
    assert "entry_url" in saved
    assert "token" not in saved["entry_url"] and "private" not in saved["entry_url"]
    assert "source=public_site" in saved["entry_url"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://evil.example/mca", "https://qualifiedcommercial.com.evil.example", "https://user:pass@qualifiedcommercial.com/mca", "https://app.qualifiedcommercial.com/buckets/request/private"])
def test_unsafe_or_credential_urls_are_not_retained(url):
    assert safe_source_url(url) is None


def test_existing_attribution_resolves_to_the_marketing_page():
    row = SimpleNamespace(variant="mca_refi_v1", intake_state={"signup_attribution": {"page": "/mca-refinance", "campaign": "mca_refinance"}})
    result = source_for_intake(row)
    assert result["recorded"] and result["marketing_url"] == "https://qualifiedcommercial.com/mca-refinance"


def test_historical_fallback_is_not_presented_as_a_known_original_source():
    result = source_for_intake(SimpleNamespace(variant="mca_refi_v1", intake_state={}))
    assert result["recorded"] is False and result["entry_url"] is None
    assert result["marketing_url"].endswith("/mca-refinance")
