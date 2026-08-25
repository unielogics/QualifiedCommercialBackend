import pytest
from pydantic import ValidationError

from app.dealer_os.router import _appointment_google_color
from app.dealer_os.schemas import RepAppointmentOutcomePatch, RepAppointmentPatch


def test_converted_outcome_requires_exactly_one_destination() -> None:
    with pytest.raises(ValidationError, match="Choose a conversion destination"):
        RepAppointmentOutcomePatch(outcome="converted")

    with pytest.raises(ValidationError, match="Choose Dealer or Real Estate"):
        RepAppointmentOutcomePatch(outcome="converted", conversion_target="ai_intake")

    payload = RepAppointmentOutcomePatch(
        outcome="converted",
        conversion_target="ai_intake",
        ai_variant="real_estate",
    )
    assert payload.ai_variant == "real_estate"


def test_reschedule_outcome_reopen_is_explicit() -> None:
    assert RepAppointmentPatch().reopen_outcome is False
    assert RepAppointmentPatch(reopen_outcome=True).reopen_outcome is True


def test_google_outcome_colors_are_stable() -> None:
    assert _appointment_google_color("not_converted") == "11"
    assert _appointment_google_color("did_not_show") == "5"
    assert _appointment_google_color("converted") == "10"
    assert _appointment_google_color(None) is None
