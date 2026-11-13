"""SMS consent the client gives for themselves while starting an application."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dealer_os.services import sms_consent as sms_consent_svc
from app.routers import dealer_ai_intake as core
from app.routers import foreclosure_rescue, public
from app.routers.dealer_ai_intake_routes import client, funding_review, mca_refinance, public_intake
from app.services import application_profiles, file_events, onboarding_consent

PHONE = "+17028632888"


def _request(forwarded: str | None = None, host: str = "198.51.100.9"):
    headers = {"user-agent": "Mozilla/5.0 Test"}
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def _payload(transactional=False, marketing=False):
    return SimpleNamespace(sms_transactional_consent=transactional, sms_marketing_consent=marketing)


@pytest.fixture
def services(monkeypatch):
    record = AsyncMock(side_effect=lambda db, **kw: SimpleNamespace(**kw))
    existing = AsyncMock(return_value=None)
    log_action = AsyncMock()
    emit = AsyncMock()
    monkeypatch.setattr(sms_consent_svc, "record_consent", record)
    monkeypatch.setattr(sms_consent_svc, "consent_for", existing)
    monkeypatch.setattr(application_profiles, "log_profile_action", log_action)
    monkeypatch.setattr(file_events, "emit", emit)
    return SimpleNamespace(record=record, existing=existing, log=log_action, emit=emit)


# --- the choices ----------------------------------------------------------------------------


def test_the_ip_is_the_first_forwarded_address_else_the_socket() -> None:
    assert onboarding_consent.client_ip(_request("203.0.113.7, 10.0.0.1")) == "203.0.113.7"
    assert onboarding_consent.client_ip(_request()) == "198.51.100.9"
    assert onboarding_consent.client_ip(SimpleNamespace(headers={}, client=None)) is None


def test_the_choice_is_kept_on_the_intake_whether_or_not_they_agreed() -> None:
    declined = onboarding_consent.choices_record(_payload())
    agreed = onboarding_consent.choices_record(_payload(True, False))

    assert declined["sms_choices"] == {"transactional": False, "marketing": False, "disclosure_version": sms_consent_svc.SMS_DISCLOSURE_VERSION}
    assert agreed["sms_choices"]["transactional"] is True and agreed["sms_choices"]["marketing"] is False


def test_every_application_start_offers_both_texts_unticked() -> None:
    for model in (core.DealerIntakeStart, core.FundingReviewStart, core.McaRefiStart, core.CapitalReadinessClaimRequest, foreclosure_rescue.ForeclosureRescueIntakeCreate):
        fields = model.model_fields
        assert fields["sms_transactional_consent"].default is False, model.__name__
        assert fields["sms_marketing_consent"].default is False, model.__name__


# --- recording --------------------------------------------------------------------------------


async def test_only_the_ticked_kinds_are_written_as_the_clients_own_consent(services) -> None:
    profile = SimpleNamespace(id=uuid4())

    recorded = await onboarding_consent.record_choices(
        SimpleNamespace(), profile, phone="(702) 863-2888", consenter_name="Terrell Parker", payload=_payload(True, False), request=_request("203.0.113.7"), entry="starting an application"
    )

    assert recorded == ["transactional"]
    services.record.assert_awaited_once()
    written = services.record.await_args.kwargs
    assert (written["kind"], written["method"], written["phone_e164"], written["profile_id"]) == ("transactional", "self_web", PHONE, profile.id)
    assert written["consenter_name"] == "Terrell Parker" and written["ip_address"] == "203.0.113.7"
    assert written["user_agent"] == "Mozilla/5.0 Test" and written["captured_by_user_id"] is None and written["dealer_id"] is None
    services.log.assert_awaited_once()
    assert services.log.await_args.args[3] == "sms.consent_captured"
    assert "application messages while starting an application" in services.log.await_args.args[4]
    assert services.emit.await_args.kwargs["visibility"] == file_events.VISIBILITY_DESK


async def test_both_kinds_when_both_are_ticked_and_none_when_neither_is(services) -> None:
    profile = SimpleNamespace(id=uuid4())

    both = await onboarding_consent.record_choices(SimpleNamespace(), profile, phone=PHONE, consenter_name="T", payload=_payload(True, True), request=_request(), entry="x")
    assert both == ["transactional", "marketing"] and services.record.await_count == 2

    services.record.reset_mock()
    none = await onboarding_consent.record_choices(SimpleNamespace(), profile, phone=PHONE, consenter_name="T", payload=_payload(), request=_request(), entry="x")
    assert none == [] and services.record.await_count == 0


async def test_a_number_we_cannot_read_or_one_that_already_agreed_writes_nothing(services) -> None:
    profile = SimpleNamespace(id=uuid4())

    assert await onboarding_consent.record_choices(SimpleNamespace(), profile, phone="call me", consenter_name="T", payload=_payload(True), request=_request(), entry="x") == []

    services.existing.return_value = SimpleNamespace()
    assert await onboarding_consent.record_choices(SimpleNamespace(), profile, phone=PHONE, consenter_name="T", payload=_payload(True), request=_request(), entry="x") == []
    services.record.assert_not_awaited()
    services.log.assert_not_awaited()


# --- the start of an application --------------------------------------------------------------------


async def test_a_start_with_nothing_ticked_does_not_touch_the_file(monkeypatch, services) -> None:
    provision = AsyncMock()
    monkeypatch.setattr(application_profiles, "provision_profile_for_intake", provision)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    assert await onboarding_consent.record_for_start(db, SimpleNamespace(), _payload(), _request(), entry="x") == []

    provision.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_a_start_gives_the_file_its_profile_records_and_commits(monkeypatch, services) -> None:
    profile = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(application_profiles, "provision_profile_for_intake", AsyncMock(return_value=profile))
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    intake = SimpleNamespace(id=uuid4(), phone="702-863-2888", full_name="Terrell Parker")

    recorded = await onboarding_consent.record_for_start(db, intake, _payload(True, True), _request(), entry="starting an application")

    assert recorded == ["transactional", "marketing"]
    assert services.record.await_args_list[0].kwargs["consenter_name"] == "Terrell Parker"
    db.commit.assert_awaited_once()


async def test_a_failure_never_stops_the_application_from_opening(monkeypatch, services) -> None:
    monkeypatch.setattr(application_profiles, "provision_profile_for_intake", AsyncMock(side_effect=RuntimeError("boom")))
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    recorded = await onboarding_consent.record_for_start(db, SimpleNamespace(id=uuid4(), phone=PHONE, full_name="T"), _payload(True), _request(), entry="x")

    assert recorded == []
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


# --- wiring ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "entry"),
    [
        (public_intake.start_dealer_intake, "starting an application"),
        (mca_refinance.start_mca_refinance, "starting an MCA refinance application"),
        (funding_review.start_funding_review, "starting a real estate funding review"),
    ],
)
def test_each_public_start_keeps_the_choice_and_records_it(handler, entry) -> None:
    source = inspect.getsource(handler)

    assert "onboarding_consent.choices_record(payload)" in source
    assert "onboarding_consent.record_for_start(" in source and entry in source


def test_the_signed_in_claim_records_the_choice_inside_its_own_transaction() -> None:
    source = inspect.getsource(client.claim_capital_readiness_diagnostic)

    assert "onboarding_consent.choices_record(payload, email=intake.email)" in source
    assert "onboarding_consent.record_choices(" in source and "db.begin_nested()" in source


def test_only_an_owner_submitting_for_themselves_can_agree_to_texts() -> None:
    source = inspect.getsource(foreclosure_rescue._create_foreclosure_rescue)

    assert source.count('payload.submitter_type == "owner_direct"') == 2
    assert "owner_consent_payload" in source and "actor is None" in source


def test_the_disclosure_is_served_to_the_public_from_the_one_copy_of_the_words() -> None:
    routes = {(route.path, method) for route in public.router.routes for method in getattr(route, "methods", set())}
    assert ("/public/sms-disclosure", "GET") in routes

    served = inspect.getsource(public.public_sms_disclosure)
    assert "sms_consent_service.disclosure()" in served


def test_the_desk_reads_what_the_client_chose_on_the_first_screen() -> None:
    from app.routers import application_communications as comms

    asked = SimpleNamespace(
        intake_state={"legal_acceptance": {"sms_choices": {"transactional": True, "marketing": False}, "timestamp": "2026-11-05T14:00:00+00:00"}}
    )
    declined = SimpleNamespace(intake_state={"legal_acceptance": {"sms_choices": {"transactional": False, "marketing": False}}})

    assert comms._onboarding_choice(asked) == {"transactional": True, "marketing": False, "asked_at": "2026-11-05T14:00:00+00:00"}
    assert comms._onboarding_choice(declined) == {"transactional": False, "marketing": False, "asked_at": None}
    assert comms._onboarding_choice(SimpleNamespace(intake_state={"legal_acceptance": {"terms_accepted": True}})) is None
    assert comms._onboarding_choice(None) is None
