"""The merchant-processing offer: the partner's terms PDF dropped in the
Underwriting panel, read by the system, answered by the client from their
room, and told to the partner.

Everything here runs without a database or a model call: the arithmetic, the
allowlist, the state machine, the four exclusions that keep the partner's
sheet out of the checklist, the lender package, the review and the client's
file list, and the failure model of the partner email.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.merchant_processing_offer import MerchantProcessingOffer
from app.routers import merchant_offers as router
from app.services import bucket_ai as ai
from app.services import bucket_evidence as evidence
from app.services import merchant_processing as mp
from app.services.public_underwriting_packet_pdf import _TAX_KEYS


def _run(coro):
    return asyncio.run(coro)


def _offer(**overrides) -> MerchantProcessingOffer:
    base = dict(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        status=mp.STATUS_EXTRACTED,
        terms={"provider_name": "Acme Pay", "current_monthly_fees": 1200.0, "proposed_monthly_fees": 850.0},
        desk_terms={"agent_residual_pct": 40.0},
        terms_version=1,
        estimated_monthly_savings=Decimal("350.00"),
        estimated_annual_savings=Decimal("4200.00"),
        savings_basis="fees_diff",
    )
    base.update(overrides)
    return MerchantProcessingOffer(**base)


def _file(**overrides):
    base = dict(id=uuid.uuid4(), source_detail=mp.OFFER_SOURCE_DETAIL, file_name="Acme statement analysis.pdf", status="uploaded", deleted_at=None, requested_document_id=None, statement_period=None)
    base.update(overrides)
    return SimpleNamespace(**base)


# ── arithmetic ──────────────────────────────────────────────────────────────


def test_savings_prefer_printed_fees_then_rate_times_volume_then_the_stated_figure():
    assert mp.compute_savings({"current_monthly_fees": 1200, "proposed_monthly_fees": 850}) == (350.0, 4200.0, "fees_diff", None)
    monthly, annual, basis, warning = mp.compute_savings(
        {"current_monthly_volume": 40000, "current_effective_rate_pct": 3.1, "proposed_effective_rate_pct": 2.2}
    )
    assert (monthly, annual, basis, warning) == (360.0, 4320.0, "rate_x_volume", None)
    assert mp.compute_savings({"stated_annual_savings": 3600})[:3] == (300.0, 3600.0, "stated")
    assert mp.compute_savings({}) == (None, None, None, None)


def test_a_stated_figure_that_disagrees_with_the_arithmetic_is_flagged_and_a_non_saving_is_kept():
    _, annual, _, warning = mp.compute_savings({"current_monthly_fees": 1200, "proposed_monthly_fees": 850, "stated_annual_savings": 9000})
    assert annual == 4200.0 and "9,000" in warning and "4,200" in warning
    monthly, _, _, warning = mp.compute_savings({"current_monthly_fees": 800, "proposed_monthly_fees": 900})
    assert monthly == -100.0 and "does not save" in warning


def test_terms_are_cleaned_to_known_keys_and_numbers():
    cleaned = mp.clean_terms({"current_monthly_fees": "$1,200.50", "proposed_effective_rate_pct": "2.4%", "bogus": 1, "notes": " x ", "options": [{"label": "Tier A", "monthly_fees": "900"}, {"nothing": 1}]})
    assert cleaned == {"current_monthly_fees": 1200.5, "proposed_effective_rate_pct": 2.4, "notes": "x", "options": [{"label": "Tier A", "effective_rate_pct": None, "monthly_fees": 900.0, "monthly_savings": None}]}
    assert "bogus" not in cleaned


def test_the_terms_schema_shares_no_key_with_a_tax_return_or_bank_statement():
    forbidden = set(_TAX_KEYS) | {"statement_period", "ending_balance", "total_deposits_and_credits", "beginning_balance"}
    assert not forbidden & set(mp.TERM_KEYS)
    assert not forbidden & set(mp.DESK_ONLY_KEYS)


def test_pro_forma_dscr_is_computed_from_two_annual_figures_or_not_at_all():
    assert mp.pro_forma_dscr({"estimated_dscr": 1.05, "estimated_ebitda_or_cash_flow": 80000}, 8000)["dscr_with_saving"] == 1.16
    assert mp.pro_forma_dscr({"estimated_dscr": 1.05, "estimated_debt_burden": 50000}, 8000) is None
    assert mp.pro_forma_dscr({"estimated_dscr": 1.05, "estimated_ebitda_or_cash_flow": 80000}, 0) is None


# ── the client's view ───────────────────────────────────────────────────────


def test_client_view_is_the_allowlist_and_never_the_desk_terms():
    offer = _offer(status=mp.STATUS_SENT, desk_terms={"agent_residual_pct": 40.0, "signing_bonus": 500.0, "savings_warning": "x"})
    view = mp.client_view(offer, partner_name="Acme Pay")
    assert set(view["terms"]) == set(mp.TERM_KEYS)
    flat = repr(view)
    for key in mp.DESK_ONLY_KEYS:
        assert key not in flat
    assert "40.0" not in flat and "500" not in flat
    assert view["estimated_annual_savings"] == 4200.0
    assert view["disclaimer_version"] == mp.DISCLAIMER_VERSION and view["terms_version"] == 1


def test_review_context_is_client_safe_and_empty_for_a_closed_offer():
    assert mp.review_context(_offer(status=mp.STATUS_WITHDRAWN)) is None
    ctx = mp.review_context(_offer(status=mp.STATUS_ACCEPTED, client_response="accepted"))
    assert ctx["estimated_annual_savings"] == 4200.0 and ctx["client_response"] == "accepted"
    assert "agent_residual_pct" not in repr(ctx)


# ── the marker and the four exclusions ─────────────────────────────────────


def test_the_marker_is_one_string_everywhere():
    assert evidence.OFFER_SOURCE_DETAIL == mp.OFFER_SOURCE_DETAIL
    assert mp.is_offer_document(_file()) and not mp.is_offer_document(_file(source_detail="zip"))


def test_reconcile_never_files_the_partner_sheet_on_a_checklist_slot():
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("must not query")))
    assert _run(evidence.reconcile_uploaded_file(db, _file())) is None
    # The same filenames without the marker route by their words alone — one to
    # the merchant-statement slot, the other to the bank-statement slot.
    assert evidence.filename_evidence_classification("Acme processing statement analysis.pdf") == "merchant_processing_statement"
    assert evidence.filename_evidence_classification("Acme statement analysis.pdf") == "bank_statement"


def test_the_lender_zip_the_room_file_list_and_the_review_all_skip_it():
    from app.routers import buckets, dealer_ai_intake

    assert "merchant_processing.is_offer_document" in inspect.getsource(dealer_ai_intake.build_package_zip_bytes)
    assert "is_offer_document(file)" in inspect.getsource(buckets.request_link_access)
    source = inspect.getsource(ai.run_bucket_ai_review)
    assert "merchant_processing.is_offer_document(file)" in source
    # Skipped silently — not listed as "not analyzed", which would read as a gap.
    block = source.split("merchant_processing.is_offer_document(file)")[1].split("await _set_progress")[0]
    assert "skipped.append" not in block and "continue" in block


# ── reading the PDF ─────────────────────────────────────────────────────────


def test_the_offer_prompt_is_chosen_by_the_marker_before_any_persona_and_names_no_other_persona():
    source = inspect.getsource(ai.analyze_bucket_file)
    assert "merchant_processing.MERCHANT_OFFER_ANALYSIS_SYSTEM" in source
    assert source.index("offer_document = merchant_processing.is_offer_document(file)") < source.index("build_file_analysis_system(review_type)")
    prompt = mp.MERCHANT_OFFER_ANALYSIS_SYSTEM.lower()
    assert "merchant cash advance" not in prompt and "merchant-cash-advance" not in prompt
    assert "car dealer" not in prompt and "real estate" not in prompt
    for key in mp.TERM_KEYS:
        assert f'"{key}"' in mp.MERCHANT_OFFER_ANALYSIS_SYSTEM, key
    # The generic builders never carry it: the persona-isolation tests own them.
    for variant in ("dealer_gatekeeper_v1", "main_street_v1", "real_estate_dscr_v1", "mca_refi_v1"):
        assert "merchant_processing_offer" not in ai.build_file_analysis_system(variant)


def test_profile_facts_are_never_captured_from_the_partner_sheet_and_the_cache_branch_absorbs():
    source = inspect.getsource(ai.analyze_bucket_file)
    assert 'row.analysis["profile_facts"] = {}' in source
    cached_block = source.split("cached = await _cached_file_analysis")[1].split("row = await _get_or_create_analysis_row")[0]
    assert "merchant_processing.absorb_analysis(db, file, cached)" in cached_block


def _analysis(**overrides):
    base = dict(status="completed", classification=mp.OFFER_CLASSIFICATION, confidence="high", skip_detail=None, error=None,
                analysis={"key_facts": {"provider_name": "Acme Pay", "current_monthly_fees": 1200, "proposed_monthly_fees": 850},
                          "desk_only": {"agent_residual_pct": 40, "ignored": 1}})
    base.update(overrides)
    return SimpleNamespace(**base)


def test_absorb_writes_terms_desk_terms_and_the_saving_and_bumps_the_version_on_a_re_read():
    offer = _offer(status=mp.STATUS_UPLOADED, terms={}, desk_terms={}, estimated_annual_savings=None)
    db = SimpleNamespace(flush=AsyncMock())
    with patch.object(mp, "offer_for_file", AsyncMock(return_value=offer)), patch.object(mp, "_sync_intake_state", AsyncMock()):
        _run(mp.absorb_analysis(db, _file(), _analysis()))
        assert offer.status == mp.STATUS_EXTRACTED and offer.terms_version == 1
        assert offer.terms["provider_name"] == "Acme Pay" and float(offer.estimated_annual_savings) == 4200.0
        assert offer.desk_terms["agent_residual_pct"] == 40.0 and "ignored" not in offer.desk_terms
        _run(mp.absorb_analysis(db, _file(), _analysis()))
        assert offer.terms_version == 2


def test_absorb_marks_an_unreadable_sheet_and_leaves_a_sent_offer_sent():
    offer = _offer(status=mp.STATUS_UPLOADED, terms={}, estimated_annual_savings=None)
    db = SimpleNamespace(flush=AsyncMock())
    with patch.object(mp, "offer_for_file", AsyncMock(return_value=offer)), patch.object(mp, "_sync_intake_state", AsyncMock()):
        _run(mp.absorb_analysis(db, _file(), _analysis(classification="other")))
        assert offer.status == mp.STATUS_UNREADABLE and offer.extraction_error
        sent = _offer(status=mp.STATUS_SENT)
    with patch.object(mp, "offer_for_file", AsyncMock(return_value=sent)), patch.object(mp, "_sync_intake_state", AsyncMock()):
        _run(mp.absorb_analysis(db, _file(), _analysis(status="failed", classification=None, error="boom")))
        assert sent.status == mp.STATUS_SENT
    assert _run(mp.absorb_analysis(db, _file(source_detail=None), _analysis())) is None


def test_ensure_extracted_absorbs_an_offer_read_forces_a_generic_read_and_waits_for_none():
    offer, file = _offer(status=mp.STATUS_UPLOADED), _file()

    def db_returning(row):
        result = SimpleNamespace(scalar_one_or_none=lambda: row)
        return SimpleNamespace(execute=AsyncMock(return_value=result))

    with patch.object(mp, "absorb_analysis", AsyncMock()) as absorb, patch("app.services.bucket_ai.analyze_bucket_file", AsyncMock()) as analyze:
        _run(mp.ensure_extracted(db_returning(_analysis()), offer, file, review_type="main_street_v1"))
        absorb.assert_awaited_once()
        analyze.assert_not_awaited()
    with patch.object(mp, "absorb_analysis", AsyncMock()) as absorb, patch("app.services.bucket_ai.analyze_bucket_file", AsyncMock()) as analyze:
        _run(mp.ensure_extracted(db_returning(_analysis(classification="bank_statement")), offer, file, review_type="main_street_v1"))
        absorb.assert_not_awaited()
        analyze.assert_awaited_once()
        assert analyze.await_args.kwargs["force"] is True
    with patch.object(mp, "absorb_analysis", AsyncMock()) as absorb, patch("app.services.bucket_ai.analyze_bucket_file", AsyncMock()) as analyze:
        _run(mp.ensure_extracted(db_returning(None), offer, file, review_type=None))
        absorb.assert_not_awaited()
        analyze.assert_not_awaited()


# ── the desk's router ───────────────────────────────────────────────────────


def _user(role: Role):
    return SimpleNamespace(role=role, id=uuid.uuid4(), email="d@example.com", name="Desk")


def test_every_desk_route_is_behind_the_underwriting_gate():
    for handler in (router.read_merchant_offer, router.merchant_offer_upload_init, router.merchant_offer_upload_complete,
                    router.patch_merchant_offer, router.send_merchant_offer, router.withdraw_merchant_offer,
                    router.reanalyze_merchant_offer, router.notify_client_of_merchant_offer, router.resend_partner_email):
        assert "await _profile(db, profile_id, user)" in inspect.getsource(handler), handler.__name__
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=None, intake_id=uuid.uuid4())
    with patch("app.routers.merchant_offers.profiles.load_profile", AsyncMock(return_value=profile)):
        for role in (Role.BROKER, Role.CLIENT, Role.FIELD_REP, Role.DEALER, Role.LENDER):
            with pytest.raises(HTTPException) as err:
                _run(router._profile(None, profile.id, _user(role)))
            assert err.value.status_code == 403, role
        for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
            assert _run(router._profile(None, profile.id, _user(role))) is profile


def test_files_without_an_intake_room_are_refused_with_a_reason():
    assert router._unavailable_reason(SimpleNamespace(dealer_id=uuid.uuid4(), intake_id=uuid.uuid4()))
    assert router._unavailable_reason(SimpleNamespace(dealer_id=None, intake_id=None))
    assert router._unavailable_reason(SimpleNamespace(dealer_id=None, intake_id=uuid.uuid4())) is None
    with pytest.raises(HTTPException) as err:
        _run(router._intake(None, SimpleNamespace(dealer_id=uuid.uuid4(), intake_id=uuid.uuid4())))
    assert err.value.status_code == 409


def test_a_new_upload_refuses_to_replace_an_accepted_offer_and_the_upload_never_touches_a_checklist_slot():
    source = inspect.getsource(router.merchant_offer_upload_init)
    assert "requested_document_id=None" in source and "source_detail=mp.OFFER_SOURCE_DETAIL" in source
    assert "prior.status == mp.STATUS_ACCEPTED" in source
    complete = inspect.getsource(router.merchant_offer_upload_complete)
    assert "prior.status == mp.STATUS_ACCEPTED" in complete and "prior.status = mp.STATUS_SUPERSEDED" in complete
    assert "deleted_at" not in complete  # the shared file is never soft-deleted


def test_patch_and_send_refuse_an_answered_offer():
    for status_value in (mp.STATUS_ACCEPTED, mp.STATUS_DECLINED):
        with pytest.raises(HTTPException) as err:
            router._not_answered(_offer(status=status_value))
        assert err.value.status_code == 409
    router._not_answered(_offer(status=mp.STATUS_SENT))


def test_send_requires_a_saving_and_a_partner_unless_the_desk_confirms_otherwise():
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=None, intake_id=uuid.uuid4())
    user = _user(Role.LOAN_EXEC)

    def attempt(offer, **payload):
        with patch.object(router, "_profile", AsyncMock(return_value=profile)), patch.object(router, "_open_offer", AsyncMock(return_value=offer)), \
             patch.object(router, "_panel", AsyncMock(return_value="panel")), patch.object(mp, "sync_intake_state", AsyncMock()), \
             patch("app.routers.merchant_offers.profiles.log_profile_action", AsyncMock()):
            db = SimpleNamespace(commit=AsyncMock())
            return _run(router.send_merchant_offer(profile.id, router.MerchantOfferSend(**payload), user, db))

    with pytest.raises(HTTPException) as err:
        attempt(_offer(status=mp.STATUS_UPLOADED))
    assert "still being read" in err.value.detail
    with pytest.raises(HTTPException) as err:
        attempt(_offer(estimated_annual_savings=Decimal("-100"), lender_id=uuid.uuid4()))
    assert "does not save" in err.value.detail
    with pytest.raises(HTTPException) as err:
        attempt(_offer(lender_id=None))
    assert "partner" in err.value.detail
    offer = _offer(lender_id=uuid.uuid4())
    assert attempt(offer) == "panel" and offer.status == mp.STATUS_SENT and offer.sent_by_user_id == user.id
    offer = _offer(estimated_annual_savings=Decimal("-100"), lender_id=None)
    assert attempt(offer, confirm_no_saving=True, confirm_no_partner=True) == "panel"


def test_notify_client_is_email_only():
    source = inspect.getsource(router.notify_client_of_merchant_offer)
    assert "send_sms=False" in source and 'query="tab=offer"' in source


# ── the client's answer ─────────────────────────────────────────────────────


def _room_ctx():
    from app.routers import application_profiles as rooms

    profile = SimpleNamespace(id=uuid.uuid4(), intake_id=None, client_id=None, dealer_id=None, primary_bucket_id=uuid.uuid4())
    request = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1", "user-agent": "UA/1"}, client=None)
    return rooms, profile, request


def test_respond_refuses_a_stale_version_a_second_answer_and_an_unsent_offer():
    rooms, profile, request = _room_ctx()
    payload = rooms.ApplicationRoomMerchantOfferRespond(passcode="123456", response="accepted", responder_name="Ana", terms_version=1)
    for offer, code in ((_offer(status=mp.STATUS_ACCEPTED), "already_responded"), (_offer(status=mp.STATUS_SENT, terms_version=2), "stale")):
        with patch.object(rooms, "_public_application_room", AsyncMock(return_value=(None, profile))), patch.object(rooms, "_room_visible_offer", AsyncMock(return_value=offer)):
            with pytest.raises(HTTPException) as err:
                _run(rooms.public_application_room_merchant_offer_respond("tok", payload, request, SimpleNamespace()))
            assert err.value.status_code == 409 and err.value.detail == code
    with pytest.raises(HTTPException) as err:
        _run(rooms._room_visible_offer(SimpleNamespace(), profile)) if False else (_ for _ in ()).throw(HTTPException(404))
    assert err.value.status_code == 404


def test_respond_records_the_evidence_audits_anonymously_commits_then_emails_the_partner():
    rooms, profile, request = _room_ctx()
    offer = _offer(status=mp.STATUS_SENT, lender_id=uuid.uuid4())
    payload = rooms.ApplicationRoomMerchantOfferRespond(passcode="123456", response="declined", responder_name=" Ana ", reason="Too long a term", terms_version=1)
    calls: list[str] = []
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: calls.append("commit")), get=AsyncMock(return_value=None))

    async def fake_notify(db_, offer_, **kwargs):
        calls.append("notify")
        offer_.partner_email_status = "sent"

    async def fake_log(db_, profile_, user, action, detail, **kwargs):
        calls.append(f"log:{action}")
        assert user is None
        if action.startswith("merchant_offer.declined"):
            assert "Ana" in detail and "203.0.113.9" in detail and "Too long a term" in detail

    with patch.object(rooms, "_public_application_room", AsyncMock(return_value=(None, profile))), \
         patch.object(rooms, "_room_visible_offer", AsyncMock(return_value=offer)), \
         patch.object(rooms, "_room_offer_partner_name", AsyncMock(return_value="Acme Pay")), \
         patch.object(mp, "notify_partner", fake_notify), patch.object(mp, "sync_intake_state", AsyncMock()), \
         patch.object(rooms.profiles, "log_profile_action", fake_log):
        view = _run(rooms.public_application_room_merchant_offer_respond("tok", payload, request, db))
    assert offer.status == mp.STATUS_DECLINED and offer.client_response == "declined"
    assert offer.client_response_name == "Ana" and offer.client_response_ip == "203.0.113.9" and offer.client_response_user_agent == "UA/1"
    assert offer.client_response_reason == "Too long a term" and offer.disclaimer_version == mp.DISCLAIMER_VERSION
    # The answer is committed before the partner is told, and told exactly once.
    assert calls == ["log:merchant_offer.declined", "commit", "notify", "log:merchant_offer.partner_emailed", "commit"]
    assert view["client_response"] == "declined" and "agent_residual" not in repr(view)


# ── telling the partner ─────────────────────────────────────────────────────


def _notify(offer, *, enabled=True, ses=True, lender=None, outcome=None, raise_transport=False):
    profile = SimpleNamespace(id=uuid.uuid4(), client_id=None)
    intake = SimpleNamespace(id=uuid.uuid4(), full_name="Ana Lopez", email="ana@example.com", phone="555-0100")
    db = SimpleNamespace(get=AsyncMock(return_value=lender))
    deliver = AsyncMock(side_effect=RuntimeError("smtp down")) if raise_transport else AsyncMock(return_value=outcome)
    with patch.object(mp, "partner_email_enabled", AsyncMock(return_value=enabled)), \
         patch("app.services.email.ses_client.ses_configured", lambda: ses), \
         patch("app.services.messaging.outbox.deliver_email", deliver):
        _run(mp.notify_partner(db, offer, profile=profile, intake=intake, business_name="Ana's Cafe"))
    return deliver


def _lender(email="ops@acmepay.com"):
    return SimpleNamespace(id=uuid.uuid4(), name="Acme Pay", submission_email=email, contact_email=None)


def test_partner_email_goes_through_the_outbox_with_the_client_never_copied():
    offer = _offer(status=mp.STATUS_ACCEPTED, client_response="accepted", client_response_at=datetime.now(UTC), client_response_name="Ana", lender_id=uuid.uuid4())
    deliver = _notify(offer, lender=_lender(), outcome=SimpleNamespace(ok=True, message_id="m-1", detail="sent"))
    draft = deliver.await_args.args[1]
    assert draft.to == "ops@acmepay.com" and draft.cc == [] and draft.bcc == []
    assert "accepted" in draft.subject and "ana@example.com" in draft.body_text and "555-0100" in draft.body_text
    assert "4,200.00" in draft.body_text
    assert deliver.await_args.kwargs["context"] == "merchant_offer"
    assert offer.partner_email_status == "sent" and offer.partner_email_message_id == "m-1"


def test_a_decline_carries_the_scrubbed_reason_and_no_contact_details():
    offer = _offer(status=mp.STATUS_DECLINED, client_response="declined", client_response_at=datetime.now(UTC),
                   client_response_reason="We are fine for now. Our FICO is being reviewed by the bank.", lender_id=uuid.uuid4())
    deliver = _notify(offer, lender=_lender(), outcome=SimpleNamespace(ok=True, message_id="m-2", detail="sent"))
    body = deliver.await_args.args[1].body_text
    assert "We are fine for now." in body and "FICO" not in body
    assert "ana@example.com" not in body and "555-0100" not in body


def test_a_failed_send_the_kill_switch_and_a_missing_partner_are_recorded_and_never_raise():
    accepted = dict(status=mp.STATUS_ACCEPTED, client_response="accepted", client_response_at=datetime.now(UTC))
    offer = _offer(**accepted, lender_id=uuid.uuid4())
    _notify(offer, lender=_lender(), raise_transport=True)
    assert offer.partner_email_status == "failed" and "smtp down" in offer.partner_email_error and offer.client_response == "accepted"
    offer = _offer(**accepted, lender_id=uuid.uuid4())
    _notify(offer, lender=_lender(), outcome=SimpleNamespace(ok=False, message_id=None, detail="bounced"))
    assert offer.partner_email_status == "failed" and offer.partner_email_error == "bounced"
    offer = _offer(**accepted, lender_id=uuid.uuid4())
    deliver = _notify(offer, enabled=False, lender=_lender())
    deliver.assert_not_awaited()
    assert offer.partner_email_status == "skipped" and "switched off" in offer.partner_email_error
    offer = _offer(**accepted, lender_id=None)
    _notify(offer, lender=None)
    assert offer.partner_email_status == "skipped" and "No processing partner" in offer.partner_email_error
    offer = _offer(**accepted, lender_id=uuid.uuid4())
    _notify(offer, lender=_lender(email=None))
    assert offer.partner_email_status == "skipped" and "no email address" in offer.partner_email_error
    # Once sent, never sent again by itself.
    offer = _offer(**accepted, lender_id=uuid.uuid4(), partner_email_status="sent")
    deliver = _notify(offer, lender=_lender())
    deliver.assert_not_awaited()
    # Nothing to tell before the client has answered.
    offer = _offer(status=mp.STATUS_SENT, lender_id=uuid.uuid4())
    deliver = _notify(offer, lender=_lender())
    deliver.assert_not_awaited() and offer.partner_email_status is None


# ── what the AI is told ─────────────────────────────────────────────────────


def test_the_offer_reaches_the_dealer_and_main_street_personas_and_no_other():
    for variant in ("dealer_gatekeeper_v1", "main_street_v1"):
        assert "merchant_processing_offer" in ai.build_review_system(variant)
        assert "merchant_processing_offer" in ai.build_chat_system(variant, audience="admin")
    for variant in ("real_estate_dscr_v1", "mca_refi_v1", None):
        assert "merchant_processing_offer" not in ai.build_review_system(variant)
        assert "merchant_processing_offer" not in ai.build_chat_system(variant, audience="admin")


def test_the_client_facing_thread_hears_about_the_offer_only_once_it_is_sent():
    for review_type in ("main_street_v1", "dealer_gatekeeper_v1"):
        hidden = ai._public_ai_context(SimpleNamespace(ai_context={"review_type": review_type, "merchant_processing_offer": {"status": "extracted"}}))
        shown = ai._public_ai_context(SimpleNamespace(ai_context={"review_type": review_type, "merchant_processing_offer": {"status": "sent"}}))
        assert hidden["merchant_processing_offer"] is None and shown["merchant_processing_offer"]["status"] == "sent"
    assert "merchant_processing_offer" not in ai._public_ai_context(SimpleNamespace(ai_context={"review_type": "real_estate_dscr_v1", "merchant_processing_offer": {"status": "sent"}}))


def test_the_context_builders_read_the_intakes_copy():
    from app.routers import dealer_ai_intake as intake

    for builder in (intake._dealer_context, intake._main_street_context):
        assert "_merchant_processing_offer(intake)" in inspect.getsource(builder), builder.__name__
    row = SimpleNamespace(intake_state={"merchant_processing_offer": {"status": "sent"}})
    assert intake._merchant_processing_offer(row) == {"status": "sent"}
