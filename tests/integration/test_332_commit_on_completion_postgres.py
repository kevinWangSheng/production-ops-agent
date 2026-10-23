"""#332 durable acceptance: F2 breakpoint 3 (tool in flight) at parallelism 2.

Contract: the #332 pre-review section 3 item 2 (user ruling 2026-10-23,
U-1 A) and #309 contract r2.  Attempt 1 runs a two-tool batch in a daemon
thread; t0's transport blocks while t1 returns at once.  The test polls
PostgreSQL until the step row holds ordinal 1, then abandons that thread
without releasing its lease (a dead worker), waits for the lease to expire and
resumes with a fresh runner/worker.  Only ordinal 0 may be re-dispatched; the
conclusion cites the evidence t1 produced in epoch 1.  Finally the abandoned
thread is let go: its commit is fenced into a late_result row and the
committed rows stay as they were.

Observed only through durable rows, the Run ledger, the outcome and the
transport calls.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import opspilot.tools.fixture as fixture_module
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools import TransportResponse
from opspilot.tools.executor import TransportRequest
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.web import DurableClock, DurableEventLog, DurableEvidenceStore
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import WINDOW_START, body

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

LEASE_SECONDS = 1
# Bound on "ordinal 1 shows up in the step row while t0 is blocked".
SIBLING_COMMIT_WAIT_SECONDS = 5.0
BLOCK_SECONDS = 30.0


def _index(request: TransportRequest) -> int:
    return int(request.operation_id.rsplit("-t", 1)[1])


class _TransportState:
    def __init__(self) -> None:
        self.requests: list[TransportRequest] = []
        self.lock = threading.Lock()
        self.release_t0 = threading.Event()
        self.t0_blocked_once = False

    def count(self, index: int) -> int:
        with self.lock:
            return sum(1 for r in self.requests if _index(r) == index)


class _StuckT0Transport:
    """The first t0 read hangs until released; every other read returns."""

    state: _TransportState

    def fetch(self, request: TransportRequest) -> TransportResponse:
        state = type(self).state
        with state.lock:
            state.requests.append(request)
            block = _index(request) == 0 and not state.t0_blocked_once
            if block:
                state.t0_blocked_once = True
        if block:
            state.release_t0.wait(timeout=BLOCK_SECONDS)
        return TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]),
            data_as_of=WINDOW_START,
        )


def _batch_reply():
    return reply(
        tool_calls=[
            tool_call(
                "call-a",
                name=FIXTURE_TOOL,
                arguments='{"expr":"rate(http_errors[5m])","step_seconds":30}',
            ),
            tool_call(
                "call-b",
                name=FIXTURE_TOOL,
                arguments='{"expr":"rate(http_errors[5m])","step_seconds":60}',
            ),
        ],
        finish="tool_calls",
    )


def _runner(store, evidence, events, clock, factory, replies):
    return InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(fixture_versions())),
        model=ScriptedModel(replies),
        executor_factory=factory,
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=events,
        evidence=evidence,
        tool_parallelism=2,
    )


def _tool_step(snapshot):
    return next(
        step
        for step in snapshot["steps"]
        if step["status"] != "late_result" and step["response"].get("kind") is None
    )


def _committed(snapshot) -> dict[int, dict]:
    try:
        step = _tool_step(snapshot)
    except StopIteration:
        return {}
    return {item["ordinal"]: item["result"] for item in step["tool_results"] or []}


def test_breakpoint3_settled_sibling_is_not_redispatched_after_resume(monkeypatch):
    state = _TransportState()
    _StuckT0Transport.state = state
    monkeypatch.setattr(fixture_module, "_CannedTransport", _StuckT0Transport)

    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    clock = DurableClock(store)
    incident, run = uuid4(), uuid4()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    input_snapshot = fixture_face().input_for(
        run_id=str(run),
        question="Why is checkout erroring?",
        target_id="checkout-prod",
        deadline=deadline,
        model_requests=5,
    )
    store.accept(
        incident,
        run,
        f"m1-332-breakpoint3-{incident}",
        deadline=deadline,
        budget_limit=10,
        versions=fixture_versions(),
        input=input_snapshot.as_json(),
    )
    factory = fixture_executor_factory(store, evidence=evidence, clock=clock)

    attempt1: dict[str, object] = {}

    def run_attempt1() -> None:
        try:
            attempt1["outcome"] = _runner(
                store, evidence, events, clock, factory, [_batch_reply()]
            ).resume(incident)
        except BaseException as exc:  # observed after release
            attempt1["error"] = exc

    thread = threading.Thread(target=run_attempt1, name="attempt-1", daemon=True)
    thread.start()
    try:
        # --- breakpoint 3: t0 in flight, t1 settled and committed ---------
        poll_deadline = time.monotonic() + SIBLING_COMMIT_WAIT_SECONDS
        committed: dict[int, dict] = {}
        while time.monotonic() < poll_deadline:
            committed = _committed(store.rebuild(incident))
            if 1 in committed:
                break
            time.sleep(0.05)
        assert 1 in committed, (
            "ordinal 1 was not committed while ordinal 0 was in flight "
            f"(waited {SIBLING_COMMIT_WAIT_SECONDS} s; #332 U-1 A)"
        )
        assert 0 not in committed
        assert thread.is_alive()
        assert state.count(0) == 1 and state.count(1) == 1

        epoch1_snapshot = store.rebuild(incident)
        epoch1 = epoch1_snapshot["run"]["epoch"]
        step_id = _tool_step(epoch1_snapshot)["step_id"]
        t1_result = committed[1]
        t1_evidence_id = t1_result["evidence_id"]
        assert isinstance(t1_evidence_id, str) and t1_evidence_id
        with store.transaction() as conn:
            row = conn.execute(
                "SELECT committed FROM opspilot_evidence WHERE evidence_id=%s",
                (t1_evidence_id,),
            ).fetchone()
        assert row is not None and row["committed"] is True

        # --- abandon attempt 1 (no lease release), let the lease expire ----
        time.sleep(LEASE_SECONDS + 0.5)
        resumed = _runner(
            store, evidence, events, clock, factory, [report_from_transcript]
        ).resume(incident)
        assert resumed.status == "published", resumed
        assert resumed.epoch != epoch1
        assert resumed.replayed_tools == 1

        assert state.count(1) == 1  # t1 never re-dispatched
        assert state.count(0) == 2  # t0: epoch 1 (stuck) + resume
        assert store.run_usage(run)["tool_operations_used"] == 3

        assert resumed.loop is not None
        assert t1_evidence_id in resumed.loop.evidence_ids
        assert t1_evidence_id in (resumed.loop.report_content or "")

        final = store.rebuild(incident)
        assert final["pending_tools"] == []
        final_rows = _committed(final)
        assert sorted(final_rows) == [0, 1]
        assert final_rows[1] == t1_result

        # --- release the abandoned attempt: its commit is fenced -----------
        state.release_t0.set()
        thread.join(timeout=15)
        assert not thread.is_alive()

        after = store.rebuild(incident)
        assert _committed(after) == final_rows
        late_keys = [
            step["logical_key"]
            for step in after["steps"]
            if step["status"] == "late_result"
        ]
        assert f"late_result:tool:{step_id}:0:e{epoch1}" in late_keys
        assert store.run_usage(run)["tool_operations_used"] == 3
        assert state.count(1) == 1 and state.count(0) == 2
    finally:
        state.release_t0.set()
        thread.join(timeout=15)
