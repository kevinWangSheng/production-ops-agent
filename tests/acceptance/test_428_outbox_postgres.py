"""PostgreSQL acceptance checks for #428 claim and terminal-state semantics."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture
def scratch_dsn():
    name = f"opspilot_428_{uuid4().hex[:12]}"
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


def _insert_pending(dsn: str, key: str, *, next_attempt_at=None):
    run_id = uuid4()
    if next_attempt_at is None:
        next_attempt_at = datetime.now(UTC)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            INSERT INTO opspilot_trace_outbox
                (idempotency_key, incident_id, run_id, kind, payload, state, next_attempt_at)
            VALUES (%s, %s, %s, 'model_step', %s::jsonb, 'pending', %s)
            """,
            (key, uuid4(), run_id, '{"span_kind":"model_step"}', next_attempt_at),
        )
    return run_id


def _state(dsn: str, key: str):
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT state, attempts, drop_reason FROM opspilot_trace_outbox "
            "WHERE idempotency_key=%s",
            (key,),
        ).fetchone()


def test_two_concurrent_deliverers_post_a_claimed_row_once(scratch_dsn):
    """FOR UPDATE SKIP LOCKED plus the 30s lease prevents duplicate POSTs."""
    from opspilot import outbox_delivery
    from opspilot.persistence.outbox import OutboxStore

    _insert_pending(scratch_dsn, "428-concurrent-once")
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def transport(_url, _body, _headers, _timeout):
        calls.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return 204

    config = outbox_delivery.ExportConfig(
        "http://127.0.0.1/otel/v1/traces", "k", "opspilot-lab-428"
    )

    def deliver():
        outbox_delivery.OutboxDelivery(
            OutboxStore(scratch_dsn), config, transport=transport
        ).deliver_once()

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    threads[0].start()
    assert entered.wait(timeout=5)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert len(calls) == 1
    with psycopg.connect(scratch_dsn) as conn:
        state, attempts = conn.execute(
            "SELECT state, attempts FROM opspilot_trace_outbox "
            "WHERE idempotency_key='428-concurrent-once'"
        ).fetchone()
    assert state == "delivered"
    assert attempts == 1


@pytest.mark.parametrize("status", [400, 408, 429, 500, 503])
def test_rejection_and_transient_failures_have_terminal_contract(scratch_dsn, status):
    from opspilot import outbox_delivery
    from opspilot.persistence.outbox import OutboxStore

    key = f"428-status-{status}"
    now = [datetime(2026, 11, 1, tzinfo=UTC)]
    _insert_pending(scratch_dsn, key, next_attempt_at=now[0])
    calls = []

    def transport(_url, _body, _headers, _timeout):
        calls.append(1)
        return status

    exporter = outbox_delivery.OutboxDelivery(
        OutboxStore(scratch_dsn),
        outbox_delivery.ExportConfig(
            "http://127.0.0.1/otel/v1/traces", "k", "opspilot-lab-428"
        ),
        clock=lambda: now[0],
        transport=transport,
    )
    if status == 400:
        result = exporter.deliver_once()
        assert result.dropped == {"REJECTED": 1}
        assert _state(scratch_dsn, key) == ("dropped", 1, "REJECTED")
        return

    for failures in range(1, 9):
        result = exporter.deliver_once()
        if failures < 8:
            assert result.retried == 1
            assert _state(scratch_dsn, key)[0] == "pending"
            now[0] = now[0].replace(microsecond=0) + timedelta(seconds=601)
        else:
            assert result.dropped == {"MAX_ATTEMPTS": 1}
            assert _state(scratch_dsn, key) == ("dropped", 8, "MAX_ATTEMPTS")
    assert len(calls) == 8


def test_backlog_drops_oldest_as_backlog_before_claiming(scratch_dsn):
    from opspilot import outbox_delivery
    from opspilot.persistence.outbox import OutboxStore

    with psycopg.connect(scratch_dsn) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO opspilot_trace_outbox "
                "(idempotency_key, incident_id, kind, payload, state, created_at) "
                "VALUES (%s,%s,'generation','{}','pending',clock_timestamp())",
                [(f"428-backlog-{i:05d}", uuid4()) for i in range(20_001)],
            )
    exporter = outbox_delivery.OutboxDelivery(
        OutboxStore(scratch_dsn),
        outbox_delivery.ExportConfig(
            "http://127.0.0.1/otel/v1/traces", "k", "opspilot-lab-428"
        ),
        transport=lambda *_args: 204,
    )
    result = exporter.deliver_once()
    assert result.dropped.get("BACKLOG") == 1
    assert _state(scratch_dsn, "428-backlog-00000")[0] == "dropped"
    assert _state(scratch_dsn, "428-backlog-00000")[2] == "BACKLOG"


