"""#427 implementation tests on PostgreSQL: the trace outbox write side.

A Run driven through the web and worker runtime roles writes one outbox row
per model step, tool result and state change, the payloads hold allowlist
keys only and none of the sentinels the Run carried; web controls and the
web's deadline sweep reach the trigger; a generation attempt writes one
row; grants refuse what the contract refuses; the 7-day cleanup; the 0024
downgrade with rows in all three states.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from psycopg import errors, sql

from opspilot import schema
from opspilot.investigation.loop import ModelReply
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.jobs import GenerationStore
from opspilot.persistence import DurableStore, outbox
from opspilot.persistence.grants import WEB_ROLE, WORKER_ROLE
from opspilot.tools.fixture import FIXTURE_TOOL
from opspilot.web import DurableEventLog, DurableEvidenceStore
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import CountingModel, Harness, output
from tests.integration.test_m1_deadline_sweep_postgres import _expire
from tests.integration.test_m1_web_worker_postgres import _worker
from tests.integration.test_runtime_roles_postgres import (
    _control,
    _submit,
    _workbench,
)
from tests.m1_investigation_support import report_from_transcript, tool_call
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

# Built at run time (gitleaks); none may reach an outbox row.
SENTINEL_KEY = "sk-" + "f00dfeed" * 6
SENTINEL_REASONING = "REASONING-SENTINEL-" + uuid4().hex
SENTINEL_PROMPT = "PROMPT-SENTINEL-" + uuid4().hex


@pytest.fixture(scope="module")
def web_dsn():
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def worker_dsn():
    with login(DSN, WORKER_ROLE) as dsn:
        yield dsn


def _rows(run_id: UUID) -> list[dict]:
    with psycopg.connect(DSN, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(
            "SELECT idempotency_key,kind,state,payload,incident_id,run_id "
            "FROM opspilot_trace_outbox WHERE run_id=%s ORDER BY entry_id",
            (run_id,),
        ).fetchall()


def _tool_round_with_sentinels() -> ModelReply:
    return ModelReply(
        content=f"Calling the metric with {SENTINEL_KEY} {SENTINEL_PROMPT}",
        reasoning_content=SENTINEL_REASONING,
        tool_calls=(tool_call(call_id="call-427", name=FIXTURE_TOOL),),
        finish_reason="tool_calls",
        response_model="deepseek-flash",
        usage={
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
        raw={"id": "resp-427"},
    )


def _state_changes(run_id: UUID) -> int:
    return sum(1 for row in _rows(run_id) if row["kind"] == "run_state")


def test_one_run_writes_one_row_per_step_tool_result_and_state_change(
    web_dsn, worker_dsn
):
    app, _, web_store, _ = _workbench(web_dsn)
    worker_store = DurableStore(worker_dsn)
    worker_store.install()
    events = DurableEventLog(worker_store)
    evidence = DurableEvidenceStore(worker_store)
    subject = _submit(app)
    run_id = UUID(str(web_store.rebuild(subject)["run"]["run_id"]))
    loop, _ = _worker(
        worker_store,
        events,
        evidence,
        [_tool_round_with_sentinels(), report_from_transcript],
        subject=subject,
    )
    ((_, result),) = loop.poll_once()
    assert result.status == "published", result

    rows = _rows(run_id)
    with psycopg.connect(DSN, row_factory=psycopg.rows.dict_row) as conn:
        steps = conn.execute(
            "SELECT sequence,tool_results FROM opspilot_steps "
            "WHERE run_id=%s AND status<>'late_result' ORDER BY sequence",
            (run_id,),
        ).fetchall()
    kinds = Counter(row["kind"] for row in rows)
    # queued -> running -> completed
    assert kinds == {
        "run_state": 2,
        "model_step": len(steps),
        "tool_result": sum(len(s["tool_results"]) for s in steps),
    }
    assert kinds["tool_result"] == 1
    keys = [row["idempotency_key"] for row in rows]
    assert len(set(keys)) == len(keys)
    assert {f"run_state:{run_id}:1", f"run_state:{run_id}:2"} <= set(keys)
    assert {f"model_step:{run_id}:{s['sequence']}" for s in steps} <= set(keys)
    assert f"tool_result:{run_id}:{steps[0]['sequence']}:call-427" in keys
    states = [r["payload"]["run_state"] for r in rows if r["kind"] == "run_state"]
    assert states == ["running", "completed"]

    text = json.dumps([row["payload"] for row in rows])
    for sentinel in (
        SENTINEL_KEY,
        SENTINEL_REASONING,
        SENTINEL_PROMPT,
        "reasoning_content",
        "Why is checkout erroring",
        "checkout-prod",
        "rate(http_errors",
    ):
        assert sentinel not in text, sentinel
    for row in rows:
        assert set(row["payload"]) <= outbox.PAYLOAD_KEYS
        assert row["state"] == "pending" and row["payload"]["span_kind"] == row["kind"]
        assert row["incident_id"] == subject
    first_step = next(r for r in rows if r["kind"] == "model_step")["payload"]
    assert first_step["input_tokens"] == 120
    assert first_step["output_tokens"] == 30
    assert first_step["total_tokens"] == 150
    assert first_step["reasoning_tokens"] == 12
    assert first_step["finish_reason"] == "tool_calls"
    assert first_step["model"] == "deepseek-flash"
    assert first_step["prompt_revision"]
    tool = next(r for r in rows if r["kind"] == "tool_result")["payload"]
    assert tool["tool_status_class"] == "ok"
    assert tool["tool_name"]

    # web controls on another Run: pause and resume (no state change), cancel
    other = _submit(app)
    other_run = UUID(str(web_store.rebuild(other)["run"]["run_id"]))
    generation = _control(app, other, "pause", 0)
    generation = _control(app, other, "resume", generation)
    _control(app, other, "cancel", generation)
    assert [
        r["payload"]["run_state"] for r in _rows(other_run) if r["kind"] == "run_state"
    ] == ["cancelled"]  # pause/resume of a queued Run leave it queued
    assert web_store.rebuild(other)["run"]["state"] == "cancelled"

    # the web's deadline sweep
    swept = _submit(app)
    swept_run = UUID(str(web_store.rebuild(swept)["run"]["run_id"]))
    _expire(DurableStore(DSN), swept_run)
    assert web_store.sweep_expired_runs(incident_id=swept)
    assert _state_changes(swept_run) == 1


def test_a_no_op_state_update_writes_nothing_and_the_probe_skips_taken_keys(
    web_dsn,
):
    app, _, web_store, _ = _workbench(web_dsn)
    subject = _submit(app)
    run_id = UUID(str(web_store.rebuild(subject)["run"]["run_id"]))
    with psycopg.connect(web_dsn) as conn:
        conn.execute("UPDATE opspilot_runs SET state=state WHERE run_id=%s", (run_id,))
    assert _state_changes(run_id) == 0
    with psycopg.connect(DSN) as conn:
        # a key already taken (e.g. a later ordinal left after a cleanup)
        conn.execute(
            "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,run_id,kind,payload) "
            "VALUES(%s,%s,%s,'run_state','{}')",
            (f"run_state:{run_id}:1", subject, run_id),
        )
    with psycopg.connect(web_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET state='paused' WHERE run_id=%s", (run_id,)
        )
    keys = [r["idempotency_key"] for r in _rows(run_id)]
    assert keys == [f"run_state:{run_id}:1", f"run_state:{run_id}:2"]


def test_a_repeated_entry_adds_nothing_and_a_rollback_takes_the_row_with_it():
    incident, run = uuid4(), uuid4()
    key = f"model_step:{run}:0"
    with psycopg.connect(DSN) as conn:
        for _ in range(2):
            outbox.insert_entry(
                conn,
                kind="model_step",
                idempotency_key=key,
                incident_id=incident,
                run_id=run,
                fields={"run_id": run, "input_tokens": 3},
            )
    assert len(_rows(run)) == 1
    other = uuid4()
    with psycopg.connect(DSN) as conn:
        outbox.insert_entry(
            conn,
            kind="model_step",
            idempotency_key=f"model_step:{other}:0",
            incident_id=incident,
            run_id=other,
            fields={},
        )
        conn.rollback()
    assert _rows(other) == []


@pytest.fixture
def harness(web_dsn, worker_dsn):
    app, _, web_store, _ = _workbench(web_dsn)
    knowledge = KnowledgeStore(worker_dsn)
    jobs = GenerationStore(worker_dsn)
    events = DurableEventLog(DurableStore(worker_dsn))
    try:
        yield Harness(app, knowledge, jobs, web_store, DSN, events)
    finally:
        jobs.close()
        knowledge.close()


def test_a_generation_attempt_writes_one_row(harness):
    incident, ev, _, _ = harness.seed()
    harness.generate(incident, CountingModel(output(ev)))
    with psycopg.connect(DSN, row_factory=psycopg.rows.dict_row) as conn:
        attempts = conn.execute(
            "SELECT attempt_id FROM opspilot_postmortem_generation_attempts WHERE incident_id=%s",
            (incident,),
        ).fetchall()
        rows = conn.execute(
            "SELECT idempotency_key,run_id,payload FROM opspilot_trace_outbox "
            "WHERE incident_id=%s AND kind='generation'",
            (incident,),
        ).fetchall()
    assert len(attempts) == 1 and len(rows) == 1
    (row,) = rows
    assert row["idempotency_key"] == f"generation:{attempts[0]['attempt_id']}"
    assert row["run_id"] is None
    assert set(row["payload"]) <= outbox.PAYLOAD_KEYS
    assert row["payload"]["attempt_id"] == str(attempts[0]["attempt_id"])


def _refused(dsn: str, statement: str, *args) -> None:
    with psycopg.connect(dsn) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement, args)


INSERT = (
    "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,run_id,kind,payload) "
    "VALUES(%s,%s,%s,'model_step','{}')"
)


def test_grants_refuse_what_the_contract_refuses(web_dsn, worker_dsn):
    for statement in (
        "SELECT 1 FROM opspilot_trace_outbox",
        "UPDATE opspilot_trace_outbox SET state='dropped'",
        "DELETE FROM opspilot_trace_outbox",
    ):
        _refused(web_dsn, statement)
    _refused(worker_dsn, "UPDATE opspilot_trace_outbox SET payload='{}'")
    _refused(
        worker_dsn,
        "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,kind,payload,state) "
        "VALUES('k',gen_random_uuid(),'generation','{}','delivered')",
    )
    for group in ("opspilot_observer", "opspilot_retention"):
        with login(DSN, group) as dsn:
            _refused(dsn, "SELECT 1 FROM opspilot_trace_outbox")
            _refused(dsn, INSERT, f"k-{uuid4()}", uuid4(), uuid4())
    # the worker's own paths
    run = uuid4()
    with psycopg.connect(worker_dsn) as conn:
        conn.execute(INSERT, (f"model_step:{run}:0", uuid4(), run))
        conn.execute(
            "UPDATE opspilot_trace_outbox SET attempts=1,state='dropped',"
            "drop_reason='REJECTED',next_attempt_at=NULL WHERE run_id=%s",
            (run,),
        )
        assert (
            conn.execute(
                "DELETE FROM opspilot_trace_outbox WHERE run_id=%s", (run,)
            ).rowcount
            == 1
        )


def test_purge_deletes_rows_seven_days_old_and_keeps_younger_ones():
    incident = uuid4()
    runs = {}
    with psycopg.connect(DSN) as conn:
        for state in ("pending", "delivered", "dropped"):
            for age in (8, 6):
                run = uuid4()
                runs[(state, age)] = run
                conn.execute(
                    "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,run_id,"
                    "kind,payload,state,created_at) VALUES(%s,%s,%s,'model_step','{}',%s,"
                    "clock_timestamp() - make_interval(days=>%s))",
                    (f"model_step:{run}:0", incident, run, state, age),
                )
        now = conn.execute("SELECT clock_timestamp()").fetchone()[0]

        def alive() -> set[tuple[str, int]]:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT run_id FROM opspilot_trace_outbox WHERE incident_id=%s",
                    (incident,),
                )
            }
            return {k for k, run in runs.items() if run in present}

        # export on (PR-2): only final rows go; inside a savepoint we undo
        with conn.transaction(force_rollback=True):
            outbox.purge_expired(conn, now=now, export_enabled=True)
            assert alive() == set(runs) - {("delivered", 8), ("dropped", 8)}
        outbox.purge_expired(conn, now=now)
        assert alive() == {(s, 6) for s in ("pending", "delivered", "dropped")}
        # exactly at the boundary is deleted
        outbox.purge_expired(
            conn, now=now + timedelta(days=1), retention=timedelta(days=7)
        )
        assert alive() == set()


def test_the_store_purges_on_the_database_clock(worker_dsn):
    run = uuid4()
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,run_id,kind,"
            "payload,created_at) VALUES(%s,%s,%s,'model_step','{}',"
            "clock_timestamp() - interval '8 days')",
            (f"model_step:{run}:0", uuid4(), run),
        )
    store = outbox.OutboxStore(worker_dsn)
    try:
        assert store.purge() >= 1
    finally:
        store.close()
    assert _rows(run) == []


BUSINESS = ("opspilot_incidents", "opspilot_runs", "opspilot_steps")


def test_downgrade_reports_three_counts_drops_the_outbox_and_keeps_business_rows(
    caplog,
):
    name = f"opspilot_427_{uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scratch = psycopg.conninfo.make_conninfo(DSN, dbname=name)
    try:
        schema.upgrade_head(scratch)
        with psycopg.connect(scratch) as conn:
            incident, run = uuid4(), uuid4()
            conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,state,control_generation,intake_key) "
                "VALUES(%s,'investigating',0,%s)",
                (incident, uuid4().hex),
            )
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,"
                "budget_limit,deadline,versions) VALUES(%s,%s,'queued',0,4,"
                "clock_timestamp()+interval '1 hour','{}')",
                (run, incident),
            )
            conn.execute(
                "UPDATE opspilot_runs SET state='running' WHERE run_id=%s", (run,)
            )
            for i, state in enumerate(("delivered", "dropped", "dropped")):
                conn.execute(
                    "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,run_id,"
                    "kind,payload,state) VALUES(%s,%s,%s,'model_step','{}',%s)",
                    (f"model_step:{run}:{i}", incident, run, state),
                )
            before = {
                t: conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
                ).fetchone()[0]
                for t in BUSINESS
            }
        with caplog.at_level(logging.WARNING, logger="opspilot.migrations"):
            command.downgrade(schema._config(scratch), "0023_retention_raw_cleanup")
        assert (
            "opspilot_trace_outbox dropped by downgrade: pending=1, delivered=1, dropped=2"
            in caplog.text
        )
        with psycopg.connect(scratch) as conn:
            assert schema.current_revision(conn) == "0023_retention_raw_cleanup"
            assert (
                conn.execute("SELECT to_regclass('opspilot_trace_outbox')").fetchone()[
                    0
                ]
                is None
            )
            assert not conn.execute(
                "SELECT 1 FROM pg_proc WHERE proname='opspilot_trace_outbox_run_state'"
            ).fetchall()
            after = {
                t: conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
                ).fetchone()[0]
                for t in BUSINESS
            }
            assert after == before
            # the trigger is gone: a state change still works
            conn.execute(
                "UPDATE opspilot_runs SET state='completed' WHERE run_id=%s", (run,)
            )
        schema.upgrade_head(scratch)
        with psycopg.connect(scratch) as conn:
            assert (
                conn.execute("SELECT count(*) FROM opspilot_trace_outbox").fetchone()[0]
                == 0
            )
    finally:
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
