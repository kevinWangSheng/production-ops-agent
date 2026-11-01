"""F2 PR-B1 implementation tests: worker capacity slots and claim priority.

Implementer-side checks with test doubles (no database); the independent
acceptance tests live elsewhere. Contract: docs/tasks/2026-10-21-f2-b1-contract.md.
"""

from __future__ import annotations

import threading
import time
from uuid import UUID, uuid4

import pytest

from opspilot import worker_main
from opspilot.alertmanager import priority_of
from opspilot.investigation.runner import RunnerOutcome
from opspilot.persistence import PoolConfig
from opspilot.worker_main import WorkerLoop, worker_capacity


class Queue:
    """A listing the test rewrites at will; records every limit asked."""

    def __init__(self, listing=()):
        self.lock = threading.Lock()
        self.listing = list(listing)
        self.limits: list[int] = []

    def claimable_incidents(self, *, limit=20):
        with self.lock:
            self.limits.append(limit)
            return tuple(self.listing[:limit])

    def remove(self, incident_id):
        with self.lock:
            if incident_id in self.listing:
                self.listing.remove(incident_id)


class Sweeper:
    def sweep_expired_runs_with_generations(self, *, incident_id=None, limit=100):
        return ()


class Gate:
    """Shared state of the blocking runners: who runs, the concurrency peak."""

    def __init__(self, queue):
        self.queue = queue
        self.lock = threading.Lock()
        self.running: set[UUID] = set()
        self.peak = 0
        self.started: list[UUID] = []
        self.release: dict[UUID, threading.Event] = {}
        self.fail: set[UUID] = set()
        self.by_runner: dict[int, list[UUID]] = {}

    def event(self, incident_id):
        with self.lock:
            return self.release.setdefault(incident_id, threading.Event())


class BlockingRunner:
    def __init__(self, gate: Gate):
        self.gate = gate

    def resume(self, incident_id):
        gate = self.gate
        with gate.lock:
            gate.running.add(incident_id)
            gate.peak = max(gate.peak, len(gate.running))
            gate.started.append(incident_id)
            gate.by_runner.setdefault(id(self), []).append(incident_id)
        try:
            assert gate.event(incident_id).wait(5.0), "test never released it"
            if incident_id in gate.fail:
                raise RuntimeError("attempt crashed")
            gate.queue.remove(incident_id)
            return RunnerOutcome(status="published")
        finally:
            with gate.lock:
                gate.running.discard(incident_id)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _loop(queue, gate, *, capacity, poll=0.05, stop=None):
    runners: list[BlockingRunner] = []

    def factory():
        runner = BlockingRunner(gate)
        runners.append(runner)
        return runner

    loop = WorkerLoop(
        queue=queue,
        runner=factory(),
        sweeper=Sweeper(),
        events=None,
        stop=stop or threading.Event(),
        poll_seconds=poll,
        capacity=capacity,
        runner_factory=factory,
    )
    return loop, runners


def _start(loop):
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    return thread


def test_run_never_exceeds_capacity_and_refills_a_freed_slot_at_once():
    ids = [uuid4() for _ in range(5)]
    queue = Queue(ids)
    gate = Gate(queue)
    # A long poll interval: only a freed slot's wake can explain the refill.
    loop, runners = _loop(queue, gate, capacity=2, poll=30.0)
    assert len(runners) == 2
    thread = _start(loop)
    try:
        assert _wait_for(lambda: len(gate.started) == 2)
        time.sleep(0.1)
        assert gate.started == ids[:2] and gate.peak == 2
        gate.event(ids[0]).set()
        assert _wait_for(lambda: len(gate.started) == 3, timeout=1.0)
        assert gate.started[2] == ids[2]
        for incident_id in ids:
            gate.event(incident_id).set()
        assert _wait_for(lambda: len(gate.started) == 5)
        assert gate.peak == 2
    finally:
        loop.stop.set()
        for incident_id in ids:
            gate.event(incident_id).set()
        thread.join(3.0)
    assert not thread.is_alive()
    # U-B1-4: both slot runners were used, each one attempt at a time.
    assert len(gate.by_runner) == 2


def test_a_later_higher_priority_incident_takes_the_next_free_slot():
    low = [uuid4() for _ in range(4)]
    queue = Queue(low)
    gate = Gate(queue)
    loop, _ = _loop(queue, gate, capacity=2, poll=30.0)
    thread = _start(loop)
    try:
        assert _wait_for(lambda: len(gate.started) == 2)
        critical = uuid4()
        with queue.lock:  # the store lists it first (priority 0)
            queue.listing.insert(0, critical)
        gate.event(low[0]).set()
        assert _wait_for(lambda: len(gate.started) == 3, timeout=1.0)
        assert gate.started[2] == critical
    finally:
        loop.stop.set()
        for incident_id in [*low, critical]:
            gate.event(incident_id).set()
        thread.join(3.0)
    assert not thread.is_alive()


