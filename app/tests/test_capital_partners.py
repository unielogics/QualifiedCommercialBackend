"""Capital Partners: the gate, the subscription rules, the Form D clock, the
document log, transfers, and the two auth seams in app.deps.

Runs the real routers against an in-memory SQLite copy of the schema (the
pattern in test_application_submission_persistence.py). Emails, storage and
Clerk are patched; everything else is the production code path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app import deps as app_deps
from app.capital_partners import enums as E
from app.capital_partners import models as cp
from app.capital_partners import router_admin, router_member, router_public
from app.capital_partners.services import emails, storage
from app.db import Base, get_db
from app.deps import get_current_user
from app.enums import Role
from app.models.activity import Activity
from app.models.user import User
from app.models.user_access import UserProductAccess


@compiles(JSONB, "sqlite")
def _sqlite_json_type(_type, _compiler, **_kwargs):
    return "JSON"


MODELS = [
    User, UserProductAccess, Activity, cp.CpIssuerSettings, cp.CpAccessRequest, cp.CpMember, cp.CpMemberEntity, cp.CpQuestionnaire,
    cp.CpListing, cp.CpListingDocument, cp.CpListingDocumentGrant, cp.CpListingEvent, cp.CpSubscription, cp.CpParticipation, cp.CpTransfer,
    cp.CpFiling, cp.CpDocumentAccess,
]


async def make_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    tables = [metadata.tables[model.__tablename__] for model in MODELS]
    for table in tables:
        for column in table.columns:
            if column.server_default is not None:
                literal = str(column.server_default.arg)
                if "::jsonb" in literal:
                    column.server_default = sa.DefaultClause(sa.text(literal.replace("::jsonb", "")))
                elif literal == "now()":
                    column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
        for index in table.indexes:
            condition = index.dialect_options["postgresql"].get("where")
            if condition is not None:
                index.dialect_options["sqlite"]["where"] = condition
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: metadata.create_all(sync, tables=tables))
    return engine


class Harness:
    """One app, one session, a switchable current user."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.current: User | None = None
        app = FastAPI()
        for r in (router_public.router, router_member.router, router_admin.router):
            app.include_router(r, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: self.current
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)

    async def user(self, email: str, role: Role, name: str = "Person") -> User:
        u = User(clerk_id=f"clerk_{email}", email=email, name=name, role=role)
        self.db.add(u)
        await self.db.flush()
        return u


async def harness():
    engine = await make_engine()
    db = AsyncSession(engine, expire_on_commit=False)
    await db.__aenter__()
    return Harness(db)


def _patches():
    return [
        patch.object(emails, "_send", AsyncMock(return_value=True)),
        patch.object(storage, "configured", lambda: True),
        patch.object(storage, "presign_get", lambda *a, **k: "https://s3.example/doc?sig=1"),
        patch.object(storage, "presign_put", lambda key, **k: {"upload_url": "https://s3.example/put", "headers": {"Content-Type": "application/pdf"}, "s3_key": key}),
        patch.object(storage, "head_object", lambda key: {"size_bytes": 1234, "content_type": "application/pdf"}),
        patch.object(router_admin.clerk_service, "invite_user", AsyncMock(return_value={"id": "inv_test"})),
    ]


