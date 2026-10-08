"""M1-01: a global/target suspension that lands after ``claim()`` fences every
durable write path, not only the next claim (PRODUCT-CONSTRAINTS "human
control decisions ... scoped outside the model's authority"; C3 §6/§7).

``claim()`` snapshots the global and target suspension generations into the
``Lease``. The scenarios drive the store through its public API (claim,
suspend, then each write path with the now-stale lease) and check the
committed rows; raw SQL only reads state or, in the lock test, holds the
in-flight transaction open.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

_ONE_TOOL = {"tool_calls": [{"name": "query", "arguments": {}}]}


def _store() -> DurableStore:
    s = DurableStore(DSN)
    s.install()
    with s.transaction() as conn:
        row = conn.execute(
            "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1"
        ).fetchone()
    if row["global_suspended"]:
        s.set_global_suspension(
            False, expected_generation=row["global_generation"], actor="operator"
        )
    return s


def _accept(s: DurableStore, *, target=None):
    i, r = uuid4(), uuid4()
    s.accept(
        i,
        r,
        "suspension-fence-" + str(i),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=5,
        versions={"v": "1"},
        target_id=target,
    )
    return i, r


class _Scope:
    """Suspend/release either scope through the public API only."""

    def __init__(self, kind: str, s: DurableStore):
        self.kind, self.s = kind, s
        self.target = s.register_target("target-" + str(uuid4()))
        self.generation: int | None = None

    def suspend(self, lease) -> None:
        if self.kind == "global":
            self.generation = self.s.set_global_suspension(
                True,
                expected_generation=lease.global_suspension_generation,
                actor="operator",
            )
        else:
            self.generation = self.s.set_target_suspension(
                self.target,
                True,
                expected_generation=lease.target_suspension_generation,
                actor="operator",
            )

    def release(self) -> None:
        assert self.generation is not None
        if self.kind == "global":
            self.s.set_global_suspension(
                False, expected_generation=self.generation, actor="operator"
            )
        else:
            self.s.set_target_suspension(
                self.target,
                False,
                expected_generation=self.generation,
                actor="operator",
            )


def _run_row(s: DurableStore, run_id):
    with s.transaction() as conn:
        return conn.execute(
            "SELECT state,owner,lease_until,tool_operations_used,tool_seconds_used,budget_reserved FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()


def _fresh_charge(s: DurableStore, lease, operation_id: str = "round-0:0") -> None:
    s.charge_tool(
        lease,
        operation_id,
        0.0,
        max_operations=10,
        max_tool_seconds=100.0,
        dispatch_id=uuid4(),
    )


def _assert_every_write_denied(s: DurableStore, lease, step, reservation) -> None:
    assert s.lease_current(lease) is False
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.renew_lease(lease, 30)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.reserve_budget(lease, uuid4(), 1)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.settle_budget(lease, reservation, "spent")
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        _fresh_charge(s, lease)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.begin_round(lease, "round-1")
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.commit_step(lease, "round-1", {"tool_calls": []})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.commit_tool(lease, step, 0, {"ok": True})
    assert s.publish(lease, _ONE_TOOL, step_id=step) is False
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.hand_off(lease)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.block(lease)


@pytest.mark.parametrize("kind", ["global", "target"])
def test_a_suspension_after_claim_fences_every_durable_write_path(kind):
    s = _store()
    scope = _Scope(kind, s)
    i, r = _accept(s, target=scope.target)
    lease = s.claim(i, r, uuid4(), {"v": "1"})
    step = s.commit_step(lease, "round-0", _ONE_TOOL)
    reservation = uuid4()
    s.reserve_budget(lease, reservation, 1)
    before = _run_row(s, r)

    scope.suspend(lease)
    try:
        _assert_every_write_denied(s, lease, step, reservation)

        after = _run_row(s, r)
        rebuilt = s.rebuild(i)
        # Nothing charged, adopted or published; the Run is parked by the
        # suspension itself, not moved by the stale worker.
        assert after["state"] == "paused"
        assert after["owner"] is None and after["lease_until"] is None
        assert after["tool_operations_used"] == before["tool_operations_used"]
        assert after["tool_seconds_used"] == before["tool_seconds_used"]
        assert after["budget_reserved"] == before["budget_reserved"]
        assert rebuilt["conclusion"] is None
        adopted = [x for x in rebuilt["steps"] if x["status"] != "late_result"]
        assert len(adopted) == 1 and adopted[0]["tool_results"] in (None, [])
        late = {
            x["logical_key"] for x in rebuilt["steps"] if x["status"] == "late_result"
        }
        assert any(k.startswith("late_result:step:") for k in late)
        assert any(k.startswith("late_result:tool:") for k in late)
        assert any(k.startswith("late_result:publish:") for k in late)
    finally:
        scope.release()

    # Releasing the gate flips the flags back but the generation moved on:
    # the stale lease stays revoked on every path, it is never revived.
    _assert_every_write_denied(s, lease, step, reservation)
    assert _run_row(s, r)["state"] == "paused"
    assert s.rebuild(i)["conclusion"] is None


@pytest.mark.parametrize("kind", ["global", "target"])
def test_settling_a_dispatch_reserved_before_the_suspension_is_still_allowed(kind):
    """The one documented exception in ``charge_tool``: settling a dispatch
    this same lease already reserved replaces the reserved seconds with the
    measured ones. It adopts nothing and reserves nothing new; refusing it
    would leave the full timeout occupied forever."""
    s = _store()
    scope = _Scope(kind, s)
    i, r = _accept(s, target=scope.target)
    lease = s.claim(i, r, uuid4(), {"v": "1"})
    dispatch = uuid4()
    s.charge_tool(
        lease,
        "round-0:0",
        6.0,
        max_operations=10,
        max_tool_seconds=100.0,
        dispatch_id=dispatch,
    )
    assert _run_row(s, r)["tool_seconds_used"] == 6.0

    scope.suspend(lease)
    try:
        s.charge_tool(
            lease,
            "round-0:0",
            1.5,
            max_operations=10,
            max_tool_seconds=100.0,
            dispatch_id=dispatch,
        )
        row = _run_row(s, r)
        assert row["tool_operations_used"] == 1 and row["tool_seconds_used"] == 1.5
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            _fresh_charge(s, lease, "round-0:1")
        assert _run_row(s, r)["tool_operations_used"] == 1
    finally:
        scope.release()


@pytest.mark.parametrize("kind", ["global", "target"])
@pytest.mark.parametrize("path", ["reserve_budget", "renew_lease"])
def test_a_suspension_waits_for_an_in_flight_write_and_fences_the_next_one(
    kind, path, monkeypatch
):
    """A suspension issued while a fenced write is mid-transaction lands only
    after that write commits, and the next write on the same lease is denied:
    there is no window in which the pause is committed while a write that
    read "not suspended" also commits.

    The in-flight write is a real store method. It is held open by delaying
    the ``clock_timestamp()`` read that every write path performs after taking
    its locks; the suspension is started from another thread at that point.
    ``reserve_budget`` shares the scope row; ``renew_lease`` does not read it
    and is serialized through the incident row instead.
    """
    s = _store()
    scope = _Scope(kind, s)
    i, r = _accept(s, target=scope.target)
    lease = s.claim(i, r, uuid4(), {"v": "1"})

    real_now = DurableStore._db_now
    done = threading.Event()
    seen: dict[str, object] = {"armed": True}

    def suspend() -> None:
        try:
            scope.suspend(lease)
        except BaseException as exc:  # surfaced by the assertions below
            seen["error"] = exc
        finally:
            seen["suspended_at"] = time.monotonic()
            done.set()

    thread = threading.Thread(target=suspend)

    def held_open_now(conn):
        if seen.pop("armed", False):
            thread.start()
            time.sleep(0.8)
            seen["suspended_before_commit"] = done.is_set()
        return real_now(conn)

    monkeypatch.setattr(DurableStore, "_db_now", staticmethod(held_open_now))
    try:
        if path == "reserve_budget":
            s.reserve_budget(lease, uuid4(), 1)
        else:
            s.renew_lease(lease, 30)
        seen["committed_at"] = time.monotonic()
        assert done.wait(5.0)
    finally:
        thread.join(5.0)
        monkeypatch.undo()
    assert "error" not in seen, seen["error"]
    # Blocked while the write was open; landed only after it committed.
    assert seen["suspended_before_commit"] is False
    assert seen["suspended_at"] >= seen["committed_at"]
    try:
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            s.reserve_budget(lease, uuid4(), 1)
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            s.renew_lease(lease, 30)
        row = _run_row(s, r)
        assert row["state"] == "paused"
        assert row["budget_reserved"] == (1 if path == "reserve_budget" else 0)
    finally:
        scope.release()