def test_a_slot_freed_after_the_listing_is_filled_from_a_fresh_listing():
    # The CI flake of the test above, made deterministic: a slot that frees
    # while the poller still walks an older listing must not take a stale,
    # lower candidate; the critical that arrived meanwhile takes it.
    x, w, y, z = (uuid4() for _ in range(4))
    critical = uuid4()
    queue = Queue([x, w])
    gate = Gate(queue)
    loop, _ = _loop(queue, gate, capacity=2, poll=30.0)
    listed = queue.claimable_incidents
    calls = []

    def claimable(*, limit=20):
        snapshot = listed(limit=limit)
        calls.append(snapshot)
        if len(calls) == 2:  # the refill after ``w`` freed one slot
            gate.event(x).set()
            assert _wait_for(lambda: len(loop._idle) == 2)
            with queue.lock:
                queue.listing.insert(0, critical)
        return snapshot

    queue.claimable_incidents = claimable  # type: ignore[method-assign]
    thread = _start(loop)
    try:
        assert _wait_for(lambda: len(gate.started) == 2)
        with queue.lock:
            queue.listing.extend([y, z])
        gate.event(w).set()
        assert _wait_for(lambda: len(gate.started) == 4, timeout=1.0)
        assert set(gate.started[:2]) == {x, w}
        assert gate.started[2:] == [y, critical]
    finally:
        loop.stop.set()
        for incident_id in (x, w, y, z, critical):
            gate.event(incident_id).set()
        thread.join(3.0)
    assert not thread.is_alive()


def test_an_in_flight_incident_is_not_dispatched_to_a_second_slot():
    incident = uuid4()
    queue = Queue([incident])
    gate = Gate(queue)
    # The listing keeps showing it (a stale hint); the other slot stays idle.
    queue.remove = lambda _incident_id: None
    loop, _ = _loop(queue, gate, capacity=2, poll=0.02)
    thread = _start(loop)
    try:
        assert _wait_for(lambda: len(gate.started) == 1)
        time.sleep(0.2)
        assert gate.started == [incident] and gate.peak == 1
    finally:
        loop.stop.set()
        gate.event(incident).set()
        thread.join(3.0)
    assert not thread.is_alive()


def test_a_crashed_slot_frees_its_slot_and_the_loop_keeps_serving():
    bad, good, later = uuid4(), uuid4(), uuid4()
    queue = Queue([bad, good, later])
    gate = Gate(queue)
    gate.fail.add(bad)
    loop, _ = _loop(queue, gate, capacity=2, poll=30.0)
    thread = _start(loop)
    try:
        assert _wait_for(lambda: len(gate.started) == 2)
        gate.event(bad).set()  # raises inside the slot
        assert _wait_for(lambda: later in gate.started, timeout=1.0)
        assert good in gate.running
        assert thread.is_alive()
    finally:
        loop.stop.set()
        for incident_id in (bad, good, later):
            gate.event(incident_id).set()
        thread.join(3.0)
    assert not thread.is_alive()


def test_stop_dispatches_nothing_more_and_run_returns_after_in_flight_attempts():
    ids = [uuid4() for _ in range(4)]
    queue = Queue(ids)
    gate = Gate(queue)
    loop, _ = _loop(queue, gate, capacity=2, poll=0.02)
    thread = _start(loop)
    assert _wait_for(lambda: len(gate.started) == 2)
    loop.stop.set()
    time.sleep(0.1)
    assert thread.is_alive(), "run() waits for the in-flight attempts"
    gate.event(ids[0]).set()
    gate.event(ids[1]).set()
    thread.join(3.0)
    assert not thread.is_alive()
    assert gate.started == ids[:2]


def test_poll_once_with_capacity_runs_the_batch_concurrently_in_listing_order():
    ids = [uuid4() for _ in range(3)]
    queue = Queue(ids)
    gate = Gate(queue)
    loop, _ = _loop(queue, gate, capacity=2)
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("value", loop.poll_once()), daemon=True
    )
    thread.start()
    assert _wait_for(lambda: gate.peak == 2)
    for incident_id in reversed(ids):
        gate.event(incident_id).set()
    thread.join(3.0)
    assert not thread.is_alive()
    assert [incident for incident, _ in result["value"]] == ids
    assert queue.limits == [20]


