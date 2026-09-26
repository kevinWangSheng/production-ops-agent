"""The long-running investigation worker: ``python -m opspilot.worker_main``.

Upstream shape (HolmesGPT ``conversations_worker``): poll the database
queue, claim, run, write the terminal state; SIGTERM stops claiming and
joins the in-flight attempt for a bounded grace period. Here the queue is
``DurableStore.claimable_incidents`` (a read-only hint), the claim and the
attempt are ``InvestigationRunner.resume`` (lease fence, C3 §6-7 recovery,
ADR-0005 settling), and every poll first runs the deadline sweep (ADR-0005
decision 2) so an overdue Run is parked even when no page is open.

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

* ``OPSPILOT_DSN``                  PostgreSQL DSN (required).
* ``DEEPSEEK_API_KEY``              the model credential, or
* ``OPSPILOT_ENV_FILE``             a private ``KEY=value`` file holding it.
* ``OPSPILOT_WORKER_POLL_SECONDS``  idle poll interval (default 2).
* ``OPSPILOT_WORKER_GRACE_SECONDS`` SIGTERM join bound (default 30).
* ``OPSPILOT_WORKER_BATCH``         incidents per poll (default 20).
* ``OPSPILOT_TOOL_PROFILE``         ``fixture`` (default) or ``otel-demo``;
  must match the workbench (``opspilot.tools.profiles``).

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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.progress import ExpirySweeper, ProgressLog, sweep_expired
from opspilot.investigation.runner import InvestigationRunner, RunnerOutcome
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools.profiles import ToolProfile, select_profile
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import LEASE_SECONDS
from opspilot.web.store import DurableClock
from opspilot.worker import Worker

__all__ = ["WorkerLoop", "main"]

_log = logging.getLogger("opspilot.worker")


class ClaimQueue(Protocol):
    def claimable_incidents(self, *, limit: int = 20) -> tuple[UUID, ...]: ...


class Resumer(Protocol):
    def resume(self, incident_id: UUID) -> RunnerOutcome: ...


@dataclass
class WorkerLoop:
    """Poll -> sweep -> resume each claimable incident, until ``stop`` is set.

    One attempt at a time: ``resume`` is synchronous and holds the Run's
    lease for its duration; ``stop`` is checked before every claim, never
    mid-attempt (the runner owns the attempt's fences).
    """

    queue: ClaimQueue
    runner: Resumer
    sweeper: ExpirySweeper
    events: ProgressLog | None
    stop: threading.Event = field(default_factory=threading.Event)
    poll_seconds: float = 2.0
    batch: int = 20

    def poll_once(self) -> list[tuple[UUID, RunnerOutcome]]:
        """One pass. Storage refusals and crashed attempts are logged by
        code/type and skipped; the next pass converges on the rows."""
        try:
            for incident_id, run_id in sweep_expired(self.sweeper, self.events):
                _log.info("swept incident=%s run=%s", incident_id, run_id)
        except PersistenceError as exc:
            _log.warning("sweep refused code=%s", exc)
        try:
            candidates = self.queue.claimable_incidents(limit=self.batch)
        except PersistenceError as exc:
            _log.warning("listing refused code=%s", exc)
            return []
        results: list[tuple[UUID, RunnerOutcome]] = []
        for incident_id in candidates:
            if self.stop.is_set():
                break
            try:
                outcome = self.runner.resume(incident_id)
            except PersistenceError as exc:
                _log.warning("attempt refused incident=%s code=%s", incident_id, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                # A crashed attempt keeps its lease to expiry, like a killed
                # worker; only the type is logged (a message could quote a
                # provider response).
                _log.error(
                    "attempt crashed incident=%s error=%s",
                    incident_id,
                    type(exc).__name__,
                )
                continue
            _log.info(
                "attempt incident=%s status=%s reason=%s",
                incident_id,
                outcome.status,
                outcome.reason,
            )
            results.append((incident_id, outcome))
        return results

    def run(self) -> int:
        """Poll until ``stop``; returns the number of passes made."""
        polls = 0
        while not self.stop.is_set():
            self.poll_once()
            polls += 1
            self.stop.wait(self.poll_seconds)
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


def build_loop(
    store: DurableStore,
    api_key: str,
    *,
    stop: threading.Event,
    profile: ToolProfile | None = None,
) -> WorkerLoop:
    """The product composition: durable events/evidence/ledger, one tool profile."""
    profile = profile or select_profile(os.environ)
    versions = profile.versions()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    clock = DurableClock(store)
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, versions),
        model=DeepSeekClient(api_key, clock=clock),
        executor_factory=profile.executor_factory(store, evidence, clock),
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=events,
        evidence=evidence,
    )
    _log.info(
        "worker owner=%s profile=%s versions=%s",
        runner.worker.owner,
        profile.name,
        versions,
    )
    return WorkerLoop(
        queue=store,
        runner=runner,
        sweeper=store,
        events=events,
        stop=stop,
        poll_seconds=_float_env("OPSPILOT_WORKER_POLL_SECONDS", 2.0),
        batch=_int_env("OPSPILOT_WORKER_BATCH", 20),
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    dsn = os.environ.get("OPSPILOT_DSN")
    if not dsn:
        raise SystemExit("OPSPILOT_DSN is required")
    grace = _float_env("OPSPILOT_WORKER_GRACE_SECONDS", 30.0)
    store = DurableStore(dsn)
    store.install()
    stop = threading.Event()
    loop = build_loop(store, _credential(), stop=stop)

    def request_stop(signum: int, _frame: Any) -> None:
        _log.info("signal=%s: stop claiming, finishing the in-flight attempt", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    thread = threading.Thread(target=loop.run, name="opspilot-worker", daemon=True)
    thread.start()
    _log.info("worker started")
    # Wait in short slices so the signal handlers get to run on this thread.
    # A loop thread that died (anything but the refusals ``poll_once``
    # handles) must not leave a live process that never polls.
    while not stop.wait(0.5):
        if not thread.is_alive():
            _log.error("worker loop died; exiting")
            return 1
    thread.join(grace)
    if thread.is_alive():
        _log.warning(
            "attempt still in flight after grace=%ss: exiting; its lease lapses "
            "within %ss and the next claim resumes from the committed rows",
            grace,
            LEASE_SECONDS,
        )
        return 1
    _log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
