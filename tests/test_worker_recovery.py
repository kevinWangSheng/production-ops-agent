import threading
import time
from uuid import UUID, uuid4

import pytest

from opspilot.domain import tool_operation_id
from opspilot.persistence import Lease, PersistenceError
from opspilot.recovery import rebuild_plan
from opspilot.worker import RecoverySession, Worker


def test_rebuild_plan_only_resumes_committed_pending_work():
    run, step_id = uuid4(), uuid4()
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": run, "state": "running"},
            "steps": [{"step_id": step_id}],
            "pending_tools": [{"step_id": step_id, "ordinal": 1}],
            "conclusion": None,
        }
    )
    assert plan.candidate
    assert [item["ordinal"] for item in plan.pending_tools] == [1]


def test_pending_tool_carries_the_canonical_operation_id():
    """A replayed call must present the id executors deduplicate by, or a
    restart looks like a brand new operation and splits its evidence."""
    step_id = uuid4()
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "running"},
            "steps": [],
            "pending_tools": [
                {
                    "step_id": step_id,
                    "ordinal": 0,
                    "tool_call": {"name": "query", "arguments": {"x": 1}},
                }
            ],
            "conclusion": None,
        }
    )
    assert plan.pending_tools[0]["operation_id"] == tool_operation_id(str(step_id), 0)
    assert plan.pending_tools[0]["operation_id"] == f"{step_id}#0"
    assert plan.pending_tools[0]["tool_call"]["name"] == "query"


def test_recovery_plan_nested_values_are_immutable():
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "running"},
            "steps": [],
            "pending_tools": [
                {"step_id": uuid4(), "ordinal": 0, "tool_call": {"name": "query"}}
            ],
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
    """``execute_pending`` renews before dispatching each tool and
    ``renew_lease`` never shortens a lease, so it is the renewal -- not this
    default -- that covers a tool's own execution. Equal defaults still matter
    for the window from claim to that first renewal, and for a store double
    with no ``renew_lease`` where the initial claim is the only cover.
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

    ``rebuild`` serves a durable pending set that ``commit_tool`` consumes,
    mirroring ``DurableStore``: a committed ordinal stops being listed as
    pending. ``renew_lease`` is attached only when ``with_renew`` is set,
    mirroring the minimal store doubles that can be passed to
    ``RecoverySession``: absent, ``getattr`` in ``RecoverySession._renew``
    finds nothing and the call is a no-op.
    """

    def __init__(
        self,
        *,
        run_id: UUID,
        pending: list[dict],
        with_renew: bool = False,
        deny_after: int | None = None,
        order: list[str] | None = None,
    ) -> None:
        self._run_id = run_id
        self._pending = [dict(item) for item in pending]
        self._deny_after = deny_after
        self._order = order if order is not None else []
        self.commit_calls: list[tuple[UUID, int, dict]] = []
        self.renew_calls: list[tuple[Lease, int]] = []
        self.abandon_calls: list[Lease] = []
        self.abandon_error: Exception | None = None
        self.renew_error: Exception | None = None
        self.commit_error: Exception | None = None
        if with_renew:
            self.renew_lease = self._renew_lease  # type: ignore[method-assign]

    def lease_current(self, lease: Lease) -> bool:
        return True

    def rebuild(self, incident_id: UUID) -> dict:
        return {
            "control_generation": 0,
            "run": {"run_id": self._run_id},
            "pending_tools": [dict(item) for item in self._pending],
        }

    def commit_tool(
        self, lease: Lease, step_id: UUID, ordinal: int, result: dict
    ) -> None:
        self._order.append("commit")
        if self.commit_error is not None:
            raise self.commit_error
        self.commit_calls.append((step_id, ordinal, dict(result)))
        self._pending = [
            item
            for item in self._pending
            if (item["step_id"], int(item["ordinal"])) != (step_id, ordinal)
        ]

    def _renew_lease(self, lease: Lease, extend_seconds: int) -> None:
        self._order.append("renew")
        self.renew_calls.append((lease, extend_seconds))
        if self.renew_error is not None:
            raise self.renew_error
        if self._deny_after is not None and len(self.renew_calls) >= self._deny_after:
            raise PersistenceError("CONTROL_DENIED")

    def abandon(self, lease: Lease) -> None:
        self.abandon_calls.append(lease)
        if self.abandon_error is not None:
            raise self.abandon_error


