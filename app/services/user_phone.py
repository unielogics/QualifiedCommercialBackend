"""One rule for a person's phone.

`users.phone` is the record. It prints as the relationship manager's phone on
both production agreements, and it is how the desk reaches an agent. Two
places write it — the operator's own profile (routers/me.py) and the field
desk profile in the rep app (dealer_os/crm_router.py, which mirrors it onto
the business card) — and both come through here so the stored string is the
same whichever door it arrived by.

`needs_phone` is what both apps gate on at first login: the roles that sign
or are named on agreements must have a mobile on file, once.
"""

from __future__ import annotations

from app.dealer_os.deps import is_rep
from app.dealer_os.services.consent_delivery import normalize_phone
from app.enums import Role

# Super admin, underwriter, field rep: the people named on a production
# agreement or reachable by the desk. A dealer partner is not the house's
# employee; a client is not staff.
PHONE_REQUIRED_ROLES: frozenset[Role] = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP})


def store_phone(raw: str | None) -> str | None:
    """E.164 where the number is unambiguous, otherwise exactly what they
    typed: this one is printed on an agreement, not texted, so an extension
    or a switchboard note must survive rather than vanish. Blank is None."""
    text = (raw or "").strip()
    if not text:
        return None
    return normalize_phone(text) or text


def needs_phone(user) -> bool:
    """Whether this person still owes the one-time mobile number."""
    role = getattr(user, "role", None)
    required = role in PHONE_REQUIRED_ROLES or is_rep(user)
    return bool(required and not (getattr(user, "phone", None) or "").strip())
