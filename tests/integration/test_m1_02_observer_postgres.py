"""The Observer loop end to end on PostgreSQL (M1-02 step 4, #86).

The loop runs under the ``opspilot_observer`` login against a Prometheus
stand-in (an HTTP server in this process answering ``/api/v1/query`` with
scripted vectors), so the whole path is real except the telemetry source:
claim under lease, instant queries at the window end, scope check before
each request, verdict, atomic submission, lifecycle. Covers F6's four
outcomes at the product boundary: sustained health -> ``resolved``; stopped
scrapes -> stale samples that never confirm; a suspension during a sample ->
no further request, the session ends; continued degradation -> stays
``observing_recovery`` until the budget hands back to ``open``.
"""

import json
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.domain.intake import Target
from opspilot.observation import ObservationStore
from opspilot.observer import PROFILE_DIRECTORY, HealthProfile, load_health_profile
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.persistence import DurableStore, PoolConfig
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=3, timeout=5.0)
SHIPPED = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
CANONICAL = json.dumps(
    SHIPPED.model_dump(mode="json"),
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
# every signal's value when the lab is healthy (within the shipped bounds)
HEALTHY_VALUES = {
    "deployment_available_replicas": 1.0,
    "request_rate_per_second": 0.0125,
    "error_ratio": 0.0,
    "latency_p95_milliseconds": 120.0,
    "pods_running": 1.0,
    "pod_restarts_in_window": 0.0,
    "dependency_deployments_available": 8.0,
    "dependency_error_ratio": 0.0,
}


class Telemetry:
    """Scripted Prometheus: ``values`` by signal, raw point count, and how
    long ago the newest raw sample was taken (``age_seconds``)."""

    def __init__(self) -> None:
        self.values = dict(HEALTHY_VALUES)
        self.count = 5
        self.age_seconds = 20.0
        # per-signal override of the freshness answer (the oldest newest
        # sample among the signal's series), in seconds before ``at``
        self.age_by_signal: dict[str, float] = {}
        self.requests: list[tuple[str, float]] = []
        self.lock = threading.Lock()
        self.on_request: list[object] = []

    def answer(self, expr: str, at: float) -> dict[str, object]:
        with self.lock:
            self.requests.append((expr, at))
            for hook in list(self.on_request):
                hook(len(self.requests))  # type: ignore[operator]
            signal = next(
                s
                for s in SHIPPED.signals
                if expr in (s.query, s.coverage_query, s.freshness_query)
            )
            if expr == signal.query:
                value = self.values.get(signal.name)
            elif expr == signal.coverage_query:
                value = self.count
            else:
                value = at - self.age_by_signal.get(signal.name, self.age_seconds)
            if value is None:
                return {
                    "status": "success",
                    "data": {"resultType": "vector", "result": []},
                }
            return {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [at, str(value)]}],
                },
            }


