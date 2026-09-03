"""Signatures on file: the anchor stamper (template + legacy schemes) and the
adopt/revoke/read service."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.services import pdf_stamping as stamping
from app.services import stored_signatures as sigs

fitz = pytest.importorskip("fitz")

# The heading-based layout of the hand-built stage-one agreement
# (app.services.production_presentation constants), inlined so this module
# does not import the presentation stack.
DEALER_ANCHOR = "SIGNATURE - DEALER AUTHORIZED REPRESENTATIVE"
SPONSOR_ANCHOR = "SIGNATURE - SPONSOR"
QC_ANCHOR = "SIGNATURE - QUALIFIED COMMERCIAL LLC"
ELECTRONIC_PLACEHOLDER = "Electronic signature"
ELECTRONIC_DATE_PLACEHOLDER = "Signed electronically after review"
RECORDED_PLACEHOLDER = "Recorded signature"
RECORDED_DATE_PLACEHOLDER = "Recorded date"


def _template_pdf() -> bytes:
    """Two dealer signature columns, a dealer date and initials line, and a
    QC block, the way the agreement templates carry their anchors."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "By (signature)", fontname="helv", fontsize=9)
    page.insert_text((60, 120), "[[SIG:dealer:1]]", fontname="helv", fontsize=3)
    page.insert_text((60, 140), "Date", fontname="helv", fontsize=8)
    page.insert_text((90, 140), "[[DATE:dealer:1]]", fontname="helv", fontsize=3)
    page.insert_text((330, 80), "By (signature)", fontname="helv", fontsize=9)
    page.insert_text((330, 120), "[[SIG:qc:1]]", fontname="helv", fontsize=3)
    page.insert_text((60, 300), "Dealer initials", fontname="helv", fontsize=8)
    page.insert_text((140, 300), "[[INI:dealer:1]]", fontname="helv", fontsize=3)
    page2 = doc.new_page()
    page2.insert_text((60, 500), "MASTER SIGNATURE PAGE", fontname="helv", fontsize=10)
    page2.insert_text((60, 560), "[[SIG:dealer:2]]", fontname="helv", fontsize=3)
    page2.insert_text((330, 560), "[[SIG:sponsor:1]]", fontname="helv", fontsize=3)
    return doc.tobytes()


def _agreement_like_pdf() -> bytes:
    """A PDF carrying the three legacy signature blocks the way the
    hand-built agreement renders them (ported from test_production_packages)."""
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for anchor, placeholder, date_placeholder in (
        (DEALER_ANCHOR, ELECTRONIC_PLACEHOLDER, ELECTRONIC_DATE_PLACEHOLDER),
        (SPONSOR_ANCHOR, RECORDED_PLACEHOLDER, RECORDED_DATE_PLACEHOLDER),
        (QC_ANCHOR, RECORDED_PLACEHOLDER, RECORDED_DATE_PLACEHOLDER),
    ):
        page.insert_text((60, y), anchor, fontname="helv", fontsize=10)
        page.insert_text((60, y + 50), placeholder, fontname="helv", fontsize=9)
        page.insert_text((380, y + 50), date_placeholder, fontname="helv", fontsize=9)
        y += 160
    return doc.tobytes()


def _png(width: int = 120, height: int = 40) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pix.clear_with(255)
    return pix.tobytes("png")


def _text(pdf: bytes) -> str:
    return "\n".join(page.get_text("text") for page in fitz.open(stream=pdf, filetype="pdf"))


# ---- template scheme --------------------------------------------------------

def test_find_anchors_counts_every_token():
    anchors = stamping.find_anchors(_template_pdf())
    assert {k: len(v) for k, v in anchors.items()} == {
        "SIG:dealer:1": 1, "DATE:dealer:1": 1, "SIG:qc:1": 1, "INI:dealer:1": 1, "SIG:dealer:2": 1, "SIG:sponsor:1": 1,
    }
    assert anchors["SIG:dealer:2"][0][0] == 1, "page index travels with the rect"
    assert stamping.parties_in(anchors) == {
        "dealer": {"SIG": 2, "DATE": 1, "INI": 1}, "qc": {"SIG": 1}, "sponsor": {"SIG": 1},
    }
    assert stamping.has_template_anchors(_template_pdf())
    assert not stamping.has_template_anchors(_agreement_like_pdf())


