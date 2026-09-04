"""Production Package: stamping, access semantics, send preconditions, prefill."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import production_arrangement as pa
from app.services import production_packages as pkgs
from app.services import production_prefill as prefill
from app.services import production_presentation as pres
from app.services import production_signing as signing

fitz = pytest.importorskip("fitz")


def _agreement_like_pdf() -> bytes:
    """A PDF carrying the three signature blocks the way the agreement renders them."""
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for anchor, placeholder, date_placeholder in (
        (pres.DEALER_ANCHOR, pres.ELECTRONIC_PLACEHOLDER, pres.ELECTRONIC_DATE_PLACEHOLDER),
        (pres.SPONSOR_ANCHOR, pres.RECORDED_PLACEHOLDER, pres.RECORDED_DATE_PLACEHOLDER),
        (pres.QC_ANCHOR, pres.RECORDED_PLACEHOLDER, pres.RECORDED_DATE_PLACEHOLDER),
    ):
        page.insert_text((60, y), anchor, fontname="helv", fontsize=10)
        page.insert_text((60, y + 50), placeholder, fontname="helv", fontsize=9)
        page.insert_text((380, y + 50), date_placeholder, fontname="helv", fontsize=9)
        y += 160
    return doc.tobytes()


def _png() -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 40), False)
    pix.clear_with(255)
    return pix.tobytes("png")


def test_stamp_lands_on_the_right_party_block():
    raw = _agreement_like_pdf()
    when = datetime(2026, 9, 3, tzinfo=UTC)
    stamped = signing._stamp_party(
        raw, anchor=pres.SPONSOR_ANCHOR, placeholder=pres.RECORDED_PLACEHOLDER,
        date_placeholder=pres.RECORDED_DATE_PLACEHOLDER, typed_name="Jane Sponsor, CEO", signature_png=None, signed_at=when,
    )
    doc = fitz.open(stream=stamped, filetype="pdf")
    page = doc[0]
    sponsor_anchor = page.search_for(pres.SPONSOR_ANCHOR)[0]
    qc_anchor = page.search_for(pres.QC_ANCHOR)[0]
    sig = page.search_for("/s/ Jane Sponsor, CEO")
    assert sig, "typed adoption not written"
    assert sponsor_anchor.y1 < sig[0].y0 < qc_anchor.y0, "signature must sit inside the sponsor block"
    # The sponsor's placeholders are gone; the QC block still shows its own.
    remaining = page.search_for(pres.RECORDED_PLACEHOLDER)
    assert len(remaining) == 1 and remaining[0].y0 > qc_anchor.y0
    assert page.search_for("September 03, 2026")
    assert hashlib.sha256(stamped).hexdigest() != hashlib.sha256(raw).hexdigest()


def test_stamp_with_image_removes_electronic_placeholder():
    raw = _agreement_like_pdf()
    stamped = signing._stamp_party(
        raw, anchor=pres.DEALER_ANCHOR, placeholder=pres.ELECTRONIC_PLACEHOLDER,
        date_placeholder=pres.ELECTRONIC_DATE_PLACEHOLDER, typed_name="Rafael Delgado", signature_png=_png(),
        signed_at=datetime.now(UTC),
    )
    page = fitz.open(stream=stamped, filetype="pdf")[0]
    assert not page.search_for(pres.ELECTRONIC_PLACEHOLDER)
    assert not page.search_for(pres.ELECTRONIC_DATE_PLACEHOLDER)
    assert page.search_for(pres.RECORDED_PLACEHOLDER), "other parties untouched"
    assert page.get_images(), "drawn signature embedded"


def test_stamp_refuses_unknown_anchor():
    with pytest.raises(ValueError):
        signing._stamp_party(_agreement_like_pdf(), anchor="NOPE", placeholder="x", date_placeholder="y",
                             typed_name="a", signature_png=None, signed_at=datetime.now(UTC))


# ---- access semantics -------------------------------------------------------

def _user(role: Role, **kw) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role=role, name="Test User", email="t@example.com",
                           account_access_types=[], deleted_at=None, **kw)


def _access(status: str = "draft", role: Role = Role.LOAN_EXEC, mode: str = "operator", link=None, stage: int = 1, **pkg) -> pkgs.PackageAccess:
    fields = {"id": uuid.uuid4(), "status": status, "version": 1, "arrangement": {}, "prefill_provenance": {}, "stage": stage,
              "sent_by_user_id": None, "execution_pending": False, "sponsor_company_id": None}
    fields.update(pkg)
    package = SimpleNamespace(**fields)
    profile = SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    return pkgs.PackageAccess(package=package, profile=profile, user=_user(role), mode=mode, link=link)


def test_capabilities_by_role_and_status():
    draft_exec = _access("draft", Role.LOAN_EXEC).capabilities()
    assert draft_exec.can_edit and draft_exec.can_send and draft_exec.can_share and not draft_exec.can_record
    assert draft_exec.can_manage_terms and not draft_exec.can_draft_final and not draft_exec.can_adopt_sponsor_signature
    sent_admin = _access("out_for_signature", Role.SUPER_ADMIN).capabilities()
    assert not sent_admin.can_edit and sent_admin.can_record and sent_admin.can_reopen and sent_admin.can_remind
    assert not sent_admin.can_execute  # execution is automatic; the retry appears only when pending
    pending_admin = _access("out_for_signature", Role.SUPER_ADMIN, execution_pending=True).capabilities()
    assert pending_admin.can_execute
    sent_exec = _access("out_for_signature", Role.LOAN_EXEC).capabilities()
    assert sent_exec.can_reopen and not sent_exec.can_record and not sent_exec.can_void
    # agents: a rep holding a live link, and a dealer partner on their own lead, may send stage one
    rep = _access("draft", Role.FIELD_REP, mode="rep", link=SimpleNamespace(id=uuid.uuid4())).capabilities()
    assert rep.can_edit and rep.can_generate and rep.can_send and not rep.can_share and not rep.can_pick_sponsor and not rep.can_manage_terms
    partner = _access("draft", Role.DEALER_PARTNER, mode="partner").capabilities()
    assert partner.can_edit and partner.can_send and not partner.can_share and not partner.can_pick_sponsor
    rep_no_link = _access("draft", Role.FIELD_REP, mode="rep").capabilities()
    assert not rep_no_link.can_edit and not rep_no_link.can_send
    rep_sent = _access("out_for_signature", Role.FIELD_REP, mode="rep", link=SimpleNamespace(id=uuid.uuid4())).capabilities()
    assert not rep_sent.can_edit and not rep_sent.can_remind  # did not send it
    me = _user(Role.FIELD_REP)
    mine = pkgs.PackageAccess(package=SimpleNamespace(id=uuid.uuid4(), status="out_for_signature", version=1, arrangement={}, prefill_provenance={}, stage=1, sent_by_user_id=me.id, execution_pending=False, sponsor_company_id=None),
                              profile=SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None), user=me, mode="rep", link=SimpleNamespace(id=uuid.uuid4()))
    assert mine.capabilities().can_remind
    executed = _access("executed", Role.SUPER_ADMIN).capabilities()
    assert not (executed.can_edit or executed.can_send or executed.can_void or executed.can_execute)
    assert not executed.can_draft_final  # needs a term sheet
    exec_with_sheet = _access("executed", Role.LOAN_EXEC)
    exec_with_sheet.term_sheet = SimpleNamespace(id=uuid.uuid4())
    assert exec_with_sheet.capabilities().can_draft_final and exec_with_sheet.capabilities().can_compare is False
    exec_with_sheet.child = SimpleNamespace(id=uuid.uuid4(), status="draft")
    assert not exec_with_sheet.capabilities().can_draft_final and exec_with_sheet.capabilities().can_compare
    # the final: desk only, no sharing, no presentation
    final = _access("draft", Role.LOAN_EXEC, stage=2).capabilities()
    assert final.can_edit and final.can_send and not final.can_share and not final.can_generate and final.can_compare
    final_rep = _access("draft", Role.FIELD_REP, mode="rep", link=SimpleNamespace(id=uuid.uuid4()), stage=2).capabilities()
    assert not final_rep.can_edit and not final_rep.can_send


async def test_rep_share_requires_rep_role_then_misses_identically():
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, _user(Role.LOAN_EXEC), "tok")
    assert exc.value.status_code == 403
    rep = _user(Role.FIELD_REP)
    # unknown token
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "unknown")
    assert exc.value.status_code == 404
    unknown_body = exc.value.detail
    # someone else's link → identical 404
    other = SimpleNamespace(rep_user_id=uuid.uuid4(), revoked_at=None, expires_at=datetime.now(UTC) + timedelta(days=1), package_id=uuid.uuid4())
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: other)
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "theirs")
    assert exc.value.status_code == 404 and exc.value.detail == unknown_body


async def test_rep_share_revoked_and_expired_are_410():
    rep = _user(Role.FIELD_REP)
    pkgs._MISSES.clear()
    revoked = SimpleNamespace(rep_user_id=rep.id, revoked_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(days=1), package_id=uuid.uuid4())
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: revoked)))
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "tok")
    assert exc.value.status_code == 410
    expired = SimpleNamespace(rep_user_id=rep.id, revoked_at=None, expires_at=datetime.now(UTC) - timedelta(seconds=1), package_id=uuid.uuid4())
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: expired)))
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "tok")
    assert exc.value.status_code == 410


async def test_rep_share_lockout_after_repeated_misses():
    rep = _user(Role.FIELD_REP)
    pkgs._MISSES.clear()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    for _ in range(pkgs._MISS_LIMIT):
        with pytest.raises(HTTPException):
            await pkgs.resolve_rep_share(db, rep, "x")
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "x")
    assert exc.value.status_code == 429
    pkgs._MISSES.clear()


async def test_rep_share_training_file_is_404_and_archived_is_410():
    rep = _user(Role.FIELD_REP)
    pkgs._MISSES.clear()
    link = SimpleNamespace(rep_user_id=rep.id, revoked_at=None, expires_at=datetime.now(UTC) + timedelta(days=1),
                           package_id=uuid.uuid4(), last_used_at=None, use_count=0)
    package = SimpleNamespace(id=link.package_id, profile_id=uuid.uuid4(), status="draft", stage=1)
    profile = SimpleNamespace(id=package.profile_id, vertical="dealer", dealer_id=uuid.uuid4(), intake_id=None)
    training = SimpleNamespace(is_training=True, archived_at=None)

    async def get(model, key):
        return {package.id: package, profile.id: profile, profile.dealer_id: training}.get(key)

    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: link)), get=get, flush=AsyncMock())
    with patch.object(pkgs, "_load_family", AsyncMock()):
        pass
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "tok")
    assert exc.value.status_code == 404
    training.is_training = False
    training.archived_at = datetime.now(UTC)
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "tok")
    assert exc.value.status_code == 410
    training.archived_at = None
    with patch.object(pkgs, "_load_family", AsyncMock()):
        access = await pkgs.resolve_rep_share(db, rep, "tok")
    assert access.mode == "rep" and access.via == "share_link" and access.editable and link.use_count == 1


async def test_rep_cannot_touch_sponsor_keys():
    rep = _user(Role.FIELD_REP)
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=3, arrangement={}, prefill_provenance={}, stage=1, sent_by_user_id=None, execution_pending=False)
    access = pkgs.PackageAccess(package=package, profile=SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None),
                                user=rep, mode="rep", link=SimpleNamespace(id=uuid.uuid4()))

    async def get(model, key, with_for_update=False):
        return package

    db = SimpleNamespace(get=get, flush=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await pkgs.apply_changes(db, access, changes={"sponsor_platform": "x"}, version=3)
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "maintained_by_desk"
    with pytest.raises(HTTPException) as exc:
        await pkgs.apply_changes(db, access, changes={"dealer_dba": "x"}, version=2)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "stale_version"


async def test_apply_changes_bumps_version_and_records_diff():
    user = _user(Role.LOAN_EXEC)
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=1, arrangement={"dealer_name": "Old"}, stage=1,
                              prefill_provenance={"dealer_name": {"source": "intake", "label": "AI intake", "confirmed": False}})
    profile = SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None, primary_bucket_id=None)
    access = pkgs.PackageAccess(package=package, profile=profile, user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return package

    db = SimpleNamespace(get=get, flush=AsyncMock())
    with patch.object(pkgs.profiles, "log_profile_action", AsyncMock()) as log:
        await pkgs.apply_changes(db, access, changes={"dealer_name": "New", "lot_units": "142"}, version=1, confirm=["dealer_name"])
    assert package.version == 2
    assert package.arrangement["dealer_name"] == "New" and package.arrangement["lot_units"] == 142
    assert package.prefill_provenance["dealer_name"]["source"] == "user"
    assert package.updated_via == "operator"
    meta = log.await_args.kwargs["metadata"]
    assert meta["changes"]["dealer_name"] == {"before": "Old", "after": "New"}
    assert any(a["key"] == "products.vsc.repay" or a["key"] for a in package.attention)


async def test_frozen_package_refuses_edits():
    user = _user(Role.SUPER_ADMIN)
    package = SimpleNamespace(id=uuid.uuid4(), status="out_for_signature", version=1, arrangement={}, prefill_provenance={}, stage=1)
    access = pkgs.PackageAccess(package=package, profile=SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None), user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return package

    with pytest.raises(HTTPException) as exc:
        await pkgs.apply_changes(SimpleNamespace(get=get, flush=AsyncMock()), access, changes={"dealer_dba": "x"}, version=1)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "package_frozen"


# ---- send preconditions -----------------------------------------------------

async def test_send_refuses_blanks_and_is_idempotent_once_out():
    user = _user(Role.LOAN_EXEC)
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=1, arrangement=pa.empty_arrangement(),
                              prefill_provenance={}, stage=1, delivery_history=[], sent_by_user_id=None, execution_pending=False, sponsor_company_id=None)
    profile = SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    access = pkgs.PackageAccess(package=package, profile=profile, user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return package

    db = SimpleNamespace(get=get, flush=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await signing.send(db, access, channel="sms", recipient_email=None, recipient_phone=None, request=None)
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "attention"
    assert any(i["key"] == "dealer_name" for i in exc.value.detail["items"])
    package.status = "out_for_signature"
    out = await signing.send(db, access, channel="sms", recipient_email=None, recipient_phone=None, request=None)
    assert out["already_sent"] is True and out["delivered"] is False


async def test_send_requires_a_signed_sponsor():
    import sys
    sys.path.insert(0, "app/tests")
    from test_production_arrangement import seed
    arr = seed()
    arr["thresholds"] = {}
    # make the covenant clear so the only blocker is the sponsor
    arr["debt_service"] = 30000
    assert pa.compute(arr)["attention"] == []
    user = _user(Role.LOAN_EXEC)
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=1, arrangement=arr, prefill_provenance={},
                              stage=1, delivery_history=[], sponsor_company_id=None, sent_by_user_id=None, execution_pending=False)
    profile = SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    access = pkgs.PackageAccess(package=package, profile=profile, user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return package

    db = SimpleNamespace(get=get, flush=AsyncMock())
    with patch.object(pkgs, "client_contact", AsyncMock(return_value=("Delgado", "owner@example.com", "+19735550148"))):
        with pytest.raises(HTTPException) as exc:
            await signing.send(db, access, channel="sms", recipient_email=None, recipient_phone=None, request=None)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "sponsor_missing"


# ---- gate -------------------------------------------------------------------

async def test_pending_gate_is_none_without_intake_or_pending_row():
    assert await signing.pending_client_signature(SimpleNamespace(), None) is None
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    assert await signing.pending_client_signature(db, uuid.uuid4()) is None


# ---- prefill ----------------------------------------------------------------

async def test_prefill_precedence_dealer_then_intake_then_actor():
    dealer_id = uuid.uuid4()
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=dealer_id, intake_id=uuid.uuid4(), entity_type="s_corp",
                              vertical="dealer", naics_code=None)
    dealer = _dealer_row(dealer_id)
    dap = _dap_row()
    intake = SimpleNamespace(business_name="Other Name", full_name="Rafael Delgado", email="r@example.com", phone="+19735550148",
                             requested_loan_amount=1200000, intake_state={"dealer_details": {"stated_monthly_debt_payments": 39000}},
                             result_snapshot={"key_metrics": {"monthly_debt_service": "$40,000"}})
    owner = SimpleNamespace(first_name="Rafael", last_name="Delgado", is_primary=True, ownership_pct=100,
                            email="r@example.com", phone="+19735550148", invite_sent_at=None, credit_pull_id=None)

    async def get(model, key):
        return {dealer_id: dealer, profile.intake_id: intake}.get(key)

    db = _prefill_db(get, dap, facts=[])
    with patch.object(prefill.profiles, "owner_rows", AsyncMock(return_value=[owner])):
        result = await prefill.build_prefill(db, profile, _user(Role.LOAN_EXEC))
    v, p = result.values, result.provenance
    assert v["dealer_name"] == "Delgado Auto Group LLC" and p["dealer_name"]["source"] == "dealer"
    assert v["dealer_entity"] == "Limited liability company"
    assert v["dealer_dba"] == "Delgado Auto Sales" and p["dealer_dba"]["source"] == "dealer_profile"
    assert v["dealer_state"] == "Texas"
    assert v["dealer_signer_name"] == "Rafael Delgado" and p["dealer_signer_name"]["source"] == "owners"
    assert v["dealer_signer_title"] == "Managing member"
    assert v["term"] == 24 and v["debt_service"] == 41300 and p["debt_service"]["source"] == "dealer_profile"
    assert v["requested"] == 1200000 and p["requested"]["source"] == "intake"
    assert v["rm_name"] == "Test User" and p["rm_name"]["source"] == "user"
    assert v["rm_employer"] == "Qualified Commercial LLC"
    assert "sponsor_name" not in v and "sponsor_name" not in result.missing
    assert "lot_units" in result.missing and "sponsor_platform" not in result.missing
    # §9.1 and §9.2 arrive from the file rather than being typed at stage one.
    assert v["identity_ein"] == "74-1234567" and v["identity_formation_date"] == "2014-03-11"
    assert v["identity_naics"] == "441110" and v["identity_website"] == "https://delgadoauto.example"
    assert v["dealer_notice_email"] == "r@example.com"
    assert v["owners"] == [{"name": "Rafael Delgado", "pct": 100.0, "title": "",
                            "email": "r@example.com", "phone": "+19735550148", "auth": ""}]


def _dealer_row(dealer_id):
    return SimpleNamespace(id=dealer_id, legal_name="Delgado Auto Group LLC", name="Delgado", entity_type="llc",
                           address="4411 Gulf Fwy", city="Houston", state="TX", zip="77023",
                           started_on=date(2014, 3, 11), ein="74-1234567", naics_code="441110",
                           client_requested_amount=None, funding_goal=900000)


def _dap_row():
    return SimpleNamespace(dba_name="Delgado Auto Sales", state_of_formation="Texas", mailing_address=None, mailing_city=None,
                           mailing_state=None, mailing_zip=None, signer_title="Managing member", term_requested_months=24,
                           monthly_debt_payments=41300, website="https://delgadoauto.example")


def _fact(field_key, value, *, status="suggested", confidence=0.9):
    return SimpleNamespace(field_key=field_key, normalized_value=value, value={"value": value},
                           status=status, confidence=confidence)


def _prefill_db(get, dap, *, facts):
    """build_prefill selects the dealer application profile only when there is a
    dealer, then always selects the extracted facts."""
    pending = [SimpleNamespace(scalar_one_or_none=lambda: dap)] if dap is not None else []

    async def execute(_stmt):
        if pending:
            return pending.pop(0)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: facts))

    return SimpleNamespace(get=get, execute=execute)


@pytest.mark.asyncio
async def test_a_document_reading_fills_what_no_column_holds():
    """The AI reads entity type off an upload into application_extracted_facts.

    Nothing in the package had ever read that table, so an intake-only file
    asked an operator to type a fact the system had already extracted.
    """
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=None, intake_id=None, entity_type=None,
                              vertical="dealer", naics_code=None)
    facts = [_fact("entity_type", "llc"), _fact("legal_entity_name", "Bradley Motors LLC"), _fact("naics_code", "441110")]
    db = _prefill_db(lambda *_: None, None, facts=facts)
    with patch.object(prefill.profiles, "owner_rows", AsyncMock(return_value=[])):
        result = await prefill.build_prefill(db, profile, None)

    assert result.values["dealer_entity"] == "Limited liability company"
    assert result.values["dealer_name"] == "Bradley Motors LLC"
    assert result.values["identity_naics"] == "441110"
    # Unconfirmed, and labelled, so the operator is asked to check it.
    assert result.provenance["dealer_entity"]["confirmed"] is False
    assert result.provenance["dealer_entity"]["label"] == "Read from an uploaded document"


@pytest.mark.asyncio
async def test_a_typed_column_always_beats_a_document_reading():
    dealer_id = uuid.uuid4()
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=dealer_id, intake_id=None, entity_type=None,
                              vertical="dealer", naics_code=None)
    dealer = _dealer_row(dealer_id)

    async def get(_model, key):
        return {dealer_id: dealer}.get(key)

    db = _prefill_db(get, _dap_row(), facts=[_fact("entity_type", "corporation", status="accepted", confidence=1.0)])
    with patch.object(prefill.profiles, "owner_rows", AsyncMock(return_value=[])):
        result = await prefill.build_prefill(db, profile, None)

    assert result.values["dealer_entity"] == "Limited liability company"
    assert result.provenance["dealer_entity"]["source"] == "dealer"


@pytest.mark.asyncio
async def test_a_rejected_reading_is_never_used():
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=None, intake_id=None, entity_type=None,
                              vertical="dealer", naics_code=None)
    db = _prefill_db(lambda *_: None, None, facts=[_fact("entity_type", "corporation", status="rejected")])
    with patch.object(prefill.profiles, "owner_rows", AsyncMock(return_value=[])):
        result = await prefill.build_prefill(db, profile, None)

    assert "dealer_entity" not in result.values


@pytest.mark.asyncio
async def test_applying_a_prefill_requires_the_right_to_edit():
    """run_prefill was the one mutating path gating on _require_editable alone.

    Safe only because agents are 404'd at stage two; a write hole the moment
    they are not.
    """
    access = _access("draft", Role.FIELD_REP, mode="rep", link=SimpleNamespace(id=uuid.uuid4()), stage=2)
    result = prefill.PrefillResult()
    result.put("dealer_name", "Filled Co", "intake")
    with patch.object(pkgs.prefill_svc, "build_prefill", AsyncMock(return_value=result)):
        with pytest.raises(HTTPException) as err:
            await pkgs.run_prefill(SimpleNamespace(), access, force=False, fields=None, apply=True)
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_an_agent_cannot_touch_the_programme_economics():
    """The owner's line: "this is delicate and I dont want the agents to touch
    that." A rep gathers what the dealer says; the cost of running the facility
    and whether it clears is the desk's."""
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=3, arrangement={}, prefill_provenance={},
                              stage=1, sent_by_user_id=None, execution_pending=False)
    access = pkgs.PackageAccess(package=package, profile=SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None),
                                user=_user(Role.FIELD_REP), mode="rep", link=SimpleNamespace(id=uuid.uuid4()))

    async def get(_model, _key, with_for_update=False):
        return package

    db = SimpleNamespace(get=get, flush=AsyncMock())
    for field, value in (("prof_fees", 46000), ("dealer_cof", 14.5), ("requested", 500000), ("sizing", "fixed")):
        with pytest.raises(HTTPException) as err:
            await pkgs.apply_changes(db, access, changes={field: value}, version=3)
        assert err.value.status_code == 422, field
        assert err.value.detail["code"] == "maintained_by_desk"
        assert err.value.detail["fields"] == [field]


