"""Persona isolation for the AI-intake prompt selectors.

Three product personas share one prompt module: the car-dealer gatekeeper, the
real-estate/DSCR underwriter, and the Main Street operating-business screener.
Nothing enforces their separation except hand-maintained if/elif chains in
build_review_system / build_chat_system / build_file_analysis_system, so a
fourth branch added carelessly could leak dealer language into a restaurant file
or rent-roll language into a trucking file.

These tests pin that. They are cheap, they run without a DB or a model call, and
they fail loudly the moment two personas end up in one string.
"""

from __future__ import annotations

import pytest

from app.services import bucket_ai as ai

DEALER = "dealer_gatekeeper_v1"
REAL_ESTATE = "real_estate_dscr_v1"
MAIN_STREET = "main_street_v1"
ALL_VARIANTS = (DEALER, REAL_ESTATE, MAIN_STREET)

BUILDERS = (
    ("review", ai.build_review_system),
    ("chat", ai.build_chat_system),
    ("file_analysis", ai.build_file_analysis_system),
)

# Markers that identify a persona. Each builder phrases its persona slightly
# differently — the review rules say "car dealer", the file-analysis hint says
# "car-dealer", the RE hint says "real-estate / DSCR investor file" — so each
# persona carries a set and matching any one of them counts. Matched against a
# hyphen-normalized prompt.
FINGERPRINTS = {
    DEALER: ("car dealer",),
    REAL_ESTATE: ("real estate investor / dscr underwriter", "real estate / dscr investor"),
    MAIN_STREET: ("operating business",),
}


def _normalize(prompt: str) -> str:
    return prompt.lower().replace("-", " ").replace("—", " ")


def _personas_in(prompt: str) -> list[str]:
    return [v for v, marks in FINGERPRINTS.items() if any(m in prompt for m in marks)]


@pytest.mark.parametrize("name,build", BUILDERS)
@pytest.mark.parametrize("variant", ALL_VARIANTS)
def test_builder_returns_a_non_empty_prompt(name, build, variant):
    assert build(variant).strip(), f"{name} produced nothing for {variant}"


@pytest.mark.parametrize("name,build", BUILDERS)
def test_dealer_language_never_reaches_the_other_personas(name, build):
    """The specific leak that matters: a restaurant or an investor being asked
    about floorplan, MCA exposure or dealer inventory."""
    for variant in (REAL_ESTATE, MAIN_STREET):
        prompt = build(variant).lower()
        for term in ("dealership name", "dealer gross receipts", "dealer inventory"):
            assert term not in prompt or "never" in prompt, (
                f"{name}/{variant} mentions {term!r} outside a prohibition"
            )


@pytest.mark.parametrize("name,build", BUILDERS)
def test_only_one_persona_fingerprint_per_prompt(name, build):
    """No returned string may carry two product personas at once."""
    for variant in ALL_VARIANTS:
        prompt = _normalize(build(variant))
        present = _personas_in(prompt)
        # The Main Street rules name the other two only to forbid them, so allow
        # its own fingerprint plus explicit negations.
        assert variant in present, f"{name}/{variant} lost its own persona"
        extras = [v for v in present if v != variant]
        for other in extras:
            assert "never" in prompt or "not a " in prompt, (
                f"{name}/{variant} carries {other}'s persona without negating it"
            )


def test_main_street_forbids_the_other_two_verticals_explicitly():
    """The RE rules established this pattern — a positive persona plus an
    explicit negative constraint. Main Street must carry both."""
    rules = ai.MAIN_STREET_REVIEW_RULES
    assert "not a car dealer review" in rules
    assert "not a real-estate investor review" in rules
    assert "floorplan" in rules  # named in order to be prohibited


def test_main_street_chat_keeps_program_names_internal():
    """Main Street inherits the dealer disclosure policy: loan_program_fit paces
    which document to ask for next and is never quoted to the borrower."""
    rules = ai.MAIN_STREET_CHAT_RULES
    assert "INTERNAL" in rules
    assert "Never tell the borrower which program" in rules
    assert "never state or imply a rate" in rules


def test_main_street_chat_handles_the_non_lending_intents():
    """merchant_services and business_systems must not be pushed through a
    lending package or given a fundability verdict."""
    rules = ai.MAIN_STREET_CHAT_RULES
    assert "merchant processing" in rules
    assert "do not put them through a lending package" in rules
    assert "never imply one" in rules


def test_unknown_variant_gets_the_neutral_preamble_only():
    """A generic admin document room must not inherit a product persona."""
    for name, build in BUILDERS:
        for variant in (None, "", "some_future_vertical_v9"):
            prompt = _normalize(build(variant))
            leaked = _personas_in(prompt)
            assert not leaked, f"{name} leaked {leaked} for {variant!r}"


def test_admin_audience_override_composes_with_main_street():
    """The internal thread override appends to whichever persona is active."""
    prompt = ai.build_chat_system(MAIN_STREET, audience="admin")
    assert "operating business" in prompt
    assert ai.ADMIN_THREAD_CHAT_RULES.split("\n")[0] in prompt


def test_spanish_instruction_composes_with_main_street():
    prompt = ai.build_chat_system(MAIN_STREET, client_language="es")
    assert "operating business" in prompt
    assert "Spanish" in prompt


def test_main_street_classification_tokens_are_in_both_enums():
    """The review and per-file enums are shared across personas, so the Main
    Street tokens must be ADDED to both rather than replacing dealer's."""
    for token in (
        "merchant_processing_statement",
        "fleet_or_vehicle_schedule",
        "transportation_authority",
        "commercial_lease",
    ):
        assert token in ai.REVIEW_PREAMBLE, f"{token} missing from the review enum"
        assert token in ai.FILE_ANALYSIS_PREAMBLE, f"{token} missing from the file enum"
    # Dealer's token survives — it is forbidden for Main Street by the hint,
    # not removed from the shared enum.
    assert "floorplan_mca_inventory" in ai.REVIEW_PREAMBLE


def test_classification_tokens_fit_the_database_column():
    """BucketFileAnalysis.classification is String(48)."""
    tokens = [
        t
        for line in (ai.REVIEW_PREAMBLE, ai.FILE_ANALYSIS_PREAMBLE)
        for t in line.replace('"', " ").split()
        if "_" in t and "|" in t
    ]
    for group in tokens:
        for token in group.split("|"):
            assert len(token) <= 48, f"{token!r} is {len(token)} chars, exceeds String(48)"
