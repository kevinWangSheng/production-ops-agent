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

    def executor_factory(self, lease, input):
        executor, transport, _sink, _clock = build(
            clock=self.clock,
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
    assert outcome.status == "published"
    assert outcome.loop.execution == "budget_exhausted"
    assert outcome.loop.handoff is True
    assert h.rows()["conclusion"]["conclusion"]["execution"] == "budget_exhausted"
    assert h.rows()["conclusion"]["conclusion"]["handoff_reasons"] == [
        "BUDGET_EXHAUSTED"
    ]


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
    assert outcome.status == "unpublished" and outcome.loop.handoff_reasons == (
        "CONTROL_DENIED",
    )
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
    assert outcome.status == "published"
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
