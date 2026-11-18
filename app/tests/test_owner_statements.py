"""A personal financial statement for each owner (app/services/owner_statements.py).

Every person here is made up.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import owner_statements as os_

NOW = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def owner(first, last, pct, **changes):
    return SimpleNamespace(
        **{
            "id": uuid4(),
            "first_name": first,
            "last_name": last,
            "ownership_pct": pct,
            "email": f"{first.lower()}@example.com",
            "phone": "555-0100",
            "is_primary": False,
            "archived_at": None,
            "street": "1 Sample Way",
            "city": "Springfield",
            "state": "NJ",
            "zip": "07001",
            "created_at": NOW - timedelta(days=30),
            **changes,
        }
    )


def version(number, status="filed", *, source="form", days_ago=1, facts=None, body=None, upload=False, **changes):
    file_id = uuid4()
    return SimpleNamespace(
        **{
            "id": uuid4(),
            "version_number": number,
            "status": status,
            "source": source,
            "created_at": NOW - timedelta(days=days_ago),
            "key_facts": facts or {},
            "body": body or {},
            "body_sha256": "saved",
            "pdf_file_id": file_id,
            "source_file_id": file_id if upload else None,
            "actor_name": "Jordan Sample",
            "actor_user_id": None,
            **changes,
        }
    )


def statement(*, name="", owners=None, versions=(), current=None, answers=False, links=(), days_ago=10, subject=None, requests=None, set_aside=False):
    identity = {"subject": subject if subject is not None else (name or "applicant")}
    if owners is not None:
        identity["owner_ids"] = [str(item.id) for item in owners]
    if set_aside:
        identity["set_aside"] = True
    if requests:
        identity["owner_requests"] = requests
    body = {"applicant": {"name": name}, "assets": {"cash": 1200 if answers else None}, "liabilities": {}, "income": {}, "contingent": {}, "schedules": {}, "notes": ""}
    ordered = sorted(versions, key=lambda row: row.version_number, reverse=True)
    family = SimpleNamespace(
        id=uuid4(),
        identity=identity,
        label="Personal financial statement",
        current_version_id=current.id if current is not None else None,
        created_at=NOW - timedelta(days=days_ago),
        kind="pfs",
    )
    draft = SimpleNamespace(body=body, base_version_id=family.current_version_id, updated_at=NOW - timedelta(hours=2))
    return os_.Statement(family=family, draft=draft, versions=ordered, links=list(links))


def link(*, opened=False, days_ago=2, **changes):
    return SimpleNamespace(
        **{
            "created_at": NOW - timedelta(days=days_ago),
            "invitee_email": "alex@example.com",
            "last_used_at": NOW - timedelta(days=1) if opened else None,
            "use_count": 2 if opened else 0,
            "completed_at": None,
            "expires_at": NOW + timedelta(days=28),
            "revoked_at": None,
            "permission": "edit",
            **changes,
        }
    )


def filed(name="", owners=None, **changes):
    saved = version(1, facts={"net_worth": 510000, "liquid_assets": 42000, "total_assets": 700000, "total_liabilities": 190000, "statement_date": "2026-11-01"})
    return statement(name=name, owners=owners, versions=[saved], current=saved, **changes)


# --- whose statement it is --------------------------------------------------------------


@pytest.mark.parametrize(
    "owner_name, statement_name, expected",
    [
        ("Jordan Sample", "Jordan Sample", 3),
        ("Jordan Sample", "  jordan   SAMPLE ", 3),
        ("Jordan Sample", "Jordan A. Sample", 2),
        ("Jordan Sample", "Jordan Sample Jr.", 3),
        ("Jordan Sample", "J. Sample", 1),
        ("Jordan Sample", "Alex Sample", 0),
        ("Jordan Sample", "Jordan Rivera", 0),
        ("Jordan Sample", "", 0),
        ("Jordan Sample", "Sample", 0),
    ],
)
def test_two_names_are_one_person_only_when_first_and_last_agree(owner_name, statement_name, expected):
    assert os_.name_match(owner_name, statement_name) == expected


def test_every_owner_at_twenty_percent_or_more_owes_a_statement_and_each_keeps_their_own():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine, theirs = filed("Jordan Sample", [jordan]), statement(name="Alex Rivera", owners=[alex])
    tracker = os_.build([jordan, alex], [mine, theirs])
    assert [row.required for row in tracker.rows] == [True, True]
    assert tracker.row_for(jordan.id).statement is mine and tracker.row_for(alex.id).statement is theirs
    assert tracker.row_for(jordan.id).how == "assigned"
    assert tracker.unassigned == []


def test_the_name_on_a_statement_ties_it_to_its_owner_when_it_names_exactly_one():
    jordan, alex = owner("Jordan", "Sample", 60, is_primary=True), owner("Alex", "Rivera", 40)
    tracker = os_.build([jordan, alex], [statement(name="Alex J. Rivera", answers=True)])
    assert tracker.row_for(alex.id).how == "name"
    assert tracker.row_for(jordan.id).statement is None


def test_a_name_that_could_be_two_owners_is_nobodys_until_the_desk_says():
    john, jane = owner("John", "Sample", 50, is_primary=True), owner("Jane", "Sample", 50)
    ambiguous = filed("J. Sample")
    tracker = os_.build([john, jane], [ambiguous])
    assert tracker.unassigned == [ambiguous]
    assert all(row.statement is None for row in tracker.rows)


def test_on_a_file_with_one_owner_who_owes_a_statement_every_statement_is_theirs():
    jordan, minor = owner("Jordan", "Sample", 90, is_primary=True), owner("Alex", "Rivera", 10)
    upload = filed("Somebody Else")
    tracker = os_.build([jordan, minor], [upload])
    assert tracker.row_for(jordan.id).statement is upload
    assert tracker.row_for(jordan.id).how == "only_owner"
    assert tracker.row_for(minor.id).required is False


def test_a_statement_nobody_named_is_the_primary_owners_and_a_second_one_waits():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    first, second = filed("", days_ago=9), filed("", days_ago=3)
    tracker = os_.build([jordan, alex], [second, first])
    assert tracker.row_for(jordan.id).statement is first and tracker.row_for(jordan.id).how == "primary"
    assert tracker.unassigned == [second]


def test_a_statement_taken_off_its_owner_waits_for_the_desk_and_one_set_aside_is_not_listed():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    gone = owner("Riley", "Chen", 0, archived_at=NOW)
    orphan, spouse = filed("Riley Chen", [gone], days_ago=9), filed("Morgan Lee", [], set_aside=True)
    # "Not theirs": named for Jordan, but the desk took it off them. It is not matched back by its name.
    taken = filed("Jordan Sample", [], days_ago=3)
    tracker = os_.build([jordan, alex, gone], [orphan, spouse, taken])
    assert tracker.unassigned == [orphan, taken]
    assert [row.owner.id for row in tracker.rows] == [jordan.id, alex.id]
    assert all(row.statement is None for row in tracker.rows)


def test_the_desk_can_ask_an_owner_under_the_line_or_say_one_is_not_needed():
    jordan, alex, minor = owner("Jordan", "Sample", 60, is_primary=True), owner("Alex", "Rivera", 30), owner("Riley", "Chen", 10)
    asked = statement(name="Riley Chen", owners=[minor], requests={str(minor.id): {"state": "requested", "at": "2026-11-18T10:00:00+00:00"}})
    excused = statement(
        name="Alex Rivera",
        owners=[alex],
        requests={str(alex.id): {"state": "not_needed", "reason": "Silent partner, lender waived it", "by_name": "Sam Desk", "at": "2026-11-18T11:00:00+00:00"}},
    )
    tracker = os_.build([jordan, alex, minor], [asked, excused])
    assert [(row.required, row.reason) for row in tracker.rows] == [(True, "ownership"), (False, "not_needed"), (True, "requested")]
    assert os_.row_status(tracker.row_for(alex.id)) == "not_needed"
    assert [row.owner.id for row in tracker.needed] == [jordan.id, minor.id]


def test_an_owner_with_two_statements_is_read_by_the_latest_one_received():
    jordan = owner("Jordan", "Sample", 100, is_primary=True)
    upload = version(1, "original", source="original", days_ago=20, upload=True)
    older = statement(name="Jordan Sample", owners=[jordan], versions=[upload], current=upload, days_ago=20)
    newer = filed("Jordan Sample", [jordan], days_ago=5)
    tracker = os_.build([jordan], [older, newer])
    assert tracker.row_for(jordan.id).statements == [newer, older]


# --- a link that names nobody -------------------------------------------------------------


def test_a_link_that_names_nobody_opens_the_applicants_own_statement_never_a_co_owners():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    theirs = filed("Alex Rivera", [alex])
    tracker = os_.build([jordan, alex], [theirs])
    person, found = os_.default_statement(tracker)
    assert person.owner is jordan and found is None
    person, found = os_.default_statement(tracker, email="ALEX@example.com ")
    assert person.owner is alex and found is theirs


def test_a_file_with_no_owner_rows_keeps_its_newest_statement():
    old, new = filed("", days_ago=40), filed("", days_ago=2)
    person, found = os_.default_statement(os_.build([], [old, new]))
    assert person is None and found is new


# --- where each owner's statement stands ----------------------------------------------------


def test_a_statement_moves_from_not_started_to_sent_opened_in_progress_and_received():
    alex = owner("Alex", "Rivera", 50)

    def status_of(item=None):
        return os_.row_status(os_.build([alex], [item] if item else []).row_for(alex.id), now=NOW)

    assert status_of() == "not_started"
    assert status_of(statement(name="Alex Rivera", owners=[alex], links=[link()])) == "sent"
    assert status_of(statement(name="Alex Rivera", owners=[alex], links=[link(opened=True)])) == "opened"
    assert status_of(statement(name="Alex Rivera", owners=[alex], links=[link(opened=True)], answers=True)) == "in_progress"
    assert status_of(filed("Alex Rivera", [alex])) == "received"


def test_an_expired_or_revoked_link_no_longer_counts_as_sent():
    alex = owner("Alex", "Rivera", 50)
    dead = [link(expires_at=NOW - timedelta(days=1)), link(revoked_at=NOW - timedelta(days=3))]
    row = os_.build([alex], [statement(name="Alex Rivera", owners=[alex], links=dead)]).row_for(alex.id)
    assert os_.row_status(row, now=NOW) == "not_started"


def test_an_uploaded_statement_counts_as_received_before_the_desk_has_reviewed_it():
    alex = owner("Alex", "Rivera", 50)
    upload = version(1, "original", source="original", upload=True)
    item = statement(name="Alex Rivera", owners=[alex], versions=[upload])
    assert os_.pending_upload(item) is upload and os_.received_version(item) is upload
    assert os_.row_status(os_.build([alex], [item]).row_for(alex.id)) == "received"


def test_a_version_the_desk_saved_without_filing_is_in_progress_not_received():
    alex = owner("Alex", "Rivera", 50)
    saved = version(1, "published")
    item = statement(name="Alex Rivera", owners=[alex], versions=[saved], current=saved)
    assert os_.received_version(item) is None
    assert os_.row_status(os_.build([alex], [item]).row_for(alex.id)) == "in_progress"


def test_an_owner_who_owes_none_shows_as_not_required_until_one_arrives():
    minor = owner("Riley", "Chen", 5)
    assert os_.row_status(os_.build([minor], []).row_for(minor.id)) == "not_required"
    jordan = owner("Jordan", "Sample", 95, is_primary=True)
    tracker = os_.build([jordan, minor], [filed("Riley Chen")])
    assert os_.row_status(tracker.row_for(minor.id)) == "received"
    assert tracker.row_for(minor.id).required is False


def test_the_desk_reads_each_owners_figures_separately_and_never_a_sum():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    with patch("app.services.financial_workspace.body_hash", return_value="saved"):
        model = os_.read_model(os_.build([jordan, alex], [filed("Jordan Sample", [jordan]), statement(name="Alex Rivera", owners=[alex], links=[link()])]), now=NOW)
    assert (model["required_count"], model["received_count"], model["complete"]) == (2, 1, False)
    assert model["missing_owner_ids"] == [str(alex.id)]
    first, second = model["owners"]
    assert first["status"] == "received" and first["statement"]["figures"]["net_worth"] == 510000
    assert first["statement"]["versions"][0]["version_number"] == 1 and first["statement"]["versions"][0]["is_current"]
    assert second["status"] == "sent" and second["statement"]["link"]["sent_to"] == "alex@example.com"
    assert second["statement"]["figures"] is None
    assert "net_worth" not in model and "liquid_assets" not in model


def test_a_joint_statement_covers_both_owners_and_names_the_other_on_each_row():
    jordan, riley = owner("Jordan", "Sample", 50, is_primary=True), owner("Riley", "Sample", 50)
    joint = filed("Jordan and Riley Sample", [jordan, riley])
    with patch("app.services.financial_workspace.body_hash", return_value="saved"):
        model = os_.read_model(os_.build([jordan, riley], [joint]), now=NOW)
    assert model["complete"] and [item["joint_with"] for item in model["owners"]] == [["Riley Sample"], ["Jordan Sample"]]


# --- the checklist waits for every required owner --------------------------------------------


def _files(item):
    return os_.file_ids(item)


def test_one_statement_on_a_two_owner_file_is_one_of_two():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine = filed("Jordan Sample", [jordan])
    snapshot = os_.coverage_snapshot(os_.build([jordan, alex], [mine]), linked=_files(mine), accepted=_files(mine))
    assert (snapshot["received_owner_count"], snapshot["required_owner_count"]) == (1, 2)
    assert not snapshot["complete"] and snapshot["missing"] == ["Alex Rivera"]
    assert os_.reason_words(snapshot) == "1 of 2 owner statements received. Still needed from Alex Rivera."


def test_an_upload_tied_to_no_statement_counts_for_a_lone_owner_and_for_nobody_among_several():
    jordan, alex = owner("Jordan", "Sample", 100, is_primary=True), owner("Alex", "Rivera", 50)
    loose = {str(uuid4())}
    alone = os_.coverage_snapshot(os_.build([jordan], []), linked=loose, accepted=loose)
    assert alone["complete"] and alone["accepted_complete"] and alone["unassigned_count"] == 0
    jordan.ownership_pct = 50
    several = os_.coverage_snapshot(os_.build([jordan, alex], []), linked=loose, accepted=loose)
    assert several["received_owner_count"] == 0 and several["unassigned_count"] == 1
    assert "1 uploaded statement is not assigned to an owner yet." in os_.reason_words(several)


def test_the_room_names_who_is_in_and_who_is_not_and_never_a_figure():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine = filed("Jordan Sample", [jordan])
    words = os_.room_words(os_.coverage_snapshot(os_.build([jordan, alex], [mine]), linked=_files(mine), accepted=set()))
    assert "Received from Jordan Sample." in words and "Still needed from Alex Rivera." in words
    assert "510" not in words and "$" not in words


def _state(status="verified", **provenance):
    return SimpleNamespace(
        status=status,
        verified_at=NOW,
        verified_by_user_id=None,
        state_reason="Accepted by automatic AI evidence review",
        provenance={"coverage": {"current": 1, "required": 1, "unit": "documents", "complete": True}, **provenance},
    )


def _requirement(key=os_.REQUIREMENT_KEY):
    return SimpleNamespace(requirement_key=key, canonical_key=None)


@pytest.mark.asyncio
async def test_a_verified_requirement_reopens_when_a_second_owner_has_not_sent_theirs():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine = filed("Jordan Sample", [jordan])
    ids = sorted(_files(mine))
    state = _state(linked_file_ids=ids, accepted_file_ids=ids, collection_completion={"source": "files", "complete": True, "can_complete": True})
    slot = SimpleNamespace(status="uploaded", requires_signature=False, description="Upload a completed personal financial statement (PFS) for each owner.")
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], [mine]))):
        assert await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), state, slot) is True
    assert state.status == "received_unverified" and state.verified_at is None
    assert state.state_reason == "1 of 2 owner statements received. Still needed from Alex Rivera."
    assert state.provenance["coverage"] == {"current": 1, "required": 2, "unit": "owner statements", "complete": False}
    assert state.provenance["verified_coverage"]["complete"] is False
    assert state.provenance["collection_completion"]["can_complete"] is False
    assert slot.status == "requested" and "Still needed from Alex Rivera." in slot.description
    assert state.provenance["owner_statements"]["slot_description"].startswith("Upload a completed")


@pytest.mark.asyncio
async def test_the_requirement_is_met_once_every_required_owners_statement_is_accepted():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine, theirs = filed("Jordan Sample", [jordan]), filed("Alex Rivera", [alex])
    ids = sorted(_files(mine) | _files(theirs))
    state = _state(linked_file_ids=ids, accepted_file_ids=ids)
    slot = SimpleNamespace(status="requested", requires_signature=False, description="")
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], [mine, theirs]))):
        await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), state, slot)
    assert state.status == "verified" and slot.status == "uploaded"
    assert state.provenance["verified_coverage"] == {"current": 2, "required": 2, "unit": "owner statements", "complete": True}


@pytest.mark.asyncio
async def test_a_single_owner_file_stays_verified_on_the_one_document_it_always_needed():
    jordan = owner("Jordan", "Sample", 100, is_primary=True)
    loose = [str(uuid4())]
    state = _state(linked_file_ids=loose, accepted_file_ids=loose)
    slot = SimpleNamespace(status="uploaded", requires_signature=False, description="Original wording")
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan], []))):
        await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), state, slot)
    assert state.status == "verified" and slot.status == "uploaded" and slot.description == "Original wording"
    assert state.provenance["coverage"]["unit"] == "owner statements" and state.provenance["coverage"]["required"] == 1


@pytest.mark.asyncio
async def test_a_file_with_no_owner_who_owes_one_and_other_requirements_are_left_alone():
    state = _state(linked_file_ids=[], accepted_file_ids=[])
    before = dict(state.provenance)
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([], []))):
        assert await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), state, None) is False
    assert state.provenance == before and state.status == "verified"
    assert await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement("business_debt_schedule"), state, None) is False


@pytest.mark.asyncio
async def test_a_requirement_the_desk_set_aside_is_not_reopened_and_an_expired_statement_does_not_count():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine = filed("Jordan Sample", [jordan])
    ids = sorted(_files(mine))
    waived = _state("waived", linked_file_ids=ids, accepted_file_ids=ids)
    tracker = os_.build([jordan, alex], [mine])
    with patch.object(os_, "load", AsyncMock(return_value=tracker)):
        await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), waived, None)
        assert waived.status == "waived"
        expired = _state(linked_file_ids=ids, accepted_file_ids=ids)
        expired.provenance["coverage"]["expired_file_ids"] = ids
        await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), expired, None)
    assert expired.provenance["owner_statements"]["received_owner_count"] == 0


@pytest.mark.asyncio
async def test_the_rooms_original_wording_comes_back_when_only_one_owner_owes_a_statement_again():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    state = _state("requested", linked_file_ids=[], accepted_file_ids=[])
    slot = SimpleNamespace(status="requested", requires_signature=False, description="Original wording")
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], []))):
        await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), state, slot)
    assert slot.description.startswith("A statement is needed from each owner.")
    alex.ownership_pct = 10
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], []))):
        await os_.materialize(SimpleNamespace(), SimpleNamespace(id=uuid4()), _requirement(), state, slot)
    assert slot.description == "Original wording"


# --- uploads --------------------------------------------------------------------------------


def test_an_upload_joins_its_own_owners_statement_and_never_whichever_is_newest():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    mine, theirs = statement(name="Jordan Sample", owners=[jordan]), statement(name="Alex Rivera", owners=[alex])
    tracker = os_.build([jordan, alex], [mine, theirs])
    families = [theirs.family, mine.family]
    assert os_.joinable(tracker, families, "Alex Rivera") == [theirs.family]
    assert os_.joinable(tracker, families, "applicant") == []
    assert os_.joinable(tracker, families, "Morgan Lee") == []


def test_an_unnamed_upload_still_joins_the_only_statement_on_a_one_owner_file():
    jordan = owner("Jordan", "Sample", 100, is_primary=True)
    mine = statement(name="Jordan Sample", owners=[jordan])
    assert os_.joinable(os_.build([jordan], [mine]), [mine.family], "applicant") == [mine.family]
    plain = statement(name="")
    assert os_.joinable(os_.build([], [plain]), [plain.family], "Anybody") == [plain.family]


# --- the owner's own link --------------------------------------------------------------------


def test_the_email_carries_the_owners_own_link_and_asks_them_not_to_forward_it():
    subject, body = os_.compose_owner_email(
        first_name="Alex",
        business="Sample Fabrication LLC",
        url="https://app.example.test/forms/pfs/token-value",
        expires_on="December 18, 2026",
        sender="Sam Desk",
    )
    assert subject == "Action needed: your personal financial statement for Sample Fabrication LLC"
    assert "Hello Alex," in body and "https://app.example.test/forms/pfs/token-value" in body
    assert "do not forward it" in body and "Each owner receives their own." in body
    assert "It works until December 18, 2026." in body and body.rstrip().endswith("Sam Desk")
    assert "Sam Desk can add it to your file instead." in body
    # Nothing in it asks for a reply to an address nobody may be reading, and no room PIN travels by email.
    assert "reply" not in body.lower() and "PIN" not in body
    _subject, unsigned = os_.compose_owner_email(first_name="", business=None, url="https://app.example.test/forms/pfs/t", expires_on=None, sender=None)
    assert unsigned.startswith("Hello,") and "we can add it to your file instead." in unsigned and "for None" not in unsigned


def test_the_owners_page_is_seeded_with_their_own_details():
    alex = owner("Alex", "Rivera", 50)
    assert os_.owner_prefill(alex, business_name="Sample Fabrication LLC") == {
        "business_name": "Sample Fabrication LLC",
        "owner_name": "Alex Rivera",
        "home_address": "1 Sample Way, Springfield, NJ 07001",
        "business_phone": "555-0100",
    }


class _Session:
    """Just enough of a session for the decision paths: a transaction marker and a lock that does nothing."""

    def __init__(self):
        self.info = {}

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), scalar_one_or_none=lambda: None)

    async def flush(self):
        return None


def _profile():
    return SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None, client_id=None)


@pytest.mark.asyncio
async def test_a_link_for_the_applicant_starts_their_statement_when_only_a_co_owner_has_one():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    theirs = filed("Alex Rivera", [alex])
    started = SimpleNamespace(id=uuid4(), identity={})
    with (
        patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], [theirs]))),
        patch.object(os_, "_waiting_statement", AsyncMock(return_value=None)),
        patch.object(os_, "_start", AsyncMock(return_value=started)) as start,
    ):
        assert await os_.link_target(_Session(), _profile()) == (None, started.id)
    assert start.await_args.args[2] is jordan


@pytest.mark.asyncio
async def test_a_link_sent_to_a_co_owner_opens_the_co_owners_statement():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    theirs = filed("Alex Rivera", [alex])
    theirs.family.identity["statement_id"] = str(uuid4())
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], [filed("Jordan Sample", [jordan]), theirs]))):
        statement_id, family_id = await os_.link_target(_Session(), _profile(), email="alex@example.com")
    assert family_id == theirs.family.id and str(statement_id) == theirs.family.identity["statement_id"]


@pytest.mark.asyncio
async def test_a_statement_filled_in_before_versions_existed_is_left_for_the_engine_to_adopt():
    jordan, alex = owner("Jordan", "Sample", 50, is_primary=True), owner("Alex", "Rivera", 50)
    waiting = SimpleNamespace(id=uuid4())
    with (
        patch.object(os_, "load", AsyncMock(return_value=os_.build([jordan, alex], []))),
        patch.object(os_, "_waiting_statement", AsyncMock(return_value=waiting)),
        patch.object(os_, "_start", AsyncMock()) as start,
    ):
        assert await os_.link_target(_Session(), _profile()) == (waiting.id, None)
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_with_nothing_on_it_and_no_owner_rows_keeps_the_older_placeholder_path():
    with patch.object(os_, "load", AsyncMock(return_value=os_.build([], []))), patch.object(os_, "_waiting_statement", AsyncMock(return_value=None)):
        assert await os_.link_target(_Session(), _profile()) == (None, None)
    assert await os_.link_target(SimpleNamespace(), _profile()) == (None, None)


def test_sending_a_link_asks_an_owner_under_the_line_and_takes_back_a_not_needed():
    user = SimpleNamespace(id=uuid4(), name="Sam Desk", email="sam@example.com")
    minor, major = owner("Riley", "Chen", 10), owner("Alex", "Rivera", 50)
    family = SimpleNamespace(identity={"owner_ids": [str(minor.id)]})
    os_._ask(family, minor, user=user)
    assert family.identity["owner_requests"][str(minor.id)]["state"] == "requested"
    excused = SimpleNamespace(identity={"owner_requests": {str(major.id): {"state": "not_needed", "reason": "Lender waived it"}}})
    os_._ask(excused, major, user=user)
    assert excused.identity["owner_requests"] == {}


@pytest.mark.asyncio
async def test_saying_a_statement_is_not_needed_takes_a_reason():
    alex = owner("Alex", "Rivera", 50)
    with pytest.raises(HTTPException) as refused:
        await os_.set_requirement(_Session(), _profile(), alex, required=False, reason="no", user=SimpleNamespace(id=uuid4(), name="Sam Desk"))
    assert refused.value.status_code == 422
    family = SimpleNamespace(identity={"owner_ids": [str(alex.id)]})
    with patch.object(os_, "statement_for", AsyncMock(return_value=family)), patch("app.services.application_profiles.log_profile_action", AsyncMock()):
        await os_.set_requirement(
            _Session(), _profile(), alex, required=False, reason="Silent partner, lender waived it", user=SimpleNamespace(id=uuid4(), name="Sam Desk", email="sam@example.com")
        )
    entry = family.identity["owner_requests"][str(alex.id)]
    assert entry["state"] == "not_needed" and entry["reason"] == "Silent partner, lender waived it" and entry["by_name"] == "Sam Desk"


@pytest.mark.asyncio
async def test_an_owner_with_no_email_cannot_be_sent_a_link():
    alex = owner("Alex", "Rivera", 50, email=None)
    with pytest.raises(HTTPException) as refused:
        await os_.email_link(_Session(), _profile(), alex, user=SimpleNamespace(id=uuid4(), name="Sam Desk"))
    assert refused.value.status_code == 409 and "Alex Rivera" in refused.value.detail


# --- the desk's routes -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_client_cannot_read_the_tracker_and_only_underwriting_sends_or_decides():
    from app.routers import owner_statements as routes

    client = SimpleNamespace(id=uuid4(), role=Role.CLIENT)
    broker = SimpleNamespace(id=uuid4(), role=Role.BROKER)
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=_profile())):
        with pytest.raises(HTTPException) as refused:
            await routes.read_owner_statements(uuid4(), client, _Session())
        assert refused.value.status_code == 403
        for call in (
            routes.email_owner_statement_link(uuid4(), uuid4(), broker, _Session()),
            routes.set_owner_statement_requirement(uuid4(), uuid4(), routes.OwnerRequirementWrite(required=True), broker, _Session()),
            routes.assign_statement_owners(uuid4(), uuid4(), routes.StatementOwnersWrite(owner_ids=[]), broker, _Session()),
        ):
            with pytest.raises(HTTPException) as refused:
                await call
            assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_sending_the_missing_links_emails_each_owner_their_own_and_reports_who_could_not_be_reached():
    from app.routers import owner_statements as routes

    jordan, alex, riley = owner("Jordan", "Sample", 40, is_primary=True), owner("Alex", "Rivera", 30), owner("Riley", "Chen", 30, email=None)
    tracker = os_.build([jordan, alex, riley], [filed("Jordan Sample", [jordan])])
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN, name="Sam Desk")
    sent = AsyncMock(side_effect=lambda _db, _pid, _profile, person, _user: {"owner_id": str(person.id), "owner_name": os_.owner_name(person), "sent": True, "detail": None})
    with (
        patch.object(routes.profiles, "load_profile", AsyncMock(return_value=_profile())),
        patch.object(os_, "load", AsyncMock(return_value=tracker)),
        patch.object(os_, "_owner_or_404", AsyncMock(side_effect=lambda _db, _profile, owner_id: next(row.owner for row in tracker.rows if row.owner.id == owner_id))),
        patch.object(routes, "_email", sent),
    ):
        result = await routes.email_missing_owner_statements(uuid4(), user, _Session())
    assert [(item["owner_name"], item["sent"]) for item in result["items"]] == [("Alex Rivera", True), ("Riley Chen", False)]
    assert result["items"][1]["detail"] == "No personal email on file for this owner"
    assert sent.await_count == 1
