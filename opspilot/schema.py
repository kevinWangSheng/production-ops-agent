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
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row
from sqlalchemy.engine import URL

_log = logging.getLogger(__name__)

VERSION_TABLE = "alembic_version"
# The revision a database built by the pre-Alembic inline DDL is stamped at
# before the later revisions run (ADR-0007, task record 2026-10-05).
BASELINE_REVISION = "0001_baseline"
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


class SchemaVersionUnreadable(RuntimeError):
    """The runtime role may not read the version table; nothing else is known."""

    def __init__(self) -> None:
        super().__init__(
            f"permission denied reading {VERSION_TABLE}: the runtime role needs "
            f"`GRANT SELECT ON {VERSION_TABLE} TO <runtime role>` after `make migrate`"
        )


class TakeoverRefused(RuntimeError):
    """An unversioned database differs from a fresh head; nothing was changed."""

    def __init__(self, diff: str, *, column_order_only: bool = False) -> None:
        self.diff = diff
        self.column_order_only = column_order_only
        hint = (
            " (only the column order inside tables differs; rerun with"
            " --accept-column-order to stamp and record this diff)"
            if column_order_only
            else ""
        )
        super().__init__(
            f"existing schema differs from a fresh head; refusing to stamp{hint}:\n"
            + diff
        )


class IllegalStateValues(RuntimeError):
    """Rows hold values a new CHECK constraint would reject; nothing was changed.

    Raised inside a migration (``0002_state_checks``) before any ``ALTER``;
    ``transaction_per_migration`` rolls that revision back, so the database
    stays at the previous revision and the data is untouched. The operator
    fixes or removes the listed rows, then reruns ``make migrate``.
    """

    def __init__(self, rows: list[tuple[str, str, str, int]]) -> None:
        self.rows = rows
        report = "\n".join(
            f"  {table}.{column} = {value!r}: {count} row(s)"
            for table, column, value, count in rows
        )
        super().__init__(
            "existing rows violate the state CHECK constraints; refusing to migrate, "
            "nothing was changed:\n" + report
        )


#: JSON file mapping an operator target id (the registry's ``resource_uid``)
#: to its immutable identity: ``{"<uid>": {"integration_id": ..,
#: "cluster_uid": .., "namespace": ..}}``. Read by the workbench at intake
#: and by migration 0004 to complete rows registered before it existed.
TARGET_IDENTITIES_ENV = "OPSPILOT_TARGET_IDENTITIES"


class TargetIdentityMissing(RuntimeError):
    """Registered targets have no complete identity to migrate to; nothing was changed.

    Raised inside ``0004_target_identity`` before any ``ALTER``;
    ``transaction_per_migration`` rolls the revision back and the database
    stays at 0003. The operator completes ``OPSPILOT_TARGET_IDENTITIES``
    for every listed ``resource_uid`` and reruns ``make migrate``. The
    migration never invents an identity.
    """

    def __init__(self, resource_uids: list[str], *, detail: str | None = None) -> None:
        self.resource_uids = list(resource_uids)
        self.detail = detail
        report = "\n".join(f"  {uid}" for uid in self.resource_uids)
        message = (
            "registered targets without a complete identity in "
            f"{TARGET_IDENTITIES_ENV}; refusing to migrate, nothing was changed"
        )
        if detail:
            message += f" ({detail})"
        super().__init__(message + (":\n" + report if report else ""))


TARGET_IDENTITY_FIELDS = ("integration_id", "cluster_uid", "namespace")


