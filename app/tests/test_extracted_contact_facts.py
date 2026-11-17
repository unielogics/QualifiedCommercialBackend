"""A phone fact must be a phone and an email fact an email before it is kept."""

from __future__ import annotations

import pytest

from app.services.application_profiles import contact_fact_reads


@pytest.mark.parametrize(
    ("key", "value", "kept"),
    [
        ("phone", "Jordan Sample", False),
        ("business_phone", "Jordan Sample", False),
        ("contact_phone", "(704) 555-0123", True),
        ("phone", "+44 20 7946 0958", True),
        ("Main Phone", "704-555-0123", True),
        ("email", "jordan@example.com", True),
        ("borrower_email", "Jordan Sample", False),
        ("business_email", "not an email@", False),
        ("principal_name", "Jordan Sample", True),
        ("entity_type", "LLC", True),
    ],
)
def test_contact_facts_must_read_as_what_they_claim(key: str, value: str, kept: bool) -> None:
    assert contact_fact_reads(key, value) is kept
