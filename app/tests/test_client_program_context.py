"""Client-thread program knowledge and the prompt rules around it (contract item 7)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import application_programs, bucket_ai


class _Result:
    def __init__(self, rows=None, *, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


def _selection(key: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), playbook_id=uuid4(), playbook_version=1, program_key=key, program_name=name
    )


def _definition(key: str, visibility: list[str], *, order: int = 0, template: str | None = None):
    return SimpleNamespace(
        id=uuid4(),
        requirement_key=key,
        visibility=visibility,
        display_order=order,
        ai_request_message_template=template,
        objective_text="",
        completion_criteria="",
        review_checks=[],
    )


def _state(key: str, label: str, *, status: str = "missing", required_level: str = "required"):
    return SimpleNamespace(
        requirement_key=key, label=label, status=status, required_level=required_level, source_program_keys=[]
    )


def _catalog(key: str, name: str, slug: str, description: str | None):
    return SimpleNamespace(program_key=key, name=name, public_slug=slug, short_description=description)


SELECTIONS = [_selection("ez_term", "EZ Term Loan"), _selection("sba_7a", "SBA 7(a)")]
STATES = [
    _state("owner_personal_financial_statement", "Personal financial statement"),
    _state("business_debt_schedule", "Business debt schedule", status="verified"),
    _state("ytd_p_and_l_balance_sheet", "YTD P&L and balance sheet"),
    _state("business_tax_returns_2_years", "Business tax returns"),
    _state("internal_credit_memo", "Internal credit memo"),
]
DEFINITIONS = [
    _definition("owner_personal_financial_statement", ["borrower"], order=1, template="Ask for the PFS."),
    _definition("business_debt_schedule", ["client"], order=2),
    _definition("ytd_p_and_l_balance_sheet", ["borrower", "staff"], order=3),
    _definition("business_tax_returns_2_years", ["borrower"], order=4),
    _definition("internal_credit_memo", ["staff"], order=5),
]
CATALOG = [
    _catalog("sba_7a", "SBA 7(a) Loan", "sba-7a", "Government-backed working capital and refinance."),
    _catalog("ez_term", "EZ Term Loan", "ez-term", "A fixed-payment term loan for established businesses."),
]


def _context(*, selection_mode: str, selections=SELECTIONS, catalog=CATALOG, audience="uploader"):
    profile = SimpleNamespace(id=uuid4(), program_selection_mode=selection_mode)
    results = [_Result(STATES), _Result(DEFINITIONS), _Result([])]
    if selection_mode == "manual" and audience == "uploader":
        results.append(_Result(catalog))
    if audience == "admin":
        # The admin branch also loads the selected versions' descriptions.
        results.append(_Result([]))
    db = SimpleNamespace(execute=AsyncMock(side_effect=results))
    with patch.object(
        application_programs, "profile_for_chat_scope", AsyncMock(return_value=profile)
    ), patch.object(application_programs, "active_selections", AsyncMock(return_value=selections)):
        context = asyncio.run(
            bucket_ai._program_context_for_chat(
                db,
                bucket_id=uuid4(),
                intake_id=uuid4(),
                upload_link_id=uuid4(),
                audience=audience,
            )
        )
    return context, db


# --- staff_selected / selected_programs / worksheet_available -----------------


def test_manual_selection_names_programs_with_catalog_copy_in_selection_order() -> None:
    context, db = _context(selection_mode="manual")

    assert context["staff_selected"] is True
    assert context["worksheet_available"] is True
    assert context["selected_programs"] == [
        {
            "program_name": "EZ Term Loan",
            "public_slug": "ez-term",
            "short_description": "A fixed-payment term loan for established businesses.",
        },
        {
            "program_name": "SBA 7(a) Loan",
            "public_slug": "sba-7a",
            "short_description": "Government-backed working capital and refinance.",
        },
    ]
    # Only the public catalog columns travel; no fit score, rank, or version.
    for program in context["selected_programs"]:
        assert set(program) == {"program_name", "public_slug", "short_description"}
    # The catalog is read once, keyed on the active selections.
    assert db.execute.await_count == 4
    catalog_sql = str(db.execute.await_args_list[3].args[0])
    assert "funding_program_catalog" in catalog_sql
    assert "funding_program_catalog.program_key IN" in catalog_sql


def test_manual_selection_instructions_allow_names_but_never_eligibility_or_terms() -> None:
    context, _ = _context(selection_mode="manual")
    instructions = context["instructions"]

    assert (
        "You may tell the borrower the desk is considering these programs, by name, and describe each "
        "only with its short_description. Never state or imply eligibility, approval, a rate, a payment, "
        "or terms."
    ) in instructions
    assert "Do not disclose program names" not in instructions
    assert "fit scores, rankings, or unpublished criteria" in instructions
    # The deterministic-state sentence is unchanged.
    assert instructions.startswith("Use these deterministic requirement states to choose one next missing item.")


def test_auto_selection_keeps_programs_internal() -> None:
    context, db = _context(selection_mode="auto")

    assert context["staff_selected"] is False
    assert context["selected_programs"] == []
    assert context["worksheet_available"] is True
    assert "Do not disclose program names, fit scores, rankings, or unpublished criteria." in context["instructions"]
    assert "desk is considering" not in context["instructions"]
    # No catalog lookup when the names may not be disclosed.
    assert db.execute.await_count == 3


def test_manual_selection_without_a_catalog_row_falls_back_to_the_selection_name() -> None:
    context, _ = _context(selection_mode="manual", catalog=CATALOG[:1])

    assert context["selected_programs"] == [
        {"program_name": "EZ Term Loan", "public_slug": None, "short_description": None},
        {
            "program_name": "SBA 7(a) Loan",
            "public_slug": "sba-7a",
            "short_description": "Government-backed working capital and refinance.",
        },
    ]


@pytest.mark.parametrize("selection_mode", ["auto", "manual"])
def test_uploader_instructions_point_at_the_form_buttons(selection_mode) -> None:
    context, _ = _context(selection_mode=selection_mode)

    instructions = context["instructions"]
    assert '"Complete now"' in instructions
    assert '"Open the worksheet"' in instructions
    assert "beneath your message" in instructions
    assert "one short sentence" in instructions


@pytest.mark.parametrize("selection_mode", ["auto", "manual"])
def test_requirements_stay_client_visible_only_with_online_action_flags(selection_mode) -> None:
    context, _ = _context(selection_mode=selection_mode)

    by_key = {item["requirement_key"]: item for item in context["requirements"]}
    assert set(by_key) == {
        "owner_personal_financial_statement",
        "business_debt_schedule",
        "ytd_p_and_l_balance_sheet",
        "business_tax_returns_2_years",
    }
    assert by_key["owner_personal_financial_statement"]["online_or_template_actions_available"] is True
    assert by_key["ytd_p_and_l_balance_sheet"]["online_or_template_actions_available"] is True
    assert by_key["business_tax_returns_2_years"]["online_or_template_actions_available"] is False
    assert by_key["owner_personal_financial_statement"]["request_message"] == "Ask for the PFS."
    assert by_key["business_debt_schedule"]["status"] == "verified"


def test_uploader_without_active_selections_gets_no_program_context() -> None:
    context, _ = _context(selection_mode="manual", selections=[])

    assert context is None


def test_admin_context_is_not_the_client_shape() -> None:
    context, db = _context(selection_mode="manual", audience="admin")

    assert context["selection_mode"] == "manual"
    assert "staff_selected" not in context
    assert "worksheet_available" not in context
    assert [item["program_key"] for item in context["selected_programs"]] == ["ez_term", "sba_7a"]
    assert db.execute.await_count == 4


# --- where the client thread sees it ------------------------------------------


def test_uploader_chat_context_injects_program_readiness_under_the_key_the_rules_name() -> None:
    program_context = {"staff_selected": True, "selected_programs": [], "worksheet_available": True, "requirements": []}
    bucket = SimpleNamespace(
        id=uuid4(),
        name="Room",
        client_name="Fixture Client",
        purpose="Underwriting",
        bucket_type="underwriting",
        ai_context={},
        files=[],
        requested_documents=[],
        notes=[],
    )
    upload_link = SimpleNamespace(id=uuid4(), recipient_name="Client")
    db = SimpleNamespace(execute=AsyncMock())
    with patch.object(bucket_ai, "latest_review", AsyncMock(return_value=None)), patch.object(
        bucket_ai, "visible_action_items", AsyncMock(return_value=[])
    ), patch.object(bucket_ai, "_program_context_for_chat", AsyncMock(return_value=program_context)) as ctx:
        context = asyncio.run(
            bucket_ai._chat_context(
                db,
                bucket=bucket,
                audience="uploader",
                upload_link=upload_link,
                share=None,
                vendor_access=None,
                intake_id=None,
            )
        )

    ctx.assert_awaited_once()
    assert ctx.await_args.kwargs["audience"] == "uploader"
    assert context["program_readiness"] is program_context
    assert "deterministic_requirements" not in context


# --- prompt text ---------------------------------------------------------------

STAFF_SELECTED_EXCEPTION = (
    "unless context.program_readiness.staff_selected is true, in which case you may name only the "
    "programs listed in context.program_readiness.selected_programs and describe them only with the "
    "provided description."
)


def test_chat_preamble_declares_requested_email_and_the_strict_rule() -> None:
    preamble = bucket_ai.CHAT_PREAMBLE
    assert '"requested_email": null' in preamble
    assert '{"kind": "outstanding_items", "reason": "<one sentence>"}' in preamble
    assert "ONLY when the client explicitly asks to be emailed the list of outstanding items or the room link" in preamble
    assert "says they will finish later" in preamble
    assert "accountant, bookkeeper, or partner" in preamble
    assert "Otherwise it is null." in preamble
    assert "Never populate it for a client who has not asked" in preamble
    assert "never in the admin, share, or vendor threads" in preamble


def test_dealer_rules_keep_the_program_ban_with_the_staff_selected_exception() -> None:
    rules = bucket_ai.DEALER_CHAT_RULES
    last_bullet = rules.strip().splitlines()[-1]
    assert last_bullet.startswith("- Never tell the borrower which loan program")
    assert STAFF_SELECTED_EXCEPTION in last_bullet
    assert "never as a qualification or approval" in last_bullet
    assert "Never state or imply a rate, advance rate, or pricing figure" in last_bullet


def test_main_street_rules_keep_the_program_ban_with_the_staff_selected_exception() -> None:
    rules = bucket_ai.MAIN_STREET_CHAT_RULES
    bullet = next(line for line in rules.splitlines() if line.startswith("- context.loan_program_fit is an INTERNAL signal."))
    assert STAFF_SELECTED_EXCEPTION in bullet
    assert "never name a program" in bullet
    assert "never state or imply eligibility, a rate, an amount they can borrow, or approval odds" in bullet


def test_real_estate_and_mca_rules_are_untouched_by_the_exception() -> None:
    assert STAFF_SELECTED_EXCEPTION not in bucket_ai.RE_CHAT_RULES
    assert STAFF_SELECTED_EXCEPTION not in bucket_ai.MCA_CHAT_RULES


@pytest.mark.parametrize(
    "review_type",
    ["dealer_gatekeeper_v1", "real_estate_dscr_v1", "main_street_v1", "mca_refi_v1", None],
)
def test_every_client_persona_system_prompt_carries_the_requested_email_rule(review_type) -> None:
    system = bucket_ai.build_chat_system(review_type, client_language="en", audience="uploader")
    assert '"requested_email": null' in system
    assert "Never populate it for a client who has not asked" in system


def test_uploader_instruction_constants_match_the_contract() -> None:
    assert (
        "You may tell the borrower the desk is considering these programs, by name, and describe each "
        "only with its short_description. Never state or imply eligibility, approval, a rate, a payment, "
        "or terms."
    ) in bucket_ai.UPLOADER_STAFF_SELECTED_PROGRAMS_INSTRUCTION
    assert bucket_ai.UPLOADER_PROGRAM_NON_DISCLOSURE_INSTRUCTION == (
        "Do not disclose program names, fit scores, rankings, or unpublished criteria."
    )
    assert '"Complete now" and "Open the worksheet" buttons appear' in bucket_ai.UPLOADER_FORM_BUTTONS_INSTRUCTION
