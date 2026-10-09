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
from opspilot.domain.runs import RunExecution
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
HEAD = "0008_postmortem_knowledge"
# opspilot_* tables at head: 15 in the baseline + 5 of 0003 (profiles,
# sessions, samples, readings, endings) + 10 of 0008 (postmortems, versions,
# conclusions, disputes, proposals, knowledge entries/revisions/revocations,
# requests, audit).
TABLES_AT_HEAD = 30


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

    assert result == schema.MigrateResult("upgraded", HEAD)
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == HEAD
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'opspilot\\_%'"
            )
        }
    assert len(tables) == TABLES_AT_HEAD
    _install_all(scratch_dsn)
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP).action == "unchanged"


def test_legacy_database_is_stamped_when_identical(scratch_dsn: str) -> None:
    _apply_legacy(scratch_dsn)
    before = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()

    result = schema.migrate(scratch_dsn, pg_dump=PG_DUMP)

    # Stamped at the baseline (identical to a fresh 0001), then upgraded.
    assert result == schema.MigrateResult("stamped", HEAD)
    assert before == schema.fresh_dump(scratch_dsn, "0001_baseline", pg_dump=PG_DUMP)
    after = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    assert after != before
    assert after == schema.fresh_head_dump(scratch_dsn, pg_dump=PG_DUMP)
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

    assert result == schema.MigrateResult("stamped", HEAD, result.accepted_diff)
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


# --- 0002_state_checks (task record 2026-10-05, PR-c; hardening record C2) ---


def _seed_run(dsn: str, *, run_state: str = "queued", lifecycle: str = "open") -> None:
    """One incident and run through plain SQL (the tables may lack CHECKs)."""
    with psycopg.connect(dsn) as conn:
        incident, run = uuid4(), uuid4()
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle) VALUES(%s,%s,'queued',%s)",
            (incident, f"seed-{incident}", lifecycle),
        )
        conn.execute(
            "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) VALUES(%s,%s,%s,0,10,clock_timestamp()+interval '1 hour','{}')",
            (run, incident, run_state),
        )


def _check_constraints(dsn: str) -> set[str]:
    """The 0002 constraints (0003 adds its own tables with their own CHECKs)."""
    with psycopg.connect(dsn) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint WHERE contype='c' AND conrelid IN ('opspilot_runs'::regclass,'opspilot_incidents'::regclass) AND conname LIKE 'opspilot\\_%\\_check'"
            )
        }


EXPECTED_CHECKS = {
    "opspilot_runs_state_check",
    "opspilot_incidents_lifecycle_check",
    # 0005 (M1-02 step 3b): the control mode, same discipline
    "opspilot_incidents_mode_check",
}


def test_illegal_state_write_is_rejected_at_head(scratch_dsn: str) -> None:
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    _seed_run(scratch_dsn)
    assert _check_constraints(scratch_dsn) == EXPECTED_CHECKS
    with psycopg.connect(scratch_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as rejected:
            conn.execute("UPDATE opspilot_runs SET state='banana'")
        assert "opspilot_runs_state_check" in str(rejected.value)
    with psycopg.connect(scratch_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE opspilot_incidents SET lifecycle='banana'")
    # Every domain value is still accepted.
    with psycopg.connect(scratch_dsn) as conn:
        for state in RunExecution.__args__:
            conn.execute("UPDATE opspilot_runs SET state=%s", (state,))
        assert conn.execute("SELECT count(*) FROM opspilot_runs").fetchone() == (1,)


def test_legacy_database_with_illegal_values_stays_at_the_baseline(
    scratch_dsn: str,
) -> None:
    _apply_legacy(scratch_dsn)
    _seed_run(scratch_dsn, run_state="banana")
    _seed_run(scratch_dsn, run_state="banana", lifecycle="peach")
    before = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)

    with pytest.raises(schema.IllegalStateValues) as refused:
        schema.migrate(scratch_dsn, pg_dump=PG_DUMP)

    assert refused.value.rows == [
        ("opspilot_runs", "state", "banana", 2),
        ("opspilot_incidents", "lifecycle", "peach", 1),
    ]
    assert "opspilot_runs.state = 'banana': 2 row(s)" in str(refused.value)
    with psycopg.connect(scratch_dsn) as conn:
        # Takeover stamped the baseline; 0002 rolled back and changed no data.
        assert schema.current_revision(conn) == "0001_baseline"
        assert conn.execute(
            "SELECT count(*) FROM opspilot_runs WHERE state='banana'"
        ).fetchone() == (2,)
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == before
    assert _check_constraints(scratch_dsn) == set()
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()

    # Once the rows are fixed the same command finishes the upgrade.
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("UPDATE opspilot_runs SET state='cancelled' WHERE state='banana'")
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE lifecycle='peach'"
        )
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    _install_all(scratch_dsn)


