"""A phone number we can actually reach someone on.

Every form that starts a file now has to collect one. It is the only channel
that works when an email bounces or sits unread, and the pre-call sequence, the
consent grant and the room PIN all depend on having one.

The number is normalised on the way in rather than stored as typed, because the
rest of the system keys on E.164: the consent grant, the opt-out suppression
list and the SMS ledger all look numbers up that way, and "(973) 555-0148" and
"+19735550148" being the same person has to be true in the database, not just to
a reader.

We ask for a mobile and say so in the label, but nothing here claims to verify
it: telling a mobile from a landline needs a carrier lookup we do not run, and a
validator that guessed would reject real numbers.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BeforeValidator

#: Kept in step with app.dealer_os.services.consent_delivery.normalize_phone,
#: which is what the SMS side uses. Deliberately conservative: a number we
#: cannot make confident sense of is refused rather than guessed at, because
#: texting the wrong person is worse than asking someone to retype.
_MIN_INTERNATIONAL_DIGITS = 8
_MAX_INTERNATIONAL_DIGITS = 15


def normalize(raw: str | None) -> str | None:
    """A typed number as E.164, or None when it cannot be made confident."""
    text = (raw or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if text.startswith("+") and _MIN_INTERNATIONAL_DIGITS <= len(digits) <= _MAX_INTERNATIONAL_DIGITS:
        return f"+{digits}"
    return None


def _require(value: object) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("A mobile number is required so we can reach you about your file")
    normalized = normalize(str(value))
    if normalized is None:
        raise ValueError(
            "That number does not look complete. Enter a 10-digit US mobile, "
            "or include the country code for an international number"
        )
    return normalized


def _optional(value: object) -> str | None:
    """For edit schemas, where absent means "leave it alone"."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    normalized = normalize(str(value))
    if normalized is None:
        raise ValueError(
            "That number does not look complete. Enter a 10-digit US mobile, "
            "or include the country code for an international number"
        )
    return normalized


# Normalisation bounds the stored value at E.164 length, so no max_length
# constraint is declared here: applied after the validator it would be dead
# weight, and applied to a None it raises instead of validating.

#: Required on every form that starts a file.
RequiredPhone = Annotated[str, BeforeValidator(_require)]

#: For PATCH bodies, where None genuinely means "no change". Still normalised,
#: so an edit cannot reintroduce an unreachable number.
OptionalPhone = Annotated[str | None, BeforeValidator(_optional)]
