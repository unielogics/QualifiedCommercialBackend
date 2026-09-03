"""Ties the signing core to the template pipeline and the stored-signature placement."""

from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import pdf_stamping
from app.services import production_agreements as agreements
from app.services import production_arrangement as pa
from app.services import production_fields as fields
from app.services import production_packages as pkgs
from app.services import production_signing as signing

fitz = pytest.importorskip("fitz")
sys.path.insert(0, "app/tests")
from test_production_arrangement import seed  # noqa: E402


def _anchored_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for token in ("[[SIG:qc:1]]", "[[DATE:qc:1]]", "[[INI:qc:1]]", "[[SIG:dealer:1]]", "[[DATE:dealer:1]]", "[[INI:dealer:1]]",
                  "[[SIG:sponsor:1]]", "[[DATE:sponsor:1]]", "[[SIG:rm:1]]", "[[DATE:rm:1]]", "[[SIG:dealer:2]]"):
        page.insert_text((60, y), token, fontname="helv", fontsize=9)
        y += 40
    return doc.tobytes()


def _stored(name: str, title: str | None = None):
    return SimpleNamespace(id=uuid.uuid4(), typed_name=name, title=title, adopted_at=datetime(2026, 9, 1, tzinfo=UTC),
                           adoption_consent_version="2026-09-03-1", source="self_adopted", signature_sha256="ab" * 32)


def test_place_signatures_stamps_qc_sponsor_and_rm_and_records_each():
    raw = _anchored_pdf()
    placed = {"qc": _stored("Denny Matos", "CEO"), "sponsor": _stored("Jane Sponsor", "President"), "rm": _stored("Marisol Vega")}
    with patch.object(signing, "_now", lambda: datetime(2026, 9, 3, tzinfo=UTC)):
        with patch("app.services.stored_signatures.signature_png", lambda sig: None):
            out, records = signing._place_signatures(raw, placed, datetime(2026, 9, 3, tzinfo=UTC))
    assert [r["party"] for r in records] == ["qc", "sponsor", "rm"]
    page = fitz.open(stream=out, filetype="pdf")[0]
    assert page.search_for("/s/ Denny Matos") and page.search_for("/s/ Jane Sponsor") and page.search_for("/s/ Marisol Vega")
    assert not page.search_for("[[SIG:qc:1]]") and page.search_for("[[SIG:dealer:1]]"), "dealer blocks stay for the fresh signature"
    assert records[0]["document_sha256_before"] == hashlib.sha256(raw).hexdigest()
    assert records[0]["stats"]["blocks"] == 1 and records[0]["stats"]["initials"] == 1
    # the dealer's fresh signature then lands on both dealer blocks and the initials line
    signed, stats = pdf_stamping.stamp_party(out, party="dealer", typed_name="Rafael Delgado", signature_png=None,
                                             signed_at=datetime(2026, 9, 4, tzinfo=UTC), initials="RD", scheme=pdf_stamping.STAMP_SCHEME_TEMPLATE)
    assert stats["blocks"] == 2 and stats["initials"] == 1
    clean = pdf_stamping.redact_remaining_anchors(signed)
    assert not any(fitz.open(stream=clean, filetype="pdf")[0].search_for(t) for t in ("[[SIG", "[[DATE", "[[INI"))


def test_place_signatures_tolerates_a_missing_rm_block_but_not_a_missing_qc_block():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "[[SIG:qc:1]]")
    page.insert_text((60, 120), "[[SIG:sponsor:1]]")
    raw = doc.tobytes()
    placed = {"qc": _stored("Denny Matos"), "sponsor": _stored("Jane Sponsor"), "rm": _stored("Marisol Vega")}
    with patch("app.services.stored_signatures.signature_png", lambda sig: None):
        _out, records = signing._place_signatures(raw, placed, datetime.now(UTC))
    assert [r["party"] for r in records] == ["qc", "sponsor"]
    doc2 = fitz.open()
    doc2.new_page().insert_text((60, 80), "[[SIG:sponsor:1]]")
    with patch("app.services.stored_signatures.signature_png", lambda sig: None):
        with pytest.raises(ValueError):
            signing._place_signatures(doc2.tobytes(), placed, datetime.now(UTC))


