"""Long-horizon loop recovery against real PostgreSQL (C3 §7 breakpoints).

Every case runs one attempt that dies at a chosen point -- an exception out
of the model client or the tool executor leaves exactly the rows a killed
process would -- lets the short lease lapse, and then resumes through
``InvestigationRunner`` with a fresh ``Worker`` (new owner, new epoch).
Deterministic doubles only: no model HTTP, no real data source.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import ContextError
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits
from opspilot.investigation.loop import (
    DISCIPLINE_VARIANT,
    InvestigationLoop,
    prompt_revision_versions,
)
from opspilot.investigation.runner import InvestigationRunner
from opspilot.investigation.store import DurableStepStore
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools import TransportResponse
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import WINDOW_END, WINDOW_START, body, build

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

VERSIONS = {
    **prompt_revision_versions(DISCIPLINE_VARIANT),
    "tool_schema_revision": "t1",
}
LEASE = 1


class Crash(RuntimeError):
    pass


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


def _tool_rounds(count, *, start=1):
    return [
        reply(tool_calls=[tool_call(call_id=f"call-{start + i}")], finish="tool_calls")
        for i in range(count)
    ]


def _input(run_id, *, model_requests=4, limits=M1_FROZEN_LIMITS):
    return {
        "version": "opspilot-investigation-input-v1",
        "question": "Why is checkout erroring?",
        "model_requests": model_requests,
        "limits": limits.as_json(),
        "tool_schemas": [dict(s) for s in TOOL_SCHEMAS],
        "tool_face_sha256": __import__("hashlib")
        .sha256(
            __import__("opspilot.tools.registry", fromlist=["canonical"])
            .canonical([dict(s) for s in TOOL_SCHEMAS])
            .encode()
        )
        .hexdigest(),
        "evidence_context": {
            "type": "opspilot-evidence-context-v4",
            "run_id": str(run_id),
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": {
                        "start": WINDOW_START.isoformat(),
                        "end": WINDOW_END.isoformat(),
                    },
                }
            ],
        },
        "variant_id": DISCIPLINE_VARIANT,
        "bound_target_id": None,
        "scope_facts": {},
    }


class Harness:
    """One incident/run with shared transport doubles across attempts."""

    def __init__(
        self, *, budget_limit=4, model_requests=4, input=None, deadline_minutes=5
    ):
        self.store = DurableStore(DSN)
        self.store.install()
        self.incident, self.run = uuid4(), uuid4()
        self.clock = SystemClock()
        self.deadline = datetime.now(timezone.utc) + timedelta(minutes=deadline_minutes)
        self.store.accept(
            self.incident,
            self.run,
            f"m1-loop-resume-{self.incident}",
            deadline=self.deadline,
            budget_limit=budget_limit,
            versions=VERSIONS,
            input=_input(self.run, model_requests=model_requests)
            if input is None
            else input,
        )
        self.transport_requests = 0
        self.executor_hook = None
        # The evidence store the executor registers into; the runner's
        # ``evidence`` projection pins the same rows, so both must agree.
        self.sink = None

    def executor_factory(self, lease, input):
        executor, transport, _sink, _clock = build(
            clock=self.clock,
            sink=self.sink,
            # The control generation stays the double's default: the tool
            # gateway checks it against its own control snapshot, not the
            # incident row (that fence is the store's).
            scope_overrides={"run_id": str(lease.run_id), "deadline": self.deadline},
        )
        transport.response = TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
        )
        harness = self

        class Counting:
            scope = executor.scope
            tool_seconds_used = 0.0

            def execute(self, request):
                if harness.executor_hook is not None:
                    harness.executor_hook(request)
                harness.transport_requests += 1
                return executor.execute(request)

        return Counting()

    def runner(self, replies):
        return InvestigationRunner(
            store=self.store,
            worker=Worker.create(self.store, dict(VERSIONS)),
            model=ScriptedModel(replies),
            executor_factory=self.executor_factory,
            clock=self.clock,
            lease_seconds=LEASE,
        )

    def wait_lease(self):
        time.sleep(LEASE + 0.3)

    def rows(self):
        return self.store.rebuild(self.incident)

    def live_keys(self):
        return [
            s["logical_key"]
            for s in self.rows()["steps"]
            if s["status"] != "late_result" and s["response"].get("kind") is None
        ]


def test_a_fresh_run_starts_from_its_input_snapshot_and_publishes():
    h = Harness()
    outcome = h.runner([*_tool_rounds(2), report_from_transcript]).resume(h.incident)
    assert outcome.status == "published", outcome
    assert outcome.loop.execution == "completed"
    rows = h.rows()
    assert rows["run"]["state"] == "completed"
    assert rows["conclusion"]["kind"] == "conclusion"
    assert rows["conclusion"]["conclusion"]["execution"] == "completed"
    assert "reasoning_content" not in str(rows["conclusion"])
    assert h.live_keys() == ["ctx0:round-1", "ctx0:round-2", "ctx0:round-3"]
    usage = h.store.run_usage(h.run)
    assert usage["model_requests_used"] == 3 and usage["model_seconds_used"] > 0
    # Publish acknowledged: a further resume finds nothing to do.
    assert h.runner([]).resume(h.incident).status == "already_completed"


def test_kill_before_the_model_response_commits_then_resume():
    h = Harness()
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), Crash("in flight")]).resume(h.incident)
    assert h.live_keys() == ["ctx0:round-1"]
    assert h.store.run_usage(h.run)["model_requests_used"] == 2  # slot stays occupied
    queries = h.transport_requests
    h.wait_lease()
    outcome = h.runner([*_tool_rounds(1, start=2), report_from_transcript]).resume(
        h.incident
    )
    assert outcome.status == "published" and outcome.epoch == 2
    assert outcome.loop.model_requests_used == 4  # 1 + 1 crashed + 2, never reset
    assert h.transport_requests == queries + 1  # only round 2's own query
    assert h.live_keys() == ["ctx0:round-1", "ctx0:round-2", "ctx0:round-3"]
    assert len(outcome.loop.evidence_ids) == 2


def test_kill_after_the_response_commits_before_its_tool_runs_then_resume():
    h = Harness()

    def die(request):
        h.executor_hook = None
        raise Crash("before the tool ran")

    h.executor_hook = die
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1)]).resume(h.incident)
    rows = h.rows()
    assert h.live_keys() == ["ctx0:round-1"] and len(rows["pending_tools"]) == 1
    h.wait_lease()
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "published" and outcome.replayed_tools == 1
    assert h.transport_requests == 1  # the committed plan ran exactly once
    assert outcome.loop.evidence_ids and outcome.loop.execution == "completed"
    assert h.rows()["pending_tools"] == []


def test_kill_after_the_tool_result_commits_before_the_next_call_then_resume():
    h = Harness()
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(2), Crash("before round 3 went out")]).resume(
            h.incident
        )
    queries = h.transport_requests
    h.wait_lease()
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "published" and outcome.replayed_tools == 0
    assert h.transport_requests == queries  # nothing re-queried
    assert tuple(outcome.loop.evidence_ids) == tuple(
        h.rows()["conclusion"]["conclusion"]["evidence_ids"]
    )
    assert len(outcome.loop.evidence_ids) == 2


def test_a_committed_conclusion_whose_publish_never_landed_is_published_on_resume():
    h = Harness()
    worker = Worker.create(h.store, dict(VERSIONS))
    session = worker.resume(h.incident, lease_seconds=30, renew_seconds=30)
    executor = h.executor_factory(session.lease, None)
    from opspilot.investigation.context import InvestigationInput, rebuild_transcript

    input = InvestigationInput.from_json(h.rows()["run"]["input"])
    transcript = rebuild_transcript(
        h.rows(),
        run_id=str(h.run),
        authorized_targets=executor.scope.target_ids,
        input=input,
    )
    loop = InvestigationLoop(
        model=ScriptedModel([*_tool_rounds(1), report_from_transcript]),
        executor=executor,
        store=DurableStepStore(h.store, session.lease),
        clock=h.clock,
    )
    first = loop.resume(transcript)
    assert first.final_step_id is not None and h.rows()["conclusion"] is None
    h.store.abandon(session.lease)  # the process died before publish
    outcome = h.runner([]).resume(h.incident)  # a model call here would raise
    assert outcome.status == "published"
    assert h.rows()["conclusion"] == first.conclusion
    assert h.rows()["run"]["state"] == "completed"


def test_budget_and_active_time_continue_across_attempts():
    h = Harness(budget_limit=4, model_requests=4)
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(2), Crash("dead")]).resume(h.incident)
    usage = h.store.run_usage(h.run)
    assert usage["model_requests_used"] == 3
    # The crashed request is unsettled: it counts at its reserved upper bound,
    # which is the request timeout clamped to the remaining deadline (~5 min).
    assert usage["model_seconds_used"] >= 200
    h.wait_lease()
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "published"
    # One slot was left: the resumed attempt went straight to the report.
    assert outcome.loop.model_requests_used == 4
    assert h.store.run_usage(h.run)["model_requests_used"] == 4
    assert h.rows()["conclusion"]["conclusion"]["rounds"] == 3


def test_an_exhausted_budget_after_restart_is_a_handoff_not_a_completion():
    h = Harness(budget_limit=4, model_requests=4)
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(3), Crash("dead")]).resume(h.incident)
    h.wait_lease()
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    # ADR-0005: a handoff is never published as the conclusion. The Run is
    # parked for a human, the incident stays open, and the committed
    # conclusion step keeps the reasons readable.
    assert outcome.status == "handed_off", outcome
    assert outcome.reason == "BUDGET_EXHAUSTED"
    assert outcome.loop.execution == "budget_exhausted"
    assert outcome.loop.handoff is True
    rows = h.rows()
    assert rows["conclusion"] is None
    assert rows["run"]["state"] == "waiting_human"
    assert rows["run"]["owner"] is None and rows["run"]["lease_until"] is None
    committed = [
        s["response"]["conclusion"]
        for s in rows["steps"]
        if s["response"].get("kind") == "conclusion"
    ]
    assert committed[-1]["execution"] == "budget_exhausted"
    assert committed[-1]["handoff_reasons"] == ["BUDGET_EXHAUSTED"]
    # No worker re-runs a parked Run on its own: a further resume neither
    # claims nor calls the model (a model call here would raise).
    parked = h.runner([]).resume(h.incident)
    assert parked.status == "handed_off" and parked.reason == "AWAITING_HUMAN"
    assert h.rows()["run"]["state"] == "waiting_human"
    # Human control stays open: follow_up and correct re-queue the Run.
    assert h.store.control(h.incident, 0, "follow_up", "operator", {"q": "x"}) == 1
    assert h.rows()["run"]["state"] == "queued"
    assert h.store.control(h.incident, 1, "correct", "operator", {"q": "y"}) == 2
    assert h.rows()["run"]["state"] == "queued"


def test_cancel_and_new_run_are_accepted_after_a_handoff():
    h = Harness(budget_limit=1, model_requests=1)
    # A tool plan on the last slot: the loop hands off (failed).
    outcome = h.runner([*_tool_rounds(1)]).resume(h.incident)
    assert outcome.status == "handed_off" and outcome.reason == "TOOL_PLAN_ON_FINAL"
    assert outcome.loop.execution == "failed"
    assert h.rows()["run"]["state"] == "waiting_human"
    assert h.store.control(h.incident, 0, "cancel", "operator") == 1
    assert h.rows()["run"]["state"] == "cancelled"
    new_run = uuid4()
    assert (
        h.store.new_run(
            h.incident,
            new_run,
            expected_generation=1,
            deadline=h.deadline,
            budget_limit=4,
            versions=VERSIONS,
            actor="operator",
            input=_input(new_run),
        )
        == 2
    )
    assert h.rows()["run"]["run_id"] == new_run
    outcome = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert outcome.status == "published" and h.rows()["run"]["state"] == "completed"


def test_a_handoff_conclusion_committed_before_a_crash_is_not_published_on_resume():
    """Breakpoint 4 with a handoff row: resume parks the Run, never publishes."""
    h = Harness(budget_limit=2, model_requests=2)
    worker = Worker.create(h.store, dict(VERSIONS))
    session = worker.resume(h.incident, lease_seconds=30, renew_seconds=30)
    executor = h.executor_factory(session.lease, None)
    from opspilot.investigation.context import InvestigationInput, rebuild_transcript

    input = InvestigationInput.from_json(h.rows()["run"]["input"])
    transcript = rebuild_transcript(
        h.rows(),
        run_id=str(h.run),
        authorized_targets=executor.scope.target_ids,
        input=input,
    )
    loop = InvestigationLoop(
        model=ScriptedModel(_tool_rounds(2)),
        executor=executor,
        store=DurableStepStore(h.store, session.lease),
        clock=h.clock,
    )
    first = loop.resume(transcript)
    assert first.handoff is True and first.final_step_id is not None
    h.store.abandon(session.lease)  # the process died before it could settle
    assert h.rows()["run"]["state"] == "running"
    outcome = h.runner([]).resume(h.incident)  # a model call here would raise
    assert outcome.status == "handed_off" and outcome.reason == "TOOL_PLAN_ON_FINAL"
    assert outcome.loop is None  # settled from the rows, not by a new attempt
    assert h.rows()["conclusion"] is None
    assert h.rows()["run"]["state"] == "waiting_human"


def test_a_stale_publish_cannot_land_on_a_parked_run():
    """The fence: once parked, an older attempt's conclusion is only history."""
    h = Harness(budget_limit=1, model_requests=1)
    worker = Worker.create(h.store, dict(VERSIONS))
    session = worker.resume(h.incident, lease_seconds=30, renew_seconds=30)
    conclusion = {
        "kind": "conclusion",
        "assistant": {"role": "assistant", "content": None},
        "conclusion": {"execution": "completed", "handoff": False},
    }
    step_id = DurableStepStore(h.store, session.lease).commit_step(
        "conclusion:g0:ctx0:round-1", conclusion
    )
    h.store.hand_off(session.lease)
    assert h.rows()["run"]["state"] == "waiting_human"
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        h.store.hand_off(session.lease)
    assert h.store.publish(session.lease, conclusion, step_id=step_id) is False
    rows = h.rows()
    assert rows["conclusion"] is None and rows["run"]["state"] == "waiting_human"
    late = [s for s in rows["steps"] if s["status"] == "late_result"]
    assert len(late) == 1 and late[0]["logical_key"].startswith("late_result:publish:")