def test_upgrade_downgrade_upgrade_round_trip(scratch_dsn: str) -> None:
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    head_dump = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    _seed_run(scratch_dsn)

    schema.command.downgrade(schema._config(scratch_dsn), "0001_baseline")

    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == "0001_baseline"
    assert _check_constraints(scratch_dsn) == set()
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == schema.fresh_dump(
        scratch_dsn, "0001_baseline", pg_dump=PG_DUMP
    )
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("UPDATE opspilot_runs SET state='banana'")
    with pytest.raises(PersistenceError, match="SCHEMA_NOT_MIGRATED"):
        DurableStore(scratch_dsn).install()

    with pytest.raises(schema.IllegalStateValues):
        schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("UPDATE opspilot_runs SET state='queued'")
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == head_dump
    assert _check_constraints(scratch_dsn) == EXPECTED_CHECKS
    _install_all(scratch_dsn)


def test_upgrade_through_0006_keeps_the_job_of_a_session_it_ends(
    scratch_dsn: str,
) -> None:
    """0006 ends open sessions and clears their task slots; 0007's columns
    exist by then, so a direct 0005 -> head upgrade records the in-flight job
    (bot review of PR #165, P1). Downgrading to 0005 leaves no trace."""
    schema.upgrade_to(scratch_dsn, "0005_incident_mode")
    before_dump = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    incident, target, session, job = uuid4(), uuid4(), uuid4(), uuid4()
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
            (target, f"uid-{target}"),
        )
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,target_id) VALUES(%s,%s,'queued','observing_recovery',%s)",
            (incident, f"seed-{incident}", target),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds,issued_sequence,active_sample_job_id,active_sample_sequence,active_sample_due_at) VALUES(%s,%s,'incident_recovery',%s,'{}',0,1,'t',0,0,clock_timestamp()+interval '1 hour',10,30,60,3,%s,3,clock_timestamp())",
            (session, incident, target, job),
        )
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute(
            "SELECT ended_reason, job_id, job_sequence FROM opspilot_observation_endings WHERE session_id=%s",
            (session,),
        ).fetchall() == [("authority_revoked", job, 3)]
    schema.command.downgrade(schema._config(scratch_dsn), "0005_incident_mode")
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == before_dump


# --- 0003_observation_store (M1-02 step 2, issue #84) ---


def _observer_grants(dsn: str) -> set[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        return {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT table_name, privilege_type FROM information_schema.table_privileges WHERE grantee='opspilot_observer' UNION SELECT table_name, privilege_type || ':' || column_name FROM information_schema.column_privileges WHERE grantee='opspilot_observer' AND table_name IN ('opspilot_incidents','opspilot_observation_sessions','opspilot_observation_samples','opspilot_observation_endings')"
            )
        }


def _role_exists() -> bool:
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname='opspilot_observer'"
        ).fetchone() == (1,)


