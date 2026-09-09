"""The sponsor defaults from the agent's linked business relationship profile.

The owner's rule: "the agent and every employee should be linked to a business
relationship profile; whatever that account is, is what it defaults to. Super
admin and underwriting can override that." Read strictly: the house is what
internal staff are linked to and is transparent; an unsigned linked company
stops the walk rather than letting a later candidate attribute the deal to
someone else; a sponsor already on the package — confirmed or not — is never
replaced by a default.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.referral_partner_company import (
    HOUSE_COMPANY_NAME,
    KIND_HOUSE,
    KIND_REFERRAL_PARTNER,
)
from app.services import production_arrangement as pa
from app.services import production_packages as pkgs
from app.services import production_prefill as prefill


def _company(name, *, kind=KIND_REFERRAL_PARTNER, **over):
    row = SimpleNamespace(id=uuid4(), name=name, kind=kind, entity_type="Limited liability company",
                          state_of_formation="NJ", principal_address="12 Harbor Rd", notice_email=None,
                          notice_attention=None, notice_address=None, platform_name="Endurance",
                          signatory_name=None, signatory_title=None, phone=None)
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _person(name, company, role=Role.FIELD_REP):
    return SimpleNamespace(id=uuid4(), name=name, email=f"{name.split()[0].lower()}@example.com", role=role,
                           deleted_at=None, referral_partner_company_id=company.id if company else None)


class _Db:
    """get() dispatches on the row id; execute() answers the few selects the walk makes."""

    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}
        self.flush = AsyncMock()
        self.add = lambda row: None

    async def get(self, _model, key, **_kw):
        return self.rows.get(key)

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: SimpleNamespace(all=lambda: []))


def _signed(*companies):
    """Patch the RPA lookup: these companies signed, everyone else did not."""
    ids = {c.id for c in companies}

    async def latest(_db, company_id):
        return SimpleNamespace(id=uuid4(), field_values={}) if company_id in ids else None

    return patch.object(pkgs, "_latest_rpa", latest)


# --- the walk -------------------------------------------------------------------


async def test_default_sponsor_follows_the_rm_then_the_files_agent_then_the_creator():
    acme, beta, gamma = _company("Acme Warranty"), _company("Beta Admin"), _company("Gamma Sales")
    rm, agent, creator = _person("Rita Moss", acme), _person("Andy Gale", beta, Role.DEALER_PARTNER), _person("Cleo Desk", gamma, Role.LOAN_EXEC)
    intake = SimpleNamespace(id=uuid4(), broker_id=agent.id)
    profile = SimpleNamespace(id=uuid4(), intake_id=intake.id, dealer_id=None)
    db = _Db([acme, beta, gamma, rm, agent, creator, intake])
    with _signed(acme, beta, gamma):
        d = await pkgs.default_sponsor_company_id(db, profile, {"rm_user_id": str(rm.id)}, creator)
        assert d and d.via == "rm" and d.company_id == acme.id and d.person_name == "Rita Moss" and d.signed
        d = await pkgs.default_sponsor_company_id(db, profile, {}, creator)
        assert d and d.via == "agent" and d.company_id == beta.id
        profile.intake_id = None
        d = await pkgs.default_sponsor_company_id(db, profile, {}, creator)
        assert d and d.via == "creator" and d.company_id == gamma.id
        assert await pkgs.default_sponsor_company_id(db, profile, {}, None) is None


async def test_a_rep_owned_file_defaults_from_the_dealers_owner():
    acme = _company("Acme Warranty")
    rep = _person("Rita Moss", acme)
    dealer = SimpleNamespace(id=uuid4(), owner_user_id=rep.id)
    profile = SimpleNamespace(id=uuid4(), intake_id=None, dealer_id=dealer.id)
    db = _Db([acme, rep, dealer])
    with _signed(acme):
        d = await pkgs.default_sponsor_company_id(db, profile, {}, None)
    assert d and d.via == "agent" and d.company_id == acme.id


async def test_the_house_is_transparent_and_never_a_sponsor():
    house = _company(HOUSE_COMPANY_NAME, kind=KIND_HOUSE)
    beta = _company("Beta Admin")
    rm = _person("Cleo Desk", house, Role.LOAN_EXEC)
    agent = _person("Andy Gale", beta, Role.DEALER_PARTNER)
    intake = SimpleNamespace(id=uuid4(), broker_id=agent.id)
    profile = SimpleNamespace(id=uuid4(), intake_id=intake.id, dealer_id=None)
    db = _Db([house, beta, rm, agent, intake])
    with _signed(beta):
        d = await pkgs.default_sponsor_company_id(db, profile, {"rm_user_id": str(rm.id)}, None)
    # The house-linked manager is skipped, not returned; the agent's company decides.
    assert d and d.via == "agent" and d.company_id == beta.id
    # Everyone house-linked: no default at all, no error.
    with _signed(beta):
        assert await pkgs.default_sponsor_company_id(db, SimpleNamespace(id=uuid4(), intake_id=None, dealer_id=None), {"rm_user_id": str(rm.id)}, rm) is None
    # The house can never be chosen, by the desk or by a default.
    option = SimpleNamespace(company_id=house.id, name=house.name, kind=KIND_HOUSE, has_agreement=False, state_of_formation=None,
                             entity_type=None, principal_address=None, notice_email=None, platform_name=None)
    package = SimpleNamespace(arrangement={}, prefill_provenance={}, sponsor_company_id=None)
    access = SimpleNamespace(package=package, user=None)
    with patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=option)), pytest.raises(HTTPException) as err:
        await pkgs._apply_sponsor(db, access, house.id)
    assert err.value.status_code == 409 and err.value.detail["code"] == "house_is_not_a_sponsor"
    with pytest.raises(HTTPException) as err:
        await pkgs.require_signed_sponsor(db, SimpleNamespace(sponsor_company_id=house.id))
    assert err.value.detail["code"] == "house_is_not_a_sponsor"
    # The house row is named for the constant the arrangement already prints as the employer.
    assert HOUSE_COMPANY_NAME == pa.DEFAULTS["rm_employer"]


async def test_an_unsigned_linked_company_stops_the_walk_and_says_why():
    unsigned, gamma = _company("Newco Motors"), _company("Gamma Sales")
    rm, creator = _person("Rita Moss", unsigned), _person("Cleo Desk", gamma, Role.LOAN_EXEC)
    profile = SimpleNamespace(id=uuid4(), intake_id=None, dealer_id=None)
    db = _Db([unsigned, gamma, rm, creator])
    with _signed(gamma):
        d = await pkgs.default_sponsor_company_id(db, profile, {"rm_user_id": str(rm.id)}, creator)
    # The manager's own company, unsigned — not the creator's signed one.
    assert d and d.company_id == unsigned.id and d.signed is False
    # Nothing is applied from an unsigned default.
    package = SimpleNamespace(sponsor_company_id=None)
    arrangement, provenance = pa.empty_arrangement(), {}
    with _signed(gamma):
        out = await pkgs._default_sponsor_onto(db, package, profile, {**arrangement, "rm_user_id": str(rm.id)}, provenance, creator)
    assert out == d and package.sponsor_company_id is None and not provenance
    # The blank-sponsor row says why and carries the signing-link action; other rows are untouched.
    rows = [{"key": "sponsor_name", "title": "Sponsor legal name is blank", "detail": "x"}, {"key": "requested", "detail": "y"}]
    decorated = pkgs._say_why_the_sponsor_is_blank(rows, d)
    assert "Ask Newco Motors to sign" in decorated[0]["detail"] and "Rita Moss is linked to it" in decorated[0]["detail"]
    assert decorated[0]["action"] == {"kind": "copy_signing_link", "label": "Copy signing link"}
    assert decorated[1] == rows[1]


# --- the copy, the provenance, and the override ---------------------------------


def _option(company, *, has_agreement=True):
    return SimpleNamespace(company_id=company.id, name=company.name, kind=company.kind, has_agreement=has_agreement,
                           state_of_formation=company.state_of_formation, entity_type=company.entity_type,
                           principal_address=company.principal_address, notice_email=company.notice_email,
                           platform_name=company.platform_name)


async def test_a_defaulted_sponsor_is_unconfirmed_and_the_desks_pick_wins():
    acme, beta = _company("Acme Warranty"), _company("Beta Admin")
    rm = _person("Rita Moss", acme)
    profile = SimpleNamespace(id=uuid4(), intake_id=None, dealer_id=None)
    db = _Db([acme, beta, rm])
    package = SimpleNamespace(sponsor_company_id=None)
    arrangement, provenance = {**pa.empty_arrangement(), "rm_user_id": str(rm.id)}, {}
    with _signed(acme), patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=_option(acme))):
        d = await pkgs._default_sponsor_onto(db, package, profile, arrangement, provenance, None)
    assert d and package.sponsor_company_id == acme.id
    assert arrangement["sponsor_name"] == "Acme Warranty" and arrangement["sponsor_platform"] == "Endurance"
    for key in pa.SPONSOR_KEYS:
        if arrangement[key]:
            assert provenance[key] == {"source": "sponsor_default", "label": "Linked profile of Rita Moss", "confirmed": False}, key
    # A sponsor already on the package is never replaced — even an unconfirmed default.
    with _signed(acme, beta), patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=_option(beta))):
        again = await pkgs._default_sponsor_onto(db, package, profile, arrangement, provenance, None)
    assert again is None and package.sponsor_company_id == acme.id and arrangement["sponsor_name"] == "Acme Warranty"
    # The desk's pick goes through the same copy, confirmed.
    access = SimpleNamespace(package=SimpleNamespace(arrangement=arrangement, prefill_provenance=provenance, sponsor_company_id=acme.id), user=None)
    with patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=_option(beta))):
        out = await pkgs._apply_sponsor(db, access, beta.id)
    assert out["arrangement"]["sponsor_name"] == "Beta Admin"
    assert out["provenance"]["sponsor_name"] == {"source": "sponsor", "label": prefill.SOURCE_LABELS["sponsor"], "confirmed": True}


async def test_confirming_the_sponsor_name_confirms_the_whole_block():
    acme = _company("Acme Warranty")
    rm = _person("Rita Moss", acme)
    provenance = {k: {"source": "sponsor_default", "label": "Linked profile of Rita Moss", "confirmed": False}
                  for k in ("sponsor_name", "sponsor_state", "sponsor_platform")}
    package = SimpleNamespace(id=uuid4(), status="draft", version=1, arrangement={**pa.empty_arrangement(), "rm_user_id": str(rm.id), "sponsor_name": "Acme Warranty"},
                              prefill_provenance=provenance, stage=1, sent_by_user_id=None, execution_pending=False,
                              sponsor_company_id=acme.id, created_by_user_id=None)
    profile = SimpleNamespace(id=uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    access = pkgs.PackageAccess(package=package, profile=profile, user=SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Desk", email="d@x"), mode="operator")
    db = _Db([acme, rm, package])

    async def get(_model, key, **_kw):
        return db.rows.get(key, package)

    db.get = get
    with patch.object(pkgs.profiles, "log_profile_action", AsyncMock()):
        await pkgs.apply_changes(db, access, changes={}, version=1, confirm=["sponsor_name"])
    assert all(package.prefill_provenance[k]["confirmed"] for k in ("sponsor_name", "sponsor_state", "sponsor_platform"))


async def test_a_manager_change_brings_the_default_only_onto_a_package_with_no_sponsor():
    acme = _company("Acme Warranty")
    rm = _person("Rita Moss", acme)
    package = SimpleNamespace(id=uuid4(), status="draft", version=1, arrangement={**pa.empty_arrangement()},
                              prefill_provenance={}, stage=1, sent_by_user_id=None, execution_pending=False,
                              sponsor_company_id=None, created_by_user_id=None)
    profile = SimpleNamespace(id=uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    access = pkgs.PackageAccess(package=package, profile=profile, user=SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Desk", email="d@x"), mode="operator")
    db = _Db([acme, rm, package])

    async def get(_model, key, **_kw):
        return db.rows.get(key, package)

    db.get = get
    log = AsyncMock()
    with _signed(acme), patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=_option(acme))), patch.object(pkgs.profiles, "log_profile_action", log):
        await pkgs.apply_changes(db, access, changes={"rm_user_id": str(rm.id), "rm_name": "Rita Moss"}, version=1)
    assert package.sponsor_company_id == acme.id and package.arrangement["sponsor_name"] == "Acme Warranty"
    assert package.prefill_provenance["sponsor_name"]["confirmed"] is False
    changes = log.call_args.kwargs["metadata"]["changes"]
    assert changes["sponsor_company_id"]["defaulted_from"] == "rm"
    # The employer line followed the manager's linked profile.
    assert package.arrangement["rm_employer"] == "Acme Warranty" and "rm_employer" in changes


# --- the employer line ------------------------------------------------------------


async def test_rm_employer_reads_the_linked_company():
    acme = _company("Choice Car Care")
    house = _company(HOUSE_COMPANY_NAME, kind=KIND_HOUSE)
    profile = SimpleNamespace(id=uuid4(), dealer_id=None, intake_id=None, entity_type=None, vertical="dealer", naics_code=None)

    def db_for(company):
        async def get(_model, key, **_kw):
            return company if key == company.id else None

        async def execute(_stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), scalar_one_or_none=lambda: None)

        return SimpleNamespace(get=get, execute=execute)

    with patch.object(prefill.profiles, "owner_rows", AsyncMock(return_value=[])):
        linked = await prefill.build_prefill(db_for(acme), profile, _person("Rita Moss", acme))
        housed = await prefill.build_prefill(db_for(house), profile, _person("Cleo Desk", house, Role.LOAN_EXEC))
        unlinked = await prefill.build_prefill(db_for(acme), profile, _person("Nobody Yet", None))
    assert linked.values["rm_employer"] == "Choice Car Care" and linked.provenance["rm_employer"]["source"] == "user"
    assert housed.values["rm_employer"] == HOUSE_COMPANY_NAME
    assert unlinked.values["rm_employer"] == pa.DEFAULTS["rm_employer"] and unlinked.provenance["rm_employer"]["source"] == "derived"


# --- the picker's own defects -------------------------------------------------------


def test_the_sponsor_option_says_whether_an_agreement_exists_and_what_kind_it_is():
    acme = _company("Acme Warranty")
    with patch.object(pkgs, "_agreement_read", return_value=None):
        signed = pkgs._sponsor_option(acme, SimpleNamespace(field_values={}), user=None)
        unsigned = pkgs._sponsor_option(acme, None, user=None)
    assert signed.has_agreement is True and unsigned.has_agreement is False
    assert signed.kind == KIND_REFERRAL_PARTNER


def test_a_rep_read_keeps_has_agreement_but_never_the_agreement():
    """serialize narrows the sponsor for non-operators. The narrowed copy must
    carry has_agreement (so the rep does not see a false "No signed agreement")
    and must not carry the agreement itself (the desk's)."""
    import inspect

    source = inspect.getsource(pkgs.serialize)
    narrowed = source.split("if sponsor is not None and not operator:", 1)[1].split("\n", 3)[2]
    assert "has_agreement=sponsor.has_agreement" in narrowed and "kind=sponsor.kind" in narrowed
    assert "agreement=" not in narrowed.replace("has_agreement=", "")


def test_sponsor_fields_are_the_desks_to_clear():
    rows = {a["key"]: a for a in pa.field_attention(pa.empty_arrangement(), scope="stage_one")}
    for key in pa.SPONSOR_KEYS:
        if key in rows:
            assert rows[key]["owner"] == "desk", key
    assert "sponsor_name" in rows and "sponsor_platform" in rows
    assert rows["rm_phone"]["owner"] == "any"


# --- linking any role ---------------------------------------------------------------


def _request():
    return SimpleNamespace(headers={}, client=None)


def _actor():
    return SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)


