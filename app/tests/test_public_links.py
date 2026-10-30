"""One address for a client's secure link: a token resolves to the screen it
opens, and to nothing when no kind knows it."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

from app.routers import public_links


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalars(self) -> "_Result":
        return self

    def first(self) -> object:
        return self._row


class _DB:
    """Answers each lookup in order with the canned row for that table, else None."""

    def __init__(self, hits: dict[str, object] | None = None) -> None:
        self.hits = hits or {}
        self.statements: list[str] = []

    async def execute(self, statement) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        for table, row in self.hits.items():
            if table in sql:
                return _Result(row)
        return _Result(None)


def test_hashed_kinds_are_looked_up_by_their_sha256_hex() -> None:
    assert public_links.sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()


async def test_a_room_token_opens_the_secure_room() -> None:
    db = _DB({"bucket_upload_links": uuid4()})
    resolved = await public_links.resolve_public_link(db, "room-token")
    assert resolved is not None and resolved.kind == "room"
    assert resolved.path == "/buckets/request/room-token"


async def test_an_intake_token_resumes_on_the_screen_its_variant_uses() -> None:
    from app.routers import dealer_ai_intake as intake_routes

    funding = SimpleNamespace(variant=intake_routes.FUNDING_VARIANT)
    db = _DB({"public_underwriting_intakes": funding})
    resolved = await public_links.resolve_public_link(db, "tok en")
    assert resolved is not None and resolved.kind == "intake"
    assert resolved.path == f"{intake_routes.FUNDING_PUBLIC_PATH}?token=tok%20en"

    mca = SimpleNamespace(variant=intake_routes.MCA_VARIANT)
    resolved = await public_links.resolve_public_link(_DB({"public_underwriting_intakes": mca}), "t")
    assert resolved is not None and resolved.path == f"{intake_routes.MCA_PUBLIC_PATH}?token=t"


async def test_a_form_token_opens_its_kind_and_a_worksheet_its_grid() -> None:
    pfs = SimpleNamespace(kind="pfs")
    resolved = await public_links.resolve_public_link(_DB({"financial_form_links": pfs}), "f1")
    assert resolved is not None and resolved.kind == "form" and resolved.path == "/forms/pfs/f1"
    sheet = SimpleNamespace(kind="worksheet")
    resolved = await public_links.resolve_public_link(_DB({"financial_form_links": sheet}), "f2")
    assert resolved is not None and resolved.path == "/forms/worksheet/f2"


async def test_the_other_kinds_land_on_their_own_screens() -> None:
    cases = {
        "bucket_shares": ("share", "/buckets/share/x"),
        "bucket_public_shares": ("public_share", "/buckets/public-share/x"),
        "production_package_share_links": ("production_package", "/production-package/link/x"),
        "lender_submissions": ("lender_submission", "/lender-submissions/x"),
        "application_verification_invitations": ("bank_verification", "/application-verification#t=x"),
        "hud_share_links": ("hud_share", "/hud/share/x"),
    }
    for table, (kind, path) in cases.items():
        resolved = await public_links.resolve_public_link(_DB({table: uuid4()}), "x")
        assert resolved is not None, table
        assert (resolved.kind, resolved.path) == (kind, path), table


async def test_an_unknown_blank_or_oversized_token_resolves_to_nothing() -> None:
    assert await public_links.resolve_public_link(_DB(), "nobody-minted-this") is None
    db = _DB({"bucket_upload_links": uuid4()})
    assert await public_links.resolve_public_link(db, "   ") is None
    assert await public_links.resolve_public_link(db, "x" * 201) is None
    assert db.statements == [], "a blank or oversized token must not reach the database"


async def test_the_route_answers_404_when_nothing_knows_the_token() -> None:
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await public_links.resolve_link("nobody-minted-this", _DB())
    assert excinfo.value.status_code == 404