def test_0003_downgrade_removes_tables_column_and_grants(scratch_dsn: str) -> None:
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    head_dump = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    grants = _observer_grants(scratch_dsn)
    assert ("opspilot_incidents", "UPDATE:lifecycle") in grants
    assert ("opspilot_incidents", "UPDATE:conclusion") not in grants
    assert ("opspilot_incidents", "SELECT:conclusion") not in grants
    assert ("opspilot_observation_sessions", "UPDATE:deadline_at") not in grants
    assert ("opspilot_runs", "SELECT") not in grants
    assert ("opspilot_observation_samples", "INSERT:sample_id") in grants
    assert ("opspilot_observation_samples", "INSERT:submitted_at") not in grants
    assert ("opspilot_observation_endings", "INSERT:ending_id") in grants
    assert ("opspilot_observation_endings", "INSERT:recorded_at") not in grants
    assert ("opspilot_observation_samples", "UPDATE") not in grants

    schema.command.downgrade(schema._config(scratch_dsn), "0002_state_checks")

    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == "0002_state_checks"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'opspilot\\_observation%'"
            )
        }
        assert tables == set()
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='opspilot_incidents' AND column_name='observation_generation'"
        ).fetchone() == (0,)
    assert _observer_grants(scratch_dsn) == set()
    # The role is cluster-wide: it is dropped only once no database on the
    # server references it any more. The lab database (migrated to head by
    # the session fixture) still does, so it stays; see the next test.
    assert _role_exists()
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == head_dump
    _install_all(scratch_dsn)


def test_0003_role_is_dropped_only_when_no_database_references_it(
    scratch_dsn: str,
) -> None:
    """Two databases at head share the one role; downgrading one keeps it,
    downgrading the last drops it, upgrading recreates it."""
    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    other = f"opspilot_mig_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(other)
            )
        )
    other_dsn = make_conninfo(DSN, dbname=other)
    try:
        schema.migrate(other_dsn, pg_dump=PG_DUMP)
        assert _role_exists()
        schema.command.downgrade(schema._config(other_dsn), "0002_state_checks")
        assert _role_exists(), "still referenced by scratch_dsn and the lab"
        assert _observer_grants(scratch_dsn)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(other)
                )
            )


# --- 0004_target_identity (M1-02 step 3, issue #85) ---


IDENTITY_COLUMNS = ("integration_id", "cluster_uid", "namespace", "workload")


def _target_columns(dsn: str) -> dict[str, bool]:
    """column -> is_nullable for opspilot_targets."""
    with psycopg.connect(dsn) as conn:
        return {
            row[0]: row[1] == "YES"
            for row in conn.execute(
                "SELECT column_name,is_nullable FROM information_schema.columns WHERE table_name='opspilot_targets'"
            )
        }


def test_0004_adds_nullable_identity_columns_and_keeps_existing_rows(
    scratch_dsn: str,
) -> None:
    """Intake registers a target by uid alone (M1-01 contract kept by user
    decision 2026-10-07): rows registered before 0004 stay as they are, the
    migration needs no input, and a present value must be non-empty."""
    schema.upgrade_to(scratch_dsn, "0003_observation_store")
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,'checkout-a')",
            (uuid4(),),
        )
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    head_dump = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    columns = _target_columns(scratch_dsn)
    assert all(columns[column] for column in IDENTITY_COLUMNS), columns
    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute(
            "SELECT integration_id,cluster_uid,namespace,workload FROM opspilot_targets WHERE resource_uid='checkout-a'"
        ).fetchone() == (None, None, None, None)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE opspilot_targets SET integration_id='' WHERE resource_uid='checkout-a'"
            )
    # Intake on the new schema still registers by uid alone, idempotently.
    store = DurableStore(scratch_dsn)
    store.install()
    registered = store.register_target("checkout-b")
    assert store.register_target("checkout-b") == registered
    store.close()

    schema.command.downgrade(schema._config(scratch_dsn), "0003_observation_store")
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == "0003_observation_store"
        assert conn.execute("SELECT count(*) FROM opspilot_targets").fetchone() == (2,)
    assert not set(IDENTITY_COLUMNS) & set(_target_columns(scratch_dsn))
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == head_dump


