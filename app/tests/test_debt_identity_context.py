"""Reviewed loan identity follows the accepted row without changing figures."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import debt_package_evidence as package
from app.services.financial_publication import current_version_context, debt_row_identity_context
from app.services.financial_workspace import facts_for_body
from app.services.public_underwriting_packet_pdf import _debt_evidence_section


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


def saved_context():
    profile_id, file_id = uuid4(), uuid4()
    sources = [uuid4(), uuid4(), uuid4()]
    debts = [{"id": str(uuid4()), "lender": "SBA", "loan_reference": f"001234567{index}",
              "balance": "1000", "monthly_payment": "100", "status": "active",
              "count_in_dscr": index != 2,
              "evidence_links": [{"file_id": str(sources[index]), "role": "agreement"}]}
             for index in range(3)]
    debts += [{"id": str(uuid4()), "lender": "Removed", "status": "dismissed", "loan_reference": "9999999999", "balance": "50000", "monthly_payment": "900"},
              {"id": str(uuid4()), "lender": "Duplicate", "status": "duplicate", "duplicate_of_id": debts[0]["id"], "loan_reference": "8888888888", "balance": "50000", "monthly_payment": "900"}]
    body = {"business_name": "Example business", "debts": debts}
    version = SimpleNamespace(id=uuid4(), version_number=3, created_at=datetime.now(UTC), pdf_file_id=file_id, source_file_id=None,
                              source="form", status="filed", body=body, key_facts=facts_for_body("debt_schedule", body))
    family = SimpleNamespace(id=uuid4(), profile_id=profile_id, kind="debt_schedule", label="Debt schedule", current_version_id=version.id)
    return family, version, sources


@pytest.mark.asyncio
@pytest.mark.parametrize("client_safe", [False, True])
async def test_accepted_identity_is_separate_from_unchanged_amounts_and_excludes_removed_rows(client_safe):
    family, version, sources = saved_context()
    before_body, before_facts = deepcopy(version.body), deepcopy(version.key_facts)
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([(family, version)])))
    value = (await current_version_context(db, profile_id=family.profile_id, visible_file_ids={version.pdf_file_id, *sources}, client_safe=client_safe))[0]
    assert value["debt_service_for_dscr"] == 200
    assert value["key_facts"] == before_facts and before_facts["total_monthly_debt_service"] == 300
    identities = value["debt_rows"]
    assert [row["ordinal"] for row in identities] == [1, 2, 3]
    assert [row["loan_reference"] for row in identities] == [f"001234567{index}" for index in range(3)]
    assert [row["row_id"] for row in identities] == [row["id"] for row in version.body["debts"][:3]]
    assert all(row["lender"] == "SBA" and "balance" not in row and "monthly_payment" not in row for row in identities)
    assert "9999999999" not in str(identities) and "8888888888" not in str(identities)
    assert version.body == before_body and version.key_facts == before_facts
    assert "current_version_id" in str(db.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_scoped_context_filters_supporting_ids_and_never_grants_access_from_a_supporting_file():
    family, version, sources = saved_context()
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([(family, version)])))
    value = (await current_version_context(db, visible_file_ids={version.pdf_file_id, sources[0]}))[0]
    assert value["debt_rows"][0]["documents"] == [{"file_id": str(sources[0]), "role": "agreement"}]
    assert value["debt_rows"][1]["documents"] == value["debt_rows"][2]["documents"] == []
    assert str(sources[1]) not in str(value) and str(sources[2]) not in str(value)
    assert await current_version_context(db, visible_file_ids={sources[0]}) == []


@pytest.mark.asyncio
async def test_partial_current_version_cannot_expose_accepted_row_metadata():
    family, version, _sources = saved_context()
    version.body["debts"][0]["monthly_payment"] = None
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([(family, version)])))
    value = (await current_version_context(db, profile_id=family.profile_id))[0]
    assert not value["authoritative"] and "debt_rows" not in value


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pfs", "real_estate_schedule"])
async def test_client_safe_personal_form_exclusions_remain(kind):
    family, version, _sources = saved_context()
    family.kind = kind
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([(family, version)])))
    assert await current_version_context(db, profile_id=family.profile_id, client_safe=True) == []


@pytest.mark.asyncio
async def test_legacy_context_without_identity_or_links_keeps_the_existing_contract():
    family, version, _sources = saved_context()
    for row in version.body["debts"]:
        row.pop("loan_reference", None)
        row.pop("evidence_links", None)
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows([(family, version)])))
    value = (await current_version_context(db, profile_id=family.profile_id))[0]
    assert "debt_rows" not in value
    assert debt_row_identity_context(version.body) == []
    assert value["key_facts"] == version.key_facts and value["debt_service_for_dscr"] == 200


def test_explicitly_cleared_links_do_not_return_and_references_are_display_bounded():
    family, version, sources = saved_context()
    row = version.body["debts"][0]
    row.update(evidence_links=[], documents=[{"file_id": str(sources[0]), "role": "agreement"}])
    assert debt_row_identity_context(version.body)[0]["documents"] == []
    for unsafe in ("123-45-6789", "SSN 123456789", "<script>alert(1)</script>", "x" * 81):
        assert package.reviewed_loan_reference({"loan_reference": unsafe}) is None


def test_package_headings_distinguish_three_sba_accounts_without_rewriting_lenders():
    _family, version, sources = saved_context()
    files = [SimpleNamespace(id=source, file_name=f"Loan {index}.png", content_type="image/png", content_hash="a" * 64,
                             status="uploaded", deleted_at=None, document_version_status="current") for index, source in enumerate(sources)]
    before = deepcopy(version.body)
    value = package.build_manifest(version, files, scoped_ids=sources, latest_ids=sources, reviewed_payoff_ids=[])
    assert [row["lender"] for row in value["debts"]] == ["SBA", "SBA", "SBA"]
    html = _debt_evidence_section({"debt_evidence": value})
    text = package.index_text(value)
    index = package.index_html(value)
    for ordinal in range(1, 4):
        label = f"{ordinal}. SBA - loan 001234567{ordinal - 1}"
        assert label in html and label in text and label in index
    assert version.body == before