class Scenario:
    def __init__(self, h: Harness):
        self.h = h

    async def admin(self) -> User:
        return await self.h.user("desk@example.com", Role.SUPER_ADMIN, "Desk")

    async def member(self, email: str = "fo@example.com", *, state: str = "NJ", cleared: bool = True) -> tuple[User, cp.CpMember]:
        u = await self.h.user(email, Role.CAPITAL_PARTNER, "Family Office")
        m = cp.CpMember(user_id=u.id, status=E.MemberStatus.REQUESTED, display_name="M. Ridgeway", state_of_residence=state)
        m.entities.append(cp.CpMemberEntity(name="Ridgeway Family Trust", kind="trust", is_default=True))
        self.h.db.add(m)
        await self.h.db.flush()
        if cleared:
            now = datetime.now(UTC)
            m.status = E.MemberStatus.APPROVED
            m.relationship_established_at = now - timedelta(days=31)
            m.waiting_period_days = 30
            m.waiting_period_ends_at = now - timedelta(days=1)
            m.approved_at = now
            await self.h.db.flush()
        return u, m

    async def listing(self, admin: User, *, publish: bool = True, offered: str = "1000000", minimum: str = "25000") -> cp.CpListing:
        self.h.current = admin
        r = await self.h.client.post("/api/v1/capital/admin/listings", json={
            "vertical": "real_estate", "name": "Multifamily acquisition bridge", "program_label": "Bridge", "city": "Newark",
            "state": "nj", "collateral_type": "Multifamily", "note_rate_pct": "11.25", "term_months": 12, "ltv_pct": "68",
            "loan_amount": "2400000", "offered_amount": offered, "minimum_investment": minimum, "personal_guarantee": True,
        })
        assert r.status_code == 201, r.text
        lid = r.json()["id"]
        if publish:
            # These lifecycle tests exercise grandfathered offerings. New uploads
            # have their own explicit, source-bound review lifecycle tests.
            listing = await self.h.db.get(cp.CpListing, UUID(lid))
            await self.h.db.refresh(listing, attribute_names=["documents"])
            legacy = cp.CpListingDocument(id=uuid4(), listing_id=listing.id, kind=E.DocumentKind.PPM,
                name="Previously reviewed PPM.pdf", s3_key="synthetic/legacy-ppm.pdf", content_type="application/pdf",
                size_bytes=1234, version=1, status=E.DocumentStatus.READY, uploaded_by_id=admin.id, uploaded_at=datetime.now(UTC))
            listing.documents.append(legacy)
            self.h.db.add(legacy)
            await self.h.db.flush()
            pub = await self.h.client.post(f"/api/v1/capital/admin/listings/{lid}/publish", json={"reason": "Desk approved", "confirmed": True})
            assert pub.status_code == 200, pub.text
            assert pub.json()["status"] == "open"
        return await self.h.db.get(cp.CpListing, __import__("uuid").UUID(lid))


async def approve_uploaded_ppm(h: Harness, path: str) -> dict:
    """Explicitly bind synthetic upload metadata and review its frozen bytes."""
    state = (await h.client.get(path + "/ppm")).json()
    assert state["current_draft"]["source"] == "uploaded"
    legal = {row["key"]: f"Staff supplied {row['label']}" for row in state["legal_fields"] if row["required"]}
    legal["offering_currency"] = "USD"
    saved = await h.client.patch(path + "/ppm/draft", json={"expected_version": state["current_draft"]["version"],
        "expected_source_fingerprint": state["source_fingerprint"], "idempotency_key": str(uuid4()),
        "legal_inputs": legal, "narrative_sections": {}})
    assert saved.status_code == 200, saved.text
    state = saved.json()
    reviewed = await h.client.post(path + "/ppm/review", json={"expected_version": state["current_draft"]["version"],
        "expected_source_fingerprint": state["source_fingerprint"], "idempotency_key": str(uuid4()),
        "confirmed": True, "reason": "Reviewed synthetic offering provisions and uploaded PDF"})
    assert reviewed.status_code == 200, reviewed.text
    return reviewed.json()


@pytest.mark.asyncio
async def test_public_access_request_persists_and_drops_bots():
    body = {"investing_as": "family_office", "entity_name": "Ridgeway FO", "capital_band": "$1M – $5M", "source": "Referred by my advisor",
            "full_name": "Maya Ridgeway", "email": "Maya@Example.com", "phone": "(917) 555-0148", "note": "Mandate: short-duration credit",
            "consent": True, "locale": "en", "website": ""}
    async with await harness() as h:
        with patch.object(emails, "_send", AsyncMock(return_value=True)):
            router_public._LAST_SUBMIT.clear()
            bot = await h.client.post("/api/v1/capital/public/access-request", json={**body, "website": "https://spam.example"})
            assert bot.status_code == 201 and bot.json()["id"] is None
            no_consent = await h.client.post("/api/v1/capital/public/access-request", json={**body, "consent": False})
            assert no_consent.status_code == 400
            ok = await h.client.post("/api/v1/capital/public/access-request", json=body)
            assert ok.status_code == 201, ok.text
            again = await h.client.post("/api/v1/capital/public/access-request", json=body)
            assert again.status_code == 429
        rows = (await h.db.execute(sa.select(cp.CpAccessRequest))).scalars().all()
        assert len(rows) == 1 and rows[0].email == "maya@example.com" and rows[0].status == "new" and rows[0].emailed
        acts = (await h.db.execute(sa.select(Activity))).scalars().all()
        assert [a.kind for a in acts] == ["capital_partner.access_request"]


