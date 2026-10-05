"""Schema versioning: Alembic owns the DDL, the runtime only checks the version.

Migrations run before the product starts, through a connection that owns the
tables (``make migrate`` / ``python -m opspilot.schema migrate``). The
runtime ``install()`` methods call :func:`verify_head` and refuse to start
when the database is not at the newest revision, so the restricted roles of
M1-02 never need DDL rights (ADR-0007; task record 2026-10-05).

Taking over a database built by the old inline ``install()`` DDL is only
allowed when ``pg_dump --schema-only`` of its ``opspilot_*`` objects is
byte-identical to a fresh database upgraded to head; then the version table
is *stamped* instead of running the baseline again. Any difference refuses
the takeover and reports the diff; nothing is altered.
"""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row
from sqlalchemy.engine import URL

VERSION_TABLE = "alembic_version"
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_PG_DUMP_FLAGS = (
    "--schema-only",
    "--no-owner",
    "--no-privileges",
    "--no-comments",
    "--no-security-labels",
    "--no-tablespaces",
    "--table=opspilot_*",
)


class SchemaNotMigrated(RuntimeError):
    """The database is not at the head revision; run ``make migrate``."""

    def __init__(self, current: str | None, head: str) -> None:
        self.current = current
        self.head = head
        state = (
            "has no alembic_version table" if current is None else f"is at {current}"
        )
        super().__init__(
            f"schema {state}, head is {head}: run `make migrate` with an owner connection"
        )


class TakeoverRefused(RuntimeError):
    """An unversioned database differs from a fresh head; nothing was changed."""

    def __init__(self, diff: str) -> None:
        self.diff = diff
        super().__init__(
            "existing schema differs from a fresh head; refusing to stamp:\n" + diff
        )


@dataclass(frozen=True)
class MigrateResult:
    action: Literal["upgraded", "stamped", "unchanged"]
    revision: str


def sqlalchemy_url(dsn: str) -> URL:
    """libpq conninfo (what psycopg takes) -> ``postgresql+psycopg`` URL for Alembic."""
    params = {key: str(value) for key, value in conninfo_to_dict(dsn).items()}
    port = params.pop("port", None)
    return URL.create(
        "postgresql+psycopg",
        username=params.pop("user", None),
        password=params.pop("password", None),
        host=params.pop("host", None),
        port=int(port) if port else None,
        database=params.pop("dbname", None),
        query=params,
    )


def _config(dsn: str) -> Config:
    config = Config(attributes={"dsn": dsn})
    config.set_main_option("script_location", str(_MIGRATIONS))
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config("")).get_current_head()
    if head is None:
        raise RuntimeError("no migrations found")
    return head


def current_revision(conn: psycopg.Connection[object]) -> str | None:
    """Revision recorded in the version table, ``None`` when there is no table."""
    # Callers may hand over dict_row connections (DurableStore); read tuples.
    with conn.cursor(row_factory=tuple_row) as cur:
        present = cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (VERSION_TABLE,)
        ).fetchone()
        if present is None or not present[0]:
            return None
        row = cur.execute(f"SELECT version_num FROM {VERSION_TABLE}").fetchone()
    return None if row is None else str(row[0])


def verify_head(conn: psycopg.Connection[object]) -> None:
    """Raise :class:`SchemaNotMigrated` unless the database is at head."""
    head = head_revision()
    current = current_revision(conn)
    if current != head:
        raise SchemaNotMigrated(current, head)


def upgrade_head(dsn: str) -> str:
    command.upgrade(_config(dsn), "head")
    return head_revision()


def stamp_head(dsn: str) -> str:
    command.stamp(_config(dsn), "head")
    return head_revision()


def schema_dump(dsn: str, *, pg_dump: str = "pg_dump") -> str:
    """Normalized ``pg_dump --schema-only`` of the ``opspilot_*`` objects."""
    try:
        completed = subprocess.run(
            [pg_dump, *_PG_DUMP_FLAGS, f"--dbname={dsn}"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{pg_dump!r} not found: taking over an existing database needs a pg_dump "
            "of the server's major version; set OPSPILOT_PG_DUMP (or --pg-dump) to it"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"{pg_dump} failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    lines = []
    for line in completed.stdout.splitlines():
        # Drop the parts that vary between runs or servers but not between
        # schemas: comments (dump headers), \restrict tokens (pg_dump >= 17.6),
        # SET/SELECT session setup and blank lines.
        if not line or line.startswith(
            ("--", "\\", "SET ", "SELECT pg_catalog.set_config")
        ):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _has_business_tables(conn: psycopg.Connection[object]) -> bool:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'opspilot\\_%'"
        ).fetchone()
    return row is not None and int(row[0]) > 0


def fresh_head_dump(dsn: str, *, pg_dump: str = "pg_dump") -> str:
    """Dump of a throwaway database on the same server upgraded to head.

    Same server, same ``template0``: the only difference to the database under
    takeover can then be the schema itself. Needs CREATEDB on the owner role.
    """
    scratch = f"opspilot_head_{uuid4().hex[:12]}"
    scratch_dsn = make_conninfo(dsn, dbname=scratch)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(scratch)
            )
        )
        try:
            upgrade_head(scratch_dsn)
            return schema_dump(scratch_dsn, pg_dump=pg_dump)
        finally:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(scratch)
                )
            )


def migrate(dsn: str, *, pg_dump: str = "pg_dump") -> MigrateResult:
    """Bring ``dsn`` to head: upgrade, or take over an unversioned database.

    * version table present -> ``alembic upgrade head`` (no-op when at head);
    * no ``opspilot_*`` tables -> ``alembic upgrade head`` from empty;
    * tables but no version table -> compare dumps, stamp only if identical.
    """
    with psycopg.connect(dsn) as conn:
        current = current_revision(conn)
        legacy = current is None and _has_business_tables(conn)
    head = head_revision()
    if current == head:
        return MigrateResult("unchanged", head)
    if not legacy:
        return MigrateResult("upgraded", upgrade_head(dsn))
    existing = schema_dump(dsn, pg_dump=pg_dump)
    expected = fresh_head_dump(dsn, pg_dump=pg_dump)
    if existing != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                existing.splitlines(keepends=True),
                fromfile="fresh head",
                tofile="existing database",
            )
        )
        raise TakeoverRefused(diff)
    return MigrateResult("stamped", stamp_head(dsn))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m opspilot.schema",
        description="Run or verify OpsPilot schema migrations (owner connection, OPSPILOT_DSN).",
    )
    parser.add_argument("action", choices=("migrate", "check"))
    parser.add_argument(
        "--pg-dump",
        default=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"),
        help="pg_dump binary matching the server major version (default: $OPSPILOT_PG_DUMP or pg_dump)",
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get("OPSPILOT_DSN")
    if not dsn:
        print("OPSPILOT_DSN is required", file=sys.stderr)
        return 2
    if args.action == "check":
        try:
            with psycopg.connect(dsn) as conn:
                verify_head(conn)
        except SchemaNotMigrated as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"schema at head {head_revision()}")
        return 0
    try:
        result = migrate(dsn, pg_dump=args.pg_dump)
    except TakeoverRefused as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"schema {result.action}: {result.revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
