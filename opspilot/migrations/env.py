"""Alembic ``env.py``: one owner connection, migrations serialized by a PG lock.

The DSN comes from ``config.attributes["dsn"]`` (``opspilot.schema``) or
``OPSPILOT_DSN`` (the ``alembic`` CLI). Offline SQL generation is not
supported: every revision is plain SQL already.
"""

import os

from alembic import context
from sqlalchemy import create_engine, text

from opspilot.schema import sqlalchemy_url

# Any constant works; it only has to be the same for every migrator.
MIGRATION_LOCK_KEY = 0x6F707370696C6F74  # "opspilot"


def _dsn() -> str:
    dsn = context.config.attributes.get("dsn") or os.environ.get("OPSPILOT_DSN")
    if not dsn:
        raise SystemExit("OPSPILOT_DSN is required to run migrations")
    return str(dsn)


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url(_dsn()), poolclass=None)
    with engine.connect() as connection:
        # Two migrators started together (web + worker) must not both run
        # the DDL; the second waits here and then finds nothing to do. The
        # session-level lock outlives the commit that closes the autobegun
        # transaction, so Alembic starts its own transactions on a clean
        # connection and actually commits them.
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )
        connection.commit()
        try:
            context.configure(connection=connection, transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            connection.commit()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migration generation is not supported")
run_migrations_online()