def test_template_stamps_every_dealer_block_and_leaves_the_others():
    raw = _template_pdf()
    when = datetime(2026, 9, 3, tzinfo=UTC)
    stamped, counts = stamping.stamp_party(
        raw, party="dealer", typed_name="Rafael Delgado", signature_png=None, signed_at=when, initials="RD",
    )
    assert counts == {"blocks": 2, "dates": 1, "initials": 1}
    doc = fitz.open(stream=stamped, filetype="pdf")
    assert len(doc[0].search_for("/s/ Rafael Delgado")) == 1
    assert len(doc[1].search_for("/s/ Rafael Delgado")) == 1
    assert doc[0].search_for("September 03, 2026")
    ini = doc[0].search_for("RD")
    assert ini and abs(ini[0].x0 - 140) < 3, "initials land on the initials line"
    text = _text(stamped)
    for gone in ("[[SIG:dealer:1]]", "[[SIG:dealer:2]]", "[[DATE:dealer:1]]", "[[INI:dealer:1]]"):
        assert gone not in text
    for kept in ("[[SIG:qc:1]]", "[[SIG:sponsor:1]]"):
        assert kept in text, "other parties' anchors wait for their own pass"
    assert "By (signature)" in text and "Dealer initials" in text, "labels survive the white-out"
    assert hashlib.sha256(stamped).hexdigest() != hashlib.sha256(raw).hexdigest()


def test_template_draws_the_image_on_the_line_within_30pt():
    raw = _template_pdf()
    stamped, counts = stamping.stamp_party(
        raw, party="dealer", typed_name="Rafael Delgado", signature_png=_png(600, 200), signed_at=datetime.now(UTC),
    )
    assert counts["blocks"] == 2 and counts["initials"] == 0
    doc = fitz.open(stream=stamped, filetype="pdf")
    assert doc[0].get_images() and doc[1].get_images(), "drawn signature embedded on both blocks"
    infos = doc[0].get_image_info()
    assert infos
    bbox = fitz.Rect(infos[0]["bbox"])
    assert bbox.height <= 30.5 and abs(bbox.x0 - 60) < 1.5, "left-aligned on the underline, height capped"
    assert not doc[0].search_for("/s/ Rafael Delgado")


def test_template_qc_pass_after_dealer_pass_stamps_only_its_block():
    first, _ = stamping.stamp_party(_template_pdf(), party="dealer", typed_name="Rafael Delgado",
                                    signature_png=None, signed_at=datetime.now(UTC))
    second, counts = stamping.stamp_party(first, party="qc", typed_name="Denny Matos", signature_png=None,
                                          signed_at=datetime.now(UTC))
    assert counts == {"blocks": 1, "dates": 0, "initials": 0}
    text = _text(second)
    assert "[[SIG:qc:1]]" not in text and "[[SIG:sponsor:1]]" in text
    assert "/s/ Denny Matos" in text and "/s/ Rafael Delgado" in text


def test_template_refuses_a_party_without_a_block():
    with pytest.raises(ValueError):
        stamping.stamp_party(_template_pdf(), party="fp", typed_name="Nobody", signature_png=None,
                             signed_at=datetime.now(UTC))
    with pytest.raises(ValueError):
        stamping.stamp_party(_template_pdf(), party="dealer", typed_name="x", signature_png=None,
                             signed_at=datetime.now(UTC), scheme="nope")


def test_redact_remaining_anchors_clears_every_token():
    stamped, _ = stamping.stamp_party(_template_pdf(), party="dealer", typed_name="Rafael Delgado",
                                      signature_png=None, signed_at=datetime.now(UTC), initials="RD")
    assert "[[" in _text(stamped)
    clean = stamping.redact_remaining_anchors(stamped)
    text = _text(clean)
    assert "[[" not in text
    assert "/s/ Rafael Delgado" in text and "MASTER SIGNATURE PAGE" in text
    assert stamping.find_anchors(clean) == {}
    # Nothing left → the bytes come back untouched (hash-stable).
    assert stamping.redact_remaining_anchors(clean) is clean


# ---- legacy scheme ----------------------------------------------------------

