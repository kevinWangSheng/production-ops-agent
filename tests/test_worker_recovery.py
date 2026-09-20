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


def test_resume_refreshes_pending_plan_after_claim():
    """A same-generation handoff must use rows committed before the new claim."""
    incident_id, run_id = uuid4(), uuid4()
    lease = Lease(incident_id, run_id, uuid4(), 2, 0)
    old_plan = {
        "incident_id": incident_id,
        "control_generation": 0,
        "run": {"run_id": run_id, "state": "queued"},
        "steps": [],
        "pending_tools": [{"step_id": uuid4(), "ordinal": 0}],
        "conclusion": None,
    }
    refreshed = dict(old_plan)
    refreshed["run"] = {"run_id": run_id, "state": "running"}
    refreshed["pending_tools"] = [{"step_id": uuid4(), "ordinal": 1}]

    class Store:
        def __init__(self):
            self.snapshots = [old_plan, refreshed]
            self.rebuild_calls = []
            self.events = []

        def rebuild(self, _incident_id):
            snapshot = self.snapshots.pop(0)
            self.rebuild_calls.append(snapshot)
            self.events.append("rebuild")
            return snapshot

        def claim(self, *_args, **_kwargs):
            self.events.append("claim")
            return lease

        def abandon(self, _lease):
            raise AssertionError("the refreshed plan should still be current")

    store = Store()
    session = Worker.create(store, {"state": "v1"}).resume(incident_id)
    assert old_plan != refreshed
    assert store.events == ["rebuild", "claim", "rebuild"]
    assert store.rebuild_calls == [old_plan, refreshed]
    assert session.plan.pending_tools[0]["ordinal"] == 1
    assert session.plan.run["state"] == "running"


def test_resume_releases_lease_when_post_claim_refresh_fails():
    incident_id, run_id = uuid4(), uuid4()
    lease = Lease(incident_id, run_id, uuid4(), 2, 0)
    snapshot = {
        "incident_id": incident_id,
        "control_generation": 0,
        "run": {"run_id": run_id, "state": "queued"},
        "steps": [],
        "pending_tools": [],
        "conclusion": None,
    }

    class Store:
        def __init__(self):
            self.rebuild_count = 0
            self.abandoned = []

        def rebuild(self, _incident_id):
            self.rebuild_count += 1
            if self.rebuild_count == 2:
                raise PersistenceError("TIMEOUT")
            return snapshot

        def claim(self, *_args, **_kwargs):
            return lease

        def abandon(self, value):
            self.abandoned.append(value)

    store = Store()
    with pytest.raises(PersistenceError, match="TIMEOUT"):
        Worker.create(store, {"state": "v1"}).resume(incident_id)
    assert store.abandoned == [lease]


def test_resume_preserves_refresh_error_when_lease_release_also_fails():
    incident_id, run_id = uuid4(), uuid4()
    lease = Lease(incident_id, run_id, uuid4(), 2, 0)
    snapshot = {
        "incident_id": incident_id,
        "control_generation": 0,
        "run": {"run_id": run_id, "state": "queued"},
        "steps": [],
        "pending_tools": [],
        "conclusion": None,
    }

    class Store:
        def __init__(self):
            self.rebuild_count = 0

        def rebuild(self, _incident_id):
            self.rebuild_count += 1
            if self.rebuild_count == 2:
                raise PersistenceError("STORAGE_UNAVAILABLE")
            return snapshot

        def claim(self, *_args, **_kwargs):
            return lease

        def abandon(self, _lease):
            raise PersistenceError("LEASE_RELEASE_FAILED")

    with pytest.raises(PersistenceError, match="STORAGE_UNAVAILABLE"):
        Worker.create(Store(), {"state": "v1"}).resume(incident_id)


def test_the_initial_claim_defaults_to_the_same_length_as_renewal():
    """Renewal only runs after a tool executes (between execute and commit),
    so the *first* pending tool's own execution is covered only by the
    initial claim, not by any renewal. A short initial claim (the pre-#35
    lease_seconds=30) would defeat the renewal wiring for that first tool;
    the defaults must match so it is covered too.
    """
    import inspect

    from opspilot.worker import DEFAULT_LEASE_SECONDS

    assert (
        inspect.signature(Worker.claim).parameters["lease_seconds"].default
        == DEFAULT_LEASE_SECONDS
    )
    assert (
        inspect.signature(Worker.resume).parameters["lease_seconds"].default
        == DEFAULT_LEASE_SECONDS
    )
    assert (
        inspect.signature(Worker.resume).parameters["renew_seconds"].default
        == DEFAULT_LEASE_SECONDS
    )
    assert (
        inspect.signature(RecoverySession).parameters["renew_seconds"].default
        == DEFAULT_LEASE_SECONDS
    )


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