def test_a_follow_up_drops_the_orphaned_plan_and_the_run_continues():
    h = Harness()

    def die(request):
        h.executor_hook = None
        raise Crash("before the tool ran")

    h.executor_hook = die
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1)]).resume(h.incident)
    h.wait_lease()
    generation = h.store.control(h.incident, 0, "follow_up", "operator")
    assert generation == 1 and h.rows()["pending_tools"] == []
    outcome = h.runner([*_tool_rounds(1, start=2), report_from_transcript]).resume(
        h.incident
    )
    assert outcome.status == "published" and outcome.replayed_tools == 0
    assert h.transport_requests == 1  # the orphaned plan was never executed
    assert h.live_keys() == ["ctx0:round-1", "ctx0:round-2", "ctx0:round-3"]
    assert len(outcome.loop.evidence_ids) == 1


def test_a_late_commit_from_a_fenced_attempt_is_history_not_transcript():
    h = Harness()

    def slow_reply(call):
        time.sleep(LEASE + 0.5)  # the lease lapses while the request is in flight
        return _tool_rounds(1)[0]

    outcome = h.runner([slow_reply]).resume(h.incident)
    # The fenced attempt can neither publish nor park the Run it no longer
    # holds: a control outcome, and the Run stays claimable.
    assert outcome.status == "control_denied" and outcome.loop.handoff_reasons == (
        "CONTROL_DENIED",
    )
    assert h.rows()["run"]["state"] == "running"
    statuses = [s["status"] for s in h.rows()["steps"]]
    assert statuses == ["late_result"]
    second = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert second.status == "published"
    assert h.live_keys() == ["ctx0:round-1", "ctx0:round-2"]