def test_the_desk_only_set_is_the_whole_advance_step():
    """Derived from the step, so a field added to Advance cannot slip in without
    someone deciding who owns it."""
    advance = {r.key for r in pa.FIELD_RULES if r.step == "advance"}
    assert pa.DESK_ONLY_KEYS == advance
    # The owner chose to lock the step in full, the requested amount included:
    # it arrives from the intake form, so an agent never needs to type it.
    assert "requested" in pa.DESK_ONLY_KEYS
    assert pa.DESK_ONLY_KEYS.isdisjoint(pa.SPONSOR_KEYS)
    # buildout is a different step and stays with the agent.
    assert "fund_target" not in pa.DESK_ONLY_KEYS


def test_an_attention_row_says_who_can_clear_it():
    """Five desk-only fields have no default and no prefill and still block the
    send, so locking the step means an agent now waits on a desk pass. They have
    to be able to see that, or the list reads as a wall of their own failures."""
    blank = pa.empty_arrangement()
    rows = pa.field_attention(blank, scope="stage_one")
    by_key = {r["key"]: r for r in rows}

    assert by_key["prof_fees"]["owner"] == "desk"
    assert by_key["dealer_name"]["owner"] == "any"
    assert {r["owner"] for r in rows} <= {"desk", "any"}
    # And the ones that actually strand an agent are real, not hypothetical.
    stranded = {k for k, r in by_key.items() if r["owner"] == "desk"}
    assert {"min_activation", "dealer_cof", "orig_cost", "prof_fees", "markup"} <= stranded