def load_target_identities(path: str | Path) -> dict[str, dict[str, str]]:
    """``resource_uid -> {integration_id, cluster_uid, namespace[,
    health_profile_id]}`` from the ``OPSPILOT_TARGET_IDENTITIES`` file.

    ``health_profile_id`` names the HealthProfile (its ``profile_id``) whose
    recovery definition applies to this target; a remediation is registered
    only under that profile (the workbench compares it with the loaded
    profile, PR #120 bot review P1).

    Shared by the workbench (intake resolves the operator's target id here)
    and migration 0004 (completing rows registered before the columns
    existed). Strict: an unreadable file, a non-object top level, an entry
    missing a field, an empty value, an unknown key or a ``resource_uid`` that
    contradicts its key raises :class:`TargetIdentityMissing`; nothing is
    guessed.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetIdentityMissing(
            [],
            detail=f"{TARGET_IDENTITIES_ENV} is unreadable: {exc.__class__.__name__}",
        ) from None
    if not isinstance(payload, dict):
        raise TargetIdentityMissing([], detail="top level is not an object")
    identities: dict[str, dict[str, str]] = {}
    for uid, entry in payload.items():
        if (
            not isinstance(uid, str)
            or not uid
            or not isinstance(entry, dict)
            or set(entry)
            - set(TARGET_IDENTITY_FIELDS)
            - {"resource_uid", "health_profile_id"}
            or (
                "health_profile_id" in entry
                and (
                    not isinstance(entry["health_profile_id"], str)
                    or not entry["health_profile_id"]
                )
            )
            or any(
                not isinstance(entry.get(column), str) or not entry[column]
                for column in TARGET_IDENTITY_FIELDS
            )
            or ("resource_uid" in entry and entry["resource_uid"] != uid)
        ):
            raise TargetIdentityMissing([str(uid)], detail="malformed entry")
        identities[uid] = {column: entry[column] for column in TARGET_IDENTITY_FIELDS}
        if "health_profile_id" in entry:
            identities[uid]["health_profile_id"] = entry["health_profile_id"]
    return identities


@dataclass(frozen=True)
class MigrateResult:
    # ``stamped``: a legacy database was stamped at BASELINE_REVISION and then
    # upgraded; ``revision`` is always where the database ended up (head).
    action: Literal["upgraded", "stamped", "unchanged"]
    revision: str
    # Non-empty only when --accept-column-order was needed: the verbatim diff
    # (column order inside tables) that the operator chose to accept.
    accepted_diff: str = ""


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
        try:
            row = cur.execute(f"SELECT version_num FROM {VERSION_TABLE}").fetchone()
        except errors.InsufficientPrivilege as exc:
            # Only this statement, only this error: a non-owner runtime role
            # that was granted the business tables but not the version table.
            raise SchemaVersionUnreadable() from exc
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


def upgrade_to(dsn: str, revision: str) -> None:
    command.upgrade(_config(dsn), revision)


def stamp(dsn: str, revision: str) -> None:
    command.stamp(_config(dsn), revision)


@contextmanager
def _subprocess_credentials(dsn: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Split ``dsn`` into a password-free conninfo and a subprocess environment.

    The owner DSN can carry a DDL-capable password; it must not appear in
    ``ps``/``/proc`` for the lifetime of a dump. The command line gets only
    the non-secret parameters; the password goes through a 0600 PGPASSFILE
    in a private temp dir that exists just for the call. PGPASSWORD is never
    used (it is readable from the environment of the process).
    """
    params = {key: str(value) for key, value in conninfo_to_dict(dsn).items()}
    password = params.pop("password", None)
    params.pop("sslpassword", None)
    env = {key: value for key, value in os.environ.items() if key != "PGPASSWORD"}
    public = make_conninfo(**params)
    if password is None:
        yield public, env
        return
    escaped = password.replace("\\", "\\\\").replace(":", "\\:")
    folder = Path(tempfile.mkdtemp(prefix="opspilot-pgpass-"))
    passfile = folder / "pgpass"
    try:
        passfile.touch(mode=0o600)
        passfile.chmod(0o600)
        passfile.write_text(f"*:*:*:*:{escaped}\n")
        env["PGPASSFILE"] = str(passfile)
        yield public, env
    finally:
        passfile.unlink(missing_ok=True)
        folder.rmdir()