def test_a_malformed_input_snapshot_blocks_the_run_durably():
    h = Harness(input={"version": "bogus"})
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "INPUT_INVALID"
    assert h.rows()["run"]["state"] == "blocked"
    again = h.runner([report_from_transcript]).resume(h.incident)
    assert again.status == "control_denied"


def test_a_malformed_committed_step_blocks_the_run_durably_instead_of_crashing():
    """Bot review (PR #29, comment 4068683858): rows this version cannot decode
    are met before any lease exists; the Run must still reach ``blocked``."""
    h = Harness()
    with h.store.transaction() as conn:
        from psycopg.types.json import Jsonb

        conn.execute(
            "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation) VALUES(%s,%s,0,'ctx0:round-1','response_committed',%s,0)",
            (uuid4(), h.run, Jsonb({"assistant": "corrupt"})),
        )
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "INCONSISTENT_STATE"
    assert outcome.epoch is not None and h.transport_requests == 0
    assert h.store.recovery_metadata(h.incident)["run_state"] == "blocked"
    again = h.runner([report_from_transcript]).resume(h.incident)
    assert again.status == "control_denied"


def test_a_malformed_pending_tool_call_blocks_the_run_instead_of_crashing():
    """Bot review finding: ``DurableStore.rebuild()`` only checks that a
    committed tool plan is a list of dicts (``INCONSISTENT_STATE`` for any
    other shape); a call shaped like ``{}`` passes that check, so it reaches
    this replay before ``rebuild_transcript()`` gets a chance to run the
    same ``validate_tool_calls()`` the live loop uses. Unvalidated,
    ``tool_request_for()`` would crash on a bare ``KeyError`` reading
    ``function`` instead of a durable handoff -- unlike the sibling test
    above (an ``assistant`` this version cannot decode at all, caught before
    any lease exists), this row decodes fine and is only caught by shape
    validation right before the pending-tool replay."""
    h = Harness()
    with h.store.transaction() as conn:
        from psycopg.types.json import Jsonb

        conn.execute(
            "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation) VALUES(%s,%s,0,'ctx0:round-1','response_committed',%s,0)",
            (
                uuid4(),
                h.run,
                Jsonb(
                    {
                        "assistant": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{}],
                        }
                    }
                ),
            ),
        )
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "INCONSISTENT_STATE"
    assert outcome.epoch is not None and h.transport_requests == 0
    assert h.store.recovery_metadata(h.incident)["run_state"] == "blocked"
    again = h.runner([report_from_transcript]).resume(h.incident)
    assert again.status == "control_denied"


