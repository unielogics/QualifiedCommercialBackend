"""The file's timeline: one writer, three tiers, batched email.

Runs without a database or a transport: the tier maths, what `emit` does
when there is no file, who is told at each tier, that a failure inside the
timeline never escapes, what the digest says, and that the drain sends one
email per person per file and honours every switch.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.enums import Role
from app.models.file_event import NOTICE_NONE, NOTICE_PENDING, NOTICE_SENT, FileEvent
from app.services import file_events, file_team


def _run(coro):
    return asyncio.run(coro)


class _Db:
    def __init__(self, *, rows=(), objects=None):
        self.rows = list(rows)
        self.objects = objects or {}
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    @asynccontextmanager
    async def begin_nested(self):
        yield

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.added.append(row)

    async def flush(self):
        return None

    async def get(self, model, key):
        return self.objects.get((model.__name__, key))

    async def execute(self, stmt):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows), scalar_one_or_none=lambda: (rows[0] if rows else None))


def _profile(**kw):
    base = dict(id=uuid.uuid4(), intake_id=None, dealer_id=None, client_id=None, loan_id=None, deal_id=None, primary_bucket_id=None, company_id=None, company_set_by_user_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _user(role, name="Person"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, name=name, email=f"{name.lower()}@example.com", deleted_at=None)


def _member(user, seat):
    return file_team.Member(user_id=user.id, name=user.name, email=user.email, role=user.role.value, seat=seat)


# ── tiers ───────────────────────────────────────────────────────────────────


def test_tiers_follow_roles_and_nest():
    assert file_events.tier_for_role(Role.SUPER_ADMIN) == "desk"
    assert file_events.tier_for_role("loan_exec") == "desk"
    for role in (Role.BROKER, Role.FIELD_REP, Role.DEALER_PARTNER, Role.REGIONAL_MANAGER):
        assert file_events.tier_for_role(role) == "team"
    for role in (Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER):
        assert file_events.tier_for_role(role) == "client"
    assert file_events.visible_at("client") == ["client"]
    assert set(file_events.visible_at("team")) == {"client", "team"}
    assert set(file_events.visible_at("desk")) == {"client", "team", "desk"}


# ── emit ────────────────────────────────────────────────────────────────────


def test_emit_skips_a_source_with_no_file_and_never_creates_one():
    db = _Db()
    with patch.object(file_events, "find_profile", AsyncMock(return_value=None)) as find, patch.object(file_events, "_notify", AsyncMock()) as notify:
        assert _run(file_events.emit(db, kind="document.received", visibility="client", title="x", bucket_id=uuid.uuid4())) is None
    find.assert_awaited_once()
    notify.assert_not_awaited()
    assert db.added == []


def test_emit_writes_the_row_with_the_notice_state_of_its_tier_and_swallows_notify_failures():
    db = _Db()
    profile = _profile()
    actor = _user(Role.LOAN_EXEC, "Desk")
    with patch.object(file_events, "_notify", AsyncMock()):
        row = _run(file_events.emit(db, profile=profile, kind="status.changed", visibility="client", title="T" * 300, actor=actor, target_type="x", target_id=uuid.uuid4()))
    assert isinstance(row, FileEvent) and row.notice_status == NOTICE_PENDING and len(row.title) == 200
    assert row.actor_user_id == actor.id and row.actor_label == "Desk"
    with patch.object(file_events, "_notify", AsyncMock()):
        desk_row = _run(file_events.emit(db, profile=profile, kind="note.added", visibility="desk", title="n"))
    assert desk_row.notice_status == NOTICE_NONE
    with patch.object(file_events, "_notify", AsyncMock(side_effect=RuntimeError("boom"))):
        assert _run(file_events.emit(db, profile=profile, kind="message.sent", visibility="team", title="m")) is None
    with pytest.raises(ValueError):
        _run(file_events.emit(db, profile=profile, kind="x", visibility="everyone", title="m"))


def _notify_setup(team, users, *, client_user_id=None):
    db = _Db(rows=users)
    captured = {"notify": [], "events": []}

    async def fake_notify_users(_db, **kwargs):
        captured["notify"].append(kwargs)
        return []

    async def fake_publish(_db, **kwargs):
        captured["events"].append(kwargs)

    patches = [
        patch.object(file_team, "team_for", AsyncMock(return_value=team)),
        patch("app.services.file_contacts.load_sources", AsyncMock(return_value=SimpleNamespace(intake=None, dealer=None, client=None))),
        patch("app.services.file_contacts.business_label", lambda s: "Acme LLC"),
        patch("app.services.file_contacts.client_recipient", AsyncMock(return_value=SimpleNamespace(user_id=client_user_id, email="c@example.com", name="Client"))),
        patch("app.services.notifications.notify_users", fake_notify_users),
        patch("app.services.communication_events.publish_communication_event", fake_publish),
    ]
    return db, captured, patches


def test_notify_tells_each_tier_the_right_people_with_their_own_link_and_never_the_actor():
    agent = _user(Role.DEALER_PARTNER, "Partner")
    uw = _user(Role.LOAN_EXEC, "Uw")
    client_user = _user(Role.CLIENT, "Client")
    team = file_team.Team(agent=_member(agent, "agent"), underwriters=[_member(uw, "underwriter")])
    profile = _profile(intake_id=uuid.uuid4())

    # The stub returns what the recipient query would: the underwriter acted, so is not among them.
    db, captured, patches = _notify_setup(team, [agent, client_user], client_user_id=client_user.id)
    event = SimpleNamespace(id=uuid.uuid4(), kind="document.requested", visibility="client", title="We asked for a bank statement", body=None)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        _run(file_events._notify(db, profile, event, actor_id=uw.id, already_notified=set()))
    by_audience = {n["deep_link"]: n for n in captured["notify"]}
    assert all(n["target_type"] == "file_event" and n["target_id"] == str(profile.id) and n["batch_key"] == f"file:{profile.id}" for n in captured["notify"])
    assert all(n["email"] is False and n["push"] is True for n in captured["notify"])
    told = set().union(*(n["recipient_ids"] for n in captured["notify"]))
    assert told == {agent.id, client_user.id}  # the underwriter acted, so is not told
    assert any("/broker/ai-underwriter-leads?lead=" in link for link in by_audience)
    assert any("/client/dealer-intakes" in link for link in by_audience)
    assert captured["events"] and captured["events"][0]["event_type"] == "file_event.created" and captured["events"][0]["profile_id"] == profile.id


def test_desk_tier_reaches_underwriters_only_and_nobody_when_none_are_seated():
    agent = _user(Role.BROKER, "Agent")
    uw = _user(Role.LOAN_EXEC, "Uw")
    event = SimpleNamespace(id=uuid.uuid4(), kind="note.added", visibility="desk", title="Reviewer notes updated", body=None)

    db, captured, patches = _notify_setup(file_team.Team(agent=_member(agent, "agent"), underwriters=[_member(uw, "underwriter")]), [uw])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        _run(file_events._notify(db, _profile(), event, actor_id=None, already_notified=set()))
    assert [n["recipient_ids"] for n in captured["notify"]] == [{uw.id}]
    assert captured["notify"][0]["push"] is False

    db, captured, patches = _notify_setup(file_team.Team(agent=_member(agent, "agent")), [])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        _run(file_events._notify(db, _profile(), event, actor_id=None, already_notified=set()))
    assert captured["notify"] == [] and captured["events"] == []


def test_already_notified_people_are_not_told_twice():
    agent = _user(Role.BROKER, "Agent")
    uw = _user(Role.LOAN_EXEC, "Uw")
    team = file_team.Team(agent=_member(agent, "agent"), underwriters=[_member(uw, "underwriter")])
    db, captured, patches = _notify_setup(team, [agent])
    event = SimpleNamespace(id=uuid.uuid4(), kind="document.received", visibility="client", title="Received", body=None)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        _run(file_events._notify(db, _profile(loan_id=uuid.uuid4()), event, actor_id=None, already_notified={uw.id}))
    assert set().union(*(n["recipient_ids"] for n in captured["notify"])) == {agent.id}


# ── the digest and the drain ────────────────────────────────────────────────


def _event(profile_id, *, visibility, title, body=None, actor=None, minutes_ago=5):
    return SimpleNamespace(
        id=uuid.uuid4(), profile_id=profile_id, kind="x", visibility=visibility, title=title, body=body,
        actor_user_id=actor, created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        notice_status=NOTICE_PENDING, notice_recipients=[], notice_at=None,
    )


def test_the_digest_orders_events_and_keeps_bodies_off_the_company_copy():
    pid = uuid.uuid4()
    events = [_event(pid, visibility="team", title="Second", body="secret detail", minutes_ago=1), _event(pid, visibility="client", title="First", minutes_ago=9)]
    subject, text, html = file_events.render_digest("Acme LLC", events, titles_only=False, link="https://x/y")
    assert subject == "2 updates on Acme LLC" and text.index("First") < text.index("Second") and "secret detail" in text and "https://x/y" in html
    subject, text, html = file_events.render_digest("Acme LLC", events[:1], titles_only=True, link=None)
    assert subject == "1 update on Acme LLC" and "secret detail" not in text and "secret detail" not in html and "href" not in html


def _drain_setup(*, settings, team, events, profile, client=None, users=None):
    db = _Db(rows=events, objects={("ApplicationProfile", profile.id): profile, **{("User", u.id): u for u in (users or [])}})
    sent = []

    async def fake_deliver(_db, draft, **kwargs):
        sent.append((draft, kwargs))
        return SimpleNamespace(ok=True, detail="sent", message_id="m")

    patches = [
        patch.object(file_events, "_settings", AsyncMock(return_value=settings)),
        patch.object(file_team, "team_for", AsyncMock(return_value=team)),
        patch("app.services.file_contacts.load_sources", AsyncMock(return_value=SimpleNamespace(intake=None, dealer=None, client=None))),
        patch("app.services.file_contacts.business_label", lambda s: "Acme LLC"),
        patch("app.services.file_contacts.client_recipient", AsyncMock(return_value=client or SimpleNamespace(user_id=None, email=None, name=None))),
        patch.object(file_events, "_room_link", AsyncMock(return_value="https://app/room?tab=updates")),
        patch("app.services.messaging.outbox.deliver_email", fake_deliver),
    ]
    return db, sent, patches


def test_drain_sends_one_email_per_person_per_file_at_their_tier_and_marks_the_events():
    agent = _user(Role.BROKER, "Agent")
    uw = _user(Role.LOAN_EXEC, "Uw")
    team = file_team.Team(
        agent=_member(agent, "agent"),
        underwriters=[_member(uw, "underwriter")],
        company=file_team.CompanyRef(id=uuid.uuid4(), name="Acme Referrals", kind="referral_partner", notice_email="legal@acme.example", derived=True),
    )
    profile = _profile(intake_id=uuid.uuid4())
    events = [
        _event(profile.id, visibility="client", title="We asked for a bank statement"),
        _event(profile.id, visibility="team", title="Jane was added as an underwriter", body="team detail"),
        _event(profile.id, visibility="desk", title="Reviewer notes updated"),
    ]
    settings = SimpleNamespace(client_email_enabled=True, team_email_enabled=True, company_email_enabled=True)
    client = SimpleNamespace(user_id=None, email="owner@client.example", name="Owner")
    db, sent, patches = _drain_setup(settings=settings, team=team, events=events, profile=profile, client=client)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        accepted = _run(file_events.drain_notices(db))
    assert accepted == 4
    by_to = {draft.to: (draft, kw) for draft, kw in sent}
    assert set(by_to) == {agent.email, uw.email, "owner@client.example", "legal@acme.example"}
    assert by_to["owner@client.example"][0].subject == "1 update on Acme LLC" and "room?tab=updates" in by_to["owner@client.example"][0].body_text
    assert by_to[agent.email][0].subject == "2 updates on Acme LLC" and "Reviewer notes" not in by_to[agent.email][0].body_text
    assert by_to[uw.email][0].subject == "3 updates on Acme LLC"
    assert "team detail" not in by_to["legal@acme.example"][0].body_text and by_to["legal@acme.example"][0].subject == "2 updates on Acme LLC"
    assert all(kw["context"] == "file_updates" and kw["subject"].profile_id == profile.id for _, kw in sent)
    assert all(e.notice_status == NOTICE_SENT and e.notice_at is not None and e.notice_recipients for e in events)
    assert all("@" in r["to"] and "***" in r["to"] for r in events[0].notice_recipients)
    db.commit.assert_awaited()


def test_drain_honours_every_switch_skips_a_lone_actor_and_uses_the_login_email():
    agent = _user(Role.BROKER, "Agent")
    client_user = _user(Role.CLIENT, "Client")
    team = file_team.Team(agent=_member(agent, "agent"), company=file_team.CompanyRef(id=uuid.uuid4(), name="Acme", kind="referral_partner", notice_email="legal@acme.example", derived=True))
    profile = _profile(loan_id=uuid.uuid4())
    # A single team-tier event whose actor is the agent: the agent is not told about their own act.
    events = [_event(profile.id, visibility="team", title="Agent did a thing", actor=agent.id)]
    settings = SimpleNamespace(client_email_enabled=True, team_email_enabled=True, company_email_enabled=False)
    client = SimpleNamespace(user_id=client_user.id, email="stale@client.example", name="Client")
    db, sent, patches = _drain_setup(settings=settings, team=team, events=events, profile=profile, client=client, users=[client_user])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        _run(file_events.drain_notices(db))
    assert sent == []  # team event: client tier cannot see it; agent acted; company switch off

    events = [_event(profile.id, visibility="client", title="Approved")]
    db, sent, patches = _drain_setup(settings=SimpleNamespace(client_email_enabled=True, team_email_enabled=False, company_email_enabled=False), team=team, events=events, profile=profile, client=client, users=[client_user])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        _run(file_events.drain_notices(db))
    assert [d.to for d, _ in sent] == [client_user.email]  # the login's address, not the stale record
    assert "/loans/" in sent[0][0].body_text


def test_drain_waits_for_the_batch_window_and_the_profile_lookup_is_read_only():
    source = inspect.getsource(file_events.drain_notices)
    assert "DRAIN_DELAY" in source and "NOTICE_PENDING" in source
    assert "resolve_profile" not in inspect.getsource(file_events)
    assert file_events.DRAIN_DELAY.total_seconds() >= 60


# ── the API surface ─────────────────────────────────────────────────────────


def test_no_sms_anywhere_in_the_feature():
    for module in (file_events, file_team):
        src = inspect.getsource(module)
        assert "app.services.sms" not in src and "consent_delivery" not in src and "send_sms" not in src


def test_the_router_carries_the_api_first_routes_and_is_mounted_before_the_profile_router():
    from app import main
    from app.routers import file_team as router

    paths = {r.path for r in router.router.routes}
    for path in ("/application-profiles/find", "/application-profiles/team/candidates", "/application-profiles/me/file-updates",
                 "/application-profiles/{profile_id}/team", "/application-profiles/{profile_id}/timeline",
                 "/application-profiles/{profile_id}/timeline/seen", "/application-profiles/public/room/{token}/timeline"):
        assert path in paths, path
    src = inspect.getsource(main)
    assert src.index("file_team.router") < src.index("application_profiles.router")
    assert file_events.SCHEMA == "file_event.v1"
    shape = file_events.event_read(FileEvent(profile_id=uuid.uuid4(), kind="k", visibility="client", title="t", created_at=datetime.now(UTC)))
    assert {"schema", "id", "profile_id", "kind", "visibility", "title", "body", "actor_label", "target_type", "target_id", "meta", "created_at"} <= set(shape)
