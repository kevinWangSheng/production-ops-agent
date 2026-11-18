"""Runtime PostgreSQL roles hold only what the declaration grants (#169).

Two layers. The catalog after ``make migrate`` equals ``RUNTIME_GRANTS``
(a migration that grants more, or a new table nobody decided, fails), and a
short list of denials is pinned independently of the declaration, so a
matching edit to both cannot quietly widen a role: the worker never writes
reviewed knowledge, the web never issues a Run lease, nobody deletes,
truncates or creates objects outside the two declared delete paths.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import catalog_grants, relations

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

OBSERVER_ROLE = "opspilot_observer"
REVIEWED_KNOWLEDGE = (
    "opspilot_knowledge_entries",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
)


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, autocommit=True) as connection:
        yield connection


def _privilege(conn, role: str, table: str, privilege: str) -> bool:
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
    ).fetchone()[0]


def _any_column(conn, role: str, table: str, privilege: str) -> bool:
    return conn.execute(
        "SELECT has_any_column_privilege(%s, %s, %s)", (role, table, privilege)
    ).fetchone()[0]


@pytest.mark.parametrize("role", sorted(RUNTIME_GRANTS))
def test_the_catalog_equals_the_declaration(conn, role):
    present = relations(conn)
    declared = RUNTIME_GRANTS[role]
    assert set(declared) == present
    actual = catalog_grants(conn, role)
    drift = {
        name: (sorted(actual[name]), sorted(declared[name]))
        for name in present
        if actual[name] != declared[name]
    }
    assert not drift


@pytest.mark.parametrize("table", REVIEWED_KNOWLEDGE)
def test_the_worker_never_writes_reviewed_knowledge(conn, table):
    for privilege in ("INSERT", "UPDATE"):
        assert not _any_column(conn, WORKER_ROLE, table, privilege), (table, privilege)
    assert not _privilege(conn, WORKER_ROLE, table, "DELETE")


def test_the_web_cannot_issue_a_run_lease(conn):
    for column in ("epoch", "owner_epoch"):
        exists = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='opspilot_runs' AND column_name=%s",
            (column,),
        ).fetchone()
        if exists:
            assert not conn.execute(
                "SELECT has_column_privilege(%s, 'opspilot_runs', %s, 'UPDATE')",
                (WEB_ROLE, column),
            ).fetchone()[0], column


@pytest.mark.parametrize("role", [WEB_ROLE, WORKER_ROLE, OBSERVER_ROLE])
def test_no_runtime_role_deletes_truncates_or_creates(conn, role):
    # The two DELETE paths the product has: web ledger keys
    # (opspilot/web/store.py) and the worker's trace outbox pruning
    # (opspilot/persistence/outbox.py). Business records are never deleted.
    allowed_delete = {
        (WEB_ROLE, "opspilot_web_ledger"),
        (WORKER_ROLE, "opspilot_trace_outbox"),
    }
    for table in sorted(relations(conn) - {"alembic_version"}):
        assert not _privilege(conn, role, table, "TRUNCATE"), (role, table)
        if (role, table) not in allowed_delete:
            assert not _privilege(conn, role, table, "DELETE"), (role, table)
    assert not conn.execute(
        "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,)
    ).fetchone()[0]


def test_the_observer_may_move_only_the_incident_lifecycle(conn):
    granted = {
        column
        for (column,) in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='opspilot_incidents' "
            "AND has_column_privilege(%s, 'opspilot_incidents', column_name, 'UPDATE')",
            (OBSERVER_ROLE,),
        )
    }
    assert granted <= {"lifecycle"}
    assert not _any_column(conn, OBSERVER_ROLE, "opspilot_incidents", "INSERT")
