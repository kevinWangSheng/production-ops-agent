"""``python -m opspilot.deploy.roles``: the runtime logins of a Compose install.

Runs once per install or upgrade, after ``python -m opspilot.schema migrate``
and before any runtime starts, on the owner connection (``OPSPILOT_DSN``,
password through libpq ``passfile=``). Migrations only create the NOLOGIN
groups (0003, 0011, 0023); this creates one SCRAM login per group, its
password taken from that runtime's own pgpass secret file
(``OPSPILOT_ROLE_SECRETS_DIR``, default ``/run/secrets``), and then connects
as each login with that same file and checks the schema is at head -- the
check every runtime repeats on start.

An existing login is reused untouched when it is a plain ``LOGIN INHERIT``
member of its group and nothing else, and refused otherwise (same rule as
``scripts/m0/postgres_lab.py runtime-roles``); its password is never reset,
so a login whose secret file was regenerated fails the login check instead
of being silently re-keyed. ``TEMP`` on the product database is revoked from
``PUBLIC`` (the database belongs to this install): a login that can create
temporary tables can shadow catalog reads (0003).
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from opspilot.schema import SchemaNotMigrated, head_revision, verify_head

__all__ = [
    "LOGINS",
    "LoginAttributes",
    "LoginSpec",
    "RoleSetupError",
    "conflicts",
    "main",
    "read_passfile",
]

SECRETS_DIR_VAR = "OPSPILOT_ROLE_SECRETS_DIR"


class RoleSetupError(Exception):
    """A refusal; the message names files and roles, never a secret."""


@dataclass(frozen=True)
class LoginSpec:
    login: str
    group: str
    #: file name of the runtime's pgpass secret
    secret: str


LOGINS: tuple[LoginSpec, ...] = (
    LoginSpec("opspilot_web_app", "opspilot_web", "web_pgpass"),
    LoginSpec("opspilot_worker_app", "opspilot_worker", "worker_pgpass"),
    LoginSpec("opspilot_observer_app", "opspilot_observer", "observer_pgpass"),
    LoginSpec("opspilot_retention_app", "opspilot_retention", "retention_pgpass"),
)


@dataclass(frozen=True)
class LoginAttributes:
    login: bool
    inherit: bool
    superuser: bool
    createrole: bool
    createdb: bool
    replication: bool
    bypassrls: bool
    groups: tuple[str, ...]


_PLAIN = {
    "login": True,
    "inherit": True,
    "superuser": False,
    "createrole": False,
    "createdb": False,
    "replication": False,
    "bypassrls": False,
}


def conflicts(attributes: LoginAttributes, group: str) -> list[str]:
    """What keeps ``attributes`` from being a plain inheriting member of
    ``group`` only; empty when the login is reusable."""
    problems = [
        f"{field.name.upper()}={getattr(attributes, field.name)}"
        for field in fields(attributes)
        if field.name in _PLAIN
        and getattr(attributes, field.name) != _PLAIN[field.name]
    ]
    if attributes.groups != (group,):
        problems.append(f"membership={list(attributes.groups)}")
    return problems


def read_passfile(path: Path) -> tuple[str, str]:
    """``(login, password)`` from a one-line ``*:*:*:<login>:<password>``
    pgpass file that only its owner can read (libpq ignores it otherwise)."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RoleSetupError(f"{path.name}: must be mode 0600 or stricter")
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    parts = lines[0].split(":") if len(lines) == 1 else []
    if len(parts) != 5 or parts[:3] != ["*", "*", "*"] or not all(parts[3:]):
        raise RoleSetupError(f"{path.name}: expected one line *:*:*:<login>:<password>")
    return parts[3], parts[4]


def _attributes(
    conn: psycopg.Connection[tuple[object, ...]], login: str
) -> LoginAttributes | None:
    row = conn.execute(
        "SELECT r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreaterole, "
        "r.rolcreatedb, r.rolreplication, r.rolbypassrls, "
        "array(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g "
        "ON g.oid = m.roleid WHERE m.member = r.oid ORDER BY 1) "
        "FROM pg_roles r WHERE r.rolname = %s",
        (login,),
    ).fetchone()
    if row is None:
        return None
    (
        can_login,
        inherit,
        superuser,
        createrole,
        createdb,
        replication,
        bypassrls,
        groups,
    ) = row
    assert isinstance(groups, list)
    return LoginAttributes(
        login=bool(can_login),
        inherit=bool(inherit),
        superuser=bool(superuser),
        createrole=bool(createrole),
        createdb=bool(createdb),
        replication=bool(replication),
        bypassrls=bool(bypassrls),
        groups=tuple(str(name) for name in groups),
    )


def ensure_logins(
    owner_dsn: str, secrets: Path, logins: Sequence[LoginSpec] = LOGINS
) -> list[str]:
    """Create or reuse every login, then log in as each; one report line per
    login. Raises :class:`RoleSetupError` or ``SchemaNotMigrated``."""
    passwords = {}
    for spec in logins:
        name, password = read_passfile(secrets / spec.secret)
        if name != spec.login:
            raise RoleSetupError(
                f"{spec.secret}: names login {name!r}, expected {spec.login!r}"
            )
        passwords[spec.login] = password
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        verify_head(conn)
        database = conn.execute("SELECT current_database()").fetchone()
        assert database is not None
        conn.execute(
            sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(str(database[0]))
            )
        )
        for spec in logins:
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (spec.group,)
            ).fetchone():
                raise RoleSetupError(f"ROLE_MISSING: {spec.group} (migrate first)")
            existing = _attributes(conn, spec.login)
            if existing is None:
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN INHERIT PASSWORD {} IN ROLE {}"
                    ).format(
                        sql.Identifier(spec.login),
                        sql.Literal(passwords[spec.login]),
                        sql.Identifier(spec.group),
                    )
                )
                continue
            problems = conflicts(existing, spec.group)
            if problems:
                raise RoleSetupError(
                    f"ROLE_CONFLICT: {spec.login} ({', '.join(problems)}; expected a "
                    f"LOGIN INHERIT member of {spec.group} only); not altered"
                )
    owner = {
        key: str(value)
        for key, value in conninfo_to_dict(owner_dsn).items()
        if key != "password" and value is not None
    }
    report = []
    head = head_revision()
    for spec in logins:
        dsn = make_conninfo(
            "", **{**owner, "user": spec.login, "passfile": str(secrets / spec.secret)}
        )
        try:
            with psycopg.connect(dsn) as conn:
                verify_head(conn)
        except psycopg.OperationalError:
            raise RoleSetupError(
                f"LOGIN_REFUSED: {spec.login} cannot log in with {spec.secret} "
                "(an existing login keeps its password; restore the original secret file)"
            ) from None
        report.append(f"{spec.group}: login {spec.login} ok, schema at head {head}")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    dsn = os.environ.get("OPSPILOT_DSN")
    if not dsn:
        print("OPSPILOT_DSN is required", file=sys.stderr)
        return 2
    secrets = Path(os.environ.get(SECRETS_DIR_VAR, "/run/secrets"))
    try:
        report = ensure_logins(dsn, secrets)
    except (RoleSetupError, SchemaNotMigrated) as exc:
        print(exc, file=sys.stderr)
        return 1
    for line in report:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