@pytest.mark.asyncio
async def test_gate_blocks_until_approved_and_past_waiting_period_and_preview_cannot_subscribe():
    async with await harness() as h:
        s = Scenario(h)
        admin = await s.admin()
        with patch.multiple(storage, configured=lambda: True, presign_put=lambda key, **k: {"upload_url": "u", "headers": {}, "s3_key": key}, head_object=lambda key: {"size_bytes": 1, "content_type": "application/pdf"}), \
             patch.object(emails, "_send", AsyncMock(return_value=True)):
            listing = await s.listing(admin)
            user, member = await s.member(cleared=False)
            h.current = user
            me = await h.client.get("/api/v1/capital/me")
            assert me.status_code == 200 and me.json()["status"] == "requested" and me.json()["gate"]["cleared"] is False
            blocked = await h.client.get("/api/v1/capital/listings")
            assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "not_approved"
            # Questionnaire moves the member to review; the desk approves with a 30-day wait.
            q = await h.client.post("/api/v1/capital/questionnaire", json={"answers": {"basis": "net_worth"}, "basis": "net_worth", "investing_as": "family_office", "state_of_residence": "nj"})
            assert q.status_code == 201 and (await h.client.get("/api/v1/capital/me")).json()["status"] == "under_review"
            h.current = admin
            rv = await h.client.post(f"/api/v1/capital/admin/members/{member.id}/review", json={"decision": "approve", "waiting_period_days": 30})
            assert rv.status_code == 200 and rv.json()["status"] == "waiting" and rv.json()["gate"]["waiting_days_left"] >= 29
            h.current = user
            waiting = await h.client.get("/api/v1/capital/listings")
            assert waiting.status_code == 403 and waiting.json()["detail"]["code"] == "waiting_period"
            # The desk may shorten the wait; zero days clears at once.
            h.current = admin
            rv = await h.client.post(f"/api/v1/capital/admin/members/{member.id}/review", json={"decision": "approve", "waiting_period_days": 0})
            assert rv.json()["status"] == "approved"
            h.current = user
            page = await h.client.get("/api/v1/capital/listings")
            assert page.status_code == 200 and page.json()["total"] == 1 and page.json()["items"][0]["ref"] == listing.ref
            # Operators preview but never subscribe.
            h.current = admin
            assert (await h.client.get("/api/v1/capital/listings")).status_code == 200
            sub = await h.client.post(f"/api/v1/capital/listings/{listing.ref}/subscriptions", json={"amount": "25000", "ack_ppm": True, "ack_accredited": True})
            assert sub.status_code == 403 and sub.json()["detail"]["code"] == "preview_only"


@pytest.mark.asyncio
async def test_publish_needs_the_ppm_and_assigns_a_reference():
    async with await harness() as h:
        s = Scenario(h)
        admin = await s.admin()
        with patch.object(emails, "_send", AsyncMock(return_value=True)):
            listing = await s.listing(admin, publish=False)
            h.current = admin
            blocked = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/publish", json={"reason": "go live", "confirmed": True})
            assert blocked.status_code == 409 and "memorandum" in blocked.json()["detail"]["message"]
            assert listing.ref == "QC-RE-0001" and listing.state == "NJ"


