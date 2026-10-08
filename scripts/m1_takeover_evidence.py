"""One bounded real Run taken over while it is in flight (M1-02 step 3b, #121).

Evidence for the takeover path (AGENTS.md: a PR that touches the
investigation Run's claim / parking path attaches one bounded real Run with
a LangSmith trace and a frozen ``summary.json``). The ``InvestigationRunner``
drives a real DeepSeek Run over the fixture tool profile in a thread; the
main thread waits until the Run row is ``running`` under a lease and then
applies ``takeover`` through ``DurableStore.control``. Recorded, from the
committed rows and the recording model client:

* the Run is ``waiting_human`` with no owner / lease, the incident
  ``human_owned`` with the generation stepped and the audit row written;
* every model HTTP request started before the takeover committed; a second
  ``resume`` of the runner after the takeover claims nothing and issues no
  model request;
* the LangSmith trace (``OPSPILOT_TRACE=lab``) of the Run, read back.

Same conventions as ``scripts/m1_live_runner.py``: credentials from
``M0_ENV_FILE`` (never printed), ``OPSPILOT_LAB_DSN`` for a throwaway
PostgreSQL, ``--lab-round`` for the LangSmith project, raw ledger outside the
repository (``tmp/lab-ledgers/``), only ``summary.json`` under
``docs/evidence/m1-02-takeover/live-runs/<run_id>/``. Development script.

    M0_ENV_FILE=/abs/.env OPSPILOT_TRACE=lab OPSPILOT_LAB_DSN='host=... ' \\
      .venv/bin/python scripts/m1_takeover_evidence.py --lab-round takeover-2026-10-07
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opspilot import tracing  # noqa: E402
from opspilot.investigation.client import DeepSeekClient  # noqa: E402
from opspilot.investigation.limits import M1_FROZEN_LIMITS  # noqa: E402
from opspilot.investigation.runner import InvestigationRunner  # noqa: E402
from opspilot.persistence import DurableStore, PersistenceError  # noqa: E402
from opspilot.tools.fixture import fixture_face  # noqa: E402
from opspilot.web import DurableEventLog, DurableEvidenceStore  # noqa: E402
from opspilot.web.service import LEASE_SECONDS  # noqa: E402
from opspilot.worker import Worker  # noqa: E402
from scripts.lab_evidence import (  # noqa: E402
    LAB_ROUND_ENV,
    freeze,
    langsmith_client,
    prepare_lab_project,
    trace_evidence,
)
from scripts.m0.postgres_lab import DSN, verify_server  # noqa: E402
from scripts.m1_context_latency import balance  # noqa: E402
from scripts.m1_live_flash_loop import (  # noqa: E402
    QUESTION,
    RecordingClient,
    cost_cny,
    load_langsmith_env,
    read_key,
    resolve_env_file,
)
from scripts.m1_live_runner import (  # noqa: E402
    LIVE_TOOL_TARGET,
    VERSIONS,
    SystemClock,
    _events,
    _outcome,
    _rows,
    executor_factory_for,
)

OUT_ROOT = ROOT / "docs/evidence/m1-02-takeover/live-runs"


class TimestampedRecordingClient(RecordingClient):
    """``RecordingClient`` plus the wall-clock start of every HTTP request, so
    the ledger shows each request started before the takeover committed."""

    def __init__(self, inner: DeepSeekClient, clock: SystemClock) -> None:
        super().__init__(inner)
        self._clock = clock

    def complete(self, call):  # type: ignore[no-untyped-def]
        started_at = self._clock.now().isoformat()
        try:
            return super().complete(call)
        finally:
            self.attempts[-1]["started_at"] = started_at


EXPERIMENT = "m1-02-takeover"
# How long the main thread waits for the worker thread to hold a lease.
CLAIM_WAIT_S = 60.0


def _db_now(store: DurableStore) -> datetime:
    with store.transaction(snapshot=True) as conn:
        row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
    assert row is not None
    return row["now"]


def _control_rows(store: DurableStore, incident) -> list[dict]:
    with store.transaction(snapshot=True) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT action,expected_generation,resulting_generation,actor,created_at FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
                (incident,),
            ).fetchall()
        ]


def _incident_row(store: DurableStore, incident) -> dict:
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT state,mode,lifecycle,control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return dict(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-requests", type=int, default=3)
    parser.add_argument("--deadline-seconds", type=int, default=12 * 60)
    parser.add_argument("--lab-round", default=os.environ.get(LAB_ROUND_ENV) or None)
    args = parser.parse_args()
    if not 1 <= args.model_requests <= M1_FROZEN_LIMITS.model_requests:
        raise SystemExit("model-requests outside the frozen ceiling")
    env_file = resolve_env_file()
    key = read_key(env_file)
    if not key:
        print(json.dumps({"status": "failed", "failure": "credential unavailable"}))
        return 2
    if not os.environ.get("OPSPILOT_LAB_DSN"):
        verify_server()
    load_langsmith_env(env_file)
    project = prepare_lab_project(
        os.environ, args.lab_round, client_factory=langsmith_client
    )
    trace = tracing.configure(os.environ)
    store = DurableStore(DSN)
    store.install()
    log = DurableEventLog(store)
    log.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()

    clock = SystemClock()
    deadline = clock.now() + timedelta(seconds=args.deadline_seconds)
    incident, run_id = uuid4(), uuid4()
    input = fixture_face().input_for(
        run_id=str(run_id),
        question=QUESTION,
        target_id=LIVE_TOOL_TARGET,
        deadline=deadline,
        model_requests=args.model_requests,
    )
    store.accept(
        incident,
        run_id,
        f"m1-takeover-evidence-{run_id}",
        deadline=deadline,
        budget_limit=args.model_requests,
        versions=VERSIONS,
        input=input.as_json(),
        target_id=store.register_target(LIVE_TOOL_TARGET),
    )
    recorder = TimestampedRecordingClient(DeepSeekClient(key, clock=clock), clock)
    balance_before = balance(key)
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(VERSIONS)),
        model=recorder,
        executor_factory=executor_factory_for(evidence, clock, deadline),
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=log,
        evidence=evidence,
    )
    started = clock.now()
    attempts: list[dict] = []
    first: dict = {}

    def investigate() -> None:
        first["outcome"] = _outcome(runner.resume(incident))

    thread = threading.Thread(target=investigate, name="investigator")
    thread.start()
    # Wait until the worker thread holds the lease (the Run is running).
    waited = 0.0
    leased = False
    while waited < CLAIM_WAIT_S:
        rows = _rows(store, incident)
        if rows["run_state"] == "running" and rows.get("owner") is not None:
            leased = True
            break
        time.sleep(0.2)
        waited += 0.2
    takeover: dict = {"applied": False}
    if leased:
        generation = store.rebuild(incident)["control_generation"]
        requests_before = len(recorder.attempts)
        takeover_at = _db_now(store)
        try:
            resulting = store.control(
                incident, generation, "takeover", "evidence-operator"
            )
            takeover = {
                "applied": True,
                "expected_generation": generation,
                "resulting_generation": resulting,
                "at": takeover_at.isoformat(),
                "model_requests_started_before": requests_before,
                "rows_after": _rows(store, incident),
            }
        except PersistenceError as exc:
            takeover = {"applied": False, "error": str(exc)}
    thread.join()
    attempts.append(
        {"label": "first (taken over while running)", "outcome": first["outcome"]}
    )
    attempts[-1]["rows_after"] = _rows(store, incident)
    requests_after_first = len(recorder.attempts)
    # The worker polls again: nothing to claim, no model request.
    attempts.append(
        {"label": "after-takeover", "outcome": _outcome(runner.resume(incident))}
    )
    attempts[-1]["rows_after"] = _rows(store, incident)
    requests_after_second = len(recorder.attempts)
    ended = clock.now()
    tracing.shutdown()
    balance_after = balance(key)
    del key
    trace_record = {
        "mode": trace.mode,
        "trace_id": trace.last_trace_id,
        "project": os.environ.get("LANGSMITH_PROJECT") if trace.mode == "lab" else None,
        "dropped_spans": getattr(trace, "dropped_spans", 0),
    }
    usages = [a["usage"] for a in recorder.attempts if isinstance(a.get("usage"), dict)]
    request_starts = [a.get("started_at") for a in recorder.attempts]
    ledger = {
        "experiment": EXPERIMENT,
        "driver": "opspilot.investigation.runner.InvestigationRunner in a thread; takeover from the main thread",
        "incident_id": str(incident),
        "run_id": str(run_id),
        "ledger_id": str(uuid4()),
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "model": "deepseek-flash",
        "model_requests_bound": args.model_requests,
        "http_count": len(recorder.attempts),
        "attempts_http": recorder.attempts,
        "known_cost_cny_upper": round(sum(cost_cny(u) for u in usages), 6),
        "balance_before": balance_before,
        "balance_after": balance_after,
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
        "run_usage": store.run_usage(run_id),
        "attempts": attempts,
        "takeover": takeover,
        "incident_after": _incident_row(store, incident),
        "controls": _control_rows(store, incident),
        "claimable_after": [str(i) for i in store.claimable_incidents(limit=100)],
        "events": _events(log, incident),
        "trace": trace_record,
    }
    out = Path(os.environ.get("M1_ACCEPTANCE_OUT") or OUT_ROOT) / str(run_id)
    verdicts = {
        "takeover_applied": takeover.get("applied", False),
        "run_state_after": attempts[-1]["rows_after"].get("run_state"),
        "run_owner_after": attempts[-1]["rows_after"].get("owner"),
        "incident_mode_after": ledger["incident_after"]["mode"],
        "control_generation_after": ledger["incident_after"]["control_generation"],
        "model_requests_before_takeover": takeover.get("model_requests_started_before"),
        "model_requests_total": len(recorder.attempts),
        "model_requests_after_first_attempt_returned": requests_after_first,
        "model_requests_after_second_resume": requests_after_second,
        "no_model_request_after_takeover_returned": requests_after_second
        == requests_after_first,
        "claimable_after_takeover": str(incident) in ledger["claimable_after"],
        "first_attempt": first["outcome"],
        "second_resume": attempts[-1]["outcome"],
        "request_started_at": request_starts,
    }
    frozen, summary_path = freeze(
        ledger,
        experiment=EXPERIMENT,
        run_id=str(run_id),
        evidence_dir=out,
        summary={
            "incident_id": str(incident),
            "driver": ledger["driver"],
            "model": ledger["model"],
            "started": ledger["started"],
            "ended": ledger["ended"],
            "takeover": {k: v for k, v in takeover.items() if k != "rows_after"},
            "verdicts": verdicts,
            "controls": ledger["controls"],
            "counts": {
                "http": ledger["http_count"],
                "prompt_tokens": ledger["prompt_tokens"],
                "completion_tokens": ledger["completion_tokens"],
                "run_usage": ledger["run_usage"],
            },
            "known_cost_cny_upper": ledger["known_cost_cny_upper"],
            "balance_before": balance_before,
            "balance_after": balance_after,
            "trace": trace_evidence(
                trace_record,
                run_id=str(run_id),
                project=project,
                client_factory=langsmith_client,
            ),
        },
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "summary": str(summary_path.relative_to(ROOT)),
                "verdicts": verdicts,
                "trace": frozen.get("trace"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