def test_the_intake_entity_name_is_never_mined_for_a_type_or_a_state():
    """`primary_operating_entity` is a company name.

    Two readers used to mine it for an entity type and a state of formation.
    They only ran on a dict and the value is a string, so they were dead — but
    had they run they would have written a company name into both fields.
    """
    import inspect as _inspect

    source = _inspect.getsource(prefill.build_prefill)
    assert "_entity_type_from_entity" not in source
    assert "_state_from_entity" not in source
    assert not hasattr(prefill, "_entity_type_from_entity")
    assert not hasattr(prefill, "_state_from_entity")


def test_apply_prefill_only_fills_blanks_unless_forced():
    result = prefill.PrefillResult()
    result.put("dealer_name", "Prefilled Co", "intake")
    result.put("term", 24, "dealer_profile")
    arrangement = {**pa.empty_arrangement(), "dealer_name": "Typed by hand"}
    merged, prov, applied, skipped = prefill.apply_prefill(arrangement, {}, result)
    assert merged["dealer_name"] == "Typed by hand" and "dealer_name" in skipped
    assert merged["term"] == 24 and "term" in applied  # untouched default gets replaced
    forced, _p, applied2, _s = prefill.apply_prefill(arrangement, {}, result, force=True)
    assert forced["dealer_name"] == "Prefilled Co" and set(applied2) == {"dealer_name", "term"}



