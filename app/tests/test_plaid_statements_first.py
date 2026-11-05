"""Plaid first for bank statements, and statement PDFs on for every file.

The owner (2026-11-04): "plaid needs to be the first option available to
upload bank statements and feed our system"; "pdf files should be enabled for
every file, however proper wiring needs to be in place to match what we do
with the pdf files or better"; "make sure that plaid or pdf upload of 6 months
can put the file on banking complete". And a fact the code could not know: the
production Plaid account is not enabled for the Statements product yet. So the
flag is a preference the deployment allowlist filters, the pulled files are
wired exactly like uploads, and the completion rule reads months from any
source.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.routers import application_profiles as router
from app.schemas.application_profile import RoomReminderCreate
from app.services import application_plaid_sync as sync
from app.services import application_profiles as profiles
from app.services import plaid_policy, provenance

# --- the preference versus the allowlist -----------------------------------------


def _owner(assets=True, statements=True):
    return SimpleNamespace(plaid_assets_enabled=assets, plaid_statements_enabled=statements)


def test_a_file_that_prefers_statements_gets_them_only_once_the_account_has_the_product(
    monkeypatch,
):
    monkeypatch.setattr(plaid_policy.plaid_client, "products", lambda: ["assets"])
    policy = plaid_policy.from_owner(_owner())
    assert policy.selected_products == ["assets"]
    assert policy.statements_enabled is False
    # Nothing to validate against: what is selected is always available.
    policy.validate()
    assert plaid_policy.preferred_products(_owner()) == ["assets", "statements"]

    monkeypatch.setattr(plaid_policy.plaid_client, "products", lambda: ["assets", "statements"])
    assert plaid_policy.from_owner(_owner()).selected_products == ["assets", "statements"]


def test_an_unreadable_allowlist_falls_back_to_assets(monkeypatch):
    def broken():
        raise plaid_policy.plaid_client.PlaidUnavailable("DEALER_OS_PLAID_PRODUCTS is bad")

    monkeypatch.setattr(plaid_policy.plaid_client, "products", broken)
    assert plaid_policy.deployment_products() == ["assets"]
    assert plaid_policy.from_owner(_owner()).selected_products == ["assets"]


def test_a_connected_bank_is_not_nagged_for_a_product_the_deployment_cannot_ask_for(monkeypatch):
    """Before: a file with statements on and an Assets-only deployment showed
    "client authorization required" forever and marked the item for update
    mode. The policy now never selects what the account cannot deliver."""
    monkeypatch.setattr(plaid_policy.plaid_client, "products", lambda: ["assets"])
    policy = plaid_policy.from_owner(_owner())
    item = SimpleNamespace(
        status="active",
        plaid_products=["assets"],
        plaid_unavailable_products=[],
        plaid_products_checked_at=object(),
    )
    assert plaid_policy.pending_products(item, policy) == []
    assert plaid_policy.authorization_state(item, policy) == "authorized"


def test_the_row_defaults_and_the_schema_default_say_statements_on():
    from app.dealer_os.models import DealerBusiness
    from app.models.application_profile import ApplicationProfile
    from app.schemas.application_profile import ApplicationProfileRead

    for model in (ApplicationProfile, DealerBusiness):
        column = model.__table__.columns["plaid_statements_enabled"]
        assert column.default.arg is True
        assert str(column.server_default.arg).strip("()").lower() == "true"
    assert ApplicationProfileRead.model_fields["plaid_statements_enabled"].default is True


# --- Plaid files are wired like uploads --------------------------------------------


def test_plaid_is_a_named_unattended_document_source():
    assert provenance.DOCUMENT_SOURCES["plaid"] == "Pulled from the bank through Plaid"
    assert "plaid" in provenance.UNATTENDED_SOURCES


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Result(self.rows)


def _slot(name, *, key=None, category=None, status="requested", signature=False, multiple=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        category=category,
        requirement_key=key,
        status=status,
        requires_signature=signature,
        allow_multiple_files=multiple,
        created_at=0,
    )


@pytest.mark.asyncio
async def test_the_statement_slot_is_the_six_month_requirement_first_then_any_open_bank_row():
    six = _slot("Last 6 months business bank statements", key="business_bank_statements_6_months")
    other = _slot("Bank Statements", category="Bank Statements")
    assert (await sync.bank_statement_slot(_DB([other, six]), uuid.uuid4())) is six
    assert (await sync.bank_statement_slot(_DB([other]), uuid.uuid4())) is other
    # A fallback row that takes one file is not stacked with six statements.
    one_file = _slot("Bank Statements", category="Bank Statements", multiple=False)
    assert (await sync.bank_statement_slot(_DB([one_file]), uuid.uuid4())) is None
    assert (
        await sync.bank_statement_slot(
            _DB([_slot("Tax returns", category="Tax Returns")]), uuid.uuid4()
        )
    ) is None


def test_the_asset_report_facts_read_like_a_statements():
    account_rows = [
        {
            "account": {
                "institution": "Chase",
                "name_hint": "Business Complete Checking",
                "mask": "4321",
                "kind_hint": "checking",
            },
            "months": [
                {
                    "month": "2026-05",
                    "total_deposits": 42000.0,
                    "total_withdrawals": 39000.0,
                    "beginning_balance": 5000.0,
                    "ending_balance": 8000.0,
                    "average_ledger_balance": 6500.0,
                    "low_daily_balance": 1200.0,
                    "nsf_count": 0,
                },
                {
                    "month": "2026-06",
                    "total_deposits": 45000.0,
                    "total_withdrawals": 41000.0,
                    "beginning_balance": 8000.0,
                    "ending_balance": 12000.0,
                    "average_ledger_balance": 9000.0,
                    "low_daily_balance": 3000.0,
                    "nsf_count": 1,
                },
            ],
        }
    ]
    facts = sync.asset_report_key_facts(account_rows, ["2026-05", "2026-06"])
    # What the account counter and the cockpit key an account on.
    assert facts["bank"] == "Chase" and facts["account_last4"] == "4321"
    # Months in the statement reader's words, with the figures.
    assert [row["month"] for row in facts["months"]] == ["2026-05", "2026-06"]
    assert facts["months"][0]["total_deposits_and_credits"] == 42000.0
    assert facts["months"][1]["nsf_or_overdraft_count"] == 1
    assert facts["months"][0]["account_last4"] == "4321" and facts["months"][0]["bank"] == "Chase"
    from app.services.bucket_evidence import statement_months_from_analysis

    assert statement_months_from_analysis({"key_facts": facts}) == {"2026-05", "2026-06"}


def test_a_multi_account_report_names_no_single_account_at_the_top():
    rows = [
        {"account": {"institution": "Chase", "mask": "1111"}, "months": [{"month": "2026-05"}]},
        {"account": {"institution": "Chase", "mask": "2222"}, "months": [{"month": "2026-05"}]},
    ]
    facts = sync.asset_report_key_facts(rows, ["2026-05"])
    assert facts["bank"] == "Chase" and "account_last4" not in facts
    assert {row["account_last4"] for row in facts["months"]} == {"1111", "2222"}


def test_an_empty_report_still_lists_its_observed_months():
    facts = sync.asset_report_key_facts([], ["2026-04"])
    assert facts["months"] == [{"month": "2026-04"}]


# --- the completion rule ----------------------------------------------------------


def test_six_months_from_any_source_or_a_connection_completes_banking():
    complete = profiles.banking_is_complete
    assert complete(connected=True, months=set(), override=False)
    assert complete(connected=False, months={f"2026-0{n}" for n in range(1, 7)}, override=False)
    assert not complete(connected=False, months={f"2026-0{n}" for n in range(1, 6)}, override=False)
    assert complete(connected=False, months=set(), override=True)
    assert profiles.REQUIRED_STATEMENT_MONTHS == 6


# --- the desk's request ----------------------------------------------------------------


def test_the_desk_can_ask_for_a_plaid_connection_by_name():
    assert RoomReminderCreate(purpose="bank_connect").purpose == "bank_connect"
    query, text = router._room_reminder_copy("bank_connect")
    assert query == "tab=banking"
    assert text.startswith("connect the business bank through Plaid")
    assert "six months of bank statements" in text
    # Short enough that the SMS stays at one or two segments.
    assert len(text) < 110
    assert router._room_reminder_copy("business_banking") == (
        "tab=banking",
        "connect or upload business bank statements",
    )
    assert router._room_reminder_copy("room")[0] == "tab=todo"


def test_the_mca_rooms_bank_card_opens_on_banking():
    import inspect

    from app.routers.dealer_ai_intake_routes import mca_refinance

    source = inspect.getsource(mca_refinance)
    assert 'tab="banking"' in source
    assert source.count('_secure_room_handoff(db, intake, request, tab="todo")') == 0