def test_connection_timeout_retries_then_drops_after_eighth_attempt(scratch_dsn):
    from opspilot import outbox_delivery
    from opspilot.persistence.outbox import OutboxStore

    key = "428-timeout"
    now = [datetime(2026, 11, 1, tzinfo=UTC)]
    _insert_pending(scratch_dsn, key, next_attempt_at=now[0])
    calls = []

    def timeout(_url, _body, _headers, _timeout):
        calls.append(1)
        raise OSError("timeout")

    exporter = outbox_delivery.OutboxDelivery(
        OutboxStore(scratch_dsn),
        outbox_delivery.ExportConfig(
            "http://127.0.0.1/otel/v1/traces", "k", "opspilot-lab-428"
        ),
        clock=lambda: now[0],
        transport=timeout,
    )
    for attempt in range(8):
        result = exporter.deliver_once()
        now[0] += timedelta(seconds=601)
        if attempt < 7:
            assert result.retried == 1
        else:
            assert result.dropped == {"MAX_ATTEMPTS": 1}
    assert len(calls) == 8
    assert _state(scratch_dsn, key) == ("dropped", 8, "MAX_ATTEMPTS")


def test_unreachable_outbox_endpoint_preserves_scripted_run_observables(
    scratch_dsn, monkeypatch
):
    """OB-R6: delivery failure cannot change the Run's terminal handoff/budget."""
    from opspilot import outbox_delivery, tracing
    from opspilot.investigation.loop import ModelError
    from opspilot.persistence.outbox import OutboxStore
    from opspilot.web.events import DurableEventLog
    from tests.integration import test_m1_loop_resume_postgres as scripted

    # The existing public Harness is the end-to-end DurableStore + Worker +
    # InvestigationRunner scripted-model path.  It reads this module global
    # DSN; redirect that seam to this test's scratch database without copying
    # or reimplementing the harness.
    monkeypatch.setattr(scripted, "DSN", scratch_dsn)

    def run_case(mode: str):
        monkeypatch.setenv("OPSPILOT_TRACE", mode)
        monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
        monkeypatch.setenv("LANGSMITH_PROJECT", "opspilot-lab-428")
        monkeypatch.setenv("LANGSMITH_API_KEY", "key-428")
        tracing.configure(dict(os.environ))
        harness = scripted.Harness(budget_limit=4, model_requests=4)
        events = DurableEventLog(harness.store)
        events.install()
        runner = harness.runner(
            [ModelError("MODEL_UNAVAILABLE"), ModelError("MODEL_UNAVAILABLE")]
        )
        runner.events = events
        outcome = runner.resume(harness.incident)
        if mode == "outbox":
            config = outbox_delivery.ExportConfig.from_env(dict(os.environ))
            assert config is not None
            config = replace(config, endpoint_url="http://127.0.0.1:9/otel/v1/traces")
            delivery = outbox_delivery.OutboxDelivery(
                OutboxStore(scratch_dsn), config, transport=None
            )
            delivery.deliver_once()
        rows = harness.rows()
        usage = harness.store.run_usage(harness.run)
        handoffs = tuple(
            (event.kind, dict(event.payload))
            for event in events.read_after(harness.incident, 0, limit=100)
            if event.kind == "run_handoff"
        )
        with psycopg.connect(scratch_dsn) as conn:
            outbox_rows = conn.execute(
                "SELECT state, drop_reason FROM opspilot_trace_outbox "
                "WHERE run_id=%s ORDER BY entry_id",
                (harness.run,),
            ).fetchall()
        return {
            "status": rows["run"]["state"],
            "reason": outcome.reason,
            "conclusion": rows["steps"][-1]["response"].get("conclusion"),
            "handoffs": handoffs,
            "budget_spent": usage,
            "outbox": outbox_rows,
        }

    off = run_case("off")
    outbox = run_case("outbox")
    assert outbox["status"] == off["status"] == "waiting_human"
    assert outbox["reason"] == off["reason"] == "MODEL_UNAVAILABLE"
    assert outbox["conclusion"] == off["conclusion"]
    assert outbox["handoffs"] == off["handoffs"]
    assert outbox["budget_spent"] == off["budget_spent"]
    assert outbox["outbox"]
    assert all(state in {"pending", "dropped"} for state, _ in outbox["outbox"])
    assert all(state != "delivered" for state, _ in outbox["outbox"])