@pytest.mark.asyncio
async def test_subscription_rules_then_signed_starts_form_d_clock_then_funded_shows_in_portfolio():
    async with await harness() as h:
        s = Scenario(h)
        admin = await s.admin()
        ps = _patches()
        for p in ps:
            p.start()
        try:
            listing = await s.listing(admin, offered="100000", minimum="25000")
            user, member = await s.member(state="NJ")
            other, _ = await s.member("other@example.com", state="TX")
            h.current = user
            url = f"/api/v1/capital/listings/{listing.ref}/subscriptions"
            below = await h.client.post(url, json={"amount": "10000", "ack_ppm": True, "ack_accredited": True})
            assert below.status_code == 409 and below.json()["detail"]["code"] == "below_minimum"
            no_ack = await h.client.post(url, json={"amount": "25000", "ack_ppm": True, "ack_accredited": False})
            assert no_ack.json()["detail"]["code"] == "acknowledgements_required"
            too_much = await h.client.post(url, json={"amount": "150000", "ack_ppm": True, "ack_accredited": True})
            assert too_much.json()["detail"]["code"] == "exceeds_remaining"
            ok = await h.client.post(url, json={"amount": "60000", "ack_ppm": True, "ack_accredited": True})
            assert ok.status_code == 201, ok.text
            dup = await h.client.post(url, json={"amount": "25000", "ack_ppm": True, "ack_accredited": True})
            assert dup.json()["detail"]["code"] == "already_subscribed"
            detail = await h.client.get(f"/api/v1/capital/listings/{listing.ref}")
            assert detail.json()["committed_amount"] == "60000.00" and detail.json()["remaining_amount"] == "40000.00"
            # The other member fills it; the listing flips to fully subscribed by itself.
            h.current = other
            fill = await h.client.post(url, json={"amount": "40000", "ack_ppm": True, "ack_accredited": True})
            assert fill.status_code == 201
            assert (await h.db.get(cp.CpListing, listing.id)).status == "fully_subscribed"
            # Nobody sees anyone else's subscriptions.
            mine = await h.client.get("/api/v1/capital/subscriptions")
            assert [x["amount"] for x in mine.json()] == ["40000.00"]
            # Desk: documents, signed (first sale), funded (participation).
            h.current = admin
            sub_id = ok.json()["id"]
            sent = await h.client.post(f"/api/v1/capital/admin/subscriptions/{sub_id}/send-documents", json={})
            assert sent.status_code == 200
            signed = await h.client.post(f"/api/v1/capital/admin/subscriptions/{sub_id}/mark-signed", json={})
            assert signed.status_code == 200, signed.text
            body = signed.json()
            assert body["first_sale_at"] is not None and body["investor_count"] == 1
            filings = (await h.client.get("/api/v1/capital/admin/filings")).json()
            kinds = sorted((f["kind"], f["state"]) for f in filings)
            assert kinds == [("form_d", None), ("state_notice", "NJ")]
            form_d = next(f for f in filings if f["kind"] == "form_d")
            first_sale = datetime.fromisoformat(body["first_sale_at"])
            assert form_d["due_at"] == (first_sale + timedelta(days=15)).date().isoformat()
            # Signing again for the same state creates nothing new; marking Form D filed schedules the amendment.
            filed = await h.client.patch(f"/api/v1/capital/admin/filings/{form_d['id']}", json={"status": "filed", "reference": "0001234567-26-000001"})
            assert filed.status_code == 200
            kinds = sorted(f["kind"] for f in (await h.client.get("/api/v1/capital/admin/filings")).json())
            assert kinds == ["form_d", "form_d_amendment", "state_notice"]
            funded = await h.client.post(f"/api/v1/capital/admin/subscriptions/{sub_id}/mark-funded", json={})
            assert funded.status_code == 200 and funded.json()["participations"][0]["invested_amount"] == "60000.00"
            summary = (await h.client.get("/api/v1/capital/admin/filings/summary")).json()
            assert summary["state_notices_due"] == 1 and summary["form_d_due"] == 0
            h.current = user
            pf = (await h.client.get("/api/v1/capital/portfolio")).json()
            assert pf["stats"]["invested"] == "60000.00" and pf["stats"]["active"] == 1 and pf["holdings"][0]["entity_name"] is None
            h.current = other
            assert (await h.client.get("/api/v1/capital/portfolio")).json()["holdings"] == []
        finally:
            for p in ps:
                p.stop()