def test_legacy_scheme_still_stamps_the_heading_based_layout():
    raw = _agreement_like_pdf()
    when = datetime(2026, 9, 3, tzinfo=UTC)
    stamped, counts = stamping.stamp_party(
        raw, party="sponsor", typed_name="Jane Sponsor, CEO", signature_png=None, signed_at=when,
        scheme=stamping.STAMP_SCHEME_LEGACY,
        legacy={"anchor": SPONSOR_ANCHOR, "placeholder": RECORDED_PLACEHOLDER, "date_placeholder": RECORDED_DATE_PLACEHOLDER},
    )
    assert counts == {"blocks": 1, "dates": 1, "initials": 0}
    page = fitz.open(stream=stamped, filetype="pdf")[0]
    sponsor_anchor = page.search_for(SPONSOR_ANCHOR)[0]
    qc_anchor = page.search_for(QC_ANCHOR)[0]
    sig = page.search_for("/s/ Jane Sponsor, CEO")
    assert sig and sponsor_anchor.y1 < sig[0].y0 < qc_anchor.y0
    remaining = page.search_for(RECORDED_PLACEHOLDER)
    assert len(remaining) == 1 and remaining[0].y0 > qc_anchor.y0
    assert page.search_for("September 03, 2026")


def test_legacy_scheme_with_image_and_unknown_anchor():
    stamped, _ = stamping.stamp_party(
        _agreement_like_pdf(), party="dealer", typed_name="Rafael Delgado", signature_png=_png(), signed_at=datetime.now(UTC),
        scheme="legacy",
        legacy={"anchor": DEALER_ANCHOR, "placeholder": ELECTRONIC_PLACEHOLDER, "date_placeholder": ELECTRONIC_DATE_PLACEHOLDER},
    )
    page = fitz.open(stream=stamped, filetype="pdf")[0]
    assert not page.search_for(ELECTRONIC_PLACEHOLDER) and not page.search_for(ELECTRONIC_DATE_PLACEHOLDER)
    assert page.search_for(RECORDED_PLACEHOLDER) and page.get_images()
    with pytest.raises(ValueError):
        stamping.stamp_party(_agreement_like_pdf(), party="dealer", typed_name="a", signature_png=None,
                             signed_at=datetime.now(UTC), scheme="legacy",
                             legacy={"anchor": "NOPE", "placeholder": "x", "date_placeholder": "y"})
    with pytest.raises(ValueError):
        stamping.stamp_party(_agreement_like_pdf(), party="dealer", typed_name="a", signature_png=None,
                             signed_at=datetime.now(UTC), scheme="legacy")


# ---- service ----------------------------------------------------------------

def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _user(**kw) -> SimpleNamespace:
    return SimpleNamespace(**{"id": uuid.uuid4(), "name": "Ana Reyes", "email": "ana@example.com", **kw})


def _request(ip: str = "203.0.113.9") -> SimpleNamespace:
    return SimpleNamespace(headers={"x-forwarded-for": ip, "user-agent": "pytest/1.0"}, client=None)


class _Db:
    """Just enough AsyncSession: the live-row query answers from `live`,
    add() collects rows, flush() is counted."""

    def __init__(self, live=None):
        self.live = live
        self.added: list = []
        self.flushes = 0
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, _q):
        row = self.live if (self.live is not None and self.live.revoked_at is None) else None
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushes += 1


async def test_adoption_requires_consent_and_a_drawing():
    user = _user()
    with pytest.raises(HTTPException) as exc:
        await sigs.adopt_user_signature(_Db(), user=user, signature_data_url=_data_url(_png()), typed_name="Ana Reyes",
                                        consent=False, request=_request())
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "consent_required"
    with pytest.raises(HTTPException) as exc:
        await sigs.adopt_user_signature(_Db(), user=user, signature_data_url="", typed_name="Ana Reyes",
                                        consent=True, request=_request())
    assert exc.value.detail["code"] == "signature_required"
    with pytest.raises(HTTPException) as exc:
        await sigs.adopt_user_signature(_Db(), user=user, signature_data_url="data:image/png;base64,@@@", typed_name="Ana",
                                        consent=True, request=_request())
    assert exc.value.detail["code"] == "signature_invalid"