def test_execute_pending_is_unchanged_when_the_store_has_no_renew_lease():
    """Optional-capability fallback: no ``renew_lease`` on the store, no call."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    step_id = uuid4()
    pending = [{"step_id": step_id, "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=False)
    assert not hasattr(store, "renew_lease")
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    assert session.execute_pending(lambda item: {"ok": True}) == 1
    assert store.commit_calls == [(step_id, 0, {"ok": True})]


def test_execute_pending_renews_before_dispatch_and_before_commit():
    """The lease must cover the in-flight call, not only the commit after it."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    order: list[str] = []
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(
        run_id=run_id, pending=pending, with_renew=True, order=order
    )
    session = RecoverySession(_pending_plan(run_id, pending), lease, store, 99)  # type: ignore[arg-type]

    def execute(item: dict) -> dict:
        order.append("execute")
        return {"ok": True}

    assert session.execute_pending(execute) == 1
    assert order == ["renew", "execute", "renew", "commit"]
    assert store.renew_calls == [(lease, 99), (lease, 99)]


def test_a_denied_renewal_stops_before_the_tool_is_dispatched():
    """Same rejection semantics as the rest of the module: PersistenceError.

    The pre-dispatch renewal is the first fence a lapsed lease meets, so the
    external call never runs and no result is committed.
    """
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(
        run_id=run_id, pending=pending, with_renew=True, deny_after=1
    )
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]
    executed: list[dict] = []

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        session.execute_pending(lambda item: executed.append(item) or {"ok": True})

    assert executed == []
    assert store.commit_calls == []
    assert store.abandon_calls == [lease]


def test_a_denied_renewal_after_execution_stops_before_the_commit():
    """A lease revoked while the callback ran must not reach ``commit_tool``."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(
        run_id=run_id, pending=pending, with_renew=True, deny_after=2
    )
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        session.execute_pending(lambda item: {"ok": True})

    assert store.commit_calls == []
    assert store.abandon_calls == [lease]


def test_renew_failure_releases_lease_and_preserves_original_error():
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    failure = PersistenceError("TIMEOUT")
    store.renew_error = failure
    store.abandon_error = PersistenceError("LEASE_RELEASE_FAILED")
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="TIMEOUT") as caught:
        session.execute_pending(lambda _item: {"ok": True})

    assert caught.value is failure
    assert store.abandon_calls == [lease]
    assert store.commit_calls == []


def test_executor_failure_releases_lease_and_preserves_original_error():
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    step_id = uuid4()
    pending = [{"step_id": step_id, "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    failure = RuntimeError("tool transport failed")
    with pytest.raises(RuntimeError, match="tool transport failed") as caught:
        session.execute_pending(lambda _item: (_ for _ in ()).throw(failure))

    assert caught.value is failure
    assert store.abandon_calls == [lease]
    # Only the pre-dispatch renewal ran; nothing was committed.
    assert store.renew_calls == [(lease, 420)]
    assert store.commit_calls == []


def test_executor_failure_does_not_mask_a_failed_lease_release():
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    store.abandon_error = PersistenceError("STORAGE_UNAVAILABLE")
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="executor failed"):
        session.execute_pending(
            lambda _item: (_ for _ in ()).throw(RuntimeError("executor failed"))
        )

    assert store.abandon_calls == [lease]


def test_commit_failure_releases_lease_and_preserves_storage_error():
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    failure = PersistenceError("STORAGE_UNAVAILABLE")
    store.commit_error = failure
    store.abandon_error = PersistenceError("LEASE_RELEASE_FAILED")
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="STORAGE_UNAVAILABLE") as caught:
        session.execute_pending(lambda _item: {"ok": True})

    assert caught.value is failure
    assert store.abandon_calls == [lease]
    assert store.commit_calls == []


def test_a_second_pass_does_not_repeat_an_already_committed_tool():
    """The plan is a snapshot; the committed rows decide what is still due.

    Re-running a still-valid session must not re-issue the external query
    that ``commit_tool`` would then discard as a duplicate.
    """
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    step_id = uuid4()
    pending = [{"step_id": step_id, "ordinal": 0, "tool_call": {"name": "logs.search"}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]
    executed: list[int] = []

    def execute(item: dict) -> dict:
        executed.append(int(item["ordinal"]))
        return {"ok": True}

    assert session.execute_pending(execute) == 1
    assert session.execute_pending(execute) == 0
    assert executed == [0]
    assert store.commit_calls == [(step_id, 0, {"ok": True})]


def test_only_the_still_outstanding_call_of_a_step_is_dispatched():
    """Partial progress under this lease leaves the plan ahead of the rows."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    step_id = uuid4()
    planned = [
        {"step_id": step_id, "ordinal": 0, "tool_call": {"name": "metrics.range"}},
        {"step_id": step_id, "ordinal": 1, "tool_call": {"name": "logs.search"}},
    ]
    # Ordinal 0 was already committed under this lease after the plan was read.
    store = _RecordingStore(run_id=run_id, pending=planned[1:], with_renew=True)
    session = RecoverySession(_pending_plan(run_id, planned), lease, store)  # type: ignore[arg-type]
    executed: list[int] = []

    def execute(item: dict) -> dict:
        executed.append(int(item["ordinal"]))
        return {"ok": True}

    assert session.execute_pending(execute) == 1
    assert executed == [1]
    assert store.commit_calls == [(step_id, 1, {"ok": True})]