def schema_dump(dsn: str, *, pg_dump: str = "pg_dump") -> str:
    """Normalized ``pg_dump --schema-only`` of the ``opspilot_*`` objects."""
    try:
        with _subprocess_credentials(dsn) as (public_dsn, env):
            completed = subprocess.run(
                [pg_dump, *_PG_DUMP_FLAGS, f"--dbname={public_dsn}"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=env,
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
    """Dump of a throwaway database on the same server upgraded to head."""
    return fresh_dump(dsn, "head", pg_dump=pg_dump)


def fresh_dump(dsn: str, revision: str, *, pg_dump: str = "pg_dump") -> str:
    """Dump of a throwaway database on the same server upgraded to ``revision``.

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
            upgrade_to(scratch_dsn, revision)
            return schema_dump(scratch_dsn, pg_dump=pg_dump)
        finally:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(scratch)
                )
            )


def sort_table_columns(dump: str) -> str:
    """Same dump with column lines sorted inside each ``CREATE TABLE`` block.

    Only the order of column definitions changes: each line keeps its type,
    default and NOT NULL text, CONSTRAINT lines keep their place after the
    columns, and everything outside the blocks (indexes, identity, sequences,
    keys) is untouched. Historical ``ADD COLUMN`` appends a column at the
    end, a fresh ``CREATE TABLE`` puts it where the DDL lists it.

    Assumes pg_dump's layout: one line per column or constraint entry inside
    the block and the closing ``)`` at column 0 (``line.startswith(")")`` is
    the terminator). pg_dump has emitted this layout for every supported
    major; a multi-line entry would be sorted as separate lines.
    """
    out: list[str] = []
    block: list[str] | None = None
    for line in dump.splitlines():
        if block is None:
            if line.startswith("CREATE TABLE ") and line.endswith("("):
                block = []
            out.append(line)
        elif line.startswith(")"):
            columns = sorted(
                c for c in block if not c.lstrip().startswith("CONSTRAINT")
            )
            constraints = [c for c in block if c.lstrip().startswith("CONSTRAINT")]
            body = columns + constraints
            out.extend(f"{entry}," for entry in body[:-1])
            out.extend(body[-1:])
            out.append(line)
            block = None
        else:
            block.append(line.rstrip(","))
    return "\n".join(out) + "\n"


def _diff(expected: str, existing: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            existing.splitlines(keepends=True),
            fromfile="fresh head",
            tofile="existing database",
        )
    )


def migrate(
    dsn: str, *, pg_dump: str = "pg_dump", accept_column_order: bool = False
) -> MigrateResult:
    """Bring ``dsn`` to head: upgrade, or take over an unversioned database.

    * version table present -> ``alembic upgrade head`` (no-op when at head);
    * no ``opspilot_*`` tables -> ``alembic upgrade head`` from empty;
    * tables but no version table -> compare with a fresh database at
      BASELINE_REVISION (the inline DDL the legacy database was built by),
      stamp the baseline only if identical, then upgrade to head; with
      ``accept_column_order`` a difference that is *only* the order of
      columns inside tables is accepted, stamped and returned as
      ``accepted_diff`` so the operator records it. Anything else refuses.
      A later revision may still refuse (``IllegalStateValues``); the
      database is then left stamped at the baseline, data untouched.
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
    expected = fresh_dump(dsn, BASELINE_REVISION, pg_dump=pg_dump)
    diff = ""
    if existing != expected:
        diff = _diff(expected, existing)
        column_order_only = sort_table_columns(existing) == sort_table_columns(expected)
        if not (column_order_only and accept_column_order):
            raise TakeoverRefused(diff, column_order_only=column_order_only)
        _log.warning("takeover accepted a column-order-only difference:\n%s", diff)
    stamp(dsn, BASELINE_REVISION)
    return MigrateResult("stamped", upgrade_head(dsn), accepted_diff=diff)


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
    parser.add_argument(
        "--accept-column-order",
        action="store_true",
        help="stamp an unversioned database whose only difference to a fresh head is the "
        "order of columns inside tables; the accepted diff is printed for the record",
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
        result = migrate(
            dsn, pg_dump=args.pg_dump, accept_column_order=args.accept_column_order
        )
    except (TakeoverRefused, IllegalStateValues, TargetIdentityMissing) as exc:
        print(exc, file=sys.stderr)
        return 1
    if result.accepted_diff:
        print("accepted column-order difference (record this with the stamp):")
        print(result.accepted_diff, end="")
    print(f"schema {result.action}: {result.revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
