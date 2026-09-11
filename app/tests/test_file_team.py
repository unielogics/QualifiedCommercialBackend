"""Who is on a file.

The agent seat is derived from the ownership pointers the system already
keeps, in a fixed order, and moves with the reassign actions that exist; the
house is never a company; underwriters and the company are the desk's; a
seat opens the roster and the timeline and nothing else.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.file_team_member import SEAT_AGENT, SEAT_UNDERWRITER
from app.models.referral_partner_company import KIND_HOUSE, KIND_REFERRAL_PARTNER
from app.services import file_team


def _run(coro):
    return asyncio.run(coro)


def _user(role: Role, *, company_id=None, deleted=False, name="Person"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, name=name, email=f"{name.lower()}@example.com", deleted_at=("gone" if deleted else None), referral_partner_company_id=company_id)


def _profile(**kw):
    base = dict(id=uuid.uuid4(), intake_id=None, dealer_id=None, client_id=None, loan_id=None, deal_id=None, primary_bucket_id=None, company_id=None, company_set_by_user_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


class _Db:
    """db.get by (model name, id); execute() returns queued rows."""

    def __init__(self, objects=(), rows=()):
        self.objects = {(type(o).__name__ if not isinstance(o, SimpleNamespace) else o.kind, o.id): o for o in objects}
        self.rows = list(rows)
        self.added = []
        self.deleted = []

    async def get(self, model, key):
        return self.objects.get((model.__name__, key))

    async def execute(self, stmt):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows), scalar_one_or_none=lambda: (rows[0] if rows else None))

    async def flush(self):
        return None

    def add(self, row):
        self.added.append(row)

    async def delete(self, row):
        self.deleted.append(row)


def _obj(kind, **fields):
    return SimpleNamespace(kind=kind, id=fields.pop("id", uuid.uuid4()), **fields)


def _db_with(*objects, rows=()):
    db = _Db(rows=rows)
    for o in objects:
        db.objects[(o.kind, o.id)] = o
    return db


# ── derivation ──────────────────────────────────────────────────────────────


def test_agent_is_derived_in_the_documented_order():
    partner = _user(Role.DEALER_PARTNER, name="Partner")
    rep = _user(Role.FIELD_REP, name="Rep")
    agent = _user(Role.BROKER, name="Agent")
    broker_user = _user(Role.BROKER, name="BrokerUser")
    creator = _user(Role.BROKER, name="Creator")
    users = [_obj("User", id=u.id, **{k: v for k, v in vars(u).items() if k != "id"}) for u in (partner, rep, agent, broker_user, creator)]

    intake = _obj("PublicUnderwritingIntake", broker_id=partner.id, source_user_id=creator.id, client_id=None)
    dealer = _obj("DealerBusiness", owner_user_id=rep.id)
    client = _obj("Client", current_agent_id=agent.id, broker_id=uuid.uuid4())
    broker = _obj("Broker", id=client.broker_id, user_id=broker_user.id)
    loan = _obj("Loan", broker_id=None)

    db = _db_with(*users, intake, dealer, client, broker, loan)
    profile = _profile(intake_id=intake.id, dealer_id=dealer.id, client_id=client.id, loan_id=loan.id)
    assert _run(file_team.derive_agent(db, profile)) == (partner.id, "intake.broker_id")

    intake.broker_id = None
    assert _run(file_team.derive_agent(db, profile)) == (rep.id, "dealer.owner_user_id")
    dealer.owner_user_id = None
    assert _run(file_team.derive_agent(db, profile)) == (agent.id, "client.current_agent_id")
    client.current_agent_id = None
    assert _run(file_team.derive_agent(db, profile)) == (broker_user.id, "client.broker_id")
    client.broker_id = None
    assert _run(file_team.derive_agent(db, profile)) == (creator.id, "intake.source_user_id")
    # A creator who is not an agent role is not the agent.
    db.objects[("User", creator.id)].role = Role.LOAN_EXEC
    assert _run(file_team.derive_agent(db, profile)) == (None, None)


def test_a_deleted_pointer_target_is_skipped():
    gone = _user(Role.DEALER_PARTNER, name="Gone", deleted=True)
    rep = _user(Role.FIELD_REP, name="Rep")
    intake = _obj("PublicUnderwritingIntake", broker_id=gone.id, source_user_id=None, client_id=None)
    dealer = _obj("DealerBusiness", owner_user_id=rep.id)
    db = _db_with(_obj("User", id=gone.id, **{k: v for k, v in vars(gone).items() if k != "id"}), _obj("User", id=rep.id, **{k: v for k, v in vars(rep).items() if k != "id"}), intake, dealer)
    profile = _profile(intake_id=intake.id, dealer_id=dealer.id)
    assert _run(file_team.derive_agent(db, profile)) == (rep.id, "dealer.owner_user_id")


def test_the_house_is_never_the_company_on_a_file():
    house = _obj("ReferralPartnerCompany", kind_=KIND_HOUSE, name="Qualified Commercial LLC", notice_email=None)
    house.kind = KIND_HOUSE
    partner_co = _obj("ReferralPartnerCompany", name="Acme Referrals", notice_email="legal@acme.example")
    partner_co.kind = KIND_REFERRAL_PARTNER
    staff = _user(Role.FIELD_REP, company_id=house.id, name="Staff")
    partner = _user(Role.DEALER_PARTNER, company_id=partner_co.id, name="Partner")
    db = _db_with(house, partner_co, _obj("User", id=staff.id, **{k: v for k, v in vars(staff).items() if k != "id"}), _obj("User", id=partner.id, **{k: v for k, v in vars(partner).items() if k != "id"}))
    # ReferralPartnerCompany objects are keyed by kind_ attr collision workaround: register under the model name
    db.objects[("ReferralPartnerCompany", house.id)] = house
    db.objects[("ReferralPartnerCompany", partner_co.id)] = partner_co
    assert _run(file_team.derive_company(db, staff.id)) is None
    assert _run(file_team.derive_company(db, partner.id)) is partner_co
    assert _run(file_team.derive_company(db, None)) is None


def test_team_for_persists_a_derived_seat_only_when_asked_and_a_desk_set_company_survives():
    partner_co = _obj("ReferralPartnerCompany", name="Acme", notice_email=None)
    partner_co.kind = KIND_REFERRAL_PARTNER
    partner = _user(Role.DEALER_PARTNER, company_id=partner_co.id, name="Partner")
    intake = _obj("PublicUnderwritingIntake", broker_id=partner.id, source_user_id=None, client_id=None)
    db = _db_with(_obj("User", id=partner.id, **{k: v for k, v in vars(partner).items() if k != "id"}), intake)
    db.objects[("ReferralPartnerCompany", partner_co.id)] = partner_co
    profile = _profile(intake_id=intake.id)

    team = _run(file_team.team_for(db, profile))
    assert team.agent.user_id == partner.id and team.agent.derived_from == "intake.broker_id"
    assert team.company.id == partner_co.id and team.company.derived is True
    assert db.added == [] and profile.company_id is None

    team = _run(file_team.team_for(db, profile, persist=True))
    assert len(db.added) == 1 and db.added[0].seat == SEAT_AGENT and db.added[0].derived_from == "intake.broker_id"
    assert profile.company_id == partner_co.id

    # The desk chose a different company; a later derivation never overwrites it.
    other = _obj("ReferralPartnerCompany", name="Other", notice_email=None)
    other.kind = KIND_REFERRAL_PARTNER
    db.objects[("ReferralPartnerCompany", other.id)] = other
    profile.company_id, profile.company_set_by_user_id = other.id, uuid.uuid4()
    team = _run(file_team.team_for(db, profile, persist=True))
    assert team.company.id == other.id and team.company.derived is False


def test_underwriter_must_be_a_desk_role_and_the_house_cannot_be_set_as_the_company():
    broker = _user(Role.BROKER, name="Broker")
    actor = _user(Role.SUPER_ADMIN, name="Desk")
    db = _db_with(_obj("User", id=broker.id, **{k: v for k, v in vars(broker).items() if k != "id"}))
    with pytest.raises(HTTPException) as err:
        _run(file_team.add_underwriter(db, _profile(), broker.id, actor))
    assert err.value.status_code == 400
    house = _obj("ReferralPartnerCompany", name="Qualified Commercial LLC", notice_email=None)
    house.kind = KIND_HOUSE
    db.objects[("ReferralPartnerCompany", house.id)] = house
    with pytest.raises(HTTPException) as err:
        _run(file_team.set_company(db, _profile(), house.id, actor))
    assert err.value.status_code == 400


def test_adding_an_underwriter_writes_the_seat_logs_and_emits_once():
    uw = _user(Role.LOAN_EXEC, name="Uw")
    actor = _user(Role.SUPER_ADMIN, name="Desk")
    db = _db_with(_obj("User", id=uw.id, **{k: v for k, v in vars(uw).items() if k != "id"}))
    profile = _profile()
    with patch.object(file_team.profiles, "log_profile_action", AsyncMock()) as log, patch("app.services.file_events.emit", AsyncMock()) as emit, patch.object(file_team, "team_for", AsyncMock(return_value=file_team.Team())):
        _run(file_team.add_underwriter(db, profile, uw.id, actor))
    assert len(db.added) == 1 and db.added[0].seat == SEAT_UNDERWRITER and db.added[0].assigned_by_user_id == actor.id
    assert log.await_args.args[3] == "file_team.underwriter_added"
    assert emit.await_args.kwargs["kind"] == "team.changed" and emit.await_args.kwargs["visibility"] == "team"


def test_adding_an_agent_keeps_a_separate_collaborator_seat():
    agent = _user(Role.FIELD_REP, name="Second Agent")
    actor = _user(Role.SUPER_ADMIN, name="Desk")
    db = _db_with(_obj("User", id=agent.id, **{k: v for k, v in vars(agent).items() if k != "id"}))
    profile = _profile()
    with patch.object(file_team.profiles, "log_profile_action", AsyncMock()) as audit, patch(
        "app.services.file_events.emit", AsyncMock()
    ) as emit, patch.object(file_team, "team_for", AsyncMock(return_value=file_team.Team())):
        _run(file_team.add_agent(db, profile, agent.id, actor))

    assert len(db.added) == 1
    assert db.added[0].seat == SEAT_AGENT
    assert db.added[0].derived_from is None
    assert db.added[0].assigned_by_user_id == actor.id
    assert audit.await_args.args[3] == "file_team.agent_added"
    assert emit.await_args.kwargs["meta"]["seat"] == SEAT_AGENT


def test_refreshing_primary_agent_preserves_manually_assigned_agents():
    manual_id = uuid.uuid4()
    primary_id = uuid.uuid4()
    manual = SimpleNamespace(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        user_id=manual_id,
        seat=SEAT_AGENT,
        derived_from=None,
    )
    db = _Db(rows=[manual])
    profile = _profile()
    with patch.object(file_team, "derive_agent", AsyncMock(return_value=(primary_id, "client.current_agent_id"))), patch.object(
        file_team, "derive_company", AsyncMock(return_value=None)
    ), patch.object(file_team, "_live_user", AsyncMock(return_value=None)), patch.object(
        file_team, "_emit_team_changed", AsyncMock()
    ):
        before, after = _run(file_team.refresh_agent_seat(db, profile))

    assert before is None and after == primary_id
    assert manual not in db.deleted
    assert len(db.added) == 1 and db.added[0].user_id == primary_id


def test_ownership_derived_primary_agent_cannot_be_removed_directly():
    actor = _user(Role.SUPER_ADMIN, name="Desk")
    derived = SimpleNamespace(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        seat=SEAT_AGENT,
        derived_from="intake.broker_id",
    )
    db = _Db(rows=[derived])
    with pytest.raises(HTTPException) as err:
        _run(file_team.remove_agent(db, _profile(), derived.user_id, actor))
    assert err.value.status_code == 409
    assert db.deleted == []


def test_seat_or_visible_admits_a_seat_and_never_a_vendor():
    profile = _profile()
    seated = _user(Role.BROKER, name="Seated")
    vendor = _user(Role.VENDOR, name="Vendor")
    with patch.object(file_team.profiles, "_profile_is_visible", AsyncMock(return_value=False)), patch.object(file_team, "holds_seat", AsyncMock(return_value=True)):
        assert _run(file_team.seat_or_visible(None, profile, seated)) is True
        assert _run(file_team.seat_or_visible(None, profile, vendor)) is False
    with patch.object(file_team.profiles, "_profile_is_visible", AsyncMock(return_value=True)):
        assert _run(file_team.seat_or_visible(None, profile, vendor)) is True


def test_the_client_shape_of_the_roster_carries_no_email_and_no_underwriters():
    primary = file_team.Member(user_id=uuid.uuid4(), name="Agent", email="a@example.com", role="broker", seat=SEAT_AGENT, derived_from="client.current_agent_id")
    collaborator = file_team.Member(user_id=uuid.uuid4(), name="Agent Two", email="b@example.com", role="field_rep", seat=SEAT_AGENT)
    team = file_team.Team(
        agent=primary,
        agents=[primary, collaborator],
        underwriters=[file_team.Member(user_id=uuid.uuid4(), name="Uw", email="u@example.com", role="loan_exec", seat=SEAT_UNDERWRITER)],
        company=file_team.CompanyRef(id=uuid.uuid4(), name="Acme", kind=KIND_REFERRAL_PARTNER, notice_email="x@acme.example", derived=True),
    )
    client_view = file_team.team_read(team, for_client=True)
    assert client_view == {
        "agent": {"name": "Agent"},
        "agents": [{"name": "Agent"}, {"name": "Agent Two"}],
        "underwriters": [],
        "company": None,
    }
    desk_view = file_team.team_read(team, for_client=False)
    assert desk_view["agent"]["email"] == "a@example.com" and len(desk_view["agents"]) == 2
    assert len(desk_view["underwriters"]) == 1 and desk_view["company"]["name"] == "Acme"
    assert team.user_ids() == {primary.user_id, collaborator.user_id, team.underwriters[0].user_id}
    assert "notice_email" not in repr(desk_view)


def test_the_three_reassign_actions_refresh_the_agent_seat():
    import inspect

    from app.routers import clients, dealer_ai_intake, loans

    for handler in (loans.update_loan, clients.reassign_agent, dealer_ai_intake.assign_lead_partner):
        assert "refresh_agent_seat" in inspect.getsource(handler), handler.__name__