def test_an_undecodable_run_of_another_version_is_reported_as_blocked():
    """Independent review (PR #29): when the claim inside the undecodable path
    blocks the Run for a version mismatch, the outcome says ``blocked``, which
    is what the durable state is."""
    h = Harness()
    with h.store.transaction() as conn:
        from psycopg.types.json import Jsonb

        conn.execute(
            "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation) VALUES(%s,%s,0,'ctx0:round-1','response_committed',%s,0)",
            (uuid4(), h.run, Jsonb({"assistant": "corrupt"})),
        )
    runner = h.runner([report_from_transcript])
    runner.worker = Worker.create(h.store, {**VERSIONS, "prompt_revision": "other"})
    outcome = runner.resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "INCOMPATIBLE_STATE"
    assert h.store.recovery_metadata(h.incident)["run_state"] == "blocked"


def test_an_executor_that_cannot_be_built_releases_the_lease():
    """Bot review (PR #29, comment 4069202731): a typed factory failure after
    the claim is an explicit outcome, and the lease is released rather than
    held to expiry."""
    from opspilot.tools.registry import ToolContractError

    h = Harness()
    runner = h.runner([*_tool_rounds(1), report_from_transcript])

    def broken_factory(lease, input):
        raise ToolContractError("LEDGER_UNAVAILABLE")

    runner.executor_factory = broken_factory
    outcome = runner.resume(h.incident)
    assert outcome.status == "aborted" and outcome.reason == "LEDGER_UNAVAILABLE"
    assert h.live_keys() == []
    # Released: the next attempt claims at once, without waiting the lease out.
    again = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert again.status == "published" and again.epoch == outcome.epoch + 1


