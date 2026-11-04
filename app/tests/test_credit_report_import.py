from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services import credit_report_import as imports
from app.services.isoftpull_report_parser import parse_full_feed, parse_report_html


def test_fico_four_is_selected_from_html_and_legacy_saved_report():
    html = '''<div id="credit_score"><div class="div-row">
      <div class="pad10 w-auto"><span>Score Model:</span><strong>FICO 4</strong></div>
      <div class="pad10 w-auto"><span>Score:</span><strong>661</strong></div>
    </div></div>'''
    report = parse_report_html(html)
    assert report.best_score == 661
    assert report.best_score_model == "fico_4"
    legacy = report.to_dict()
    legacy.pop("fico_4")
    legacy["best_score"] = None
    assert imports.stored_report({"parsed_report": legacy})["best_score"] == 661


def test_full_feed_import_includes_scores_and_report_sections():
    report = parse_full_feed({"full_feed": {
        "credit_score": {"fico_4": {"score": "661"}, "fico_8": {"score": "NA"}},
        "personal_information": {"first_name": "Test"},
        "address_history": {"current": [{"city": "Example"}]},
        "trade_accounts": [{"company": "Test creditor", "balance": 100}],
        "inquiries": [{"company": "Test inquiry"}],
        "collections": [{"balance": "20"}], "public_record": [{"type": "record"}],
    }})
    assert report.best_score == 661
    assert report.trade_accounts[0].fields["balance"] == "100"
    assert len(report.addresses) == len(report.inquiries) == len(report.collections) == len(report.public_records) == 1


def test_missing_report_is_not_misreported_as_bureau_no_score():
    from app.services.credit_pull_core import credit_status_extras
    result = credit_status_extras(SimpleNamespace(fico=None, notes=None, bureau_response={"reports": {}}))
    assert "not been imported" in result["attention"]
    assert not result["score_verified"]


async def test_import_updates_linked_owners_without_another_bureau_pull(monkeypatch):
    from app.services import isoftpull_client
    new_pull = AsyncMock(side_effect=AssertionError("must not create a new credit inquiry"))
    monkeypatch.setattr(isoftpull_client, "pull", new_pull)
    pull = SimpleNamespace(id=uuid4(), client_id=uuid4(), fico=None, notes="isoftpull_id=test",
        pulled_at=datetime(2026, 11, 3, tzinfo=UTC), expires_at=None,
        bureau_response={"full_feed": {"credit_score": {"fico_4": {"score": 661}}}})
    owner = SimpleNamespace(credit_score=None, credit_tier=None, credit_summary={"linked_how": "manual"})
    client = SimpleNamespace(fico=None)
    rows = Mock()
    rows.all.return_value = [owner]
    db = SimpleNamespace(flush=AsyncMock(), scalars=AsyncMock(return_value=rows),
        scalar=AsyncMock(return_value=pull.id), get=AsyncMock(return_value=client))
    parsed = await imports.import_existing_report(db, pull)
    assert parsed["best_score"] == pull.fico == owner.credit_score == client.fico == 661
    assert owner.credit_summary["linked_how"] == "manual"
    assert owner.credit_summary["score_verified"]
    assert pull.pulled_at == datetime(2026, 11, 3, tzinfo=UTC)
    new_pull.assert_not_awaited()


async def test_missing_credentials_preserves_the_existing_pull(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(isoftpull_login_email="", isoftpull_login_password=""))
    pull = SimpleNamespace(bureau_response={"reports": {}}, fico=None)
    with pytest.raises(imports.ReportImportUnavailable, match="No new credit pull"):
        await imports.import_existing_report(SimpleNamespace(), pull)
    assert pull.fico is None


async def test_borrower_cannot_import_report_before_profile_lookup():
    from fastapi import HTTPException

    from app.routers.application_profiles import import_owner_credit_report
    db = SimpleNamespace(get=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await import_owner_credit_report(uuid4(), uuid4(), SimpleNamespace(role="client"), db)
    assert error.value.status_code == 403
    db.get.assert_not_awaited()


async def test_provider_terms_block_is_explicit_and_preserves_report(monkeypatch):
    import httpx

    from app.services.isoftpull_session import IsoftpullSession, IsoftpullTermsRequired

    session = IsoftpullSession()
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(403, json={"error": "terms_not_accepted"})))
    monkeypatch.setattr(session, "get", AsyncMock(return_value=client))
    with pytest.raises(IsoftpullTermsRequired, match="Terms Acceptance"):
        await session.fetch("https://app.isoftpull.com/client/reports/view_only/test")


async def test_import_requires_access_to_the_specific_profile(monkeypatch):
    from fastapi import HTTPException

    from app.routers import application_profiles as routes

    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(side_effect=HTTPException(404, "Not found")))
    db = SimpleNamespace(get=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await routes.import_owner_credit_report(uuid4(), uuid4(), SimpleNamespace(role="broker"), db)
    assert error.value.status_code == 404
    db.get.assert_not_awaited()