# --- 0005_incident_mode (M1-02 step 3b, issue #121) ---


def test_0005_adds_the_mode_column_with_automatic_default(scratch_dsn: str) -> None:
    schema.upgrade_to(scratch_dsn, "0004_target_identity")
    _seed_run(scratch_dsn)
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    head_dump = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute(
            "SELECT DISTINCT mode FROM opspilot_incidents"
        ).fetchall() == [("automatic",)]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE opspilot_incidents SET mode='robot'")
    # A human_owned incident refuses the downgrade (bot review of PR #123,
    # P1): the column, the row and the revision are untouched.
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute("UPDATE opspilot_incidents SET mode='human_owned'")
    with pytest.raises(schema.HumanOwnershipWouldBeLost) as refused:
        schema.command.downgrade(schema._config(scratch_dsn), "0004_target_identity")
    assert refused.value.count == 1
    with psycopg.connect(scratch_dsn) as conn:
        # 0006 (a data step) downgrades first; the refusal is 0005's, which
        # keeps the column, the row and its own revision
        assert schema.current_revision(conn) == "0005_incident_mode"
        assert conn.execute("SELECT mode FROM opspilot_incidents").fetchall() == [
            ("human_owned",)
        ]
        conn.execute("UPDATE opspilot_incidents SET mode='automatic'")
    # Without a human_owned row the downgrade proceeds.
    schema.command.downgrade(schema._config(scratch_dsn), "0004_target_identity")
    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='opspilot_incidents' AND column_name='mode'"
        ).fetchone() == (0,)
    assert schema.migrate(scratch_dsn, pg_dump=PG_DUMP) == schema.MigrateResult(
        "upgraded", HEAD
    )
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == head_dump


def test_0005_downgrade_waits_for_an_in_flight_takeover_and_then_refuses(
    scratch_dsn: str,
) -> None:
    """Final recheck of PR #123: a takeover that has not committed yet must
    not slip past the count. The downgrade locks the table before counting,
    so it waits for the in-flight transaction and then refuses on the row
    that transaction committed; column, row and revision stay."""
    import threading

    schema.migrate(scratch_dsn, pg_dump=PG_DUMP)
    _seed_run(scratch_dsn)
    outcome: dict[str, object] = {}

    def downgrade() -> None:
        try:
            schema.command.downgrade(
                schema._config(scratch_dsn), "0004_target_identity"
            )
            outcome["result"] = "downgraded"
        except schema.HumanOwnershipWouldBeLost as exc:
            outcome["result"] = exc

    with psycopg.connect(scratch_dsn) as in_flight:
        # The takeover's write, not yet committed (row lock held).
        in_flight.execute("UPDATE opspilot_incidents SET mode='human_owned'")
        worker = threading.Thread(target=downgrade)
        worker.start()
        # The downgrade is blocked on the table lock behind the open write
        # (0008's downgrade, dropping a table that references incidents, is
        # the first statement to wait; 0005's LOCK TABLE would be next).
        deadline = __import__("time").monotonic() + 10
        waiting = False
        while __import__("time").monotonic() < deadline and not waiting:
            with psycopg.connect(scratch_dsn) as probe:
                waiting = probe.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND datname=current_database()"
                ).fetchone() == (1,)
            if not waiting:
                __import__("time").sleep(0.1)
        assert waiting, "the downgrade did not wait for the in-flight takeover"
        assert "result" not in outcome
        in_flight.commit()
    worker.join(timeout=30)
    assert isinstance(outcome.get("result"), schema.HumanOwnershipWouldBeLost)
    assert outcome["result"].count == 1
    with psycopg.connect(scratch_dsn) as conn:
        # 0006 (a data step) downgrades first; the refusal is 0005's, which
        # keeps the column, the row and its own revision
        assert schema.current_revision(conn) == "0005_incident_mode"
        assert conn.execute("SELECT mode FROM opspilot_incidents").fetchall() == [
            ("human_owned",)
        ]