def test_a_failed_usage_read_after_the_claim_releases_the_lease(monkeypatch):
    """Bot review (PR #29, comment 4069386104): a StepStoreError while the
    attempt opens is an explicit outcome and the lease is released."""
    from opspilot.investigation.store import DurableStepStore, StepStoreError

    def unavailable(self):
        raise StepStoreError("STORAGE_UNAVAILABLE")

    h = Harness()
    monkeypatch.setattr(DurableStepStore, "usage", unavailable)
    outcome = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert outcome.status == "aborted" and outcome.reason == "STORAGE_UNAVAILABLE"
    monkeypatch.undo()
    again = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert again.status == "published" and again.epoch == outcome.epoch + 1


def test_limits_above_the_freeze_are_refused_at_the_product_boundary():
    run = uuid4()
    wide = RunLimits(model_requests=8)
    h = Harness(input=_input(run, model_requests=8, limits=wide))
    # The snapshot names a different run id than the row; fix that first so the
    # only refusal left is the freeze.
    snapshot = _input(h.run, model_requests=8, limits=wide)
    with h.store.transaction() as conn:
        from psycopg.types.json import Jsonb

        conn.execute(
            "UPDATE opspilot_runs SET input=%s WHERE run_id=%s",
            (Jsonb(snapshot), h.run),
        )
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "LIMITS_EXCEED_FREEZE"
    assert h.rows()["run"]["state"] == "blocked"