def _router_db(rows, *, existing_user=None):
    """Enough of a session for invite_user / update_user."""
    table = {r.id: r for r in rows}

    async def get(_model, key, **_kw):
        return table.get(key)

    async def execute(_stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: existing_user, scalars=lambda: SimpleNamespace(all=lambda: []))

    async def refresh(_row):
        return None

    added = []

    def add(row):
        row.id = getattr(row, "id", None) or uuid4()
        added.append(row)

    return SimpleNamespace(get=get, execute=execute, flush=AsyncMock(), refresh=refresh, add=add, added=added)


async def test_any_role_may_be_linked_and_the_house_is_the_default_for_staff():
    from app.routers import users as users_router

    unsigned = _company("Newco Motors")
    house = _company(HOUSE_COMPANY_NAME, kind=KIND_HOUSE)
    db = _router_db([unsigned, house])
    with patch.object(users_router.clerk_service, "invite_user", AsyncMock()), \
         patch.object(users_router, "house_company", AsyncMock(return_value=house)), \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        # A field rep linked to a company that has not signed yet: linking precedes the signature.
        out = await users_router.invite_user(users_router.UserInvite(email="rep@example.com", name="Rita Moss", role=Role.FIELD_REP, referral_partner_company_id=unsigned.id), _request(), db, current=_actor())
        assert out.referral_partner_company_id == unsigned.id and out.company_agreement_signed is False and out.company_kind == KIND_REFERRAL_PARTNER
        # An underwriter invited with nothing is linked to the house.
        out = await users_router.invite_user(users_router.UserInvite(email="uw@example.com", name="Cleo Desk", role=Role.LOAN_EXEC), _request(), db, current=_actor())
        assert out.referral_partner_company_id == house.id and out.company_kind == KIND_HOUSE
        # A dealer partner still needs a company, and it can never be the house.
        with pytest.raises(HTTPException) as err:
            await users_router.invite_user(users_router.UserInvite(email="dp@example.com", name="Andy Gale", role=Role.DEALER_PARTNER), _request(), db, current=_actor())
        assert err.value.status_code == 400
        with pytest.raises(HTTPException) as err:
            await users_router.invite_user(users_router.UserInvite(email="dp2@example.com", name="Andy Gale", role=Role.DEALER_PARTNER, referral_partner_company_id=house.id), _request(), db, current=_actor())
        assert err.value.status_code == 400 and "house" in err.value.detail


