"""A client list is read as stored; one unfamiliar value must not empty it.

`GET /clients` answered 500 on every call from at least 6 November. Public
sign-up stamps a client's `lead_source` with the page the person came from
("public_site", "equipment_page"), the read model insisted on the CRM's own
list of sources, and a response is validated row by row: one such client and
nobody's list loaded. The Clients page showed nobody at all, which read as
"files are not creating clients".

A write is still held to the vocabularies. A read reports what the row holds.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.enums import Role
from app.models.client import Client
from app.routers import clients as clients_router
from app.routers.auth import SignupAttribution, _persist_signup_attribution
from app.schemas.client import ClientCreate, ClientRead, ClientUpdate
from app.tests._sqlite_schema import sqlite_session, sqlite_template

DESK = SimpleNamespace(id=uuid.uuid4(), role=Role.SUPER_ADMIN, client=None, broker=None)


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "client_read")


@pytest.fixture
async def db(template_database, tmp_path):
    async with sqlite_session(template_database, tmp_path) as session:
        yield session


async def test_the_list_loads_when_a_client_came_from_the_public_site(db) -> None:
    signed_up = Client(name="Avery Public", email="avery@example.com")
    # Exactly what the sign-up route does to the record.
    _persist_signup_attribution(
        signed_up,
        SignupAttribution(page="/equipment-financing"),
        SimpleNamespace(headers={}, client=None),
    )
    assert signed_up.lead_source == "public_site"
    db.add_all([signed_up, Client(name="Blake Desk", email="blake@example.com", lead_source="referral")])
    await db.flush()

    listed = await clients_router.list_clients(DESK, db)

    assert [(row.name, row.lead_source) for row in listed] == [
        ("Avery Public", "public_site"),
        ("Blake Desk", "referral"),
    ]


async def test_one_client_opens_whatever_its_stored_values(db) -> None:
    odd = Client(
        name="Casey Odd",
        lead_source="equipment_page",
        lead_temperature="lukewarm",
        financing_support_needed="later",
        contact_permission="ask_first",
        relationship_context="walk_in",
        lead_promotion_status="parked",
        client_type="buyer_and_seller",
        client_experience_mode="concierge",
        client_experience_mode_locked_by="funding_team",
    )
    db.add(odd)
    await db.flush()

    read = await clients_router.get_client(odd.id, DESK, db)

    assert read.lead_source == "equipment_page"
    assert read.lead_temperature == "lukewarm"
    assert read.financing_support_needed == "later"
    assert read.contact_permission == "ask_first"
    assert read.relationship_context == "walk_in"
    assert read.lead_promotion_status == "parked"
    assert read.client_type == "buyer_and_seller"
    assert read.client_experience_mode == "concierge"
    assert read.client_experience_mode_locked_by == "funding_team"


def test_a_plain_record_still_reads_with_its_defaults() -> None:
    read = ClientRead.model_validate(
        SimpleNamespace(
            id=uuid.uuid4(), user_id=None, broker_id=None, name="Dana Plain", email=None, phone=None,
            city=None, since=None, tier="standard", fico=None, avatar_color=None, funded_total=0, funded_count=0,
        )
    )
    assert read.stage == "lead"
    assert read.lead_promotion_status == "not_ready"
    assert read.lead_source is None


def test_a_write_is_still_held_to_the_vocabularies() -> None:
    for field, value in [
        ("lead_source", "public_site"),
        ("lead_temperature", "lukewarm"),
        ("contact_permission", "ask_first"),
        ("client_type", "buyer_and_seller"),
        ("client_experience_mode_locked_by", "funding_team"),
    ]:
        with pytest.raises(ValidationError):
            ClientUpdate(**{field: value})
    with pytest.raises(ValidationError):
        ClientCreate(name="Erin New", lead_source="public_site")
    assert ClientUpdate(lead_source="referral").lead_source == "referral"
    assert ClientCreate(name="Erin New", lead_source="other").lead_source == "other"