def test_new_run_records_its_own_input_snapshot():
    h = Harness()
    h.store.control(h.incident, 0, "cancel", "operator")
    replacement = uuid4()
    h.store.new_run(
        h.incident,
        replacement,
        expected_generation=1,
        deadline=h.deadline,
        budget_limit=4,
        versions=VERSIONS,
        actor="operator",
        input=_input(replacement),
    )
    outcome = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert outcome.status == "published"
    assert str(h.rows()["run"]["run_id"]) == str(replacement)


def test_context_errors_are_fixed_codes():
    with pytest.raises(ContextError, match="INVALID_INPUT"):
        from opspilot.investigation.context import step_key

        step_key("", 1)
    with pytest.raises(PersistenceError):
        DurableStore(DSN).run_usage(UUID(int=0))


def test_a_follow_up_after_an_unpublished_conclusion_lets_the_run_continue():
    """Review P2-D: an old-generation conclusion row must not block publish forever."""
    h = Harness()
    worker = Worker.create(h.store, dict(VERSIONS))
    session = worker.resume(h.incident, lease_seconds=30, renew_seconds=30)
    executor = h.executor_factory(session.lease, None)
    from opspilot.investigation.context import InvestigationInput, rebuild_transcript

    input = InvestigationInput.from_json(h.rows()["run"]["input"])
    transcript = rebuild_transcript(
        h.rows(),
        run_id=str(h.run),
        authorized_targets=executor.scope.target_ids,
        input=input,
    )
    loop = InvestigationLoop(
        # One tool round, then a malformed plan: the Run hands off after two
        # slots, leaving budget for the continuation.
        model=ScriptedModel(
            [*_tool_rounds(1), reply(tool_calls=[tool_call()], finish="stop")]
        ),
        executor=executor,
        store=DurableStepStore(h.store, session.lease),
        clock=h.clock,
    )
    first = loop.resume(transcript)
    assert first.execution == "failed" and first.final_step_id is not None
    assert first.model_requests_used == 2
    h.store.abandon(session.lease)
    h.store.control(h.incident, 0, "follow_up", "operator")
    outcome = h.runner([*_tool_rounds(1, start=2), report_from_transcript]).resume(
        h.incident
    )
    assert outcome.status == "published", outcome
    assert outcome.loop.execution == "completed"
    kinds = [s["response"].get("kind") for s in h.rows()["steps"]]
    assert kinds.count("conclusion") == 2  # the old one stays as history


def test_a_rejected_plan_is_never_replayed_by_recovery():
    """Review P2-B: a plan the loop refused must not surface as pending work."""
    h = Harness()
    outcome = h.runner([reply(tool_calls=[tool_call()], finish="stop")]).resume(
        h.incident
    )
    assert outcome.status == "handed_off"
    assert outcome.loop.handoff_reasons == ("TOOL_PAIRING_INVALID",)
    assert h.transport_requests == 0
    rows = h.rows()
    assert rows["pending_tools"] == []
    step = next(s for s in rows["steps"] if s["response"].get("kind") is None)
    assert step["response"]["rejected_plan"]["reason"] == "TOOL_PAIRING_INVALID"


def test_a_run_replaced_between_rebuild_and_claim_runs_with_its_own_input():
    """Bot review (PR #29, comment 4067523562): the leased Run's input, never the
    earlier snapshot's, is what the attempt runs with."""
    h = Harness()
    replacement = uuid4()
    replaced_input = _input(replacement)
    replaced_input["question"] = "REPLACEMENT QUESTION: what changed in checkout?"
    runner = h.runner([*_tool_rounds(1), report_from_transcript])
    real_resume = runner.worker.resume

    def resume_after_swap(incident_id, **kwargs):
        h.store.control(incident_id, 0, "cancel", "operator")
        h.store.new_run(
            incident_id,
            replacement,
            expected_generation=1,
            deadline=h.deadline,
            budget_limit=4,
            versions=VERSIONS,
            actor="operator",
            input=replaced_input,
        )
        return real_resume(incident_id, **kwargs)

    runner.worker.resume = resume_after_swap  # type: ignore[method-assign]
    outcome = runner.resume(h.incident)
    assert outcome.status == "published", outcome
    sent = runner.model.calls[0].messages
    assert sent[1]["content"] == replaced_input["question"]
    rows = h.rows()
    assert str(rows["run"]["run_id"]) == str(replacement)
    assert all(str(s["run_id"]) == str(replacement) for s in rows["steps"])