# ---- stage two, terms and signatures on file -------------------------------

def test_initials_match_rules():
    assert signing._initials_match("RD", "Rafael Delgado")
    assert signing._initials_match("rmd", "Rafael M. Delgado")
    assert signing._initials_match("RD", "Rafael M. Delgado")  # middle optional
    assert not signing._initials_match("XD", "Rafael Delgado")
    assert not signing._initials_match("R", "Rafael Delgado")
    assert signing._initials_of("Rafael M. Delgado") == "RMD"


async def test_draft_final_preconditions():
    user = _user(Role.LOAN_EXEC)
    parent = SimpleNamespace(id=uuid.uuid4(), stage=1, status="draft", frozen_revision_id=None, profile_id=uuid.uuid4())
    access = pkgs.PackageAccess(package=parent, profile=SimpleNamespace(id=parent.profile_id, vertical="dealer", dealer_id=None), user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return parent

    db = SimpleNamespace(get=get, flush=AsyncMock(), execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    with pytest.raises(HTTPException) as exc:
        await pkgs.draft_final(db, access)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "stage_one_not_executed"
    rep_access = pkgs.PackageAccess(package=parent, profile=access.profile, user=_user(Role.FIELD_REP), mode="rep", link=SimpleNamespace(id=uuid.uuid4()))
    with pytest.raises(HTTPException) as exc:
        await pkgs.draft_final(db, rep_access)
    assert exc.value.status_code == 403
    # executed parent + executed revision but no term sheet
    parent.status = "executed"
    parent.frozen_revision_id = uuid.uuid4()
    revision = SimpleNamespace(id=parent.frozen_revision_id, status="executed", revision_no=1, snapshot={"arrangement": {}}, content_sha256="x")

    async def get2(model, key, with_for_update=False):
        return revision if key == parent.frozen_revision_id else parent

    db2 = SimpleNamespace(get=get2, flush=AsyncMock(), execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    with patch.object(pkgs.sheets_svc, "current_sheet", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await pkgs.draft_final(db2, access)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "terms_missing"


async def test_stage_two_apply_changes_locks_term_keys():
    user = _user(Role.LOAN_EXEC)
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=1, arrangement={}, prefill_provenance={}, stage=2,
                              sent_by_user_id=None, execution_pending=False, sponsor_company_id=None)
    access = pkgs.PackageAccess(package=package, profile=SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None), user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return package

    with pytest.raises(HTTPException) as exc:
        await pkgs.apply_changes(SimpleNamespace(get=get, flush=AsyncMock()), access, changes={"debt_service": 1}, version=1)
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "maintained_by_term_sheet"


async def test_delete_guard_reads_every_row():
    rows = [SimpleNamespace(status="void"), SimpleNamespace(status="draft")]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))))
    await signing.delete_guard(db, uuid.uuid4())  # nothing retained yet
    rows.append(SimpleNamespace(status="executed"))
    with pytest.raises(HTTPException) as exc:
        await signing.delete_guard(db, uuid.uuid4())
    assert exc.value.status_code == 409


