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


def test_stale_version_is_refused_by_runtime(scratch_dsn: str) -> None:
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("UPDATE alembic_version SET version_num='0000_older'")
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()
