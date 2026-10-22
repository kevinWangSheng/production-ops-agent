"""F11 PR-4a implementer PG chain: anomaly ending -> Observer comparison.

Reuses PR-2b's harness (test-only calibration, non-owner web and Observer
logins) to reach a real anomaly ending, then drives the Observer loop with a
synthetic before-window source answering at ``released_at`` only.
"""

import os
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.observation.release_comparison import (
    COMPARISON_MAX_ATTEMPTS,
    ReleaseComparisonEvidence,
    before_window,
    build_release_comparison_plan,
)
from opspilot.observation.release_policy import canonical_json, sha256
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import PrometheusReadOnlySource, VectorResult
from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.release_sampling import (
    take_release_comparison,
    take_release_sample,
)
from scripts.m0.postgres_lab import DSN
from tests.integration import test_f11_pr2b_impl_postgres as pr2b
from tests.integration.test_f11_pr2b_impl_postgres import (
    DIGEST,
    Telemetry,
    authorized_due,
)
from tests.runtime_roles_support import login
from tests.test_f11_pr2b_impl import Opener

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
release_harness = pr2b.release_harness  # PR-2b's calibrated release fixture
OLD_UID = UUID("87654321-4321-4321-4321-210987654321")
OLD_RS = "checkout-aaa"
OLD_POD = "checkout-aaa-pod"


def _value(context, name):
    value = (
        0.1
        if name == "request_rate_per_second"
        else 0.0
        if name in ("error_ratio", "dependency_error_ratio")
        else 100.0
        if name == "latency_p95_milliseconds"
        else 1.0
    )
    signal = context.health_profile.signal(name)
    if signal.healthy.min is not None and value < signal.healthy.min:
        value = signal.healthy.min
    return value


class BeforeTelemetry:
    """Answers the comparison plan for the old ReplicaSet at ``released_at``."""

    def __init__(self, context, *, failing=0, on_call=None):
        self.context = context
        self.failing = failing
        self.on_call = on_call
        self.calls = []

    def instant(self, *args, **kwargs):
        pytest.fail("release telemetry must use vector")

    def instant_vector(self, expr, *, at, timeout_seconds):
        assert at == self.context.release.released_at
        plan = build_release_comparison_plan(self.context, instance_uids=(OLD_UID,))
        key = next(k for k, v in plan.items() if v == expr)
        self.calls.append((key, at))
        if self.on_call:
            self.on_call(len(self.calls))
        if self.failing:
            self.failing -= 1
            return VectorResult(
                expr=expr,
                status="failed",
                series=(),
                body=b"",
                http_status=503,
                detail="HTTP 503",
                body_complete=True,
                sent_at=at,
            )
        parts = key.split(":")
        labels, value = {}, 1.0
        if parts[1] == "mapping":
            name = parts[2]
            labels = {"namespace": "otel-demo"}
            if name == "replicaset_owner":
                labels.update(
                    replicaset=OLD_RS, owner_kind="Deployment", owner_name="checkout"
                )
            else:
                labels.update(uid=str(OLD_UID), pod=OLD_POD)
                if name == "pod_owner":
                    labels.update(owner_kind="ReplicaSet", owner_name=OLD_RS)
                else:
                    labels.update(container="app", image_id="repo@" + DIGEST)
            kind = parts[3]
        else:
            name, kind = parts[1], parts[2]
            if kind == "instances":
                labels = {"service_instance_id": str(OLD_UID)}
            elif kind == "query":
                value = _value(self.context, name)
        if kind == "coverage":
            value = 4.0
        if kind == "freshness":
            value = at.timestamp()
        raw = canonical_json(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": labels, "value": [at.timestamp(), str(value)]}
                    ],
                },
            }
        ).encode()
        return PrometheusReadOnlySource(
            "http://synthetic.example", opener=Opener(raw), clock=lambda: at
        ).instant_vector(expr, at=at, timeout_seconds=timeout_seconds)


def _claim(observer, sid):
    # Other modules may leave many due sessions in a shared database.
    leases = observer.claim_due_samples(uuid4(), limit=500, lease_seconds=120)
    return next(lease for lease in leases if lease.session_id == sid)


def _anomalous(h):
    web, observer, oid, _, _ = h
    auth = authorized_due(h)
    lease = _claim(observer, auth.session_id)
    context = observer.release_context(lease.session_id)
    taken = take_release_sample(lease, Telemetry(context, degraded=True), observer)
    assert taken.receipt.release_status == "anomalous"
    # Endings left pending by earlier tests in this database would be taken
    # first by the oldest-first loop; file them as zero-query records.
    for other in observer.pending_comparisons(limit=100):
        if other.observation_id != oid:
            observer.submit_comparison(
                other,
                ReleaseComparisonEvidence(
                    format_version=1,
                    comparison_id=other.comparison_id,
                    attempts=1,
                    window=before_window(observer.comparison_context(other)),
                    queries=(),
                ),
            )
    return context