def test_capacity_one_poll_once_stays_inline_on_the_calling_thread():
    seen: list[str] = []

    class Inline:
        def resume(self, incident_id):
            seen.append(threading.current_thread().name)
            return RunnerOutcome(status="published")

    loop = WorkerLoop(
        queue=Queue([uuid4(), uuid4()]),
        runner=Inline(),
        sweeper=Sweeper(),
        events=None,
    )
    assert len(loop.poll_once()) == 2
    assert seen == [threading.current_thread().name] * 2


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        WorkerLoop(
            queue=Queue(), runner=object(), sweeper=Sweeper(), events=None, capacity=0
        )


@pytest.mark.parametrize(
    ("env", "pool", "expected"),
    [
        ({}, 5, 2),
        ({"OPSPILOT_WORKER_CAPACITY": "1"}, 4, 1),
        ({"OPSPILOT_WORKER_CAPACITY": "4"}, 7, 4),
    ],
)
def test_worker_capacity_accepts_a_pool_of_capacity_plus_three(
    monkeypatch, env, pool, expected
):
    monkeypatch.delenv("OPSPILOT_WORKER_CAPACITY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert worker_capacity(PoolConfig(max_size=pool)) == expected


@pytest.mark.parametrize(
    ("raw", "pool"), [("3", 5), ("0", 5), ("-1", 5), ("two", 5), (None, 4)]
)
def test_worker_capacity_refuses_to_start(monkeypatch, raw, pool):
    monkeypatch.delenv("OPSPILOT_WORKER_CAPACITY", raising=False)
    if raw is not None:
        monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", raw)
    with pytest.raises(SystemExit):
        worker_capacity(PoolConfig(max_size=pool))


def test_main_refuses_an_oversized_capacity_before_touching_the_database(
    monkeypatch,
):
    monkeypatch.setenv("OPSPILOT_WORKER_DSN", "host=127.0.0.1 port=1 dbname=none")
    monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", "3")
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", "4")

    def no_store(*_args, **_kwargs):
        raise AssertionError("opened the store")

    monkeypatch.setattr(worker_main, "DurableStore", no_store)
    with pytest.raises(SystemExit, match="OPSPILOT_WORKER_CAPACITY"):
        worker_main.main([])


@pytest.mark.parametrize(
    ("labels", "priority"),
    [
        ({"severity": "critical"}, 0),
        ({"severity": " Critical "}, 0),
        ({"severity": "WARNING"}, 1),
        ({"severity": "warning"}, 1),
        ({"severity": "info"}, 2),
        ({"severity": "page"}, 2),
        ({"severity": "error"}, 2),
        ({"severity": ""}, 2),
        ({}, 2),
        ({"alertname": "X"}, 2),
    ],
)
def test_priority_of_is_the_fixed_severity_mapping(labels, priority):
    assert priority_of(labels) == priority


def _stop_inside_checkout(loop):
    """A SIGTERM landing after the caller's stop check, inside ``_checkout``."""
    checkout = loop._checkout

    def racing(*, wait):
        runner = checkout(wait=wait)
        loop.stop.set()
        return runner

    loop._checkout = racing


def test_a_stop_racing_the_checkout_starts_no_attempt_in_run():
    ids = [uuid4() for _ in range(3)]
    queue = Queue(ids)
    gate = Gate(queue)
    loop, _ = _loop(queue, gate, capacity=2, poll=0.02)
    _stop_inside_checkout(loop)
    thread = _start(loop)
    thread.join(3.0)
    assert not thread.is_alive()
    assert gate.started == []
    assert len(loop._idle) == 2 and not loop._in_flight


def test_a_stop_racing_the_checkout_starts_no_attempt_in_poll_once():
    ids = [uuid4() for _ in range(3)]
    queue = Queue(ids)
    gate = Gate(queue)
    loop, _ = _loop(queue, gate, capacity=2)
    _stop_inside_checkout(loop)
    assert loop.poll_once() == []
    assert gate.started == []
    assert len(loop._idle) == 2 and not loop._in_flight


def test_a_stop_after_launch_but_before_the_slot_runs_claims_nothing():
    incident = uuid4()
    queue = Queue([incident])
    gate = Gate(queue)
    loop, runners = _loop(queue, gate, capacity=2)
    runner = loop._checkout(wait=False)
    loop.stop.set()
    loop._slot(incident, runner, None)
    assert gate.started == [] and len(loop._idle) == 2


def test_shutdown_joins_every_thread_within_one_shared_grace():
    release = threading.Event()
    stuck = [
        threading.Thread(target=release.wait, args=(10.0,), daemon=True)
        for _ in range(2)
    ]
    for thread in stuck:
        thread.start()
    try:
        began = time.monotonic()
        assert worker_main.join_within(stuck, 0.5) is False
        # One grace in all, not one per thread (R-B1-5).
        assert time.monotonic() - began < 0.5 + 0.2
    finally:
        release.set()
    assert worker_main.join_within(stuck, 2.0) is True
