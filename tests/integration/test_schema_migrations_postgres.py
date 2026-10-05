"""Schema migration paths on a real PostgreSQL (task record 2026-10-05, PR-a).

Each test creates its own database on the lab server so the three entry
states are exercised from scratch: empty, built by the pre-Alembic inline
DDL (frozen in ``legacy_schema_2026-10-05.sql``), and drifted. The runtime
``install()`` must refuse an unmigrated database and accept a migrated one.
"""

import os
import pathlib
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.store import DurableWebLedger
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

LEGACY_DDL = (
    pathlib.Path(__file__).parent / "legacy_schema_2026-10-05.sql"
).read_text()
PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_mig_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    try:
        yield make_conninfo(DSN, dbname=name)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _apply_legacy(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(LEGACY_DDL)


# A database that grew through the historical ``ADD COLUMN IF NOT EXISTS``:
# ``lifecycle`` and ``sequence`` were added after the tables existed, so they
# sit last instead of where the current CREATE TABLE lists them. These are
# the two real differences found on the 55431 lab (task record 2026-10-05).
GROWN_DDL = LEGACY_DDL.replace(
    "state text NOT NULL, lifecycle text NOT NULL DEFAULT 'open', control_generation",
    "state text NOT NULL, control_generation",
).replace(
    "sequence integer NOT NULL DEFAULT 0, logical_key text NOT NULL,",
    "logical_key text NOT NULL,",
)
assert GROWN_DDL != LEGACY_DDL
# Grown further back: ``actor`` arrives through the ALTER, which carries
# DEFAULT 'unknown' where the fresh table has no default. A real difference.
GROWN_WITH_DEFAULT_DDL = GROWN_DDL.replace(
    "suspended boolean NOT NULL, generation integer NOT NULL, actor text NOT NULL,",
    "suspended boolean NOT NULL, generation integer NOT NULL,",
)
assert GROWN_WITH_DEFAULT_DDL != GROWN_DDL


def _install_all(dsn: str) -> None:
    store = DurableStore(dsn)
    store.install()
    DurableEventLog(store).install()
    DurableEvidenceStore(store).install()
    DurableWebLedger(store).install()


def test_empty_database_upgrades_to_head_and_runtime_accepts(scratch_dsn: str) -> None:
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()

    result = schema.migrate(scratch_dsn, pg_dump=PG_DUMP)

    assert result == schema.MigrateResult("upgraded", "0001_baseline")
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == "0001_baseline"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'opspilot\\_%'"
            )
        }
    assert len(tables) == 15
    _install_all(scratch_dsn)
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP).action == "unchanged"


def test_legacy_database_is_stamped_when_identical(scratch_dsn: str) -> None:
    _apply_legacy(scratch_dsn)
    before = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()

    result = schema.migrate(scratch_dsn, pg_dump=PG_DUMP)

    assert result == schema.MigrateResult("stamped", "0001_baseline")
    # Stamping records the version and touches nothing else.
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == before
    assert before == schema.fresh_head_dump(scratch_dsn, pg_dump=PG_DUMP)
    _install_all(scratch_dsn)


def test_drifted_database_is_refused_and_left_unversioned(scratch_dsn: str) -> None:
    _apply_legacy(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("ALTER TABLE opspilot_runs ADD COLUMN stray text")

    with pytest.raises(schema.TakeoverRefused) as refused:
        schema.migrate(scratch_dsn, pg_dump=PG_DUMP)

    assert "+    stray text" in refused.value.diff
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) is None
        assert conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'opspilot\\_head\\_%'"
        ).fetchone() == (0,)
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()


def test_legacy_database_missing_an_index_is_refused(scratch_dsn: str) -> None:
    _apply_legacy(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("DROP INDEX opspilot_runs_incident_id_idx")

    with pytest.raises(schema.TakeoverRefused) as refused:
        schema.migrate(scratch_dsn, pg_dump=PG_DUMP)

    assert "-CREATE INDEX opspilot_runs_incident_id_idx" in refused.value.diff
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) is None


def test_missing_pg_dump_names_the_override(scratch_dsn: str) -> None:
    _apply_legacy(scratch_dsn)
    with pytest.raises(RuntimeError, match="OPSPILOT_PG_DUMP"):
        schema.migrate(scratch_dsn, pg_dump="/nonexistent/pg_dump")
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) is None


def test_grown_database_needs_the_column_order_flag(scratch_dsn: str) -> None:
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(GROWN_DDL)
    with psycopg.connect(scratch_dsn) as conn:
        order = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT attrelid::regclass::text, array_agg(attname ORDER BY attnum) "
                "FROM pg_attribute WHERE attnum > 0 AND NOT attisdropped "
                "AND attrelid IN ('opspilot_incidents'::regclass, 'opspilot_steps'::regclass) "
                "GROUP BY attrelid"
            )
        }
    assert order["opspilot_incidents"][-2:] == ["lifecycle", "target_id"]
    assert order["opspilot_steps"][-1] == "sequence"

    with pytest.raises(schema.TakeoverRefused) as refused:
        schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    assert refused.value.column_order_only
    assert "--accept-column-order" in str(refused.value)
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) is None

    result = schema.migrate(scratch_dsn, pg_dump=PG_DUMP, accept_column_order=True)

    assert result.action == "stamped" and result.revision == "0001_baseline"
    assert "+    lifecycle text DEFAULT 'open'::text NOT NULL" in result.accepted_diff
    assert "+    sequence integer DEFAULT 0 NOT NULL" in result.accepted_diff
    _install_all(scratch_dsn)
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP).action == "unchanged"


def test_semantic_difference_is_refused_even_with_the_flag(scratch_dsn: str) -> None:
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(GROWN_WITH_DEFAULT_DDL)

    with pytest.raises(schema.TakeoverRefused) as refused:
        schema.migrate(scratch_dsn, pg_dump=PG_DUMP, accept_column_order=True)

    assert not refused.value.column_order_only
    assert "+    actor text DEFAULT 'unknown'::text NOT NULL" in refused.value.diff
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) is None
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()


def test_runtime_role_without_version_table_grant_gets_a_distinct_code(
    scratch_dsn: str,
) -> None:
    """A non-owner role (M1-02 Observer) must be told to GRANT, not 'storage unavailable'."""
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    role = f"opspilot_rt_{uuid4().hex[:12]}"
    role_dsn = make_conninfo(scratch_dsn, user=role)
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    try:
        with psycopg.connect(scratch_dsn) as conn:
            conn.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            conn.execute(
                sql.SQL("REVOKE SELECT ON alembic_version FROM {}").format(
                    sql.Identifier(role)
                )
            )
        with pytest.raises(
            PersistenceError, match="^SCHEMA_VERSION_UNREADABLE$"
        ) as denied:
            DurableStore(role_dsn).install()
        assert "GRANT SELECT ON alembic_version" in str(denied.value.__cause__)

        with psycopg.connect(scratch_dsn) as conn:
            conn.execute(
                sql.SQL("GRANT SELECT ON alembic_version TO {}").format(
                    sql.Identifier(role)
                )
            )
        DurableStore(role_dsn).install()
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_stale_version_is_refused_by_runtime(scratch_dsn: str) -> None:
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("UPDATE alembic_version SET version_num='0000_older'")
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()
