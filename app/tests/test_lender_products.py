"""The roster's product vocabulary is its own list, not LoanType.

A card-processing partner is a lender the desk emails, never a Loan.type; the
six real-estate types stay exactly what the loan page matches on; and every
other list the firm keeps (LoanProgram, the Main Street programmes) is
representable, so nothing the desk sells is unnameable on the roster.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from pydantic import ValidationError

from app.enums import LoanProgram, LoanType
from app.routers.admin import _to_lender_products
from app.schemas.lender import LenderCreate, LenderRead, LenderUpdate
from app.services import lender_products as lp
from app.services.main_street_programs import MAIN_STREET_PROGRAM_LABELS


def test_real_estate_group_is_exactly_loan_type():
    assert tuple(t.value for t in LoanType) == lp.REAL_ESTATE_PRODUCTS
    for value in LoanType:
        assert lp.group_of(value.value) == "real_estate"


def test_every_existing_vocabulary_is_representable():
    for program in LoanProgram:
        assert lp.is_known_product(program.value), program
    for key in MAIN_STREET_PROGRAM_LABELS:
        assert lp.is_known_product(key), key
    assert lp.group_of("merchant_processing") == "services"
    assert lp.LENDER_PRODUCT_LABELS["term_loan_3_5_year"] == MAIN_STREET_PROGRAM_LABELS["term_loan_3_5_year"]


def test_groups_do_not_overlap_and_every_key_has_a_label():
    seen: set[str] = set()
    for keys in lp.LENDER_PRODUCT_GROUPS.values():
        for key in keys:
            assert key not in seen, key
            seen.add(key)
            assert lp.LENDER_PRODUCT_LABELS[key]
    assert seen == set(lp.LENDER_PRODUCT_KEYS)


def test_normalize_rejects_unknown_and_dedupes():
    assert lp.normalize_products(["dscr", LoanType.DSCR, " merchant_processing "]) == ["dscr", "merchant_processing"]
    with pytest.raises(ValueError, match="nope"):
        lp.normalize_products(["dscr", "nope"])


def test_only_real_estate_products_can_match_a_loan():
    assert lp.has_lending_product(["merchant_processing", "sba"]) is False
    assert lp.has_lending_product(["merchant_processing", "bridge"]) is True


def test_read_schema_tolerates_a_legacy_row_but_writes_are_validated():
    now = datetime.datetime.now(datetime.UTC)
    row = LenderRead(id=uuid.uuid4(), name="Legacy", products=["sba_7a"], created_at=now, updated_at=now)
    assert row.products == ["sba_7a"]
    with pytest.raises(ValidationError):
        LenderCreate(name="Bad", products=["sba_7a"])
    assert LenderCreate(name="Partner", products=["merchant_processing"]).products == ["merchant_processing"]
    assert LenderUpdate(products=None).products is None
    with pytest.raises(ValidationError):
        LenderUpdate(products=["other"])


def test_capital_partner_promotion_maps_the_public_form_ids():
    assert _to_lender_products(["SBA 7(a)", "Commercial", "Multifamily", "Other", "DSCR", "fix-and-flip"]) == [
        "sba",
        "real_estate_backed",
        "dscr",
        "fix_and_flip",
    ]
    assert _to_lender_products([]) == []


def test_list_filter_rejects_an_unknown_product():
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.routers.lenders import list_lenders

    user = lambda role: SimpleNamespace(role=role)  # noqa: E731
    from app.enums import Role

    with pytest.raises(HTTPException) as err:
        asyncio.run(list_lenders(user(Role.SUPER_ADMIN), db=None, product="nope", active_only=False))
    assert err.value.status_code == 422
