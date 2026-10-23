"""F11 PR-4a implementer PG chain: anomaly ending -> Observer comparison.

Reuses PR-2b's harness (test-only calibration, non-owner web and Observer
logins) to reach a real anomaly ending, then drives the Observer loop with a
synthetic before-window source answering at ``released_at`` only.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from opspilot.observation.release_comparison import (
    COMPARISON_MAX_ATTEMPTS,
    ReleaseComparisonEvidence,
    before_window,
    build_release_comparison_plan,
)
from opspilot.observation.release_policy import canonical_json, sha256
from opspilot.observation.releases import ReleaseEvent
from opspilot.observer.health_profile import canonical_content
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import PrometheusReadOnlySource, VectorResult
from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.release_sampling import (
    take_release_comparison,
    take_release_sample,
)
from opspilot.persistence.base import PersistenceError
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
        plan = build_release_comparison_plan(
            self.context,
            released_at=at,
            after_window_end=at,
            instance_uids=(OLD_UID,),
        )
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


DRAIN_OWNER = UUID("00000000-0000-4000-8000-00000000d1a1")


def _drain(observer):
    """File endings earlier tests left claimable as zero-query records, so
    the oldest-first claim reaches this test's ending."""
    for _ in range(200):
        lease = observer.claim_comparison(DRAIN_OWNER)
        if lease is None:
            return
        observer.submit_comparison(
            lease,
            ReleaseComparisonEvidence(
                format_version=1,
                comparison_id=lease.comparison_id,
                attempts=lease.attempt,
                window=before_window(observer.comparison_context(lease)),
                queries=(),
            ),
        )
    pytest.fail("could not drain pending comparisons")


def _anomalous(h):
    web, observer, oid, _, _ = h
    _drain(observer)
    auth = authorized_due(h)
    lease = _claim(observer, auth.session_id)
    context = observer.release_context(lease.session_id)
    taken = take_release_sample(lease, Telemetry(context, degraded=True), observer)
    assert taken.receipt.release_status == "anomalous"
    return context


def _own_claim(observer, oid, owner=None):
    lease = observer.claim_comparison(owner or uuid4())
    assert lease is not None and lease.observation_id == oid, lease
    return lease


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
    assert len(source.calls) == 49
    assert all(p.observation_id != oid for p in observer.pending_comparisons(limit=100))


def test_resubmission_returns_the_first_record(release_harness):
    _, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    lease = _own_claim(observer, oid)
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
    lease = _own_claim(observer, oid)

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


def _expire_attempts(cid):
    # Test-only: age the claim leases instead of waiting 150 s.
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE opspilot_release_comparison_attempts SET lease_until=clock_timestamp()-interval '1 second' WHERE comparison_id=%s",
            (cid,),
        )


def test_attempts_are_durable_and_bounded_across_observers_and_crashes(
    release_harness,
):
    web, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    first, second = uuid4(), uuid4()
    lease = _own_claim(observer, oid, first)
    assert (lease.attempt, lease.exhausted) == (1, False)
    # another Observer cannot take the ending while the claim is live
    assert observer.claim_comparison(second) is None
    # three crashes: each claimed attempt is spent, whoever claimed it
    for attempt in (2, 3):
        _expire_attempts(lease.comparison_id)
        lease = _own_claim(observer, oid, second if attempt == 2 else first)
        assert lease.attempt == attempt
    # r5: a live third attempt is not over, not even for its own owner
    assert observer.claim_comparison(first) is None
    assert observer.claim_comparison(uuid4()) is None
    forged = lease.model_copy(update={"exhausted": True})
    with pytest.raises(PersistenceError, match="COMPARISON_LEASE_LOST"):
        observer.submit_comparison(
            forged.model_copy(update={"owner": uuid4()}),
            ReleaseComparisonEvidence(
                format_version=1,
                comparison_id=lease.comparison_id,
                attempts=3,
                window=before_window(context),
                queries=(),
            ),
        )
    _expire_attempts(lease.comparison_id)
    final = _own_claim(observer, oid, uuid4())
    assert final.exhausted and final.attempt == 3
    source = BeforeTelemetry(context)
    taken = take_release_comparison(final, source, observer)
    assert source.calls == [] and taken.receipt is not None
    assert taken.receipt.comparison.status == "unavailable"
    (stored,) = _comparisons(observer, oid)
    assert stored["attempts"] == 3
    assert replay_release_history(_history(observer, oid)).consistent
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_comparison_attempts WHERE comparison_id=%s",
            (lease.comparison_id,),
        ).fetchone() == (3,)
    with login(DSN, "opspilot_observer") as dsn, psycopg.connect(dsn) as conn:
        # a fourth attempt: refused by the guard (a record exists) or CHECK
        with pytest.raises(
            (psycopg.errors.CheckViolation, psycopg.errors.InsufficientPrivilege)
        ):
            conn.execute(
                "INSERT INTO opspilot_release_comparison_attempts(comparison_id,attempt,observation_id,ending_id,owner) VALUES(%s,4,%s,%s,%s)",
                (lease.comparison_id, oid, lease.ending_id, uuid4()),
            )


