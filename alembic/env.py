"""Alembic env — uses sync engine for migrations against async-configured DB."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.db import Base
from app.models import *  # noqa: F401, F403  (register all models on metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
_URL = settings.database_url_sync


def _redact(text: str) -> str:
    """Mask the password in anything about to be printed.

    psycopg puts the whole connection string into its OperationalError, and
    SQLAlchemy passes that straight through, so a database that is merely
    unreachable at container start writes the production password into the
    application log. Migrations run on every restart, which makes this the
    single likeliest place for that to happen.
    """
    import re

    return re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1***\2", text)


# NOT set_main_option. That routes the URL through ConfigParser, whose "%"
# interpolation turns a password containing a percent sign into an
# InterpolationSyntaxError at startup, and it also parks the plaintext
# password in a config object that gets echoed on other errors. Passing the
# URL straight to the engine avoids both.

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        _URL,
        poolclass=pool.NullPool,
        # Keeps bound parameter values out of statement errors. Separate
        # concern from the URL, and worth having for the same reason.
        hide_parameters=True,
    )
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    except Exception as exc:  # noqa: BLE001
        # Re-raise with the credentials stripped.
        #
        # Deliberately a plain RuntimeError rather than the original type.
        # SQLAlchemy's DBAPIError takes three constructor arguments, so
        # `type(exc)(msg)` raises a TypeError and buries the actual failure
        # behind a confusing one. Nothing catches a specific type here, this
        # runs once at container start, and the message is what an operator
        # actually needs.
        raise RuntimeError(
            f"{type(exc).__name__} during migrations: {_redact(str(exc))}"
        ) from None


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
