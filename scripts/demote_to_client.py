"""Demote an operator-team user to a borrower (CLIENT role).

Two operations bundled in a single transaction:
  1. UPDATE users SET role='client' WHERE email = ?
  2. INSERT INTO clients (...) linked to that user, if no row exists yet.

Idempotent — re-running on an already-demoted user is a no-op.

SAFETY: refuses to demote the last super-admin. If the target is the only
remaining super-admin row in the DB, the script aborts before any writes.
Promote another user via the Settings → Team UI first, then re-run.

USAGE
-----
From qcbackend/ with the same .env the API uses:

    python scripts/demote_to_client.py franco@unielogics.com

Add `--force` to skip the interactive confirmation (useful in CI):

    python scripts/demote_to_client.py franco@unielogics.com --force
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make the qcbackend root importable when invoked as `python scripts/...`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.enums import Role  # noqa: E402
from app.models.client import Client  # noqa: E402
from app.models.user import User  # noqa: E402


async def demote(email: str, *, force: bool) -> int:
    target_email = email.strip().lower()
    async with SessionLocal() as db:
        # 1) Load target user
        user = (
            await db.execute(select(User).where(func.lower(User.email) == target_email))
        ).scalar_one_or_none()
        if user is None:
            print(f"ERROR: no user found with email={target_email!r}", file=sys.stderr)
            return 2

        print(f"Found user: id={user.id}  email={user.email}  current role={user.role}")

        if user.role == Role.CLIENT:
            print("Already a client — nothing to update on users.role.")

        # 2) Last-super-admin guard
        if user.role == Role.SUPER_ADMIN:
            other_supers = (
                await db.execute(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.role == Role.SUPER_ADMIN,
                        User.id != user.id,
                        User.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            if other_supers == 0:
                print(
                    "ABORT: this is the only remaining super-admin in the database.",
                    "Promote another user via Settings → Team first, then re-run.",
                    sep="\n",
                    file=sys.stderr,
                )
                return 3
            print(f"Safety check OK — {other_supers} other super-admin(s) remain.")

        # 3) Confirmation
        if not force:
            print()
            answer = input(
                f"Demote {user.email} from {user.role} to client? [y/N]: "
            ).strip().lower()
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return 1

        # 4) Apply (idempotent)
        if user.role != Role.CLIENT:
            user.role = Role.CLIENT
            await db.flush()
            print(f"users.role → client")

        # 5) Ensure linked clients row
        existing = (
            await db.execute(select(Client).where(Client.user_id == user.id))
        ).scalar_one_or_none()
        if existing:
            print(f"clients row already exists: id={existing.id}")
        else:
            client = Client(
                user_id=user.id,
                name=user.name or user.email.split("@")[0],
                email=user.email,
                tier="standard",
                funded_total=0,
                funded_count=0,
            )
            db.add(client)
            await db.flush()
            await db.refresh(client)
            print(f"clients row created: id={client.id}")

        await db.commit()

        # 6) Verify
        await db.refresh(user)
        linked = (
            await db.execute(select(Client).where(Client.user_id == user.id))
        ).scalar_one_or_none()
        print()
        print("=== AFTER ===")
        print(f"users.role     = {user.role}")
        print(f"clients.id     = {linked.id if linked else '(missing)'}")
        print(f"clients.tier   = {linked.tier if linked else '—'}")
        print(f"clients.email  = {linked.email if linked else '—'}")
        print()
        print("Note: existing browser sessions stay authenticated until the JWT")
        print("expires. Sign out and back in to refresh the role on the client.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Demote a user to the CLIENT role.")
    parser.add_argument("email", help="Email of the user to demote (case-insensitive).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the interactive yes/no confirmation.",
    )
    args = parser.parse_args()
    code = asyncio.run(demote(args.email, force=args.force))
    sys.exit(code)


if __name__ == "__main__":
    main()