def test_filled_templates_carry_anchors_for_every_placed_party_and_strip_cleanly():
    arr = seed()
    c = pa.compute(arr)
    sponsor = {"name": "Acme Warranty Administrators Inc", "entity_type": "Corporation", "state_of_formation": "NV",
               "signer_name": "Jane Sponsor", "signer_title": "President", "agreement": {"contract_number": "QC-RPA-2026-0001", "signed_at": "2026-08-05"}}
    parties = {"dealer": {"signer_name": "Rafael Delgado", "signer_title": "Managing member", "email": "r@example.com"},
               "qc": {"signer_name": "Denny Matos", "signer_title": "CEO"}, "sponsor": {"signer_name": "Jane Sponsor"}, "relationship_manager": {"name": "Marisol Vega"}}
    ctx = {"identity": {}, "owners": [], "qc": {"notice_email": "notices@qc.example", "signer_name": "Denny Matos", "signer_title": "CEO"}, "dealer_notice": {}, "sponsor_notice": {}}
    meta = {"agreement_no": "QC-PA-TEST-R1", "revision_no": 1, "generated_on": "2026-09-03"}
    values, checks = fields.commitment_values(arr, c, sponsor, parties, ctx, meta)
    html = agreements.fill_template("commitment_v1", values, set(checks), footer=pa.STAGE_ONE_TITLE)
    for party in ("qc", "dealer", "sponsor", "rm"):
        assert f"[[SIG:{party}:1]]" in html, party
    assert "[[INI:dealer:1]]" in html
    assert agreements.strip_anchors("x [[SIG:qc:1]] y [[DATE:rm:1]] z") == "x  y  z"
    arr2, _ = pa.apply_term_sheet(arr, {"approved_amount": 1000000, "min_activation_amount": 900000, "rate_pct": 12.5, "term_months": 36,
                                        "monthly_debt_service": 33453.63, "funding_party_kind": "Lender", "funding_party_name": "First Bank",
                                        "facility_type": "Dealer capital advance", "expected_funding_date": "2026-09-10", "activation_date": "2026-09-10",
                                        "commencement_date": "2026-10-01", "maturity_date": "2029-09-10"})
    c2 = pa.compute(arr2, stage=2)
    values2, checks2 = fields.activation_values(arr2, c2, sponsor, parties, ctx, {**meta, "commitment_agreement_date": "2026-09-05"},
                                                original={"title": pa.STAGE_ONE_TITLE, "revision_no": 1}, funding={"amount_funded": 1000000})
    html2 = agreements.fill_template("activation_v1", values2, set(checks2), footer=pa.STAGE_TWO_TITLE)
    assert "[[SIG:dealer:2]]" in html2 and "[[SIG:fp:1]]" in html2 and "First Bank" in html2


async def test_send_refuses_when_a_signature_on_file_is_missing():
    user = SimpleNamespace(id=uuid.uuid4(), role=Role.LOAN_EXEC, name="Desk")
    arr = seed()
    arr["debt_service"] = 30000  # clears the covenant
    package = SimpleNamespace(id=uuid.uuid4(), status="draft", version=1, arrangement=arr, prefill_provenance={}, stage=1,
                              delivery_history=[], sponsor_company_id=uuid.uuid4(), sent_by_user_id=None, execution_pending=False, agreement_no=None)
    profile = SimpleNamespace(id=uuid.uuid4(), vertical="dealer", dealer_id=None, intake_id=None, primary_bucket_id=None)
    access = pkgs.PackageAccess(package=package, profile=profile, user=user, mode="operator")

    async def get(model, key, with_for_update=False):
        return package

    db = SimpleNamespace(get=get, flush=AsyncMock())
    status_map = {"qc": {"present": True}, "sponsor": {"present": False, "how_to_fix": "Authorize the sponsor's signature on file"}, "rm": {"present": True}, "ready": False}
    with patch.object(pkgs, "client_contact", AsyncMock(return_value=("Delgado", "owner@example.com", None))), \
         patch.object(pkgs, "require_signed_sponsor", AsyncMock(return_value=(SimpleNamespace(id=package.sponsor_company_id, name="Acme", entity_type="Corporation", state_of_formation="NV", principal_address="1 Main St"), SimpleNamespace(field_values={}, typed_name="Jane", contract_number="QC-RPA-1", document_version="v", document_hash="h", signed_at=None, id=uuid.uuid4())))), \
         patch.object(pkgs, "signatures_on_file_status", AsyncMock(return_value=status_map)), \
         patch.object(signing, "_training_guard", AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await signing.send(db, access, channel="email", recipient_email=None, recipient_phone=None, request=None)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "signature_on_file_missing" and exc.value.detail["parties"] == ["sponsor"]
