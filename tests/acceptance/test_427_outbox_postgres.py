"""Independent PostgreSQL acceptance tests for #427's write-side contract."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import catalog_grants, login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

OUTBOX = "opspilot_trace_outbox"
PAYLOAD_SENTINELS = (
    "SENTINEL_PROMPT_427",
    "SENTINEL_REASONING_427",
    "SENTINEL_CREDENTIAL_427",
    "SENTINEL_TOOL_OUTPUT_427",
)


@pytest.fixture
def scratch_dsn():
    name = f"opspilot_427_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn)
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _rows(dsn: str, *, run_id=None):
    where = " WHERE run_id=%s" if run_id else ""
    args = (run_id,) if run_id else ()
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            f"SELECT kind, idempotency_key, payload, state FROM {OUTBOX}{where} ORDER BY entry_id",
            args,
        ).fetchall()


def _assert_payloads_clean(rows):
    from opspilot.persistence.outbox import PAYLOAD_KEYS

    for _, _, payload, _ in rows:
        assert set(payload) <= PAYLOAD_KEYS
        encoded = json.dumps(payload, sort_keys=True, default=str)
        assert not any(sentinel in encoded for sentinel in PAYLOAD_SENTINELS)


def test_run_steps_tools_and_state_changes_are_one_idempotent_row_each(scratch_dsn):
    """Exercise the durable store seams and then inspect the committed outbox."""
    from opspilot.persistence import DurableStore
    from tests.integration.test_m1_loop_resume_postgres import _input

    store = DurableStore(scratch_dsn)
    incident, run = uuid4(), uuid4()
    versions = {"prompt_revision": "p427", "tool_schema_revision": "t427"}
    store.accept(
        incident,
        run,
        f"427-{run}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=20,
        versions=versions,
        input=_input(run),
    )
    lease = store.claim(incident, run, uuid4(), versions, lease_seconds=300)
    step = store.commit_step(
        lease,
        "round-427",
        {
            "tool_calls": [
                {
                    "name": "query",
                    "arguments": {"query": "SENTINEL_PROMPT_427"},
                }
            ],
            "reasoning_content": "SENTINEL_REASONING_427",
            "credential": "SENTINEL_CREDENTIAL_427",
        },
    )
    store.commit_tool(
        lease,
        step,
        0,
        {"raw": "SENTINEL_TOOL_OUTPUT_427", "ok": True},
    )
    # Replaying the same subjects is required to be idempotent at the outbox.
    store.commit_tool(lease, step, 0, {"raw": "SENTINEL_TOOL_OUTPUT_427", "ok": True})
    rows = _rows(scratch_dsn, run_id=run)
    assert [kind for kind, *_ in rows].count("model_step") == 1
    assert [kind for kind, *_ in rows].count("tool_result") == 1
    assert len({key for _, key, *_ in rows}) == len(rows)
    _assert_payloads_clean(rows)


def test_control_and_deadline_sweep_state_changes_emit_run_state_rows(scratch_dsn):
    from opspilot.persistence import DurableStore
    from tests.integration.test_m1_loop_resume_postgres import _input

    store = DurableStore(scratch_dsn)
    incident, run = uuid4(), uuid4()
    versions = {"prompt_revision": "p427"}
    store.accept(
        incident,
        run,
        f"427-control-{run}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=3,
        versions=versions,
        input=_input(run),
    )
    store.control(incident, 0, "pause", "acceptance")
    store.control(incident, 1, "cancel", "acceptance")
    # Deadline sweep is intentionally exercised on a separate run so the
    # control transitions remain individually observable.
    incident2, run2 = uuid4(), uuid4()
    store.accept(
        incident2,
        run2,
        f"427-sweep-{run2}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=3,
        versions=versions,
        input=_input(run2),
    )
    store.claim(incident2, run2, uuid4(), versions, lease_seconds=300)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s",
            (run2,),
        )
    store.sweep_expired_runs(incident_id=incident2)
    rows = _rows(scratch_dsn)
    state_rows = [row for row in rows if row[0] == "run_state"]
    assert len(state_rows) == 3
    assert len({row[1] for row in state_rows}) == len(state_rows)
    _assert_payloads_clean(rows)


def test_role_grants_are_narrow_and_other_tables_match_declaration(scratch_dsn):
    from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE

    with psycopg.connect(scratch_dsn) as conn:
        for role in (WEB_ROLE, WORKER_ROLE, "opspilot_observer", "opspilot_retention"):
            if role in {WEB_ROLE, WORKER_ROLE}:
                assert catalog_grants(conn, role) == RUNTIME_GRANTS[role]
        for role in ("opspilot_observer", "opspilot_retention"):
            for statement in (
                f"SELECT 1 FROM {OUTBOX} LIMIT 1",
                f"INSERT INTO {OUTBOX}(idempotency_key,kind,payload) VALUES ('427-denied-'||random()::text,'run_state','{{}}')",
            ):
                with login(scratch_dsn, role) as role_dsn:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with psycopg.connect(role_dsn) as denied:
                            denied.execute(statement)

    for role in (WEB_ROLE, WORKER_ROLE):
        declared = RUNTIME_GRANTS[role]
        with psycopg.connect(scratch_dsn) as conn:
            actual = catalog_grants(conn, role)
        assert actual[OUTBOX] == declared[OUTBOX]
        assert {k: v for k, v in actual.items() if k != OUTBOX} == {
            k: v for k, v in declared.items() if k != OUTBOX
        }


def test_purge_expired_deletes_terminal_and_disabled_export_pending_only(scratch_dsn):
    """Contract r3-L4: terminal rows age from finalized_at, pending rows from created_at."""
    from opspilot.persistence.outbox import purge_expired

    now = datetime.now(timezone.utc)
    long_ago = now - timedelta(days=30)
    insert = (
        f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,kind,payload,state,created_at,finalized_at) "
        "VALUES (%s,%s,'run_state','{}',%s,%s,%s)"
    )
    incident_id = uuid4()
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            insert,
            (
                "427-old-pending",
                incident_id,
                "pending",
                now - timedelta(days=7),
                None,
            ),
        )
        conn.execute(
            insert,
            (
                "427-young-pending",
                incident_id,
                "pending",
                now - timedelta(days=6, seconds=1),
                None,
            ),
        )
        for state in ("delivered", "dropped"):
            # created long ago in both cases: only finalized_at decides.
            conn.execute(
                insert,
                (
                    f"427-old-{state}",
                    incident_id,
                    state,
                    long_ago,
                    now - timedelta(days=7),
                ),
            )
            conn.execute(
                insert,
                (
                    f"427-young-{state}",
                    incident_id,
                    state,
                    long_ago,
                    now - timedelta(days=6, seconds=1),
                ),
            )
    with psycopg.connect(scratch_dsn) as conn:
        assert purge_expired(conn, now=now, export_enabled=False) == 3
    with psycopg.connect(scratch_dsn) as conn:
        states = dict(conn.execute(f"SELECT idempotency_key,state FROM {OUTBOX}"))
    assert all(
        f"427-old-{state}" not in states
        for state in ("pending", "delivered", "dropped")
    )
    assert all(
        f"427-young-{state}" in states for state in ("pending", "delivered", "dropped")
    )


def test_terminal_state_requires_finalized_at(scratch_dsn):
    """Contract r3-L4: a terminal row without finalized_at, or a pending row with one, is refused."""
    with psycopg.connect(scratch_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,kind,payload,state) "
                "VALUES ('427-terminal-no-final',%s,'run_state','{}','delivered')",
                (uuid4(),),
            )
    with psycopg.connect(scratch_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,kind,payload,state,finalized_at) "
                "VALUES ('427-pending-final',%s,'run_state','{}','pending',clock_timestamp())",
                (uuid4(),),
            )


def test_migration_upgrade_downgrade_upgrade_keeps_business_rows_and_notices_counts(
    scratch_dsn,
):
    incident_id = uuid4()
    with psycopg.connect(scratch_dsn) as conn:
        before = conn.execute("SELECT count(*) FROM opspilot_runs").fetchone()[0]
        for state in ("pending", "delivered", "dropped"):
            conn.execute(
                f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,kind,payload,state,finalized_at) "
                "VALUES (%s,%s,'run_state','{}',%s,CASE WHEN %s='pending' THEN NULL ELSE clock_timestamp() END)",
                (f"427-roundtrip-{state}", incident_id, state, state),
            )
    with psycopg.connect(scratch_dsn) as conn:
        config = Config(attributes={"connection": conn})
        config.set_main_option(
            "script_location", str(schema.__file__.replace("schema.py", "migrations"))
        )
        notices: list[str] = []
        conn.add_notice_handler(
            lambda diagnostic: notices.append(diagnostic.message_primary or "")
        )
        command.downgrade(config, "0023_retention_raw_cleanup")
        assert any(
            "pending" in notice and "delivered" in notice and "dropped" in notice
            for notice in notices
        )
    upgrade_config = Config(attributes={"dsn": scratch_dsn})
    upgrade_config.set_main_option(
        "script_location", str(schema.__file__.replace("schema.py", "migrations"))
    )
    command.upgrade(upgrade_config, "head")
    with psycopg.connect(scratch_dsn) as conn:
        assert (
            conn.execute("SELECT count(*) FROM opspilot_runs").fetchone()[0] == before
        )
        assert conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name=%s",
            (OUTBOX,),
        ).fetchone() == (1,)
