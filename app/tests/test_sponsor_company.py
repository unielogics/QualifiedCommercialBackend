"""The sponsor company: correctable, and copied whole onto a package.

A sponsor row held four facts and had no write path anywhere in the product —
the invite flow creates one and nothing could ever fix it, so a company that
arrived without an entity type stayed blank on every package forever. And the
one field with no source at all, `sponsor_platform`, was required to send stage
one while `_apply_sponsor` never set it and never cleared it, so a platform
typed for one sponsor survived a switch to another.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import production_arrangement as pa
from app.services import production_packages as pkgs


def _company(**over):
    row = SimpleNamespace(
        id=uuid4(), name="Choice Car Care", entity_type="Limited liability company",
        state_of_formation="NJ", principal_address="12 Harbor Rd, Newark NJ",
        notice_email=None, notice_attention=None, notice_address=None,
        platform_name=None, signatory_name=None, signatory_title=None, phone=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _rpa(**fv):
    return SimpleNamespace(field_values=fv, id=uuid4())


def _option(company, agreement, user):
    """_agreement_read builds a full signed-agreement payload; these tests are
    about the company's own fields, so stub it out."""
    with patch.object(pkgs, "_agreement_read", return_value=None):
        return pkgs._sponsor_option(company, agreement, user=user)


def _user(role=Role.SUPER_ADMIN):
    return SimpleNamespace(id=uuid4(), role=role, name="Desk", email="desk@example.com")


# --- reading -------------------------------------------------------------------


def test_the_agreement_fills_what_the_company_row_does_not_hold():
    option = _option(
        _company(),
        _rpa(referral_partner_notice_email="russ@choicecarcare.us", referral_partner_notice_attn="Russ Woodard",
             counterparty_signatory_name="Russ Woodard", counterparty_signatory_title="COO",
             referral_partner_notice_address_line1="12 Harbor Rd", referral_partner_notice_address_line2="Suite 300"),
        _user(),
    )
    assert option.notice_email == "russ@choicecarcare.us"
    assert option.signatory_name == "Russ Woodard" and option.signatory_title == "COO"
    assert option.notice_address == "12 Harbor Rd, Suite 300"


def test_a_corrected_company_row_beats_the_signed_agreement():
    """The agreement cannot be edited; a correction on the company is newer."""
    option = _option(
        _company(notice_email="legal@choicecarcare.us", signatory_title="Chief Operating Officer"),
        _rpa(referral_partner_notice_email="russ@choicecarcare.us", counterparty_signatory_title="COO"),
        _user(),
    )
    assert option.notice_email == "legal@choicecarcare.us"
    assert option.signatory_title == "Chief Operating Officer"


def test_a_company_with_no_agreement_reads_back_blank_rather_than_invented():
    option = _option(_company(entity_type=None, state_of_formation=None), None, _user())
    assert option.entity_type is None and option.notice_email is None


# --- writing -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_desk_can_correct_a_company_that_arrived_blank():
    company = _company(entity_type=None, state_of_formation=None)
    db = SimpleNamespace(get=AsyncMock(return_value=company), flush=AsyncMock())
    with patch.object(pkgs, "_latest_rpa", AsyncMock(return_value=None)):
        out = await pkgs.update_sponsor_company(
            db, company.id,
            {"entity_type": "Limited liability company", "state_of_formation": "NJ", "platform_name": "Endurance"},
            user=_user(),
        )
    assert company.entity_type == "Limited liability company"
    assert company.platform_name == "Endurance"
    assert out.platform_name == "Endurance"


@pytest.mark.asyncio
async def test_a_cleared_field_is_stored_as_null_not_an_empty_string():
    company = _company(notice_email="old@example.com")
    db = SimpleNamespace(get=AsyncMock(return_value=company), flush=AsyncMock())
    with patch.object(pkgs, "_latest_rpa", AsyncMock(return_value=None)):
        await pkgs.update_sponsor_company(db, company.id, {"notice_email": "  "}, user=_user())
    assert company.notice_email is None