def test_a_follow_up_between_rounds_then_a_restart_resumes_and_publishes():
    """Independent review (PR #31, P1) on real rows: a human follow_up lands
    after round 1 committed (it fences the running attempt), the next attempt
    sends it as the round's trailing message and dies, and the attempt after
    that must rebuild the exact bytes round 2 sent -- inputs included -- and
    finish. Also exercises the stale-boundary re-freeze: attempt 1 had already
    frozen ``ctx0:round-1`` under the old generation with no input. Four
    physical requests in total, the frozen ceiling."""
    h = Harness()

    def follow_up_then_die(call):
        # Round 1's request is in flight (its boundary is frozen at watermark
        # 0 under generation 0); the operator asks a follow-up question.
        assert (
            h.store.control(
                h.incident, 0, "follow_up", "operator", {"question": "and payments?"}
            )
            == 1
        )
        raise Crash("in flight while the follow-up landed")

    with pytest.raises(Crash):
        h.runner([follow_up_then_die]).resume(h.incident)
    assert h.live_keys() == []
    rows = h.rows()
    assert [x["sequence"] for x in rows["pending_inputs"]] == [1]
    assert [
        (x["logical_key"], x["control_generation"], x["input_watermark"])
        for x in rows["input_rounds"]
    ] == [("ctx0:round-1", 0, 0)]
    # The follow_up released the lease (owner cleared): no wait needed.
    sent: list = []

    def record_then_tool_round(call):
        sent.append(list(call.messages))
        return _tool_rounds(1)[0]

    with pytest.raises(Crash):
        h.runner([record_then_tool_round, Crash("again")]).resume(h.incident)
    assert h.live_keys() == ["ctx0:round-1"]
    trailing = sent[0][-1]
    assert trailing["role"] == "user"
    assert '"question":"and payments?"' in trailing["content"]
    rows = h.rows()
    round_one = next(s for s in rows["steps"] if s["logical_key"] == "ctx0:round-1")
    assert round_one["control_generation"] == 1
    assert round_one["response"]["context"]["input_watermark"] == 1
    assert rows["run"]["input_watermark"] == 1 and rows["pending_inputs"] == []
    assert [
        (
            x["logical_key"],
            x["control_generation"],
            x["input_watermark"],
            x["committed"],
        )
        for x in rows["input_rounds"]
    ] == [("ctx0:round-1", 1, 1, True), ("ctx0:round-2", 1, 1, False)]
    h.wait_lease()
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "published", outcome
    assert outcome.loop.execution == "completed"
    assert h.rows()["run"]["state"] == "completed"
    assert len(outcome.loop.evidence_ids) == 1
    assert h.transport_requests == 1  # round 1's query ran once, never replayed


# -- workbench live progress through the real driver (ADR-0005 / ROADMAP item 1)


def _event_log(store):
    from opspilot.web import DurableEventLog

    log = DurableEventLog(store)
    log.install()
    return log


def _kinds(log, incident):
    return [e.kind for e in log.read_after(incident, 0, limit=1000)]


def test_the_runner_announces_progress_and_completion_to_the_workbench():
    h = Harness()
    log = _event_log(h.store)
    runner = h.runner([*_tool_rounds(1), report_from_transcript])
    runner.events = log
    outcome = runner.resume(h.incident)
    assert outcome.status == "published"
    events = log.read_after(h.incident, 0, limit=1000)
    assert [e.kind for e in events] == [
        "run_claimed",
        "step_committed",
        "tool_committed",
        "step_committed",
        "run_completed",
    ]
    assert all(e.payload["run_id"] == str(h.run) for e in events)
    assert events[0].payload["epoch"] == 1
    assert events[1].payload["logical_key"] == "ctx0:round-1"
    assert events[1].payload["planned_tools"] == 1
    assert events[2].payload["ordinal"] == 0 and events[2].payload["evidence_id"]
    assert events[-1].payload["published"] is True
    assert events[-1].payload["handoff"] is False
    assert events[-1].payload["report_sha256"] == outcome.loop.report_content_sha256
    # Announcing completion is idempotent per run: a resume after publish
    # neither claims nor announces again.
    again = h.runner([])
    again.events = log
    assert again.resume(h.incident).status == "already_completed"
    assert _kinds(log, h.incident).count("run_completed") == 1


