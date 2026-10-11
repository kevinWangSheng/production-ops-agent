"""Catalog reads and login-role fixtures for the 0011 runtime roles (#169)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

RUNTIME_ROLES = ("opspilot_web", "opspilot_worker")


def relations(conn: psycopg.Connection) -> set[str]:
    """``alembic_version`` and every ``opspilot_*`` table or view in public."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "WHERE c.relnamespace = 'public'::regnamespace "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND (c.relname LIKE 'opspilot\\_%' OR c.relname = 'alembic_version')"
        )
    }


def catalog_grants(conn: psycopg.Connection, role: str) -> dict[str, frozenset[str]]:
    """The privileges ``role`` itself holds on each relation, in the
    ``RUNTIME_GRANTS`` notation: table-level types by name, column-level ones
    as ``TYPE(col_a,col_b)`` (sorted) unless the same type is held table-wide.
    Read from ``relacl``/``attacl`` directly, so PUBLIC grants and inherited
    memberships do not count and every type (TRUNCATE, REFERENCES, TRIGGER
    included) shows up."""
    grants: dict[str, set[str]] = {name: set() for name in relations(conn)}
    for name, privilege in conn.execute(
        "SELECT c.relname, a.privilege_type FROM pg_catalog.pg_class c, "
        "LATERAL pg_catalog.aclexplode(c.relacl) a "
        "WHERE c.relnamespace = 'public'::regnamespace "
        "AND a.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s)",
        (role,),
    ):
        if name in grants:
            grants[name].add(privilege)
    columns: dict[tuple[str, str], list[str]] = {}
    for name, column, privilege in conn.execute(
        "SELECT c.relname, t.attname, a.privilege_type "
        "FROM pg_catalog.pg_attribute t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.attrelid, "
        "LATERAL pg_catalog.aclexplode(t.attacl) a "
        "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.attisdropped "
        "AND a.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s)",
        (role,),
    ):
        if name in grants and privilege not in grants[name]:
            columns.setdefault((name, privilege), []).append(column)
    for (name, privilege), names in columns.items():
        grants[name].add(f"{privilege}({','.join(sorted(names))})")
    return {name: frozenset(held) for name, held in grants.items()}


@contextmanager
def login(admin_dsn: str, group: str) -> Iterator[str]:
    """A throwaway ``LOGIN IN ROLE <group>`` role; yields its DSN."""
    name = f"{group}_t_{uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(
                sql.Identifier(name), sql.Identifier(group)
            )
        )
    try:
        yield make_conninfo(admin_dsn, user=name)
    finally:
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                (name,),
            )
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
