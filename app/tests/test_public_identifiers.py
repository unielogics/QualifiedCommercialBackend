from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.bucket import BucketRequestedDocument
from app.routers.buckets import _public_requested_document_read
from app.schemas.application_profile import ApplicationEvidenceSummary
from app.services.application_profiles import client_evidence_banking_summary
from app.services.public_identifiers import public_requirement_key


def test_partner_requirement_alias_preserves_public_correlations_without_source_key():
    key = "arf_equifax_score"
    public = public_requirement_key(key)
    assert public == public_requirement_key(key)
    assert public.startswith("requirement_")
    assert "arf" not in public
    assert public != public_requirement_key("arf_equifax_verified")


def test_existing_generic_requirement_keys_and_empty_values_are_compatible():
    assert public_requirement_key("business_bank_statements_6_months") == "business_bank_statements_6_months"
    assert public_requirement_key(None) is None


@pytest.mark.asyncio
async def test_borrower_document_and_summary_use_same_source_safe_alias():
    internal_key = "arf_equifax_score"
    document = BucketRequestedDocument(
        id=uuid4(), bucket_id=uuid4(), template_id=None, name="Credit score",
        category="Credit", description=None, required=True, allow_multiple_files=True,
        status="requested", is_custom=False, requires_signature=False,
        signature_kind=None, template_file_id=None, signature_document_text=None,
        requirement_key=internal_key, requirement_source={"source_program_keys": ["arf_bankroll"]},
    )
    item = SimpleNamespace(
        requirement_key=internal_key, label="Credit score", required_level="required",
        status="missing", client_visible=True, evidence_files=[], evidence_count=0,
        verified_coverage_complete=False, verified_coverage=None, coverage={},
    )
    with patch("app.services.application_profiles.ensure_supporting_document_group", AsyncMock(return_value=None)):
        summary = await client_evidence_banking_summary(
            AsyncMock(), SimpleNamespace(), readiness=SimpleNamespace(requirements=[item]),
            verification=SimpleNamespace(business_banking_complete=False),
            banking=SimpleNamespace(items=[], evidence_summary=ApplicationEvidenceSummary()),
        )
    public_document = _public_requested_document_read(document, [])
    assert summary.requirements[0].requirement_key == public_document.requirement_key
    assert public_document.requirement_key == public_requirement_key(internal_key)
    assert public_document.requirement_source is None
    assert document.requirement_key == internal_key
    assert "arf_" not in summary.model_dump_json()
