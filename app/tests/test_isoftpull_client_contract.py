from __future__ import annotations

import pytest

from app.services import isoftpull_client


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {
            "applicant_id": "provider-request-1",
            "reports": {
                "link": "https://app.isoftpull.com/client/applicants/view_only/1/soft_pull",
                "transunion": {
                    "status": "success",
                    "link": "https://app.isoftpull.com/report/1",
                },
            },
            "full_feed": {"credit_score": {"fico_8": {"score": 711}}},
            "intelligence": {"result": "passed", "name": "Soft pull"},
        }


class _Client:
    def __init__(self, captured: dict, **_kwargs) -> None:
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict):
        self.captured.update(url=url, json=json, headers=headers)
        return _Response()


@pytest.mark.asyncio
async def test_isoftpull_v2_request_contract_includes_confirmed_owner_data(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        isoftpull_client.httpx,
        "AsyncClient",
        lambda **kwargs: _Client(captured, **kwargs),
    )

    result = await isoftpull_client.pull(
        public_key="public-key",
        private_key="private-key",
        base_url="https://app.isoftpull.com/api/v2",
        applicant=isoftpull_client.ApplicantPayload(
            legal_first_name="Jonathan",
            legal_last_name="Franco",
            street="123 Main St",
            city="Garfield",
            state="NJ",
            zip="07026",
            dob="1995-05-09",
        ),
        max_retries=0,
    )

    assert captured["url"] == "https://app.isoftpull.com/api/v2/reports"
    assert captured["headers"]["api-key"] == "public-key"
    assert captured["headers"]["api-secret"] == "private-key"
    assert captured["json"] == {
        "first_name": "Jonathan",
        "last_name": "Franco",
        "address": "123 Main St",
        "city": "Garfield",
        "state": "New Jersey",
        "zip": "07026",
        "date_of_birth": "05/09/1995",
    }
    assert result.fico == 711
    assert result.provider_pull_id == "provider-request-1"