def test_guards_refuse_a_wrong_derived_id_or_before_window(release_harness):
    _, observer, oid, _, _ = release_harness
    _anomalous(release_harness)
    lease = _own_claim(observer, oid)
    with login(DSN, "opspilot_observer") as dsn:
        with (
            psycopg.connect(dsn) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(
                "INSERT INTO opspilot_release_comparison_attempts(comparison_id,attempt,observation_id,ending_id,owner) VALUES(%s,1,%s,%s,%s)",
                (uuid4(), oid, lease.ending_id, lease.owner),
            )
        good_start = lease.released_at - timedelta(
            seconds=observer.comparison_context(
                lease
            ).health_profile.evaluation_window_seconds
        )
        for cid, start in (
            (uuid4(), good_start),
            (lease.comparison_id, good_start - timedelta(seconds=1)),
        ):
            comparison = {
                "status": "unavailable",
                "comparison_id": str(cid),
                "ending_id": str(lease.ending_id),
                "observation_id": str(oid),
                "sample_id": str(lease.sample_id),
                "attempts": 1,
            }
            with (
                psycopg.connect(dsn) as conn,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
            ):
                conn.execute(
                    "INSERT INTO opspilot_release_comparisons(comparison_id,observation_id,ending_id,session_id,sample_id,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison) VALUES(%s,%s,%s,%s,%s,1,'unavailable',1,%s,%s,%s,%s,clock_timestamp(),repeat('0',64),%s)",
                    (
                        cid,
                        oid,
                        lease.ending_id,
                        lease.session_id,
                        lease.sample_id,
                        start,
                        lease.released_at,
                        lease.window_after.start,
                        lease.window_after.end,
                        Jsonb(comparison),
                    ),
                )
    assert _comparisons(observer, oid) == []


def _record_insert(conn, lease, *, owner, attempts):
    start = lease.released_at - timedelta(seconds=2)  # fixture window W = 2 s
    cid = lease.comparison_id
    comparison = {
        "status": "unavailable",
        "comparison_id": str(cid),
        "ending_id": str(lease.ending_id),
        "observation_id": str(lease.observation_id),
        "sample_id": str(lease.sample_id),
        "attempts": attempts,
    }
    conn.execute(
        "INSERT INTO opspilot_release_comparisons(comparison_id,observation_id,ending_id,session_id,sample_id,owner,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison) VALUES(%s,%s,%s,%s,%s,%s,1,'unavailable',%s,%s,%s,%s,%s,clock_timestamp(),repeat('0',64),%s)",
        (
            cid,
            lease.observation_id,
            lease.ending_id,
            lease.session_id,
            lease.sample_id,
            owner,
            attempts,
            start,
            lease.released_at,
            lease.window_after.start,
            lease.window_after.end,
            Jsonb(comparison),
        ),
    )


def test_only_the_live_holder_of_the_latest_attempt_files_the_record(
    release_harness,
):
    _, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    a, b = uuid4(), uuid4()
    lease_a = _own_claim(observer, oid, a)
    empty = ReleaseComparisonEvidence(
        format_version=1,
        comparison_id=lease_a.comparison_id,
        attempts=1,
        window=before_window(context),
        queries=(),
    )
    # A's lease lapses: A may no longer file, even before anyone else claims
    _expire_attempts(lease_a.comparison_id)
    with pytest.raises(PersistenceError, match="COMPARISON_LEASE_LOST"):
        observer.submit_comparison(lease_a, empty)
    lease_b = _own_claim(observer, oid, b)
    assert lease_b.attempt == 2
    with pytest.raises(PersistenceError, match="COMPARISON_LEASE_LOST"):
        observer.submit_comparison(lease_a, empty)
    # the guard enforces the same fence on a direct Observer-role insert
    with login(DSN, "opspilot_observer") as dsn:
        with (
            psycopg.connect(dsn) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege, match="lease refused"),
        ):
            _record_insert(conn, lease_a, owner=a, attempts=1)
        with (
            psycopg.connect(dsn) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege, match="lease refused"),
        ):
            _record_insert(conn, lease_b, owner=a, attempts=2)
    assert _comparisons(observer, oid) == []
    taken = take_release_comparison(lease_b, BeforeTelemetry(context), observer)
    assert taken.receipt is not None and not taken.receipt.replayed
    (stored,) = _comparisons(observer, oid)
    assert (stored["attempts"], stored["owner"], stored["status"]) == (
        2,
        b,
        "complete",
    )
    assert replay_release_history(_history(observer, oid)).consistent