async def test_an_operators_link_cannot_be_cleared_and_a_house_linked_promotion_is_refused():
    from app.routers import users as users_router

    house = _company(HOUSE_COMPANY_NAME, kind=KIND_HOUSE)
    staffer = _person("Cleo Desk", house, Role.LOAN_EXEC)
    staffer.account_access_types = []
    db = _router_db([house, staffer], existing_user=staffer)
    with patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())), \
         patch.object(users_router, "house_company", AsyncMock(return_value=house)):
        with pytest.raises(HTTPException) as err:
            await users_router.update_user(staffer.id, users_router.UserPatch(referral_partner_company_id=None), _request(), db, current=_actor())
        assert err.value.status_code == 400 and "linked" in err.value.detail
        # Promoting a house-linked staffer to dealer partner without a company would lock them out for good.
        with pytest.raises(HTTPException) as err:
            await users_router.update_user(staffer.id, users_router.UserPatch(role=Role.DEALER_PARTNER), _request(), db, current=_actor())
        assert err.value.status_code == 400
        assert staffer.role == Role.LOAN_EXEC and staffer.referral_partner_company_id == house.id


def test_the_referral_companies_route_is_registered():
    from app.routers.users import router

    contract = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/users/referral-companies", "GET") in contract
    assert ("/users/referral-companies/signed", "GET") in contract