def _comparisons(observer, oid):
    return observer.release_comparisons(oid)


def _history(observer, oid):
    return {
        **observer.release_history(oid),
        "comparisons": observer.release_comparisons(oid),
    }


def test_anomaly_collects_one_comparison_at_released_at_and_replays(release_harness):
    web, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    before = web.get_release(oid)
    source = BeforeTelemetry(context)
    ObserverLoop(store=observer, source=source, batch=1).poll_once()
    assert len(source.calls) == 49
    assert {at for _, at in source.calls} == {before.released_at}
    (stored,) = _comparisons(observer, oid)
    assert stored["status"] == "complete" and stored["attempts"] == 1
    assert stored["window_before_end"] == before.released_at
    assert len(stored["queries"]) == 49
    assert {q["evaluated_at"] for q in stored["queries"]} == {before.released_at}
    old = [s for s in stored["comparison"]["signals"] if s["scope"] == "before_old"]
    assert len(old) == 4 and all(s["value"] is not None for s in old)
    assert stored["comparison"]["before_instances"][0]["pod_uid"] == str(OLD_UID)
    # A5: the comparison changes nothing about the release.
    assert web.get_release(oid) == before
    replay = replay_release_history(_history(observer, oid))
    assert replay.consistent, replay.integrity
    # Nothing left to collect: the next pass sends no query.
    ObserverLoop(store=observer, source=source, batch=1).poll_once()
    assert len(source.calls) == 49 and observer.pending_comparisons(limit=20) == ()


def test_resubmission_returns_the_first_record(release_harness):
    _, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    (lease,) = [
        lease
        for lease in observer.pending_comparisons(limit=20)
        if lease.observation_id == oid
    ]
    first = take_release_comparison(lease, BeforeTelemetry(context), observer)
    assert first.receipt is not None and not first.receipt.replayed
    again = observer.submit_comparison(
        lease,
        ReleaseComparisonEvidence(
            format_version=1,
            comparison_id=lease.comparison_id,
            attempts=1,
            window=first.receipt.comparison.window_before,
            queries=(),
        ),
    )
    assert again.replayed and again.comparison == first.receipt.comparison
    assert len(_comparisons(observer, oid)) == 1


@pytest.mark.parametrize(
    "failing,attempts,status,calls",
    [(33, 2, "complete", 33 + 49), (10_000, 3, "unavailable", 33 * 3)],
)
def test_failed_attempts_retry_up_to_three(
    release_harness, failing, attempts, status, calls
):
    _, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    source = BeforeTelemetry(context, failing=failing)
    ObserverLoop(store=observer, source=source, batch=1).poll_once()
    (stored,) = _comparisons(observer, oid)
    assert stored["attempts"] == attempts <= COMPARISON_MAX_ATTEMPTS
    assert stored["status"] == status and len(source.calls) == calls
    assert replay_release_history(_history(observer, oid)).consistent


def _suspend(target_id, suspended):
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_target_suspensions SET suspended=%s,generation=generation+1 WHERE target_id=%s",
            (suspended, target_id),
        )


def test_suspended_scope_is_not_queried_until_lifted(release_harness):
    web, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    target = web.get_release(oid).target_id
    _suspend(target, True)
    try:
        source = BeforeTelemetry(context)
        ObserverLoop(store=observer, source=source, batch=1).poll_once()
        assert source.calls == [] and _comparisons(observer, oid) == []
        assert all(
            lease.observation_id != oid
            for lease in observer.pending_comparisons(limit=20)
        )
    finally:
        _suspend(target, False)
    ObserverLoop(store=observer, source=source, batch=1).poll_once()
    assert len(source.calls) == 49 and len(_comparisons(observer, oid)) == 1


def test_suspension_during_collection_submits_nothing(release_harness):
    web, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    target = web.get_release(oid).target_id
    (lease,) = [
        lease
        for lease in observer.pending_comparisons(limit=20)
        if lease.observation_id == oid
    ]

    def suspend(n):
        if n == 5:
            _suspend(target, True)

    try:
        taken = take_release_comparison(
            lease, BeforeTelemetry(context, on_call=suspend), observer
        )
    finally:
        _suspend(target, False)
    assert taken.receipt is None and taken.scope_interrupted
    assert taken.requests_issued == 5 and _comparisons(observer, oid) == []