async def test_stage_two_gates_require_cleared_funding_and_matching_attestation():
    from datetime import date as _date
    user = _user(Role.LOAN_EXEC)
    parent = SimpleNamespace(status="executed", executed_at=datetime(2026, 9, 1, tzinfo=UTC))
    access = pkgs.PackageAccess(package=SimpleNamespace(id=uuid.uuid4(), stage=2, status="draft"), profile=SimpleNamespace(id=uuid.uuid4()), user=user, mode="operator")
    access.parent = parent
    arr = {**pa.empty_arrangement(), "funding_date": "2999-01-01", "funded_amount": 1000000, "funding_party_name": "First Bank"}
    with pytest.raises(HTTPException) as exc:
        await signing._stage_two_gates(SimpleNamespace(), access, arr, None)
    assert exc.value.detail["code"] == "funding_not_yet_occurred"
    arr["funding_date"] = _date.today().isoformat()
    with pytest.raises(HTTPException) as exc:
        await signing._stage_two_gates(SimpleNamespace(), access, arr, None)
    assert exc.value.detail["code"] == "funding_attestation_required"
    with pytest.raises(HTTPException) as exc:
        await signing._stage_two_gates(SimpleNamespace(), access, arr, {"confirm": True, "actual_funding_date": arr["funding_date"], "amount_funded": 999999, "funding_party_name": "First Bank"})
    assert exc.value.detail["code"] == "funding_mismatch" and "amount funded" in exc.value.detail["fields"]
    ok = await signing._stage_two_gates(SimpleNamespace(), access, arr, {"confirm": True, "actual_funding_date": arr["funding_date"], "amount_funded": 1000000, "funding_party_name": "first bank"})
    assert ok["attested_by_user_id"] == str(user.id) and ok["amount_funded"] == 1000000
