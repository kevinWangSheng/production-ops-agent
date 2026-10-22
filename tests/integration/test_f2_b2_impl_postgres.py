"""F2 PR-B2 implementer tests on PostgreSQL: backlog cap and BACKLOG_FULL parks.

The independent acceptance tests live elsewhere. These cover what the
implementation itself must hold (B2 contract U-B2-1/2/3, R-B2-1..3): the
count, the park order, human re-queues kept, paused incidents ignored, two
concurrent passes parking exactly the excess, the ``run_handoff`` shape,
the worker role needing no new grant, and ``OPSPILOT_BACKLOG_MAX`` parsing.

The lab database keeps every earlier test's rows and the backlog count is
global, so each test first makes every claimable Run overdue
(``neutralize_claimable``): such rows leave the backlog without being
parked by this pass.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.investigation.progress import BACKLOG_FULL, park_backlog
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.persistence.grants import WORKER_ROLE
from opspilot.web.events import DurableEventLog
from opspilot.worker_main import WorkerLoop, build_loop
from scripts.m0.postgres_lab import DSN
from tests.integration.f2_b1_support import neutralize_claimable
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

VERSIONS = {"tool_schema_revision": "f2-b2-impl"}


@pytest.fixture
def store() -> DurableStore:
    with psycopg.connect(DSN) as conn:
        neutralize_claimable(conn)
    durable = DurableStore(DSN)
    durable.install()
    return durable


@pytest.fixture(scope="module")
def worker_dsn():
    with login(DSN, WORKER_ROLE) as dsn:
        yield dsn


def _accept(store: DurableStore, *, priority: int, minutes: float) -> UUID:
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"f2-b2-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        budget_limit=10,
        versions=VERSIONS,
        priority=priority,
    )
    return incident


def _states(ids) -> dict[UUID, str]:
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT i.incident_id,r.state FROM opspilot_incidents i "
            "JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=ANY(%s)",
            (list(ids),),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _generation(incident_id: UUID) -> int:
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_parks_lowest_priority_then_latest_deadline_down_to_the_cap(store):
    # 4 critical, 4 warning, 4 other; deadlines interleaved.
    made = {
        (priority, minutes): _accept(store, priority=priority, minutes=minutes)
        for priority in (0, 1, 2)
        for minutes in (30, 40, 50, 60)
    }
    parked = store.park_backlog(max_queued=6)
    parked_ids = [incident for incident, _, _ in parked]
    # Excess 6: all four "other" (latest deadline first), then the two
    # warnings with the latest deadlines.
    assert parked_ids == [
        made[(2, 60)],
        made[(2, 50)],
        made[(2, 40)],
        made[(2, 30)],
        made[(1, 60)],
        made[(1, 50)],
    ]
    assert all(generation == 0 for _, _, generation in parked)
    states = _states(made.values())
    assert sorted(states.values()).count("waiting_human") == 6
    assert sorted(states.values()).count("queued") == 6
    # At the cap nothing more is parked.
    assert store.park_backlog(max_queued=6) == ()
    # Never a parked Run of higher priority than a still-queued one.
    queued_priorities = [p for (p, _), i in made.items() if states[i] == "queued"]
    parked_priorities = [
        p for (p, _), i in made.items() if states[i] == "waiting_human"
    ]
    assert max(queued_priorities) <= min(parked_priorities)


def test_limit_bounds_one_call_and_bad_arguments_are_refused(store):
    ids = [_accept(store, priority=2, minutes=30 + n) for n in range(5)]
    assert len(store.park_backlog(max_queued=1, limit=2)) == 2
    assert len(store.park_backlog(max_queued=1)) == 2
    assert list(_states(ids).values()).count("queued") == 1
    for bad in ({"max_queued": 0}, {"max_queued": True}, {"max_queued": 1, "limit": 0}):
        with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
            store.park_backlog(**bad)  # type: ignore[arg-type]


def test_a_human_requeue_counts_but_is_never_parked_again(store):
    low = _accept(store, priority=2, minutes=30)
    high = _accept(store, priority=0, minutes=30)
    [(parked, _, generation)] = store.park_backlog(max_queued=1)
    assert parked == low and generation == 0
    store.control(low, _generation(low), "follow_up", "oncall", {"note": "look again"})
    assert _states([low])[low] == "queued"
    # Two queued over a cap of one: the only Run a human never touched is
    # the critical one, so it is the one parked; the re-queued one stays.
    [(again, _, _)] = store.park_backlog(max_queued=1)
    assert again == high
    assert _states([low, high]) == {low: "queued", high: "waiting_human"}
    assert store.park_backlog(max_queued=1) == ()


def test_paused_incidents_are_neither_counted_nor_parked(store):
    paused = [_accept(store, priority=2, minutes=60) for _ in range(3)]
    for incident in paused:
        store.control(incident, _generation(incident), "pause", "oncall")
    kept = [_accept(store, priority=1, minutes=30) for _ in range(2)]
    assert store.park_backlog(max_queued=2) == ()
    assert set(_states(kept).values()) == {"queued"}
    assert set(_states(paused).values()) == {"queued"}


def test_two_concurrent_passes_park_exactly_the_excess(store, worker_dsn):
    ids = [_accept(store, priority=n % 3, minutes=30 + n) for n in range(20)]
    stores = [DurableStore(worker_dsn), DurableStore(DSN)]
    barrier = threading.Barrier(2)
    results: list[tuple] = []
    errors: list[BaseException] = []

    def pass_(durable: DurableStore) -> None:
        try:
            barrier.wait(5)
            results.append(durable.park_backlog(max_queued=8))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=pass_, args=(s,)) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    parked = [run for result in results for _, run, _ in result]
    assert len(parked) == 12 and len(set(parked)) == 12
    assert list(_states(ids).values()).count("queued") == 8


def test_announced_once_as_a_backlog_full_handoff(store):
    events = DurableEventLog(store)
    events.install()
    ids = [_accept(store, priority=2, minutes=30 + n) for n in range(3)]
    parked = park_backlog(store, events, 2)
    assert parked and parked[0][0] == ids[2]
    incident, run = parked[0]
    [handoff] = [e for e in events.read_after(incident, 0) if e.kind == "run_handoff"]
    assert handoff.payload["run_id"] == str(run)
    assert handoff.payload["reasons"] == [BACKLOG_FULL]
    assert handoff.payload["parked"] is True
    assert handoff.payload["published"] is False
    assert handoff.payload["execution"] == "not_started"
    assert handoff.payload["control_generation"] == 0
    assert park_backlog(store, events, 2) == ()


def test_the_worker_poll_parks_after_the_sweep_before_claiming(store, worker_dsn):
    worker_store = DurableStore(worker_dsn)
    events = DurableEventLog(worker_store)
    ids = [_accept(store, priority=2, minutes=30 + n) for n in range(4)]
    attempted: list[UUID] = []

    class Runner:
        def resume(self, incident_id: UUID):
            attempted.append(incident_id)
            raise PersistenceError("LEASE_ACTIVE")

    loop = WorkerLoop(
        queue=worker_store,
        runner=Runner(),
        sweeper=worker_store,
        events=events,
        backlog=worker_store,
        backlog_max=2,
    )
    loop.poll_once()
    states = _states(ids)
    assert [states[i] for i in ids] == [
        "queued",
        "queued",
        "waiting_human",
        "waiting_human",
    ]
    # Only the two left queued were offered to a slot.
    assert set(attempted) & set(ids) == set(ids[:2])


def test_backlog_max_is_read_from_the_environment(store, monkeypatch):
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", "4")
    for bad in ("0", "-1", "eight"):
        monkeypatch.setenv("OPSPILOT_BACKLOG_MAX", bad)
        with pytest.raises(SystemExit, match="OPSPILOT_BACKLOG_MAX"):
            build_loop(store, "unused-key", stop=threading.Event())
    monkeypatch.setenv("OPSPILOT_BACKLOG_MAX", "3")
    assert build_loop(store, "unused-key", stop=threading.Event()).backlog_max == 3
    monkeypatch.delenv("OPSPILOT_BACKLOG_MAX")
    loop = build_loop(store, "unused-key", stop=threading.Event())
    assert loop.backlog_max == 8 and loop.backlog is store


def test_one_pass_parks_past_the_store_limit_down_to_the_cap(store):
    # Codex review B2 #1: 109 eligible over a cap of 8 must end at 8 in one
    # pass, although one store call spends at most ``limit`` (100) parks.
    ids = [_accept(store, priority=2, minutes=30 + n / 10) for n in range(109)]
    parked = park_backlog(store, None, 8)
    assert len(parked) == 101
    assert list(_states(ids).values()).count("queued") == 8


def _wait_blocked(pid_query: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    with psycopg.connect(DSN, autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(pid_query).fetchone()
            if row and row[0]:
                return
            time.sleep(0.02)
    raise AssertionError("the pass never blocked on the held row lock")


def test_a_lower_priority_arrival_mid_pass_is_the_one_parked(store):
    # Codex review B2 #2, made deterministic: cap 2 with a critical and a
    # warning queued (no excess). Hold the warning's incident row so the
    # pass blocks right after picking it, commit an ``other`` arrival, then
    # release: the end-of-pass state must park the ``other`` one, never the
    # warning while a lower-priority Run stays queued.
    critical = _accept(store, priority=0, minutes=30)
    warning = _accept(store, priority=1, minutes=30)
    holder = psycopg.connect(DSN)
    holder.execute(
        "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE", (warning,)
    )
    result: list[tuple] = []
    thread = threading.Thread(
        target=lambda: result.append(store.park_backlog(max_queued=2))
    )
    thread.start()
    try:
        _wait_blocked(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
            "AND query LIKE 'SELECT 1 FROM opspilot_incidents WHERE incident_id=%' "
            "AND pid<>pg_backend_pid()"
        )
        other = _accept(store, priority=2, minutes=30)
    finally:
        holder.rollback()
        holder.close()
    thread.join(30)
    assert [incident for incident, _, _ in result[0]] == [other]
    assert _states([critical, warning, other]) == {
        critical: "queued",
        warning: "queued",
        other: "waiting_human",
    }