def test_a_record_and_a_takeover_claim_cannot_interleave(release_harness):
    """The r4 race, step by step: A files under a live lease in an open
    transaction; the lease lapses; B claims the next attempt. B must wait
    for A's transaction and is then refused, leaving A's record alone."""
    _, observer, oid, _, _ = release_harness
    _anomalous(release_harness)
    a, b = uuid4(), uuid4()
    lease = _own_claim(observer, oid, a)
    with login(DSN, "opspilot_observer") as dsn:
        first = psycopg.connect(dsn)
        try:
            _record_insert(first, lease, owner=a, attempts=1)  # open transaction
            _expire_attempts(lease.comparison_id)
            outcome = {}

            def takeover():
                try:
                    with psycopg.connect(dsn) as conn:
                        conn.execute(
                            "INSERT INTO opspilot_release_comparison_attempts(comparison_id,attempt,observation_id,ending_id,owner) VALUES(%s,2,%s,%s,%s)",
                            (lease.comparison_id, oid, lease.ending_id, b),
                        )
                    outcome["result"] = "claimed"
                except psycopg.errors.InsufficientPrivilege:
                    outcome["result"] = "refused"

            second = threading.Thread(target=takeover)
            second.start()
            second.join(1.0)
            assert second.is_alive(), outcome  # blocked on the comparison lock
            first.commit()
            second.join(10)
            assert outcome == {"result": "refused"}
        finally:
            first.close()
    (stored,) = _comparisons(observer, oid)
    assert (stored["attempts"], stored["owner"]) == (1, a)
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_comparison_attempts WHERE comparison_id=%s",
            (lease.comparison_id,),
        ).fetchone() == (1,)


class _Blocking:
    """Comparison queries: the third stops the loop; with ``hang`` every
    query blocks until ``released`` (a source that does not return)."""

    def __init__(self, inner, loop_stop, *, hang=False):
        self.inner, self.loop_stop, self.hang = inner, loop_stop, hang
        self.released = threading.Event()
        self.calls = 0

    def instant(self, *args, **kwargs):
        pytest.fail("release telemetry must use vector")

    def instant_vector(self, expr, *, at, timeout_seconds):
        if at != self.inner.context.release.released_at:
            raise RuntimeError("only comparison queries are answered")
        self.calls += 1
        if self.hang:
            self.released.wait(30)
        elif self.calls == 3:
            self.loop_stop.set()
        return self.inner.instant_vector(expr, at=at, timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("hang", [False, True])
def test_stop_during_a_comparison_exits_bounded_without_submitting(
    release_harness, hang
):
    _, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    loop = ObserverLoop(
        store=observer,
        source=None,
        batch=1,
        poll_seconds=0.1,
        comparison_join_seconds=1.0,
    )
    source = _Blocking(BeforeTelemetry(context), loop.stop, hang=hang)
    loop.source = source
    runner = threading.Thread(target=loop.run, daemon=True)
    started = time.monotonic()
    runner.start()
    try:
        if hang:
            deadline = time.monotonic() + 5
            while source.calls == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert source.calls == 1
            loop.stop.set()
        runner.join(10)
        assert not runner.is_alive()
        # a stop with a query still in flight waits at most the join bound
        assert time.monotonic() - started < (8 if hang else 5)
    finally:
        source.released.set()
    time.sleep(0.3)  # let a released query return and observe the stop
    assert _comparisons(observer, oid) == []
    assert source.calls == (1 if hang else 3)
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_comparison_attempts WHERE observation_id=%s",
            (oid,),
        ).fetchone() == (1,)