async def test_adoption_stores_the_png_and_the_evidence():
    user = _user()
    png = _png()
    db = _Db()
    with patch.object(sigs.storage, "put_bytes", return_value=True) as put:
        row = await sigs.adopt_user_signature(db, user=user, signature_data_url=_data_url(png), typed_name="  Ana   Reyes ",
                                              title="Relationship Manager", consent=True, request=_request())
    sha = hashlib.sha256(png).hexdigest()
    put.assert_called_once_with(f"stored-signatures/users/{user.id}/{sha[:16]}.png", png, "image/png")
    assert db.added == [row] and db.flushes == 1
    assert row.subject_type == "user" and row.subject_id == user.id
    assert row.typed_name == "Ana Reyes" and row.title == "Relationship Manager"
    assert row.signature_sha256 == sha and row.source == "self_adopted"
    assert row.adoption_consent_version == sigs.STORED_SIGNATURE_CONSENT_VERSION
    assert row.adopted_by_user_id == user.id and row.adopted_ip == "203.0.113.9" and row.adopted_user_agent == "pytest/1.0"
    assert row.revoked_at is None


async def test_adoption_is_refused_when_storage_is_unconfigured():
    with patch.object(sigs.storage, "put_bytes", return_value=False):
        with pytest.raises(HTTPException) as exc:
            await sigs.adopt_user_signature(_Db(), user=_user(), signature_data_url=_data_url(_png()), typed_name="Ana",
                                            consent=True, request=None)
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "storage_unavailable"


async def test_readopting_revokes_the_previous_row_before_inserting():
    user = _user()
    previous = SimpleNamespace(id=uuid.uuid4(), revoked_at=None, revoked_by_user_id=None)
    db = _Db(live=previous)
    with patch.object(sigs.storage, "put_bytes", return_value=True):
        row = await sigs.adopt_user_signature(db, user=user, signature_data_url=_data_url(_png()), typed_name="Ana Reyes",
                                              consent=True, request=None)
    assert previous.revoked_at is not None and previous.revoked_by_user_id == user.id
    assert db.flushes == 2, "the revoke is flushed before the insert so the partial unique admits the new row"
    assert db.added == [row] and row.revoked_at is None
    # After the swap exactly one row is live.
    assert await sigs.current(db, "user", user.id) is None  # the fake only knows the retired row
    assert row.revoked_at is None


async def test_revoke_then_adopt_keeps_one_live_row():
    user = _user()
    live = SimpleNamespace(id=uuid.uuid4(), revoked_at=None, revoked_by_user_id=None)
    db = _Db(live=live)
    gone = await sigs.revoke(db, subject_type="user", subject_id=user.id, user=user, reason="new pad")
    assert gone is live and live.revoked_at is not None and live.revoked_by_user_id == user.id
    assert await sigs.revoke(db, subject_type="user", subject_id=user.id, user=user) is None, "idempotent"
    with patch.object(sigs.storage, "put_bytes", return_value=True):
        row = await sigs.adopt_user_signature(db, user=user, signature_data_url=_data_url(_png()), typed_name="Ana Reyes",
                                              consent=True, request=None)
    assert [r for r in [live, row] if r.revoked_at is None] == [row]


async def test_company_adoption_copies_the_agreement_key_and_hash():
    company_id = uuid.uuid4()
    admin = _user(name="Denny Matos")
    agreement = SimpleNamespace(
        id=uuid.uuid4(), subject_type="company", subject_id=company_id, typed_name="Jane Sponsor",
        signature_s3_key="contracts/rpa/abc/signature.png", signature_hash="f" * 64,
        field_values={"counterparty_signatory_title": "Chief Executive Officer"},
    )
    db = _Db()
    with patch.object(sigs.storage, "put_bytes") as put:
        row = await sigs.adopt_company_signature_from_agreement(
            db, company_id=company_id, agreement=agreement, admin=admin, authorization_note="Authorized per RPA §12",
            request=_request("198.51.100.4"),
        )
    put.assert_not_called()
    assert row.subject_type == "company" and row.subject_id == company_id
    assert row.signature_s3_key == agreement.signature_s3_key and row.signature_sha256 == agreement.signature_hash
    assert row.typed_name == "Jane Sponsor" and row.title == "Chief Executive Officer"
    assert row.source == "agreement" and row.source_agreement_id == agreement.id
    assert row.adopted_by_user_id == admin.id and row.adopted_ip == "198.51.100.4"
    assert row.authorization_note == "Authorized per RPA §12"
    assert row.adoption_consent_version == sigs.COMPANY_SIGNATURE_AUTHORIZATION_VERSION

    with pytest.raises(HTTPException) as exc:
        await sigs.adopt_company_signature_from_agreement(
            _Db(), company_id=uuid.uuid4(), agreement=agreement, admin=admin, authorization_note=None, request=None)
    assert exc.value.detail["code"] == "agreement_mismatch"
    agreement.signature_s3_key = None
    with pytest.raises(HTTPException) as exc:
        await sigs.adopt_company_signature_from_agreement(
            _Db(), company_id=company_id, agreement=agreement, admin=admin, authorization_note=None, request=None)
    assert exc.value.detail["code"] == "agreement_signature_missing"