def test_the_runner_announces_a_handoff_and_the_page_reads_it_back():
    from opspilot.web import (
        DurableEvidenceStore,
        DurableIncidentStore,
        DurableWebLedger,
        Workbench,
    )

    h = Harness(budget_limit=2, model_requests=2)
    log = _event_log(h.store)
    evidence = DurableEvidenceStore(h.store)
    evidence.install()
    ledger = DurableWebLedger(h.store)
    ledger.install()
    h.sink = evidence
    runner = h.runner(_tool_rounds(2))
    runner.events = log
    runner.evidence = evidence
    outcome = runner.resume(h.incident)
    assert outcome.status == "handed_off" and outcome.reason == "TOOL_PLAN_ON_FINAL"
    kinds = _kinds(log, h.incident)
    assert kinds[0] == "run_claimed" and kinds[-1] == "run_handoff"
    # Round 1 ran its tool; round 2's plan was rejected (final slot), so it
    # committed a step but no tool result.
    assert kinds.count("step_committed") == 2 and kinds.count("tool_committed") == 1
    last = log.read_after(h.incident, 0, limit=1000)[-1]
    assert last.payload["reasons"] == ["TOOL_PLAN_ON_FINAL"]
    assert last.payload["published"] is False
    assert len(last.payload["evidence_ids"]) == 1
    workbench = Workbench(
        incidents=DurableIncidentStore(h.store),
        events=log,
        evidence=evidence,
        ledger=ledger,
        run_versions=dict(VERSIONS),
    )
    snapshot = workbench.snapshot(h.incident)
    assert snapshot["run"]["state"] == "waiting_human"
    assert snapshot["report"] is None
    assert snapshot["outcome"]["handoff"] is True
    assert snapshot["outcome"]["reasons"] == ["TOOL_PLAN_ON_FINAL"]
    assert len(snapshot["steps"]) == 3  # two rounds + the conclusion step
    # Evidence committed through the runner is readable from the page.
    for evidence_id in last.payload["evidence_ids"]:
        assert workbench.evidence_for(h.incident, evidence_id) is not None
    # The poll that finds a parked Run announces nothing new.
    parked = h.runner([])
    parked.events = log
    assert parked.resume(h.incident).status == "handed_off"
    assert _kinds(log, h.incident)[-1] == "run_handoff"


def test_a_refused_claim_of_a_runnable_run_is_one_event_not_an_attempt():
    # A queued Run whose deadline already passed looks runnable in the rows
    # but the claim refuses it: that refusal is news for the page.
    h = Harness(deadline_minutes=0)
    log = _event_log(h.store)
    runner = h.runner([])
    runner.events = log
    outcome = runner.resume(h.incident)
    assert outcome.status == "control_denied" and outcome.reason == "DEADLINE_EXCEEDED"
    events = log.read_after(h.incident, 0, limit=1000)
    assert [e.kind for e in events] == ["run_claim_refused"]
    assert events[0].payload == {"run_id": str(h.run), "code": "DEADLINE_EXCEEDED"}
    # A paused incident is refused on every poll by design: no event per
    # poll (the queued Run row itself is left as is by ``control()``).
    h.store.control(h.incident, 0, "pause", "operator")
    assert h.rows()["state"] == "paused" and h.rows()["run"]["state"] == "queued"
    for _ in range(3):
        assert runner.resume(h.incident).status == "control_denied"
    assert _kinds(log, h.incident) == ["run_claim_refused"]


def test_an_evidence_projection_the_executor_did_not_register_into_hands_off():
    """Independent review P2-2: a projection mismatch is a visible handoff."""
    from opspilot.web import DurableEvidenceStore

    h = Harness()
    log = _event_log(h.store)
    other = DurableEvidenceStore(h.store)
    other.install()
    # ``h.sink`` stays the transport double's own sink: the runner's
    # projection cannot find the rows the executor registered.
    runner = h.runner([*_tool_rounds(1), report_from_transcript])
    runner.events = log
    runner.evidence = other
    outcome = runner.resume(h.incident)
    assert outcome.status == "handed_off"
    assert outcome.reason == "EVIDENCE_PROJECTION_FAILED"
    assert h.rows()["run"]["state"] == "waiting_human"
    assert _kinds(log, h.incident)[-1] == "run_handoff"