@pytest.fixture(scope="module")
def telemetry() -> Iterator[tuple[Telemetry, str]]:
    state = Telemetry()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parts = urlsplit(self.path)
            if parts.path != "/api/v1/query":
                self.send_error(404)
                return
            query = parse_qs(parts.query)
            body = json.dumps(
                state.answer(query["query"][0], float(query["time"][0]))
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_observer_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    schema.migrate(dsn, pg_dump=PG_DUMP)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def observer_dsn(scratch_dsn: str) -> Iterator[str]:
    login = f"opspilot_observer_login_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
    try:
        yield make_conninfo(scratch_dsn, user=login)
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.fixture(scope="module")
def owner(scratch_dsn: str) -> Iterator[DurableStore]:
    store = DurableStore(scratch_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def controller(scratch_dsn: str) -> Iterator[ObservationStore]:
    store = ObservationStore(scratch_dsn, pool=POOL)
    yield store
    store.close()


@pytest.fixture(scope="module")
def observer(observer_dsn: str) -> Iterator[ObservationStore]:
    store = ObservationStore(observer_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture
def loop(
    observer: ObservationStore, telemetry: tuple[Telemetry, str]
) -> Iterator[tuple[ObserverLoop, Telemetry]]:
    state, url = telemetry
    state.values = dict(HEALTHY_VALUES)
    state.count, state.age_seconds = 5, 20.0
    state.age_by_signal.clear()
    state.requests.clear()
    state.on_request.clear()
    yield ObserverLoop(store=observer, source=PrometheusReadOnlySource(url)), state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _incident(owner: DurableStore) -> tuple[object, object, Target]:
    incident, run = uuid4(), uuid4()
    uid = f"deployment/checkout-{uuid4().hex[:8]}"
    target_id = owner.register_target(uid)
    owner.accept(
        incident,
        run,
        f"obs-{incident}",
        deadline=_now() + timedelta(hours=1),
        budget_limit=10,
        versions={"v": "1"},
        target_id=target_id,
    )
    target = Target(
        integration_id="prom-lab",
        cluster_uid="kind-lab",
        namespace="otel-demo",
        resource_uid=uid,
        revision="r1",
    )
    return incident, target_id, target


def _authorize(
    controller: ObservationStore,
    incident: object,
    target: Target,
    *,
    sustained: int = 1,
    max_samples: int = 40,
    profile: HealthProfile | None = SHIPPED,
) -> object:
    """A session bound to the shipped checkout profile; the first job is due
    now, every later one is made due by ``_due_now``."""
    return controller.authorize_session(
        incident,  # type: ignore[arg-type]
        target=target,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=max_samples,
        sample_interval_seconds=60,
        sustained_window_seconds=sustained,
        health_profile_revision=None if profile is None else profile.revision,
        health_profile=None if profile is None else CANONICAL,
        first_sample_due_at=_now(),
    )


def _due_now(owner: DurableStore, session: object) -> None:
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (session,),
        )


def _backdate_authorization(owner: DurableStore, session: object) -> None:
    """Data from before the authorization never confirms health (PR #114
    P1-2); the tests sample right after authorizing, so the window would
    start before it. Move the authorization an hour back, as a human
    handling an hour ago would have."""
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET authorized_at=authorized_at-interval '1 hour' WHERE session_id=%s",
            (session,),
        )


def _lifecycle(owner: DurableStore, incident: object) -> str:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return str(row["lifecycle"])


def _only(results: list, session: object):
    mine = [receipt for sid, receipt in results if sid == session]
    assert len(mine) == 1, results
    return mine[0]


def test_healthy_lab_sustained_over_the_window_resolves_the_incident(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """F6 step 1 at the product boundary: every required signal queried with
    the shipped profile, fresh and within bounds, over the sustained window
    -> ``recovery_confirmed``, incident ``resolved``, replay consistent."""
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)

    receipt = _only(observer_loop.poll_once(), session)

    assert receipt.accepted and receipt.confirms_health
    assert receipt.transition == "recovery_confirmed"
    assert _lifecycle(owner, incident) == "resolved"
    history = controller.session_history(session)
    assert history["session"]["state"] == "completed"
    sample = history["samples"][0]
    assert sample["outcome"] == "healthy" and sample["required_signals_present"]
    readings = {row["signal_name"]: row for row in sample["readings"]}
    assert set(readings) == {s.name for s in SHIPPED.signals}
    for signal in SHIPPED.signals:
        row = readings[signal.name]
        assert row["status"] == "ok" and row["query"] == signal.query
        assert row["source"] == "prometheus" and row["sample_count"] == 5
        assert row["raw"] is not None and row["raw_sha256"] is not None
        bundle = json.loads(bytes(row["raw"]))
        assert bundle["freshness"]["expr"] == signal.freshness_query
    # three instant queries per signal, all at the window end
    assert len(state.requests) == 3 * len(SHIPPED.signals)
    assert len({at for _, at in state.requests}) == 1
    assert abs(sample["window_end"].timestamp() - state.requests[0][1]) < 0.01
    assert controller.replay_session(session).consistent


def test_stopped_scrapes_are_stale_samples_that_never_confirm(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """Issue #86: values and counts still answer, the newest raw sample is
    old -> every reading stale, the sample adopted as unknown, no health,
    the session continues bounded (next job scheduled)."""
    observer_loop, state = loop
    state.age_seconds = SHIPPED.freshness_seconds + 30
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)

    receipt = _only(observer_loop.poll_once(), session)

    assert receipt.accepted and not receipt.confirms_health
    assert receipt.health_basis == "outcome_not_healthy"
    assert receipt.transition is None and receipt.next_sample_due_at is not None
    assert _lifecycle(owner, incident) == "observing_recovery"
    sample = controller.session_history(session)["samples"][0]
    assert (sample["outcome"], sample["required_signals_present"]) == ("stale", False)
    assert {row["status"] for row in sample["readings"]} == {"stale"}
    assert all(
        row["value"] is None and row["raw"] is not None for row in sample["readings"]
    )
    # fresh scrapes again: the streak starts from here, nothing before counts
    state.age_seconds = 20.0
    _due_now(owner, session)
    receipt = _only(observer_loop.poll_once(), session)
    assert receipt.confirms_health and receipt.transition == "recovery_confirmed"
    assert controller.replay_session(session).consistent


def test_one_stale_dependency_among_eight_blocks_health_and_the_streak(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """PR #119 review P2-1: the dependency signal's freshness is the oldest
    of the eight dependencies' newest samples; one at 120 s makes the
    reading stale, the sample unknown, and the healthy streak restarts."""
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    state.age_by_signal["dependency_deployments_available"] = 120.0

    receipt = _only(observer_loop.poll_once(), session)

    assert receipt.accepted and not receipt.confirms_health
    sample = controller.session_history(session)["samples"][0]
    assert (sample["outcome"], sample["required_signals_present"]) == ("stale", False)
    rows = {row["signal_name"]: row for row in sample["readings"]}
    assert rows["dependency_deployments_available"]["status"] == "stale"
    assert sum(1 for row in rows.values() if row["status"] == "ok") == 7
    assert _lifecycle(owner, incident) == "observing_recovery"
    assert controller.session(session)["healthy_since"] is None


def test_withdrawn_traffic_is_no_data_even_with_zero_errors(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """F6 step 2 at the boundary: traffic below the gate, error ratio 0."""
    observer_loop, state = loop
    state.values["request_rate_per_second"] = 0.0
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)

    receipt = _only(observer_loop.poll_once(), session)

    assert receipt.accepted and not receipt.confirms_health
    sample = controller.session_history(session)["samples"][0]
    assert sample["outcome"] == "no_data" and sample["required_signals_present"]
    assert _lifecycle(owner, incident) == "observing_recovery"


def test_a_missing_required_signal_is_unknown_and_the_budget_hands_back_to_open(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """F6 step 3: a required signal with no series -> no_data, adopted but
    never healthy; after ``max_samples`` the session expires and the
    incident returns to ``open``."""
    observer_loop, state = loop
    state.values["pods_running"] = None  # type: ignore[assignment]
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1, max_samples=2)
    _backdate_authorization(owner, session)

    first = _only(observer_loop.poll_once(), session)
    assert first.accepted and not first.confirms_health and first.transition is None
    _due_now(owner, session)
    second = _only(observer_loop.poll_once(), session)

    assert second.transition == "observation_ended_unconfirmed"
    assert _lifecycle(owner, incident) == "open"
    history = controller.session_history(session)
    assert (history["session"]["state"], history["session"]["ended_reason"]) == (
        "expired",
        "max_samples_exhausted",
    )
    for sample in history["samples"]:
        assert (sample["outcome"], sample["required_signals_present"]) == (
            "no_data",
            False,
        )
        pods = next(r for r in sample["readings"] if r["signal_name"] == "pods_running")
        assert pods["status"] == "no_data"
    assert controller.replay_session(session).consistent


def test_continued_degradation_stays_observing_without_any_actuation(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """F6 step 4: a dependency keeps failing -> degraded samples, lifecycle
    stays ``observing_recovery``; the Observer only ever issued GETs."""
    observer_loop, state = loop
    state.values["dependency_error_ratio"] = 0.3
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)

    receipt = _only(observer_loop.poll_once(), session)

    assert receipt.accepted and not receipt.confirms_health
    sample = controller.session_history(session)["samples"][0]
    assert sample["outcome"] == "degraded" and sample["required_signals_present"]
    assert _lifecycle(owner, incident) == "observing_recovery"


def test_a_suspension_during_a_sample_stops_further_requests_and_ends_the_session(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    """C3 §4 / issue #86: the target is suspended while the Observer is
    between requests; the scope check before the next request sees it, no
    further request is issued, the partial sample is filed as suspended and
    the session ends ``scope_suspended``."""
    observer_loop, state = loop
    incident, target_id, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)

    def suspend_after_four(count: int) -> None:
        if count == 4:
            owner.set_target_suspension(
                target_id, True, expected_generation=0, actor="tester"
            )

    state.on_request.append(suspend_after_four)

    receipt = _only(observer_loop.poll_once(), session)

    assert len(state.requests) == 4, "no request after the suspension was seen"
    assert not receipt.accepted and receipt.reason == "suspended"
    assert receipt.session_state == "revoked"
    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("revoked", "scope_suspended")
    assert _lifecycle(owner, incident) == "observing_recovery"
    sample = controller.session_history(session)["samples"][0]
    # only the signal fully queried before the suspension has a reading
    assert [r["signal_name"] for r in sample["readings"]] == [SHIPPED.signals[0].name]
    assert sample["disposition"] == "history_only"


def test_a_session_without_a_profile_is_sampled_as_unknown_without_a_query(
    loop: tuple[ObserverLoop, Telemetry],
    owner: DurableStore,
    controller: ObservationStore,
) -> None:
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, profile=None, max_samples=3)

    receipt = _only(observer_loop.poll_once(), session)

    assert state.requests == []
    assert receipt.accepted and not receipt.confirms_health
    assert receipt.health_basis == "no_health_profile"
    sample = controller.session_history(session)["samples"][0]
    assert sample["outcome"] == "no_data" and sample["readings"] == []


def test_the_observer_login_cannot_authorize_a_session(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """The role boundary the loop runs under: authorizing a session (the human
    handling, step 3) writes the profile table, the incident's observation
    generation and a new session row, none of which the Observer login may
    do. (Ending a session is *not* denied by the role: the Observer's own
    claim/submit paths end sessions, so the grants cover those columns.)"""
    incident, _, target = _incident(owner)
    with pytest.raises(Exception, match="InsufficientPrivilege|STORAGE_UNAVAILABLE"):
        _authorize(observer, incident, target)
    assert _lifecycle(owner, incident) == "open"
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM opspilot_observation_sessions WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None and row["n"] == 0
