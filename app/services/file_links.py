"""Where a notice about a file sends each person.

Every console reaches a file by a different route, and a notification carries
one link, so recipients are grouped by audience and each group gets its own.
No frontend host is hard-coded; the URLs come from Settings.
"""

from __future__ import annotations

from app.config import get_settings
from app.enums import Role
from app.models.application_profile import ApplicationProfile

AUDIENCE_DESK = "desk"
AUDIENCE_BROKER = "broker"
AUDIENCE_PARTNER = "partner"
AUDIENCE_REP = "rep"
AUDIENCE_CLIENT = "client"


def audience_for_role(role) -> str:
    value = role.value if hasattr(role, "value") else str(role)
    if value in (Role.SUPER_ADMIN.value, Role.LOAN_EXEC.value):
        return AUDIENCE_DESK
    if value == Role.DEALER_PARTNER.value:
        return AUDIENCE_PARTNER
    if value == Role.FIELD_REP.value:
        return AUDIENCE_REP
    if value in (Role.BROKER.value, Role.REGIONAL_MANAGER.value):
        return AUDIENCE_BROKER
    return AUDIENCE_CLIENT


def for_audience(profile: ApplicationProfile, audience: str) -> str:
    settings = get_settings()
    app_url = settings.frontend_app_url.rstrip("/")
    rep_url = settings.rep_app_url.rstrip("/")
    if audience == AUDIENCE_REP and profile.dealer_id:
        return f"{rep_url}/applications/{profile.dealer_id}"
    if audience == AUDIENCE_PARTNER and profile.intake_id:
        return f"{app_url}/broker/ai-underwriter-leads?lead={profile.intake_id}"
    if audience == AUDIENCE_CLIENT:
        if profile.loan_id:
            return f"{app_url}/loans/{profile.loan_id}"
        return f"{app_url}/client/dealer-intakes"
    if audience == AUDIENCE_BROKER:
        if profile.loan_id:
            return f"{app_url}/loans/{profile.loan_id}"
        if profile.client_id:
            return f"{app_url}/clients/{profile.client_id}"
    # The desk, and any audience whose own route does not apply to this file.
    if profile.intake_id:
        return f"{app_url}/admin/ai-underwriter-leads?lead={profile.intake_id}&view=communications"
    if profile.loan_id:
        return f"{app_url}/loans/{profile.loan_id}?tab=overview"
    if profile.dealer_id:
        return f"{app_url}/admin/ai-underwriter-leads"
    return f"{app_url}/pipeline"


def room_link(token: str) -> str:
    return f"{get_settings().frontend_app_url.rstrip('/')}/buckets/request/{token}?tab=updates"