@pytest.mark.asyncio
async def test_only_the_desk_may_correct_a_sponsor():
    db = SimpleNamespace(get=AsyncMock(return_value=_company()), flush=AsyncMock())
    for role in (Role.FIELD_REP, Role.DEALER_PARTNER):
        with pytest.raises(HTTPException) as err:
            await pkgs.update_sponsor_company(db, uuid4(), {"phone": "9735550148"}, user=_user(role))
        assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_field_is_ignored_rather_than_setting_an_attribute():
    company = _company()
    db = SimpleNamespace(get=AsyncMock(return_value=company), flush=AsyncMock())
    with patch.object(pkgs, "_latest_rpa", AsyncMock(return_value=None)):
        await pkgs.update_sponsor_company(db, company.id, {"id": "hijacked", "name": "Renamed"}, user=_user())
    # The name identifies the party on a signed agreement; renaming it would
    # silently repoint every package that cites it.
    assert company.id != "hijacked" and company.name == "Choice Car Care"


# --- copying onto a package ------------------------------------------------------


@pytest.mark.asyncio
async def test_choosing_a_sponsor_sets_every_sponsor_key():
    """The bug: sponsor_platform was absent from the map, so a platform typed
    for one sponsor survived a switch to another — on a field that is required
    to send stage one and that names the company on Schedule A."""
    package = SimpleNamespace(arrangement={**pa.empty_arrangement(), "sponsor_platform": "Old Platform"},
                              prefill_provenance={}, sponsor_company_id=None)
    access = SimpleNamespace(package=package, user=_user())
    option = SimpleNamespace(company_id=uuid4(), name="Choice Car Care", state_of_formation="NJ",
                             entity_type="llc", principal_address="12 Harbor Rd",
                             notice_email="russ@choicecarcare.us", platform_name="Endurance",
                             agreement=SimpleNamespace(id=uuid4()))
    with patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=option)):
        out = await pkgs._apply_sponsor(SimpleNamespace(), access, option.company_id)

    arrangement = out["arrangement"]
    assert arrangement["sponsor_platform"] == "Endurance"
    # Every sponsor key is written, so none can carry a previous sponsor's value.
    assert set(pa.SPONSOR_KEYS) <= set(arrangement)
    assert arrangement["sponsor_name"] == "Choice Car Care"


@pytest.mark.asyncio
async def test_a_sponsor_with_no_platform_clears_the_previous_one():
    package = SimpleNamespace(arrangement={**pa.empty_arrangement(), "sponsor_platform": "Old Platform"},
                              prefill_provenance={"sponsor_platform": {"source": "sponsor"}}, sponsor_company_id=None)
    access = SimpleNamespace(package=package, user=_user())
    option = SimpleNamespace(company_id=uuid4(), name="PEA LLC", state_of_formation="NJ", entity_type=None,
                             principal_address=None, notice_email=None, platform_name=None,
                             agreement=SimpleNamespace(id=uuid4()))
    with patch.object(pkgs, "sponsor_option_for", AsyncMock(return_value=option)):
        out = await pkgs._apply_sponsor(SimpleNamespace(), access, option.company_id)

    assert out["arrangement"]["sponsor_platform"] == ""
    assert "sponsor_platform" not in out["provenance"]


def test_the_sponsor_key_set_and_the_copy_map_cannot_drift():
    """_apply_sponsor must name every sponsor key. This is the guard that the
    next key added to SPONSOR_KEYS is not silently left uncopied."""
    import inspect

    source = inspect.getsource(pkgs._apply_sponsor)
    for key in pa.SPONSOR_KEYS:
        assert f'"{key}"' in source, key


def test_the_correction_route_is_registered():
    from app.routers.production_packages import router

    contract = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/production-packages/sponsors/{company_id}", "PATCH") in contract