async def test_company_upload_and_qc_letterhead_adoption():
    company_id = uuid.uuid4()
    admin = _user()
    png = _png()
    sha = hashlib.sha256(png).hexdigest()
    with patch.object(sigs.storage, "put_bytes", return_value=True) as put:
        row = await sigs.adopt_company_signature_upload(
            _Db(), company_id=company_id, admin=admin, signature_data_url=_data_url(png), typed_name="Jane Sponsor",
            title="CEO", authorization_note="Provided by the sponsor by email", request=None)
    put.assert_called_once_with(f"stored-signatures/companies/{company_id}/{sha[:16]}.png", png, "image/png")
    assert row.source == "admin_recorded" and row.signature_sha256 == sha and row.source_agreement_id is None

    with patch.object(sigs.storage, "get_bytes", return_value=png) as get:
        qc = await sigs.adopt_qc_signature(
            _Db(), admin=admin, signature_s3_key="firm_settings/letterhead_signature.png", signature_sha256=None,
            typed_name="Denny Matos", title="Chief Executive Officer", request=None)
    get.assert_called_once_with("firm_settings/letterhead_signature.png")
    assert qc.subject_type == "qc" and qc.subject_id is None and qc.source == "letterhead"
    assert qc.signature_s3_key == "firm_settings/letterhead_signature.png" and qc.signature_sha256 == sha
    with pytest.raises(HTTPException) as exc:
        await sigs.adopt_qc_signature(_Db(), admin=admin, signature_s3_key=None, signature_sha256=None,
                                      typed_name="Denny Matos", title=None, request=None)
    assert exc.value.detail["code"] == "letterhead_signature_missing"


async def test_current_rejects_malformed_subjects():
    with pytest.raises(ValueError):
        await sigs.current(_Db(), "dealer", uuid.uuid4())
    with pytest.raises(ValueError):
        await sigs.current(_Db(), "qc", uuid.uuid4())
    with pytest.raises(ValueError):
        await sigs.current(_Db(), "user", None)
    assert await sigs.current(_Db(), "qc", None) is None


def test_signature_png_verifies_the_hash():
    png = _png()
    sig = SimpleNamespace(id=uuid.uuid4(), signature_s3_key="k", signature_sha256=hashlib.sha256(png).hexdigest())
    with patch.object(sigs.storage, "get_bytes", return_value=png):
        assert sigs.signature_png(sig) == png
    with patch.object(sigs.storage, "get_bytes", return_value=b"tampered"):
        assert sigs.signature_png(sig) is None
    with patch.object(sigs.storage, "get_bytes", return_value=None):
        assert sigs.signature_png(sig) is None
    assert sigs.signature_png(None) is None


def test_read_model_presigns_only_when_asked():
    now = datetime.now(UTC)
    sig = SimpleNamespace(
        id=uuid.uuid4(), subject_type="user", subject_id=uuid.uuid4(), typed_name="Ana Reyes", title=None,
        source="self_adopted", adopted_at=now, adopted_by_user_id=uuid.uuid4(),
        adoption_consent_version=sigs.STORED_SIGNATURE_CONSENT_VERSION, revoked_at=None, signature_s3_key="k",
        signature_sha256="0" * 64,
    )
    with patch.object(sigs, "presign_private_s3_object", return_value="https://s3/presigned") as presign:
        plain = sigs.read_model(sig, presign=False)
        presign.assert_not_called()
        rich = sigs.read_model(sig, presign=True)
        presign.assert_called_once_with("k", ttl_seconds=900)
    assert plain["preview_url"] is None and rich["preview_url"] == "https://s3/presigned"
    assert plain["consent_version"] == sigs.STORED_SIGNATURE_CONSENT_VERSION and plain["adopted_at"] == now
    assert set(plain) == {"id", "subject_type", "subject_id", "typed_name", "title", "source", "adopted_at",
                          "adopted_by_user_id", "consent_version", "revoked_at", "preview_url"}
    assert sigs.read_model(None) is None
