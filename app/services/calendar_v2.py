from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import AppointmentOutcomeDefinition
from app.enums import Role
from app.models.user import User


CALENDAR_V2_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP}
FUNDING_FILE_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
ALLOWED_OUTCOME_EFFECTS = {
    "log_activity",
    "file_action",
    "schedule_follow_up",
    "request_documents",
    "send_no_show_rebooking",
    "close_enquiry",
}

DEFAULT_OUTCOMES = (
    {
        "name": "Qualified",
        "description": "Create or update the client file after a reviewed conversion.",
        "color": "green",
        "target_crm_status": "converted",
        "effects": ["log_activity", "file_action"],
    },
    {
        "name": "Follow up",
        "description": "Schedule the next client touch and keep the opportunity open.",
        "color": "blue",
        "target_crm_status": "follow_up",
        "effects": ["log_activity", "schedule_follow_up"],
    },
    {
        "name": "Documents requested",
        "description": "Record the request and keep the appointment in follow-up.",
        "color": "amber",
        "target_crm_status": "follow_up",
        "effects": ["log_activity", "request_documents"],
    },
    {
        "name": "No show",
        "description": "Mark the missed appointment and offer a path to rebook.",
        "color": "red",
        "target_crm_status": "no_show",
        "effects": ["log_activity", "send_no_show_rebooking"],
    },
    {
        "name": "Not a fit",
        "description": "Close the enquiry while retaining the reason and history.",
        "color": "gray",
        "target_crm_status": "not_qualified",
        "effects": ["log_activity", "close_enquiry"],
    },
)


def normalize_outcome_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def can_use_calendar_v2(user: User) -> bool:
    return user.role in CALENDAR_V2_ROLES


def can_create_funding_file(user: User) -> bool:
    return user.role in FUNDING_FILE_ROLES


async def ensure_default_outcomes(
    db: AsyncSession,
    user: User,
) -> list[AppointmentOutcomeDefinition]:
    rows = list(
        (
            await db.execute(
                select(AppointmentOutcomeDefinition)
                .where(AppointmentOutcomeDefinition.owner_user_id == user.id)
                .order_by(
                    AppointmentOutcomeDefinition.sort_order,
                    AppointmentOutcomeDefinition.created_at,
                )
            )
        ).scalars().all()
    )
    if rows:
        return rows

    for index, definition in enumerate(DEFAULT_OUTCOMES):
        row = AppointmentOutcomeDefinition(
            owner_user_id=user.id,
            normalized_name=normalize_outcome_name(definition["name"]),
            sort_order=index,
            **definition,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows
