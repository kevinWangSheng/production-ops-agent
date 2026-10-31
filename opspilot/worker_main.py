"""The long-running investigation worker: ``python -m opspilot.worker_main``.

Upstream shape (HolmesGPT ``conversations_worker``): poll the database
queue, claim, run, write the terminal state; SIGTERM stops claiming and
joins the in-flight attempts for one bounded grace period. Up to
``OPSPILOT_WORKER_CAPACITY`` attempts run at once, one per slot thread
(F2 PR-B1), claimed by incident priority, then deadline. Here the queue is
``DurableStore.claimable_incidents`` (a read-only hint), the claim and the
attempt are ``InvestigationRunner.resume`` (lease fence, C3 §6-7 recovery,
ADR-0005 settling), and every poll first runs the deadline sweep (ADR-0005
decision 2) so an overdue Run is parked even when no page is open, then the
backlog pass (F2 U4-A) that parks the lowest-priority queued Runs beyond
``OPSPILOT_BACKLOG_MAX`` as ``BACKLOG_FULL`` handoffs.

Safety properties, and where they come from:

* Several instances may run at once: ``claim()`` grants one lease per Run
  and every write is fenced on it, so a second worker meets ``LEASE_ACTIVE``
  and moves on. Nothing here coordinates instances.
* A stop never loses work: each model round and tool result is committed
  before the next begins, so an attempt cut off mid-flight (grace elapsed,
  or a hard kill) leaves its lease to lapse and the next claim -- by this
  process restarted or by any other worker -- resumes from the rows.
* Nothing secret is logged: fixed codes, ids and states only; the model
  key is read once and dropped.

Configuration (environment only):

* ``OPSPILOT_WORKER_DSN``           PostgreSQL DSN (required), as a login role
  in ``opspilot_worker`` (migration 0011), shared by the postmortem thread
  and the sweep; never the migration owner's ``OPSPILOT_DSN``, which this
  process does not read.
* ``DEEPSEEK_API_KEY``              the model credential, or
* ``OPSPILOT_ENV_FILE``             a private ``KEY=value`` file holding it.
* ``OPSPILOT_WORKER_POLL_SECONDS``  idle poll interval (default 2).
* ``OPSPILOT_WORKER_GRACE_SECONDS`` SIGTERM join bound (default 30).
* ``OPSPILOT_WORKER_BATCH``         incidents per ``poll_once`` (default 20).
* ``OPSPILOT_WORKER_CAPACITY``      concurrent attempts, one slot thread
  each (default 2); refused unless ``OPSPILOT_POOL_MAX_SIZE`` >= capacity + 2.
* ``OPSPILOT_BACKLOG_MAX``          queued Runs kept before the backlog pass
  parks the excess for a human (default 8, >= 1).
* ``OPSPILOT_TOOL_PROFILE``         ``fixture`` (default) or ``otel-demo``;
  must match the workbench (``opspilot.tools.profiles``).
* ``OPSPILOT_TOOL_PARALLELISM``     tool calls of one model turn in flight at
  once, 1-16 (default 2, C3 §13); 1 is serial dispatch.
* ``OPSPILOT_TRACE``                ``off`` (default) or ``lab``: LangSmith
  export of Run spans, lab targets only, fail-closed (``opspilot.tracing``).
* ``OPSPILOT_POSTMORTEM_POLL_SECONDS`` postmortem scan interval (default 30).
* ``OPSPILOT_POSTMORTEM_BATCH``     candidates per scan (default 5).
* ``OPSPILOT_HEALTH_BIND``         the health listener's ``host:port``
  (``opspilot.health``; default ``127.0.0.1:9464``, empty turns it off).

A second thread runs the postmortem pass (M1-03 D17): its own scan,
generation lease and model client, one generation at a time, never an
investigation Run lease (``opspilot.knowledge.worker``).

Like ``python -m opspilot.web serve`` it installs schema on start and is a
development entry point, not a deployment artifact. Under the default
profile the demo runs the real model over a canned read-only tool, not a
real telemetry source; ``otel-demo`` reads the pinned OTel Demo lab.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from opspilot import health, tracing
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import MAX_TOOL_PARALLELISM
from opspilot.investigation.progress import (
    BacklogParker,
    ExpirySweeper,
    ProgressLog,
    park_backlog,
    sweep_expired,
)
from opspilot.investigation.runner import InvestigationRunner, RunnerOutcome
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.jobs import GenerationStore
from opspilot.knowledge.worker import PostmortemWorker
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.tools.profiles import ToolProfile, select_profile
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import LEASE_SECONDS
from opspilot.web.store import DurableClock
from opspilot.worker import Worker

__all__ = ["WorkerLoop", "join_within", "main", "worker_capacity"]

_log = logging.getLogger("opspilot.worker")

TOOL_PARALLELISM_ENV = "OPSPILOT_TOOL_PARALLELISM"
DEFAULT_TOOL_PARALLELISM = 2


class ClaimQueue(Protocol):
    def claimable_incidents(self, *, limit: int = 20) -> tuple[UUID, ...]: ...


class Resumer(Protocol):
    def resume(self, incident_id: UUID) -> RunnerOutcome: ...


@dataclass
class WorkerLoop:
    """Poll -> sweep -> claim into free capacity slots, until ``stop`` is set.

    ``capacity`` slots (F2 R3, default 1 here, ``OPSPILOT_WORKER_CAPACITY``
    in ``main``) each drive one attempt at a time on their own thread with
    their own runner: slot 0 uses ``runner``, the others ``runner_factory()``
    (or ``runner`` again when no factory is given, for test doubles only --
    the product runner is not shared across threads, B1 contract U-B1-4).
    ``resume`` is synchronous and holds the Run's lease for its duration;
    ``stop`` is checked before every claim, never mid-attempt (the runner
    owns the attempt's fences).

    ``backlog`` (F2 B2, R-B2-1): when set, every sweep is followed by one
    backlog pass that parks the queued Runs beyond ``backlog_max`` --
    after the sweep, before any slot is filled (U-B2-1); ``None`` skips it.

    ``run()`` (R-B1-2): every ``poll_seconds`` the polling thread sweeps,
    then lists ``claimable_incidents`` -- priority, then deadline -- for the
    free slots and dispatches; a slot that frees wakes it to list again at
    once, so a later critical incident takes the next free slot ahead of
    queued lower-priority ones. Incidents in flight here, or attempted since
    the last timed poll, are not dispatched again (R-B1-3, and at most one
    attempt per incident per poll like before); ``claim()`` stays the
    authority. On ``stop`` it dispatches nothing more and returns once every
    in-flight attempt has returned; ``main`` bounds that by one shared grace
    (R-B1-5).

    ``poll_once()`` is the synchronous entry: one sweep, one listing of
    ``batch``, every listed incident attempted (concurrently over the slots
    when ``capacity > 1``, inline otherwise), results in listing order.
    """

    queue: ClaimQueue
    runner: Resumer
    sweeper: ExpirySweeper
    events: ProgressLog | None
    stop: threading.Event = field(default_factory=threading.Event)
    poll_seconds: float = 2.0
    batch: int = 20
    capacity: int = 1
    runner_factory: Callable[[], Resumer] | None = None
    backlog: BacklogParker | None = None
    backlog_max: int = 8
    #: ``time.monotonic()`` of the last timed poll whose sweep was not
    #: refused, for ``/readyz`` (F8-R4); ``None`` until the first one.
    last_poll_success: float | None = field(default=None, init=False)
    _runners: list[Resumer] = field(init=False, repr=False)
    _idle: list[Resumer] = field(init=False, repr=False)
    _cond: threading.Condition = field(init=False, repr=False)
    _wake: threading.Event = field(init=False, repr=False)
    _in_flight: dict[UUID, threading.Thread] = field(init=False, repr=False)
    _recent: set[UUID] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.capacity) is not int or self.capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if type(self.backlog_max) is not int or self.backlog_max < 1:
            raise ValueError("backlog_max must be a positive integer")
        factory = self.runner_factory
        self._runners = [self.runner] + [
            factory() if factory is not None else self.runner
            for _ in range(self.capacity - 1)
        ]
        self._idle = list(reversed(self._runners))
        self._cond = threading.Condition()
        self._wake = threading.Event()
        self._in_flight = {}
        self._recent = set()

    # -- one attempt -----------------------------------------------------

    def _attempt(self, runner: Resumer, incident_id: UUID) -> RunnerOutcome | None:
        """``runner.resume``; storage refusals and crashes are logged by
        code/type and yield ``None`` -- the next pass converges on the rows."""
        try:
            outcome = runner.resume(incident_id)
        except PersistenceError as exc:
            _log.warning("attempt refused incident=%s code=%s", incident_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            # A crashed attempt keeps its lease to expiry, like a killed
            # worker; only the type is logged (a message could quote a
            # provider response).
            _log.error(
                "attempt crashed incident=%s error=%s",
                incident_id,
                type(exc).__name__,
            )
            return None
        _log.info(
            "attempt incident=%s status=%s reason=%s",
            incident_id,
            outcome.status,
            outcome.reason,
        )
        return outcome

    def _sweep(self) -> bool:
        """``False`` when the storage refused the sweep or the backlog pass."""
        ok = True
        try:
            for incident_id, run_id in sweep_expired(self.sweeper, self.events):
                _log.info("swept incident=%s run=%s", incident_id, run_id)
        except PersistenceError as exc:
            _log.warning("sweep refused code=%s", exc)
            ok = False
        if self.backlog is None:
            return ok
        try:
            for incident_id, run_id in park_backlog(
                self.backlog, self.events, self.backlog_max
            ):
                _log.info("backlog parked incident=%s run=%s", incident_id, run_id)
        except PersistenceError as exc:
            _log.warning("backlog pass refused code=%s", exc)
            ok = False
        return ok

    # -- slots -----------------------------------------------------------

    def _checkout(self, *, wait: bool) -> Resumer | None:
        """A free slot's runner; ``wait`` blocks until one frees or ``stop``."""
        with self._cond:
            while not self._idle:
                if not wait or self.stop.is_set():
                    return None
                self._cond.wait(0.1)
            return self._idle.pop()

    def _launch(
        self,
        incident_id: UUID,
        runner: Resumer,
        sink: dict[UUID, RunnerOutcome] | None,
    ) -> bool:
        """Start ``incident_id`` on ``runner``'s slot; ``False`` (slot
        returned, nothing started) once ``stop`` is set -- a stop that lands
        between the caller's check and here still dispatches nothing more
        (R-B1-5). Only a thread started after this check is in flight."""
        thread = threading.Thread(
            target=self._slot,
            args=(incident_id, runner, sink),
            name=f"opspilot-slot-{incident_id}",
            daemon=True,
        )
        with self._cond:
            if self.stop.is_set():
                self._idle.append(runner)
                self._cond.notify_all()
                return False
            self._in_flight[incident_id] = thread
        thread.start()
        return True

    def _slot(
        self,
        incident_id: UUID,
        runner: Resumer,
        sink: dict[UUID, RunnerOutcome] | None,
    ) -> None:
        try:
            # Last check before the claim: a stop that arrived while the
            # thread was starting leaves the incident unclaimed.
            if self.stop.is_set():
                return
            outcome = self._attempt(runner, incident_id)
            if outcome is not None and sink is not None:
                sink[incident_id] = outcome
        finally:
            # Release the slot whatever happened, and wake the poller to
            # refill it (R-B1-2, R-B1-5).
            with self._cond:
                self._in_flight.pop(incident_id, None)
                self._recent.add(incident_id)
                self._idle.append(runner)
                self._cond.notify_all()
            self._wake.set()

    def in_flight_attempts(self) -> int:
        with self._cond:
            return len(self._in_flight)

    def _join_in_flight(self) -> None:
        while True:
            with self._cond:
                threads = list(self._in_flight.values())
            if not threads:
                return
            for thread in threads:
                thread.join()

    # -- entry points ----------------------------------------------------

    def poll_once(self) -> list[tuple[UUID, RunnerOutcome]]:
        """One pass. Storage refusals and crashed attempts are logged by
        code/type and skipped; the next pass converges on the rows."""
        self._sweep()
        with self._cond:
            self._recent.clear()  # only ``run()`` reads it
        try:
            candidates = self.queue.claimable_incidents(limit=self.batch)
        except PersistenceError as exc:
            _log.warning("listing refused code=%s", exc)
            return []
        results: list[tuple[UUID, RunnerOutcome]] = []
        if self.capacity == 1:
            for incident_id in candidates:
                if self.stop.is_set():
                    break
                outcome = self._attempt(self.runner, incident_id)
                if outcome is not None:
                    results.append((incident_id, outcome))
            return results
        sink: dict[UUID, RunnerOutcome] = {}
        started: list[UUID] = []
        for incident_id in candidates:
            if self.stop.is_set():
                break
            runner = self._checkout(wait=True)
            if runner is None:
                break
            if not self._launch(incident_id, runner, sink):
                break
            started.append(incident_id)
        self._join_in_flight()
        return [(i, sink[i]) for i in started if i in sink]

    def _refill(self) -> None:
        """Fill the free slots from the claim listing, best first. With
        several slots only those free before the listing take from it: one
        that frees while it is walked waits for a fresh listing (its wake is
        still set), so an incident listed ahead in the meantime takes it
        rather than a stale lower candidate (R-B1-2). One slot keeps walking
        the listing in order, as before capacity existed."""
        with self._cond:
            free = len(self._idle)
            excluded = set(self._in_flight) | self._recent
        if not free or self.stop.is_set():
            return
        budget = free if self.capacity > 1 else len(excluded) + free + 2
        try:
            candidates = self.queue.claimable_incidents(limit=free + len(excluded) + 2)
        except PersistenceError as exc:
            _log.warning("listing refused code=%s", exc)
            return
        for incident_id in candidates:
            if self.stop.is_set() or not budget:
                return
            if incident_id in excluded:
                continue
            budget -= 1
            runner = self._checkout(wait=False)
            if runner is None:
                return
            if not self._launch(incident_id, runner, None):
                return

    def run(self) -> int:
        """Poll until ``stop``; returns the number of timed passes made."""
        polls = 0
        next_poll = time.monotonic()
        while not self.stop.is_set():
            if time.monotonic() >= next_poll:
                if self._sweep():
                    self.last_poll_success = time.monotonic()
                with self._cond:
                    self._recent.clear()
                polls += 1
                next_poll = time.monotonic() + self.poll_seconds
            self._wake.clear()
            self._refill()
            # Until the next timed poll, a freed slot, or ``stop``.
            while not self.stop.is_set():
                remaining = next_poll - time.monotonic()
                if remaining <= 0 or self._wake.wait(min(remaining, 0.1)):
                    break
        self._join_in_flight()
        return polls


def _read_env_file(path: Path, name: str) -> str:
    """``name``'s value from a private ``KEY=value`` file; never echoed."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def _credential() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        env_file = os.environ.get("OPSPILOT_ENV_FILE")
        if env_file:
            key = _read_env_file(Path(env_file).expanduser(), "DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY (or OPSPILOT_ENV_FILE) is required")
    return key


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number") from None
    if not value > 0:
        raise SystemExit(f"{name} must be positive")
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None
    if value < 1:
        raise SystemExit(f"{name} must be positive")
    return value


def _tool_parallelism() -> int:
    """``OPSPILOT_TOOL_PARALLELISM``: 1-16, default 2 (#309, C3 §13)."""
    value = _int_env(TOOL_PARALLELISM_ENV, DEFAULT_TOOL_PARALLELISM)
    if value > MAX_TOOL_PARALLELISM:
        raise SystemExit(
            f"{TOOL_PARALLELISM_ENV} must be at most {MAX_TOOL_PARALLELISM}"
        )
    return value


def build_loop(
    store: DurableStore,
    api_key: str,
    *,
    stop: threading.Event,
    profile: ToolProfile | None = None,
    model_stats: health.ModelCallStats | None = None,
) -> WorkerLoop:
    """The product composition: durable events/evidence/ledger, one tool profile.
    Every slot's model client is counted into ``model_stats`` (F8-R3)."""
    stats = model_stats if model_stats is not None else health.ModelCallStats()
    # main() has refused a capacity the pool cannot serve (U-B1-3).
    capacity = _int_env(CAPACITY_ENV, 2)
    tool_parallelism = _tool_parallelism()
    profile = profile or select_profile(os.environ)
    versions = profile.versions()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()

    def slot_runner() -> InvestigationRunner:
        # U-B1-4: one model client (its transport handler records a single
        # in-flight connection), executor factory, clock and Worker identity
        # per slot; the store, events and evidence are shared.
        clock = DurableClock(store)
        runner = InvestigationRunner(
            store=store,
            worker=Worker.create(store, versions),
            model=health.ModelCallCounter(DeepSeekClient(api_key, clock=clock), stats),
            executor_factory=profile.executor_factory(store, evidence, clock),
            clock=clock,
            lease_seconds=LEASE_SECONDS,
            events=events,
            evidence=evidence,
            tool_parallelism=tool_parallelism,
        )
        _log.info(
            "worker slot owner=%s profile=%s tool_parallelism=%d versions=%s",
            runner.worker.owner,
            profile.name,
            tool_parallelism,
            versions,
        )
        return runner

    return WorkerLoop(
        queue=store,
        runner=slot_runner(),
        sweeper=store,
        events=events,
        stop=stop,
        poll_seconds=_float_env("OPSPILOT_WORKER_POLL_SECONDS", 2.0),
        batch=_int_env("OPSPILOT_WORKER_BATCH", 20),
        capacity=capacity,
        runner_factory=slot_runner,
        backlog=store,
        backlog_max=_int_env("OPSPILOT_BACKLOG_MAX", 8),
    )


CAPACITY_ENV = "OPSPILOT_WORKER_CAPACITY"


def worker_capacity(pool: PoolConfig) -> int:
    """``OPSPILOT_WORKER_CAPACITY`` (default 2, >= 1), refused with
    ``SystemExit`` unless the process pool holds one connection per slot
    plus the polling and postmortem threads (B1 contract U-B1-3)."""
    capacity = _int_env(CAPACITY_ENV, 2)
    if pool.max_size < capacity + 2:
        raise SystemExit(
            f"OPSPILOT_POOL_MAX_SIZE={pool.max_size} must be at least "
            f"OPSPILOT_WORKER_CAPACITY+2={capacity + 2}"
        )
    return capacity


def build_postmortem_worker(
    dsn: str,
    api_key: str,
    *,
    stop: threading.Event,
    events: ProgressLog | None,
    model_stats: health.ModelCallStats | None = None,
) -> PostmortemWorker:
    """The postmortem pass (M1-03 step 2) with its own stores and client,
    counted into ``model_stats`` like the slots' (F8-R3)."""
    knowledge = KnowledgeStore(dsn)
    knowledge.install()
    jobs = GenerationStore(dsn)
    jobs.install()
    return PostmortemWorker(
        knowledge=knowledge,
        jobs=jobs,
        model=health.ModelCallCounter(DeepSeekClient(api_key), model_stats),
        events=events,
        stop=stop,
        batch=_int_env("OPSPILOT_POSTMORTEM_BATCH", 5),
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    dsn = os.environ.get("OPSPILOT_WORKER_DSN")
    if not dsn:
        raise SystemExit("OPSPILOT_WORKER_DSN is required")
    grace = _float_env("OPSPILOT_WORKER_GRACE_SECONDS", 30.0)
    # Refuse a capacity the pool cannot serve before touching the database.
    worker_capacity(PoolConfig.from_env())
    store = DurableStore(dsn)
    store.install()
    stop = threading.Event()
    _configure_tracing()
    credential = _credential()
    model_stats = health.ModelCallStats()
    loop = build_loop(store, credential, stop=stop, model_stats=model_stats)
    postmortems = build_postmortem_worker(
        dsn, credential, stop=stop, events=loop.events, model_stats=model_stats
    )
    listener = health.serve_health(
        os.environ,
        readiness=lambda: _readiness(loop),
        metrics=lambda: _metrics(loop, model_stats),
    )
    try:
        return _run(loop, postmortems, store, stop, grace)
    finally:
        if listener is not None:
            listener.close()


def _readiness(loop: WorkerLoop) -> health.Readiness:
    """From the polling thread's record only; never a query (F8-R4)."""
    return health.poll_readiness(
        last_success_monotonic=loop.last_poll_success,
        poll_interval_seconds=loop.poll_seconds,
        now_monotonic=time.monotonic(),
        stopping=loop.stop.is_set(),
    )


def _metrics(loop: WorkerLoop, model_stats: health.ModelCallStats) -> str:
    last = loop.last_poll_success
    return health.worker_metrics(
        last_poll_age=None if last is None else time.monotonic() - last,
        in_flight=loop.in_flight_attempts(),
        model=model_stats.snapshot(),
    )


def _run(
    loop: WorkerLoop,
    postmortems: PostmortemWorker,
    store: DurableStore,
    stop: threading.Event,
    grace: float,
) -> int:
    """Signals, the two threads and the bounded shutdown (R-B1-5)."""

    def request_stop(signum: int, _frame: Any) -> None:
        _log.info("signal=%s: stop claiming, finishing the in-flight attempts", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    thread = threading.Thread(target=loop.run, name="opspilot-worker", daemon=True)
    thread.start()
    postmortem_thread = threading.Thread(
        target=postmortems.run,
        args=(_float_env("OPSPILOT_POSTMORTEM_POLL_SECONDS", 30.0),),
        name="opspilot-postmortem",
        daemon=True,
    )
    postmortem_thread.start()
    _log.info("worker started")
    # Wait in short slices so the signal handlers get to run on this thread.
    # A loop thread that died (anything but the refusals ``poll_once``
    # handles) must not leave a live process that never polls.
    while not stop.wait(0.5):
        if not thread.is_alive() or not postmortem_thread.is_alive():
            _log.error("worker loop died; exiting")
            return 1
    if not join_within((thread, postmortem_thread), grace):
        _log.warning(
            "attempt still in flight after grace=%ss: exiting; its lease lapses "
            "within %ss and the next claim resumes from the committed rows",
            grace,
            LEASE_SECONDS,
        )
        tracing.shutdown()
        return 1
    store.close()
    postmortems.knowledge.close()
    postmortems.jobs.close()
    tracing.shutdown()
    _log.info("worker stopped")
    return 0


def join_within(threads: Sequence[threading.Thread], grace: float) -> bool:
    """Join ``threads`` against one deadline ``grace`` seconds from now, so
    shutdown waits at most one grace in all (R-B1-5); ``True`` when every
    thread has ended."""
    deadline = time.monotonic() + grace
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _configure_tracing() -> None:
    """``OPSPILOT_TRACE`` (default off). The LangSmith key may come from the
    same private env file as the model key; its value is never logged."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        env_file = os.environ.get("OPSPILOT_ENV_FILE")
        if env_file:
            key = _read_env_file(Path(env_file).expanduser(), "LANGSMITH_API_KEY")
            if key:
                os.environ["LANGSMITH_API_KEY"] = key
    tracing.configure(os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