@pytest.mark.asyncio
async def test_document_links_are_logged_and_transfers_need_consent():
    async with await harness() as h:
        s = Scenario(h)
        admin = await s.admin()
        ps = _patches()
        for p in ps:
            p.start()
        try:
            listing = await s.listing(admin, offered="100000", minimum="10000")
            seller, seller_m = await s.member("seller@example.com")
            buyer, buyer_m = await s.member("buyer@example.com", state="TX")
            h.current = seller
            detail = (await h.client.get(f"/api/v1/capital/listings/{listing.ref}")).json()
            doc_id = detail["documents"][0]["id"]
            link = await h.client.post(f"/api/v1/capital/listings/{listing.ref}/documents/{doc_id}/url")
            assert link.status_code == 200 and link.json()["expires_in_seconds"] == 60
            logged = (await h.db.execute(sa.select(cp.CpDocumentAccess))).scalars().all()
            assert len(logged) == 1 and logged[0].member_id == seller_m.id and logged[0].document_id.hex == doc_id.replace("-", "")
            sub = await h.client.post(f"/api/v1/capital/listings/{listing.ref}/subscriptions", json={"amount": "50000", "ack_ppm": True, "ack_accredited": True})
            h.current = admin
            sid = sub.json()["id"]
            await h.client.post(f"/api/v1/capital/admin/subscriptions/{sid}/mark-signed", json={})
            funded = await h.client.post(f"/api/v1/capital/admin/subscriptions/{sid}/mark-funded", json={})
            part_id = funded.json()["participations"][0]["id"]
            # Seller lists half; buyer requests; the desk consents and completes; principal moves.
            h.current = seller
            listed = await h.client.post("/api/v1/capital/transfers", json={"participation_id": part_id, "principal_amount": "20000", "ask_pct": "99.5"})
            assert listed.status_code == 201 and listed.json()["ref"] == "TR-0001" and listed.json()["implied_yield_pct"] == "11.31"
            own = await h.client.post(f"/api/v1/capital/transfers/{listed.json()['id']}/request")
            assert own.status_code == 409 and own.json()["detail"]["code"] == "own_listing"
            h.current = buyer
            req = await h.client.post(f"/api/v1/capital/transfers/{listed.json()['id']}/request")
            assert req.status_code == 200 and req.json()["status"] == "pending_consent"
            h.current = admin
            early = await h.client.post(f"/api/v1/capital/admin/transfers/{listed.json()['id']}/complete", json={})
            assert early.status_code == 409
            assert (await h.client.post(f"/api/v1/capital/admin/transfers/{listed.json()['id']}/consent", json={})).json()["status"] == "approved"
            done = await h.client.post(f"/api/v1/capital/admin/transfers/{listed.json()['id']}/complete", json={})
            assert done.status_code == 200 and done.json()["status"] == "completed"
            h.current = seller
            assert (await h.client.get("/api/v1/capital/portfolio")).json()["stats"]["outstanding"] == "30000.00"
            h.current = buyer
            bp = (await h.client.get("/api/v1/capital/portfolio")).json()
            assert bp["stats"]["outstanding"] == "20000.00" and bp["stats"]["invested"] == "19900.00"
        finally:
            for p in ps:
                p.stop()


def test_signup_console_and_api_boundary_for_capital_partners():
    assert app_deps._signup_console_from_clerk({"unsafe_metadata": {"console": "capital"}}) == "capital"
    assert app_deps._signup_console_from_clerk({"unsafe_metadata": {"console": "funding"}}) is None
    assert app_deps._signup_console_from_clerk({"unsafe_metadata": "capital"}) is None
    assert app_deps._signup_console_from_clerk(None) is None

    user = SimpleNamespace(role=Role.CAPITAL_PARTNER)

    def req(path):
        return SimpleNamespace(url=SimpleNamespace(path=path))

    app_deps._enforce_external_product_boundary(user, req("/api/v1/capital/me"))
    app_deps._enforce_external_product_boundary(user, req("/api/v1/capital/listings/QC-RE-0001"))
    app_deps._enforce_external_product_boundary(user, req("/api/v1/auth/me"))
    for path in ("/api/v1/loans", "/api/v1/clients", "/api/v1/dealer-os/dealers", "/api/v1/capitalx"):
        with pytest.raises(Exception) as excinfo:
            app_deps._enforce_external_product_boundary(user, req(path))
        assert excinfo.value.status_code == 403 and excinfo.value.detail["code"] == "product_access_required"
