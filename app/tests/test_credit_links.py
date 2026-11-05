"""Credit pulls attach to file owners: by hand from the file, from the signed
authorization, or automatically by name on the same client."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.routers import application_profiles as profile_routes
from app.routers import credit_links as credit_links_router
from app.schemas.settings import AppSettingsData
from app.services import credit_links, credit_pull_core, notifications, scheduler

PULLED = datetime(2026, 11, 3, 15, 33, tzinfo=UTC)
CLIENT = uuid4()


def _pull(**over):
    row = {
        "id": uuid4(),
        "client_id": CLIENT,
        "application_owner_id": None,
        "status": "completed",
        "legal_first_name": "Linda",
        "legal_last_name": "Louis",
        "email": None,
        "dob": date(1979, 4, 2),
        "street": "1 Main St",
        "city": "Miami",
        "state": "FL",
        "zip": "33101",
        "fico": None,
        "bureau_response": {"reports": {}, "applicant": {}},
        "notes": "isoftpull_id=abc",
        "pulled_at": PULLED,
        "expires_at": PULLED + timedelta(days=90),
        "created_at": PULLED,
    }
    row.update(over)
    return SimpleNamespace(**row)


def _owner(**over):
    row = {
        "id": uuid4(),
        "profile_id": uuid4(),
        "first_name": "Linda",
        "last_name": "Louis",
        "email": "linda@bodygoodstudio.com",
        "dob": None,
        "street": None,
        "city": None,
        "state": None,
        "zip": None,
        "credit_pull_id": None,
        "credit_score": None,
        "credit_tier": None,
        "credit_pulled_at": None,
        "credit_summary": None,
        "archived_at": None,
    }
    row.update(over)
    owner = SimpleNamespace(**row)
    owner.full_name = f"{owner.first_name} {owner.last_name}"
    return owner


def _profile(**over):
    row = {"id": uuid4(), "client_id": CLIENT, "intake_id": uuid4(), "dealer_id": None}
    row.update(over)
    return SimpleNamespace(**row)


def _db(rows=(), *, get=None):
    result = Mock()
    result.scalars.return_value.all.return_value = list(rows)
    result.all.return_value = list(rows)
    return SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(side_effect=get or (lambda *a: None)), flush=AsyncMock(), commit=AsyncMock(), add=Mock())


@pytest.fixture
def quiet(monkeypatch):
    """Linking also writes the log, the timeline and the pre-call tick."""

    record = AsyncMock()
    progress = AsyncMock()
    from app.services import credit_report_import

    import_report = AsyncMock(return_value=None)
    monkeypatch.setattr(credit_report_import, "ensure_report_imported", import_report)
    monkeypatch.setattr(credit_links, "_record", record)
    monkeypatch.setattr(credit_links, "_progress", progress)
    return SimpleNamespace(record=record, progress=progress, import_report=import_report)


# --- words and evidence ---------------------------------------------------------


def test_names_normalize_and_tiers_follow_the_score() -> None:
    assert credit_links.normalize_name(" Linda ", "LOUIS") == "linda louis"
    assert credit_links.normalize_name(None, None) == ""
    assert [credit_links.tier_for(s) for s in (742, 660, 600, None)] == ["Tier 1", "Tier 2", "Tier 3", None]
    assert profile_routes._credit_tier(719) == "Tier 2"


def test_evidence_decides_how_sure_the_match_is() -> None:
    owner = _owner()
    same_client = credit_links.match_evidence(owner, _pull(), same_client=True)
    assert credit_links.confidence(same_client) == "exact"
    assert credit_links.confidence_words("exact", same_client) == "Name matches on this client"

    with_dob = credit_links.match_evidence(_owner(dob=date(1979, 4, 2)), _pull(), same_client=False)
    assert credit_links.confidence(with_dob) == "exact"
    assert credit_links.confidence_words("exact", with_dob) == "Name and date of birth match"

    other_client = credit_links.match_evidence(owner, _pull(), same_client=False)
    assert credit_links.confidence(other_client) == "likely"

    other_name = credit_links.match_evidence(owner, _pull(legal_first_name="Lynda"), same_client=True)
    assert credit_links.confidence(other_name) == "weak"

    wrong_dob = credit_links.match_evidence(_owner(dob=date(1980, 1, 1)), _pull(), same_client=True)
    assert credit_links.confidence(wrong_dob) is None
    assert credit_links.confidence(credit_links.match_evidence(owner, _pull(legal_first_name="Ann"), same_client=False)) is None


def test_score_words_say_when_there_is_no_bureau_number() -> None:
    assert credit_links.score_words(credit_links.pull_facts(_pull())) == "no score returned"
    proxy = _pull(fico=720, notes="fico_source=intelligence-proxy")
    assert credit_links.score_words(credit_links.pull_facts(proxy)) == "720 estimated from the identity check, not a bureau score"
    assert credit_links.score_words(credit_links.pull_facts(_pull(fico=742, notes="fico_source=scraped:fico_8"))) == "score 742"
    facts = credit_links.pull_facts(_pull(fico=742, notes="fico_source=scraped:fico_8", bureau_response={"report_s3_key": "k"}))
    assert facts["score_verified"] and facts["report_available"] and facts["attention"] is None


def test_report_words_count_what_happened() -> None:
    assert credit_links.report_words({"linked": [1], "unmatched": [], "pulls_checked": 1}) == "1 pull connected, 1 checked"
    assert credit_links.report_words({"linked": [], "unmatched": [1, 2], "pulls_checked": 2}) == "0 pulls connected, 2 still unmatched, 2 checked"


# --- candidates -----------------------------------------------------------------


async def test_candidates_put_the_linked_and_the_exact_first() -> None:
    owner = _owner()
    profile = _profile()
    weak = _pull(legal_first_name="Lynda", pulled_at=PULLED + timedelta(days=2))
    exact = _pull(pulled_at=PULLED)
    older_exact = _pull(pulled_at=PULLED - timedelta(days=10), application_owner_id=owner.id)
    likely = _pull(client_id=uuid4(), pulled_at=PULLED + timedelta(days=1))
    stranger = _pull(legal_first_name="Ann", client_id=uuid4())

    candidates = await credit_links.candidate_pulls(_db([weak, exact, older_exact, likely, stranger]), profile, owner)

    assert [c.pull.id for c in candidates] == [older_exact.id, exact.id, likely.id, weak.id]
    assert [c.level for c in candidates] == ["exact", "exact", "likely", "weak"]
    first = candidates[0].read()
    assert first["linked_to_this_owner"] and first["confidence"] == "exact" and first["why"] == "Name matches on this client"
    assert first["score"] is None and first["attention"].startswith("The linked report has not been imported")


async def test_candidates_need_a_client_or_a_name() -> None:
    assert await credit_links.candidate_pulls(_db(), _profile(client_id=None), _owner(first_name="", last_name="")) == []


# --- linking --------------------------------------------------------------------


async def test_linking_points_both_ways_and_fills_the_owner_identity(quiet) -> None:
    owner = _owner()
    pull = _pull()
    profile = _profile()
    db = _db()

    result = await credit_links.link_pull(db, profile, owner, pull, how=credit_links.HOW_MANUAL)

    assert pull.application_owner_id == owner.id
    assert owner.credit_pull_id == pull.id and owner.credit_pulled_at == PULLED
    assert owner.credit_score is None and owner.credit_tier is None
    assert owner.credit_summary["linked_how"] == "manual"
    assert owner.credit_summary["attention"].startswith("The linked report has not been imported")
    assert (owner.dob, owner.street, owner.city, owner.state, owner.zip) == (date(1979, 4, 2), "1 Main St", "Miami", "FL", "33101")
    assert result["words"] == "Linked by hand: pull from Nov 3, 2026, no score returned, to Linda Louis"
    assert result["intake_id"] == str(profile.intake_id)
    db.flush.assert_awaited()
    assert quiet.record.await_args.kwargs["action"] == "owner.credit_linked"
    quiet.progress.assert_awaited_once()


async def test_linking_scores_and_tiers_when_the_bureau_answered(quiet) -> None:
    owner = _owner()
    pull = _pull(fico=742, notes="fico_source=scraped:fico_8")

    result = await credit_links.link_pull(_db(), _profile(), owner, pull, how=credit_links.HOW_SIGNATURE)

    assert (owner.credit_score, owner.credit_tier) == (742, "Tier 1")
    assert owner.credit_summary["score_verified"] is True
    assert result["words"].startswith("Linked from the signed authorization: pull from Nov 3, 2026, score 742")


async def test_a_second_owner_on_the_same_file_is_refused_but_another_file_may_share(quiet) -> None:
    profile = _profile()
    first = _owner(profile_id=profile.id)
    second = _owner(profile_id=profile.id)
    pull = _pull(application_owner_id=first.id)

    with pytest.raises(credit_links.LinkConflict, match="already linked to Linda Louis"):
        await credit_links.link_pull(_db(get=lambda *a: first), profile, second, pull)

    sibling_file = _profile()
    sibling = _owner(profile_id=sibling_file.id)
    await credit_links.link_pull(_db(get=lambda *a: first), sibling_file, sibling, pull)

    assert pull.application_owner_id == first.id
    assert sibling.credit_pull_id == pull.id


async def test_relinking_releases_the_previous_pull(quiet) -> None:
    owner = _owner()
    old = _pull(application_owner_id=owner.id)
    owner.credit_pull_id = old.id
    new = _pull(fico=701, notes="fico_source=full_feed")

    await credit_links.link_pull(_db(get=lambda *a: old), _profile(), owner, new)

    assert old.application_owner_id is None
    assert new.application_owner_id == owner.id and owner.credit_score == 701


async def test_unlinking_clears_the_owner_and_releases_the_pull(quiet) -> None:
    owner = _owner()
    pull = _pull(application_owner_id=owner.id, fico=701)
    owner.credit_pull_id = pull.id
    owner.credit_score = 701
    owner.credit_pulled_at = PULLED

    await credit_links.unlink_pull(_db(get=lambda *a: pull), _profile(), owner)

    assert pull.application_owner_id is None
    assert (owner.credit_pull_id, owner.credit_score, owner.credit_tier, owner.credit_pulled_at, owner.credit_summary) == (None, None, None, None, None)
    assert quiet.record.await_args.kwargs["action"] == "owner.credit_unlinked"


# --- automatic linking ----------------------------------------------------------


async def test_auto_link_on_a_file_links_exact_matches_and_names_who_waits(quiet, monkeypatch) -> None:
    profile = _profile()
    ready = _owner(profile_id=profile.id)
    likely_only = _owner(profile_id=profile.id, first_name="Mark", last_name="Lee")
    nothing = _owner(profile_id=profile.id, first_name="Ann", last_name="Roe")
    done = _owner(profile_id=profile.id, credit_pull_id=uuid4())
    exact_pull = _pull()

    async def candidates(db, prof, owner):
        if owner is ready:
            return [credit_links.Candidate(exact_pull, {}, "exact", False, False)]
        if owner is likely_only:
            return [credit_links.Candidate(_pull(client_id=uuid4()), {}, "likely", False, False)]
        return []

    monkeypatch.setattr(credit_links, "candidate_pulls", candidates)

    report = await credit_links.auto_link_profile(_db([ready, likely_only, nothing, done]), profile, how=credit_links.HOW_SWEEP)

    assert [entry["owner_name"] for entry in report["linked"]] == ["Linda Louis"]
    assert ready.credit_pull_id == exact_pull.id
    assert [(w["owner_name"], w["reason"][:22]) for w in report["waiting"]] == [
        ("Mark Lee", "Only a name match on a"),
        ("Ann Roe", "No completed pull carr"),
    ]


async def test_a_fresh_pull_needs_exactly_one_waiting_owner(quiet, monkeypatch) -> None:
    pull = _pull()
    owner = _owner()
    profile = _profile()
    monkeypatch.setattr(credit_links, "_owners_for_pull", AsyncMock(return_value=[(owner, profile)]))
    linked, reason = await credit_links.link_fresh_pull(_db(), pull)
    assert len(linked) == 1 and reason is None and owner.credit_pull_id == pull.id

    monkeypatch.setattr(credit_links, "_owners_for_pull", AsyncMock(return_value=[]))
    assert await credit_links.link_fresh_pull(_db(), _pull()) == ([], "No owner with this name is waiting on the client's files")

    monkeypatch.setattr(credit_links, "_owners_for_pull", AsyncMock(return_value=[(owner, profile), (_owner(), profile)]))
    linked, reason = await credit_links.link_fresh_pull(_db(), _pull())
    assert linked == [] and reason == "2 owners carry this name on the client's files; link by hand"


async def test_the_sweep_reports_every_pull_it_looked_at(monkeypatch) -> None:
    connected = _pull()
    orphan = _pull(legal_first_name="Steve", legal_last_name="Johnson", fico=742, notes="fico_source=scraped:fico_8")

    async def fresh(db, pull, *, actor=None, how=credit_links.HOW_AUTOMATIC):
        if pull is connected:
            return [{"owner_name": "Linda Louis", "score": None, "fico_source": None}], None
        return [], "No owner with this name is waiting on the client's files"

    monkeypatch.setattr(credit_links, "link_fresh_pull", fresh)

    report = await credit_links.run_everywhere(_db([connected, orphan]))

    assert report["pulls_checked"] == 2 and len(report["linked"]) == 1
    assert report["unmatched"] == [
        {
            "pull_id": str(orphan.id),
            "name": "Steve Johnson",
            "client_id": str(CLIENT),
            "pulled_at": PULLED.isoformat(),
            "score_words": "score 742",
            "reason": "No owner with this name is waiting on the client's files",
        }
    ]
    assert credit_links.report_words(report) == "1 pull connected, 1 still unmatched, 2 checked"


async def test_the_hook_only_links_in_automatic_mode(monkeypatch) -> None:
    fresh = AsyncMock(return_value=([{"owner_name": "Linda Louis"}], None))
    monkeypatch.setattr(credit_links, "link_fresh_pull", fresh)

    monkeypatch.setattr(credit_links, "mode", AsyncMock(return_value="manual"))
    assert await credit_links.after_pull(_db(), _pull()) == []
    fresh.assert_not_awaited()

    monkeypatch.setattr(credit_links, "mode", AsyncMock(return_value="automatic"))
    assert await credit_links.after_pull(_db(), _pull()) == [{"owner_name": "Linda Louis"}]
    assert await credit_links.after_pull(_db(), _pull(status="revoked")) == []


# --- the switch, the memory and the desk ----------------------------------------


def test_the_switch_defaults_to_manual_and_tolerates_junk() -> None:
    assert AppSettingsData().credit_linking.mode == "manual"
    assert credit_links._section(SimpleNamespace(data={"credit_linking": {"mode": "automatic"}})) == {"mode": "automatic"}
    assert credit_links._section(None) == {}


async def test_mode_and_settings_read_from_the_section(monkeypatch) -> None:
    monkeypatch.setattr(credit_links, "_settings_row", AsyncMock(return_value=SimpleNamespace(data={"credit_linking": {"mode": "automatic", "last_run": {"at": "x"}}})))
    assert await credit_links.mode(_db()) == "automatic"
    assert await credit_links.read_settings(_db()) == {"mode": "automatic", "last_run": {"at": "x"}, "sweep_minutes": 30}

    monkeypatch.setattr(credit_links, "_settings_row", AsyncMock(return_value=SimpleNamespace(data={"credit_linking": {"mode": "sometimes"}})))
    assert await credit_links.mode(_db()) == "manual"


async def test_writing_the_section_keeps_the_rest_of_the_settings(monkeypatch) -> None:
    row = SimpleNamespace(data={"pricing": {"x": 1}, "credit_linking": {"mode": "manual", "last_run": {"at": "old"}}})
    monkeypatch.setattr(credit_links, "_settings_row", AsyncMock(return_value=row))

    section = await credit_links.write_section(_db(), mode="automatic")

    assert section == {"mode": "automatic", "last_run": {"at": "old"}}
    assert row.data["pricing"] == {"x": 1} and row.data["credit_linking"]["mode"] == "automatic"


async def test_the_desk_hears_what_the_sweep_connected(monkeypatch) -> None:
    admin = SimpleNamespace(id=uuid4())
    notify = AsyncMock()
    monkeypatch.setattr(notifications, "users_with_roles", AsyncMock(return_value=[admin]))
    monkeypatch.setattr(notifications, "notify_users", notify)

    await credit_links.notify_desk(_db(), {"at": "now", "linked": [], "unmatched": []})
    notify.assert_not_awaited()

    await credit_links.notify_desk(
        _db(),
        {
            "at": "now",
            "linked": [
                {"owner_name": "Linda Louis", "score": None, "fico_source": None},
                {"owner_name": "Steve Johnson", "score": 742, "fico_source": "scraped:fico_8"},
            ],
            "unmatched": [{}],
        },
    )
    kwargs = notify.await_args.kwargs
    assert kwargs["recipient_ids"] == {admin.id}
    assert kwargs["title"] == "2 credit pulls connected to file owners"
    assert kwargs["body"] == "Linda Louis: no score returned\nSteve Johnson: score 742"
    assert kwargs["deep_link"] == "/settings?section=credit_linking"


# --- wiring ---------------------------------------------------------------------


def test_the_file_routes_and_the_firm_routes_are_registered() -> None:
    profile_contract = {(route.path, method) for route in profile_routes.router.routes for method in getattr(route, "methods", set())}
    for path, method in (
        ("/{profile_id}/owners/{owner_id}/credit-candidates", "GET"),
        ("/{profile_id}/owners/{owner_id}/credit-link", "POST"),
        ("/{profile_id}/owners/{owner_id}/credit-link", "DELETE"),
        ("/{profile_id}/credit-links/auto", "POST"),
    ):
        assert any(p.endswith(path) and m == method for p, m in profile_contract), (path, method)
    firm_contract = {(route.path, method) for route in credit_links_router.router.routes for method in getattr(route, "methods", set())}
    assert {("/credit-links/settings", "GET"), ("/credit-links/settings", "PUT"), ("/credit-links/run", "POST")} <= firm_contract


def test_the_signature_stamp_and_the_pull_hook_go_through_the_link() -> None:
    stamp = inspect.getsource(credit_pull_core.stamp_primary_owner)
    assert "credit_links.link_pull(" in stamp and "HOW_SIGNATURE" in stamp
    assert "owner.credit_pulled_at =" not in stamp
    assert "credit_links.after_pull(" in inspect.getsource(credit_pull_core.run_soft_pull)


def test_linking_writes_the_log_and_the_desk_timeline() -> None:
    source = inspect.getsource(credit_links._record)
    assert "log_profile_action(" in source and "VISIBILITY_DESK" in source


def test_the_scheduler_runs_the_sweep_every_half_hour() -> None:
    source = inspect.getsource(scheduler.start_scheduler)
    assert 'id="credit_link_sweep"' in source and "minutes=30" in source
    assert "run_scheduled_sweep" in inspect.getsource(scheduler.job_credit_link_sweep)
