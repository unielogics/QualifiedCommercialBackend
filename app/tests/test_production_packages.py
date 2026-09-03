"""Production Package: stamping, access semantics, send preconditions, prefill."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
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


def _access(status: str = "draft", role: Role = Role.LOAN_EXEC, mode: str = "operator", link=None) -> pkgs.PackageAccess:
    package = SimpleNamespace(id=uuid.uuid4(), status=status, version=1, arrangement={}, prefill_provenance={})
    profile = SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    return pkgs.PackageAccess(package=package, profile=profile, user=_user(role), mode=mode, link=link)


def test_capabilities_by_role_and_status():
    draft_exec = _access("draft", Role.LOAN_EXEC).capabilities()
    assert draft_exec.can_edit and draft_exec.can_send and draft_exec.can_share and not draft_exec.can_record
    sent_admin = _access("out_for_signature", Role.SUPER_ADMIN).capabilities()
    assert not sent_admin.can_edit and sent_admin.can_record and sent_admin.can_execute and sent_admin.can_reopen
    sent_exec = _access("out_for_signature", Role.LOAN_EXEC).capabilities()
    assert sent_exec.can_reopen and not sent_exec.can_record and not sent_exec.can_void
    rep = _access("draft", Role.FIELD_REP, mode="rep", link=SimpleNamespace(id=uuid.uuid4())).capabilities()
    assert rep.can_edit and rep.can_generate and not rep.can_send and not rep.can_share and not rep.can_pick_sponsor
    rep_sent = _access("out_for_signature", Role.FIELD_REP, mode="rep", link=SimpleNamespace(id=uuid.uuid4())).capabilities()
    assert not rep_sent.can_edit
    executed = _access("executed", Role.SUPER_ADMIN).capabilities()
    assert not (executed.can_edit or executed.can_send or executed.can_void or executed.can_execute)


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
    package = SimpleNamespace(id=link.package_id, profile_id=uuid.uuid4(), status="draft")
    profile = SimpleNamespace(id=package.profile_id, vertical="dealer", dealer_id=uuid.uuid4(), intake_id=None)
    training = SimpleNamespace(is_training=True, archived_at=None)

    async def get(model, key):
        return {package.id: package, profile.id: profile, profile.dealer_id: training}.get(key)

    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: link)), get=get, flush=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "tok")
    assert exc.value.status_code == 404
    training.is_training = False
    training.archived_at = datetime.now(UTC)
    with pytest.raises(HTTPException) as exc:
        await pkgs.resolve_rep_share(db, rep, "tok")
    assert exc.value.status_code == 410
    training.archived_at = None
    access = await pkgs.resolve_rep_share(db, rep, "tok")
    assert access.mode == "rep" and access.editable and link.use_count == 1


async def test_rep_cannot_touch_sponsor_keys():
    rep = _user(Role.FIELD_REP)
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=3, arrangement={}, prefill_provenance={})
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
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=1, arrangement={"dealer_name": "Old"},
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
    package = SimpleNamespace(id=uuid.uuid4(), status="out_for_signature", version=1, arrangement={}, prefill_provenance={})
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
                              prefill_provenance={}, stage=1, delivery_history=[])
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
                              stage=1, delivery_history=[], sponsor_company_id=None)
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
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=dealer_id, intake_id=uuid.uuid4(), entity_type="s_corp", vertical="dealer")
    dealer = SimpleNamespace(id=dealer_id, legal_name="Delgado Auto Group LLC", name="Delgado", entity_type="llc",
                             address="4411 Gulf Fwy", city="Houston", state="TX", zip="77023",
                             client_requested_amount=None, funding_goal=900000)
    dap = SimpleNamespace(dba_name="Delgado Auto Sales", state_of_formation="Texas", mailing_address=None, mailing_city=None,
                          mailing_state=None, mailing_zip=None, signer_title="Managing member", term_requested_months=24,
                          monthly_debt_payments=41300)
    intake = SimpleNamespace(business_name="Other Name", full_name="Rafael Delgado", email="r@example.com", phone="+19735550148",
                             requested_loan_amount=1200000, intake_state={"dealer_details": {"stated_monthly_debt_payments": 39000}},
                             result_snapshot={"key_metrics": {"monthly_debt_service": "$40,000"}})
    owner = SimpleNamespace(first_name="Rafael", last_name="Delgado", is_primary=True)

    async def get(model, key):
        return {dealer_id: dealer, profile.intake_id: intake}.get(key)

    db = SimpleNamespace(get=get, execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: dap)))
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