class _SlowComparisons:
    """Comparison queries take 0.5 s each until ``released``; sampling
    queries of every other release are answered at once."""

    def __init__(self, comparison, sampling):
        self.comparison, self.sampling = comparison, sampling
        self.released = threading.Event()

    def instant(self, *args, **kwargs):
        pytest.fail("release telemetry must use vector")

    def instant_vector(self, expr, *, at, timeout_seconds):
        if at == self.comparison.context.release.released_at:
            if not self.released.is_set():
                self.released.wait(0.5)
            return self.comparison.instant_vector(
                expr, at=at, timeout_seconds=timeout_seconds
            )
        return self.sampling.instant_vector(
            expr, at=at, timeout_seconds=timeout_seconds
        )


def test_a_slow_comparison_does_not_delay_due_samples(release_harness):
    web, observer, oid, policy, catalog = release_harness
    context = _anomalous(release_harness)
    record = web.get_release(oid)
    receipt = web.record_release(
        event=ReleaseEvent(
            format_version=1,
            source="pr4a-cadence-" + uuid4().hex,
            source_release_id=uuid4().hex,
            released_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            before=record.before_revision,
            after=record.after_revision,
            auxiliary=record.auxiliary,
        ),
        target_id=record.target_id,
        identity=record.target_snapshot,
        health_profile_revision=record.health_profile_revision,
        health_profile=canonical_content(context.health_profile),
        min_tracking_seconds=record.min_tracking_seconds,
        actor="tester",
        raw_body=b"{}",
    )
    other = receipt.observation_id
    auth = authorized_due((web, observer, other, policy, catalog))
    source = _SlowComparisons(
        BeforeTelemetry(context), Telemetry(observer.release_context(auth.session_id))
    )
    loop = ObserverLoop(store=observer, source=source, batch=5, poll_seconds=0.2)
    runner = threading.Thread(target=loop.run, daemon=True)
    runner.start()
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if web.get_release(other).used_sample_count >= 3:
                break
            time.sleep(0.1)
        used = web.get_release(other).used_sample_count
        # sampling kept its 1 s cadence while the comparison (49 x 0.5 s)
        # was still collecting on its own thread
        assert used >= 3 or web.get_release(other).status == "healthy", used
        assert _comparisons(observer, oid) == []
        assert source.comparison.calls
    finally:
        source.released.set()
        loop.stop.set()
        runner.join(30)
    assert not runner.is_alive()
    # the stop abandoned the in-flight attempt unsubmitted (r4)
    assert _comparisons(observer, oid) == []
    if web.get_release(other).status == "observing":
        web.control_release(
            other,
            action="cancel",
            actor="cleanup",
            expected_control_generation=web.get_release(other).control_generation,
            idempotency_key="cleanup",
            calibrated_policy_revision=None,
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


def test_0020_downgrade_restores_0019_and_refuses_with_a_comparison(scratch):
    from opspilot import schema

    pg_dump = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
    schema.migrate(scratch, pg_dump=pg_dump)
    head = schema.schema_dump(scratch, pg_dump=pg_dump)
    schema.command.downgrade(schema._config(scratch), "0019_incident_priority")
    assert schema.schema_dump(scratch, pg_dump=pg_dump) == schema.fresh_dump(
        scratch, "0019_incident_priority", pg_dump=pg_dump
    )
    schema.migrate(scratch, pg_dump=pg_dump)
    assert schema.schema_dump(scratch, pg_dump=pg_dump) == head
    with psycopg.connect(scratch) as conn:
        # a synthetic row; replica mode skips the guard and foreign keys
        conn.execute("SET LOCAL session_replication_role=replica")
        cid = uuid4()
        conn.execute(
            "INSERT INTO opspilot_release_comparisons(comparison_id,observation_id,ending_id,session_id,sample_id,owner,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison) VALUES(%s,%s,%s,%s,%s,%s,1,'unavailable',1,now()-interval '5 minutes',now()-interval '1 minute',now()-interval '1 minute',now(),now(),repeat('0',64),jsonb_build_object('status','unavailable','comparison_id',%s::text,'ending_id',%s::text,'observation_id',%s::text,'sample_id',%s::text,'attempts',1))",
            (cid, cid, cid, cid, cid, cid, str(cid), str(cid), str(cid), str(cid)),
        )
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        schema.command.downgrade(schema._config(scratch), "0019_incident_priority")
    with psycopg.connect(scratch) as conn:
        assert schema.current_revision(conn) == "0020_release_comparisons"
