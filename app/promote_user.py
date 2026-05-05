"""Promote (or downgrade) a user's role.

Usage:
    python -m uv run python -m app.promote_user --email franco@unielogics.com --role super_admin
    python -m uv run python -m app.promote_user --clerk-id user_3DI4XBx5w... --role broker

Roles: super_admin | broker | loan_exec | client

If the user has signed in via Clerk at least once, a row exists; we update it.
If not, you can pre-provision with --create-if-missing (the row will get linked
when they sign in, since clerk_id is the unique key).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import uuid4

from sqlalchemy import select

from app.db import SessionLocal
from app.enums import Role
from app.models.user import User


async def promote(
    *,
    email: str | None,
    clerk_id: str | None,
    role: Role,
    create_if_missing: bool,
) -> int:
    if not email and not clerk_id:
        print("ERROR: pass --email or --clerk-id", file=sys.stderr)
        return 2

    async with SessionLocal() as db:
        stmt = select(User)
        if clerk_id:
            stmt = stmt.where(User.clerk_id == clerk_id)
        else:
            stmt = stmt.where(User.email == email)
        user = (await db.execute(stmt)).scalar_one_or_none()

        if user is None:
            if not create_if_missing:
                print(
                    f"ERROR: no user found for {clerk_id or email}.\n"
                    "  Either:\n"
                    "    1. Have them sign in once via the desktop /sign-in (auto-provisions),\n"
                    "    2. Or re-run with --create-if-missing.",
                    file=sys.stderr,
                )
                return 1
            placeholder_clerk = clerk_id or f"pending_{uuid4().hex[:16]}"
            user = User(
                clerk_id=placeholder_clerk,
                email=email or f"{placeholder_clerk}@pending.local",
                name=(email or "").split("@")[0] or "Pending",
                role=role,
            )
            db.add(user)
            await db.flush()
            await db.commit()
            print(
                f"PRE-PROVISIONED  email={user.email}  clerk_id={user.clerk_id}  role={role.value}"
            )
            print(
                "Note: when this person signs in to Clerk for the first time, the auto-provision "
                "in app/deps.py will create a SECOND row keyed by their real clerk_id. To avoid "
                "that, re-run this command with --clerk-id once they exist in Clerk."
            )
            return 0

        old = user.role
        user.role = role
        await db.commit()
        print(
            f"UPDATED  email={user.email}  clerk_id={user.clerk_id}  "
            f"role: {old} -> {role.value}"
        )
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Promote/downgrade a user's role.")
    p.add_argument("--email", help="Look up user by email.")
    p.add_argument("--clerk-id", help="Look up user by Clerk subject ID (preferred).")
    p.add_argument(
        "--role",
        required=True,
        choices=[r.value for r in Role],
        help=f"Role to assign. One of: {', '.join(r.value for r in Role)}",
    )
    p.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create a placeholder row if no user matches.",
    )
    args = p.parse_args()
    return asyncio.run(
        promote(
            email=args.email,
            clerk_id=args.clerk_id,
            role=Role(args.role),
            create_if_missing=args.create_if_missing,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
