"""#428 delivery against PostgreSQL as the worker role, with a loopback fake
OTLP endpoint: settlement by status, backoff and the attempt cap, backlog,
unserializable rows, stable ids on redelivery, no double claim, the purge
split between running and fail-closed delivery, and the metrics snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import health, schema
from opspilot.outbox_delivery import (
    BACKLOG_LIMIT,
    MAX_ATTEMPTS,
    ExportConfig,
    OutboxDelivery,
    root_span_id_for,
    span_id_for,
)
from opspilot.persistence.outbox import OutboxMaintainer, OutboxStore
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

OUTBOX = "opspilot_trace_outbox"
T0 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
KEY = "lsv2_" + "pt_" + "feedc0de" * 4  # built at run time (gitleaks)


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
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture
def worker_dsn(scratch_dsn):
    with login(scratch_dsn, "opspilot_worker") as dsn:
        yield dsn


class Endpoint:
    """A loopback OTLP endpoint: answers ``statuses`` in turn (last repeats)."""

    def __init__(self, *statuses: int):
        self.statuses = list(statuses) or [200]
        self.bodies: list[bytes] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                endpoint.bodies.append(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                status = (
                    endpoint.statuses.pop(0)
                    if len(endpoint.statuses) > 1
                    else endpoint.statuses[0]
                )
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/otel/v1/traces"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def spans(self, index: int = -1):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        request = ExportTraceServiceRequest()
        request.ParseFromString(self.bodies[index])
        return [
            span
            for rs in request.resource_spans
            for ss in rs.scope_spans
            for span in ss.spans
        ]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def endpoint():
    created: list[Endpoint] = []

    def make(*statuses: int) -> Endpoint:
        created.append(Endpoint(*statuses))
        return created[-1]

    yield make
    for item in created:
        item.close()


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def _delivery(dsn, url, clock=None, stats=None):
    config = ExportConfig(url, KEY, "opspilot-lab-428")
    return OutboxDelivery(OutboxStore(dsn), config, clock=clock or Clock(), stats=stats)


def _insert(
    dsn, *, kind="model_step", run_id=None, payload=None, key=None, created_at=None
):
    run_id = None if kind == "generation" else (run_id or uuid4())
    key = key or f"{kind}:{run_id or uuid4()}:{uuid4().hex[:6]}"
    payload = payload if payload is not None else {"span_kind": kind, "input_tokens": 3}
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,run_id,kind,payload,created_at) "
            "VALUES(%s,%s,%s,%s,%s,coalesce(%s,clock_timestamp())) RETURNING entry_id",
            (key, uuid4(), run_id, kind, json.dumps(payload), created_at),
        ).fetchone()[0]


def _state(dsn, entry_id):
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            f"SELECT state, drop_reason, attempts, next_attempt_at, finalized_at FROM {OUTBOX} WHERE entry_id=%s",
            (entry_id,),
        ).fetchone()


def test_2xx_delivers_with_contract_ids_and_a_root_span_on_stop(
    scratch_dsn, worker_dsn, endpoint
):
    fake = endpoint(200)
    run_id = uuid4()
    step = _insert(scratch_dsn, run_id=run_id, key=f"model_step:{run_id}:1")
    stop = _insert(
        scratch_dsn,
        kind="run_state",
        run_id=run_id,
        key=f"run_state:{run_id}:2",
        payload={"run_state": "completed", "ended_at": "2026-11-01T03:05:00+00:00"},
    )
    stats = health.OutboxStats(("ENABLED",))
    result = _delivery(worker_dsn, fake.url, stats=stats).deliver_once()
    assert (result.claimed, result.delivered, result.status) == (2, 2, 200)
    for entry in (step, stop):
        state, reason, attempts, _, finalized = _state(scratch_dsn, entry)
        assert (state, reason, attempts) == ("delivered", None, 1) and finalized == T0
    spans = fake.spans()
    trace = hashlib.sha256(str(run_id).encode()).digest()[:16]
    assert {span.trace_id for span in spans} == {trace}
    ids = {int.from_bytes(span.span_id, "big") for span in spans}
    assert ids == {
        span_id_for(f"model_step:{run_id}:1"),
        span_id_for(f"run_state:{run_id}:2"),
        root_span_id_for(run_id),
    }
    snapshot = stats.snapshot()
    assert snapshot["pending"] == 0 and snapshot["last_delivery_age"] is not None


def test_5xx_backs_off_and_the_eighth_failure_drops_max_attempts(
    scratch_dsn, worker_dsn, endpoint
):
    fake = endpoint(503)
    entry = _insert(scratch_dsn)
    clock = Clock()
    stats = health.OutboxStats(("ENABLED",))
    delivery = _delivery(worker_dsn, fake.url, clock, stats)
    delays = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = delivery.deliver_once()
        assert result.claimed == 1, attempt
        state, reason, attempts, due, _ = _state(scratch_dsn, entry)
        assert attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert state == "pending"
            delays.append((due - clock.now).total_seconds())
            assert delivery.deliver_once().claimed == 0  # not due yet
            clock.now = due
    assert delays == [5, 10, 20, 40, 80, 160, 320]
    assert (state, reason) == ("dropped", "MAX_ATTEMPTS")
    assert stats.snapshot()["dropped"]["MAX_ATTEMPTS"] == 1
    assert len(fake.bodies) == MAX_ATTEMPTS


def test_redelivery_carries_the_same_trace_and_span_ids(
    scratch_dsn, worker_dsn, endpoint
):
    fake = endpoint(500, 200)
    entry = _insert(scratch_dsn)
    clock = Clock()
    delivery = _delivery(worker_dsn, fake.url, clock)
    delivery.deliver_once()
    clock.now += timedelta(seconds=5)
    delivery.deliver_once()
    assert _state(scratch_dsn, entry)[0] == "delivered"
    first, second = fake.spans(0), fake.spans(1)
    assert [(s.trace_id, s.span_id) for s in first] == [
        (s.trace_id, s.span_id) for s in second
    ]


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(400, "REJECTED"), (401, "REJECTED"), (408, None), (429, None)],
)
def test_4xx_rejects_except_408_and_429_which_retry(
    scratch_dsn, worker_dsn, endpoint, status, outcome
):
    fake = endpoint(status)
    entry = _insert(scratch_dsn)
    result = _delivery(worker_dsn, fake.url).deliver_once()
    state, reason, *_ = _state(scratch_dsn, entry)
    if outcome:
        assert (state, reason, result.dropped) == ("dropped", outcome, {outcome: 1})
    else:
        assert (state, reason, result.retried) == ("pending", None, 1)


def test_an_unreachable_endpoint_retries(scratch_dsn, worker_dsn):
    entry = _insert(scratch_dsn)
    result = _delivery(worker_dsn, "http://127.0.0.1:9/otel/v1/traces").deliver_once()
    assert result.status is None and result.retried == 1
    assert _state(scratch_dsn, entry)[0] == "pending"


def test_an_unserializable_row_is_dropped_and_the_rest_are_sent(
    scratch_dsn, worker_dsn, endpoint
):
    fake = endpoint(200)
    bad = _insert(scratch_dsn, payload={"ended_at": "not a time"})
    good = _insert(scratch_dsn)
    stats = health.OutboxStats(("ENABLED",))
    _delivery(worker_dsn, fake.url, stats=stats).deliver_once()
    assert _state(scratch_dsn, bad)[:2] == ("dropped", "UNSERIALIZABLE")
    assert _state(scratch_dsn, good)[0] == "delivered"
    assert stats.snapshot()["dropped"]["UNSERIALIZABLE"] == 1
    assert len(fake.spans()) == 1


def test_a_backlog_over_the_limit_drops_the_oldest(scratch_dsn, worker_dsn, endpoint):
    fake = endpoint(503)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,run_id,kind,payload,created_at) "
            "SELECT 'model_step:' || g, gen_random_uuid(), gen_random_uuid(), 'model_step', '{}', "
            "timestamptz '2026-11-01 00:00+00' + g * interval '1 second' "
            "FROM generate_series(1, %s) g",
            (BACKLOG_LIMIT + 3,),
        )
    stats = health.OutboxStats(("ENABLED",))
    result = _delivery(worker_dsn, fake.url, stats=stats).deliver_once()
    assert result.dropped.get("BACKLOG") == 3
    with psycopg.connect(scratch_dsn) as conn:
        dropped = conn.execute(
            f"SELECT idempotency_key FROM {OUTBOX} WHERE drop_reason='BACKLOG' ORDER BY entry_id"
        ).fetchall()
        pending = conn.execute(
            f"SELECT count(*) FROM {OUTBOX} WHERE state='pending'"
        ).fetchone()[0]
    assert [row[0] for row in dropped] == [
        "model_step:1",
        "model_step:2",
        "model_step:3",
    ]
    assert pending == BACKLOG_LIMIT
    assert stats.snapshot()["dropped"]["BACKLOG"] == 3
    assert stats.snapshot()["pending"] == BACKLOG_LIMIT


def test_two_workers_never_send_the_same_row(scratch_dsn, worker_dsn, endpoint):
    fake = endpoint(200)
    for _ in range(150):
        _insert(scratch_dsn)
    gate = threading.Barrier(2)
    sent: list[bytes] = []
    lock = threading.Lock()

    def slow(url, body, headers, timeout):
        gate.wait(timeout=10)
        with lock:
            sent.append(body)
        return 200

    deliveries = [
        OutboxDelivery(
            OutboxStore(worker_dsn),
            ExportConfig(fake.url, KEY, "opspilot-lab-428"),
            transport=slow,
        )
        for _ in range(2)
    ]
    threads = [threading.Thread(target=d.deliver_once) for d in deliveries]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    ids = []
    for body in sent:
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        ids += [
            s.span_id
            for rs in request.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]
    assert len(sent) == 2
    assert len(ids) == len(set(ids)) == 150
    with psycopg.connect(scratch_dsn) as conn:
        assert (
            conn.execute(
                f"SELECT count(*) FROM {OUTBOX} WHERE state='delivered'"
            ).fetchone()[0]
            == 150
        )


def test_purge_keeps_old_pending_rows_only_while_delivery_runs(
    scratch_dsn, worker_dsn, endpoint
):
    old = datetime.now(UTC) - timedelta(days=8)
    entry = _insert(scratch_dsn, created_at=old)
    store = OutboxStore(worker_dsn)
    fake = endpoint(503)
    running = OutboxMaintainer(
        store, stop=threading.Event(), delivery=_delivery(worker_dsn, fake.url)
    )
    assert running.run_once() == 0
    assert _state(scratch_dsn, entry)[0] == "pending"
    stats = health.OutboxStats(("LANGSMITH_KEY_MISSING",))
    closed = OutboxMaintainer(store, stop=threading.Event(), stats=stats)
    assert closed.run_once() == 1
    closed.refresh_pending()
    assert stats.snapshot()["pending"] == 0
    assert fake.bodies == []