def test_replay_detects_tampered_comparison_evidence(release_harness):
    _, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    ObserverLoop(store=observer, source=BeforeTelemetry(context), batch=1).poll_once()
    (stored,) = _comparisons(observer, oid)
    raw = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE opspilot_release_comparison_queries SET raw=%s,raw_sha256=%s WHERE comparison_id=%s AND query_key='before_service:error_ratio:query'",
            (raw, sha256(raw), stored["comparison_id"]),
        )
    replay = replay_release_history(_history(observer, oid))
    assert not replay.consistent and "COMPARISON_MISMATCH" in replay.integrity
    # The store's own replay reads the comparison with the session history.
    (session,) = observer.release_history(oid)["sessions"]
    replay = observer.replay_session(session["session_id"])
    assert not replay.consistent and "COMPARISON_MISMATCH" in replay.integrity


def test_comparison_tables_are_append_only_and_role_scoped(release_harness):
    web, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    ObserverLoop(store=observer, source=BeforeTelemetry(context), batch=1).poll_once()
    (stored,) = _comparisons(observer, oid)
    with login(DSN, "opspilot_observer") as dsn:
        observer_dsn = dsn
        for statement in (
            "UPDATE opspilot_release_comparisons SET status='partial'",
            "DELETE FROM opspilot_release_comparison_queries",
        ):
            with psycopg.connect(observer_dsn) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        # the insert guard binds a row to an anomaly ending of that release
        with psycopg.connect(observer_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO opspilot_release_comparisons(comparison_id,observation_id,ending_id,session_id,sample_id,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison) SELECT %s,observation_id,ending_id,session_id,sample_id,1,status,attempts,window_before_start,window_before_end - interval '1 second',window_after_start,window_after_end,decision_at,evidence_sha256,jsonb_set(comparison,'{comparison_id}',to_jsonb(%s::text)) FROM opspilot_release_comparisons WHERE comparison_id=%s",
                    (uuid4(), str(uuid4()), stored["comparison_id"]),
                )
    with login(DSN, "opspilot_web") as dsn, psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_comparisons WHERE observation_id=%s",
            (oid,),
        ).fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO opspilot_release_comparison_queries(comparison_id) VALUES(%s)",
                (stored["comparison_id"],),
            )
    with login(DSN, "opspilot_worker") as dsn, psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM opspilot_release_comparisons")


def test_healthy_and_observing_releases_have_nothing_pending(release_harness):
    _, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = _claim(observer, auth.session_id)
    take_release_sample(
        lease, Telemetry(observer.release_context(lease.session_id)), observer
    )
    assert all(
        lease.observation_id != oid for lease in observer.pending_comparisons(limit=20)
    )


@pytest.fixture
def scratch():
    from psycopg.conninfo import make_conninfo

    from tests.integration.test_f2_c_impl_correlation_postgres import (
        _create_db,
        _drop_db,
    )

    name = _create_db("f11_pr4a_mig")
    try:
        yield make_conninfo(DSN, dbname=name)
    finally:
        _drop_db(name)


def test_0019_downgrade_restores_0018_and_refuses_with_a_comparison(scratch):
    from opspilot import schema

    pg_dump = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
    schema.migrate(scratch, pg_dump=pg_dump)
    head = schema.schema_dump(scratch, pg_dump=pg_dump)
    schema.command.downgrade(schema._config(scratch), "0018_alert_correlation")
    assert schema.schema_dump(scratch, pg_dump=pg_dump) == schema.fresh_dump(
        scratch, "0018_alert_correlation", pg_dump=pg_dump
    )
    schema.migrate(scratch, pg_dump=pg_dump)
    assert schema.schema_dump(scratch, pg_dump=pg_dump) == head
    with psycopg.connect(scratch) as conn:
        # a synthetic row; replica mode skips the guard and foreign keys
        conn.execute("SET LOCAL session_replication_role=replica")
        cid = uuid4()
        conn.execute(
            "INSERT INTO opspilot_release_comparisons(comparison_id,observation_id,ending_id,session_id,sample_id,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison) VALUES(%s,%s,%s,%s,%s,1,'unavailable',1,now()-interval '5 minutes',now()-interval '1 minute',now()-interval '1 minute',now(),now(),repeat('0',64),jsonb_build_object('status','unavailable','comparison_id',%s::text,'ending_id',%s::text,'observation_id',%s::text,'sample_id',%s::text,'attempts',1))",
            (cid, cid, cid, cid, cid, str(cid), str(cid), str(cid), str(cid)),
        )
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        schema.command.downgrade(schema._config(scratch), "0018_alert_correlation")
    with psycopg.connect(scratch) as conn:
        assert schema.current_revision(conn) == "0019_release_comparisons"