def test_a_transient_rebuild_failure_releases_the_live_lease():
    """The fence just passed, so a failed read must not strand the lease."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    failure = PersistenceError("STORAGE_UNAVAILABLE")

    def rebuild(_incident_id: UUID) -> dict:
        raise failure

    store.rebuild = rebuild  # type: ignore[method-assign]
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="STORAGE_UNAVAILABLE") as caught:
        session.execute_pending(lambda _item: {"ok": True})

    assert caught.value is failure
    assert store.abandon_calls == [lease]
    assert store.renew_calls == []
    assert store.commit_calls == []


def test_a_fence_rejection_does_not_release_anything():
    """``lease_current`` False means the lease is already gone; nothing to free."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    store.lease_current = lambda _lease: False  # type: ignore[method-assign]
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        session.execute_pending(lambda _item: {"ok": True})

    assert store.abandon_calls == []
    assert store.commit_calls == []


def test_concurrent_passes_do_not_dispatch_the_same_call_twice():
    """One lease is one execution attempt: dispatch is serialized per session.

    Without it, a second caller rebuilds while the first is still inside its
    callback, sees the same ordinal pending, and repeats the external query --
    a duplicate ``commit_tool`` deduplicates but cannot refund.
    """
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    step_id = uuid4()
    pending = [{"step_id": step_id, "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]
    dispatched: list[int] = []
    dispatching = threading.Event()

    def execute(item: dict) -> dict:
        dispatched.append(int(item["ordinal"]))
        dispatching.set()
        # Still in flight while the second caller tries to start.
        time.sleep(0.2)
        return {"ok": True}

    counts: list[int] = []
    first = threading.Thread(
        target=lambda: counts.append(session.execute_pending(execute))
    )
    first.start()
    assert dispatching.wait(5)
    second = threading.Thread(
        target=lambda: counts.append(session.execute_pending(execute))
    )
    second.start()
    first.join(10)
    second.join(10)

    assert dispatched == [0]
    assert sorted(counts) == [0, 1]
    assert store.commit_calls == [(step_id, 0, {"ok": True})]


def test_publish_failure_releases_the_lease_and_preserves_the_error():
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    store = _RecordingStore(run_id=run_id, pending=[], with_renew=True)
    failure = PersistenceError("STORAGE_UNAVAILABLE")

    def publish(_lease: Lease, _conclusion: dict, *, step_id: UUID) -> bool:
        raise failure

    store.publish = publish  # type: ignore[attr-defined]
    store.abandon_error = PersistenceError("LEASE_RELEASE_FAILED")
    session = RecoverySession(_pending_plan(run_id, []), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="STORAGE_UNAVAILABLE") as caught:
        session.publish({"summary": "done"}, step_id=uuid4())

    assert caught.value is failure
    assert store.abandon_calls == [lease]


def test_a_rejected_publish_keeps_the_lease():
    """``store.publish`` returns False for a revoked lease; that is not a
    failed write, so there is nothing to release."""
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    store = _RecordingStore(run_id=run_id, pending=[], with_renew=True)
    store.publish = lambda _lease, _conclusion, *, step_id: False  # type: ignore[attr-defined]
    session = RecoverySession(_pending_plan(run_id, []), lease, store)  # type: ignore[arg-type]

    assert session.publish({"summary": "done"}, step_id=uuid4()) is False
    assert store.abandon_calls == []


def test_resume_blocks_a_run_replaced_by_an_incompatible_one_mid_decode():
    """The version gate and the decode are separate transactions.

    A cancel -> new_run under a new schema between them makes old validators
    reject new rows; the incompatible Run must still reach the durable
    blocked handoff instead of surfacing that decode error.
    """
    incident_id, run_id = uuid4(), uuid4()

    class Store:
        def __init__(self):
            self.metadata_calls = 0
            self.claims = []

        def recovery_metadata(self, _incident_id):
            self.metadata_calls += 1
            # Compatible when first gated, replaced by the time it is re-read.
            versions = {"state": "v1"} if self.metadata_calls == 1 else {"state": "v2"}
            return {"run_id": run_id, "versions": versions}

        def rebuild(self, _incident_id):
            raise PersistenceError("INCONSISTENT_STATE")

        def claim(self, incident, run, owner, versions, lease_seconds):
            self.claims.append((incident, run))
            raise PersistenceError("INCOMPATIBLE_STATE")

    store = Store()
    with pytest.raises(PersistenceError, match="INCOMPATIBLE_STATE"):
        Worker.create(store, {"state": "v1"}).resume(incident_id)

    assert store.metadata_calls == 2
    assert store.claims == [(incident_id, run_id)]


def test_a_decode_failure_on_a_compatible_run_surfaces_as_itself():
    """Re-gating must not relabel an ordinary corrupt-state error."""
    incident_id, run_id = uuid4(), uuid4()

    class Store:
        def __init__(self):
            self.claims = []

        def recovery_metadata(self, _incident_id):
            return {"run_id": run_id, "versions": {"state": "v1"}}

        def rebuild(self, _incident_id):
            raise PersistenceError("INCONSISTENT_STATE")

        def claim(self, *_args, **_kwargs):
            raise AssertionError("a compatible Run must not be blocked")

    store = Store()
    with pytest.raises(PersistenceError, match="INCONSISTENT_STATE"):
        Worker.create(store, {"state": "v1"}).resume(incident_id)
    assert store.claims == []


def test_a_failed_fence_read_releases_the_live_lease():
    """``lease_current`` raising is a failed read, not a fence rejection.

    Sibling of the rebuild case: the session may still hold a live lease, so
    unwinding without releasing it blocks a replacement worker for the full
    lease term once storage recovers.
    """
    run_id = uuid4()
    lease = Lease(uuid4(), run_id, uuid4(), 1, 0)
    pending = [{"step_id": uuid4(), "ordinal": 0, "tool_call": {}}]
    store = _RecordingStore(run_id=run_id, pending=pending, with_renew=True)
    failure = PersistenceError("TIMEOUT")

    def lease_current(_lease: Lease) -> bool:
        raise failure

    store.lease_current = lease_current  # type: ignore[method-assign]
    store.abandon_error = PersistenceError("LEASE_RELEASE_FAILED")
    session = RecoverySession(_pending_plan(run_id, pending), lease, store)  # type: ignore[arg-type]

    with pytest.raises(PersistenceError, match="TIMEOUT") as caught:
        session.execute_pending(lambda _item: {"ok": True})

    assert caught.value is failure
    assert store.abandon_calls == [lease]
    assert store.renew_calls == []
    assert store.commit_calls == []
