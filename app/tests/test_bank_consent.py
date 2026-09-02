"""The gate must be closed by default and must precede credential entry."""
from __future__ import annotations

import uuid
from datetime import UTC

import pytest

from app.dealer_os.services import bank_consent


class _DB:
    def __init__(self, row=None):
        self.row, self.added = row, []
    async def execute(self, _s):
        from types import SimpleNamespace
        r = self.row
        return SimpleNamespace(scalar_one_or_none=lambda: r)
    def add(self, o): self.added.append(o)
    async def flush(self): pass


@pytest.mark.asyncio
async def test_no_consent_means_no_bank_connection():
    assert await bank_consent.has_consent(_DB(None), uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_a_revoked_consent_does_not_count():
    from datetime import datetime
    from types import SimpleNamespace
    row = SimpleNamespace(granted=True, revoked_at=datetime.now(UTC),
                          disclosure_version="v", created_at=None, consenter_name="x")
    assert await bank_consent.has_consent(_DB(row), uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_assets_require_consent_to_the_current_disclosure():
    from types import SimpleNamespace

    row = SimpleNamespace(
        granted=True,
        revoked_at=None,
        disclosure_version="statements-only-v1",
        created_at=None,
        consenter_name="Client",
        product_scope=["assets"],
    )
    assert await bank_consent.has_consent(_DB(row), uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_the_stored_text_is_the_served_text():
    """The record must hash what was on screen, not what a client sent."""
    db = _DB(None)
    row = await bank_consent.record(db, dealer_id=uuid.uuid4(), method="self_web",
        consenter_name="Jane", ip_address="1.2.3.4", user_agent="UA")
    d = bank_consent.disclosure()
    assert row.disclosure_text == d["text"]
    assert row.disclosure_hash == d["hash"]
    assert row.ip_address == "1.2.3.4"
    assert row.product_scope == ["assets"]


@pytest.mark.asyncio
async def test_expanding_to_statements_requires_new_consent():
    from types import SimpleNamespace

    row = SimpleNamespace(
        granted=True,
        revoked_at=None,
        disclosure_version=bank_consent.BANK_DISCLOSURE_VERSION,
        created_at=None,
        consenter_name="Client",
        product_scope=["assets"],
    )
    db = _DB(row)
    assert await bank_consent.has_consent(db, uuid.uuid4(), ["assets"]) is True
    assert await bank_consent.has_consent(
        db, uuid.uuid4(), ["assets", "statements"]
    ) is False


@pytest.mark.asyncio
async def test_an_unknown_method_is_refused():
    with pytest.raises(ValueError):
        await bank_consent.record(_DB(None), dealer_id=uuid.uuid4(), method="hand_wave",
            consenter_name=None, ip_address=None, user_agent=None)


def test_the_disclosure_states_the_facts_the_msa_requires():
    t = bank_consent.disclosure()["text"]
    assert "Plaid" in t
    assert "plaid.com/legal/#end-user-privacy-policy" in t
    assert "does not receive or store those credentials" in t
    assert "read-only" in t
    assert "withdraw" in t


def test_the_bank_version_is_independent_of_the_sms_one():
    from app.dealer_os.services import sms_consent
    assert bank_consent.BANK_DISCLOSURE_VERSION is not getattr(
        sms_consent, "SMS_DISCLOSURE_VERSION", object())
