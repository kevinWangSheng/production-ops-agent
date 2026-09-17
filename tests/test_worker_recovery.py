from uuid import UUID, uuid4

import pytest

from opspilot.persistence import Lease, PersistenceError
from opspilot.recovery import rebuild_plan
from opspilot.worker import RecoverySession, Worker


def test_rebuild_plan_only_resumes_committed_pending_work():
    run = uuid4()
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": run, "state": "running"},
            "steps": [{"step_id": uuid4()}],
            "pending_tools": [{"ordinal": 1}],
            "conclusion": None,
        }
    )
    assert plan.candidate
    assert plan.pending_tools == ({"ordinal": 1},)


def test_pending_tool_keeps_stable_operation_and_call_details():
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "running"},
            "steps": [],
            "pending_tools": [
                {
                    "step_id": uuid4(),
                    "ordinal": 0,
                    "operation_id": "s:0",
                    "tool_call": {"name": "query", "arguments": {"x": 1}},
                }
            ],
            "conclusion": None,
        }
    )
    assert plan.pending_tools[0]["operation_id"] == "s:0"
    assert plan.pending_tools[0]["tool_call"]["name"] == "query"


def test_recovery_plan_nested_values_are_immutable():
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "running"},
            "steps": [],
            "pending_tools": [{"ordinal": 0, "tool_call": {"name": "query"}}],
            "conclusion": None,
        }
    )
    try:
        plan.pending_tools[0]["tool_call"]["name"] = "mutated"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("recovery plan must be immutable")


def test_terminal_plan_is_not_resumable():
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "blocked"},
            "steps": [],
            "pending_tools": [],
            "conclusion": None,
        }
    )
    assert not plan.candidate


def test_worker_uses_unique_owner():
    worker = Worker.create(object(), {"schema": "v1"})
    assert worker.owner


def _pending_plan(run_id: UUID, pending: list[dict]):
    return rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": run_id, "state": "running"},
            "steps": [],
            "pending_tools": pending,
            "conclusion": None,
        }
    )


class _RecordingStore:
    """Minimal double for the store seam RecoverySession touches.

    ``renew_lease`` is attached only when ``with_renew`` is set, mirroring
    that ``DurableStore.renew_lease`` (PR #35) is an optional capability on
    this branch: absent, ``getattr`` in ``RecoverySession._renew`` finds
    nothing and the call is a no-op.
    """

    def __init__(
        self,
        *,
        run_id: UUID,
        with_renew: bool = False,
        deny_after: int | None = None,
        order: list[str] | None = None,
    ) -> None:
        self._run_id = run_id
        self._deny_after = deny_after
        self._order = order if order is not None else []
        self.commit_calls: list[tuple[UUID, int, dict]] = []
        self.renew_calls: list[tuple[Lease, int]] = []
        if with_renew:
            self.renew_lease = self._renew_lease  # type: ignore[method-assign]

    def lease_current(self, lease: Lease) -> bool:
        return True

    def rebuild(self, incident_id: UUID) -> dict:
        return {"control_generation": 0, "run": {"run_id": self._run_id}}

    def commit_tool(
        self, lease: Lease, step_id: UUID, ordinal: int, result: dict
    ) -> None:
        self._order.append("commit")
        self.commit_calls.append((step_id, ordinal, dict(result)))

    def _renew_lease(self, lease: Lease, extend_seconds: int) -> None:
        self._order.append("renew")
        self.renew_calls.append((lease, extend_seconds))
        if self._deny_after is not None and len(self.renew_calls) >= self._deny_after:
            raise PersistenceError("CONTROL_DENIED")


def test_execute_pending_is_unchanged_when_the_store_has_no_renew_lease():
    """Optional-capability fallback: no ``renew_lease`` on the store, no call."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    store = _RecordingStore(run_id=run_id, with_renew=False)
    assert not hasattr(store, "renew_lease")
    step_id = uuid4()
    plan = _pending_plan(run_id, [{"step_id": step_id, "ordinal": 0, "tool_call": {}}])
    session = RecoverySession(plan, lease, store)  # type: ignore[arg-type]

    assert session.execute_pending(lambda item: {"ok": True}) == 1
    assert store.commit_calls == [(step_id, 0, {"ok": True})]


def test_execute_pending_renews_between_execute_and_commit_when_available():
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    order: list[str] = []
    store = _RecordingStore(run_id=run_id, with_renew=True, order=order)
    step_id = uuid4()
    plan = _pending_plan(run_id, [{"step_id": step_id, "ordinal": 0, "tool_call": {}}])
    session = RecoverySession(plan, lease, store, renew_seconds=99)  # type: ignore[arg-type]

    def execute(item: dict) -> dict:
        order.append("execute")
        return {"ok": True}

    assert session.execute_pending(execute) == 1
    assert order == ["execute", "renew", "commit"]
    assert store.renew_calls == [(lease, 99)]


def test_renewal_rejection_stops_before_the_tool_result_is_committed():
    """Same rejection semantics as the rest of the module: PersistenceError."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    store = _RecordingStore(run_id=run_id, with_renew=True, deny_after=1)
    plan = _pending_plan(run_id, [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}])
    session = RecoverySession(plan, lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        session.execute_pending(lambda item: {"ok": True})

    assert store.commit_calls == []
