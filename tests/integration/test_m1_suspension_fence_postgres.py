"""M1-01: a global/target suspension that lands after ``claim()`` fences every
durable write path, not only the next claim (PRODUCT-CONSTRAINTS "human
control decisions ... scoped outside the model's authority"; C3 §6/§7).

``claim()`` snapshots the global and target suspension generations into the
``Lease``. These tests only use the public store API: claim, suspend, then
try each write path with the now-stale lease and check the committed rows.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

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


def _steps(s: DurableStore, incident_id):
    return s.rebuild(incident_id)["steps"]


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
        assert s.lease_current(lease) is False
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            s.renew_lease(lease, 30)
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            s.reserve_budget(lease, uuid4(), 1)
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            s.settle_budget(lease, reservation, "spent")
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            s.charge_tool(
                lease,
                "round-0:0",
                0.0,
                max_operations=10,
                max_tool_seconds=100.0,
                dispatch_id=uuid4(),
            )
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
        assert any(k.startswith("late_result:tool:") for k in late)
        assert any(k.startswith("late_result:publish:") for k in late)
    finally:
        scope.release()

    # Releasing the gate flips the flags back but the generation moved on:
    # the stale lease stays revoked on every path, it is never revived.
    assert s.lease_current(lease) is False
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.reserve_budget(lease, uuid4(), 1)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.charge_tool(
            lease,
            "round-0:0",
            0.0,
            max_operations=10,
            max_tool_seconds=100.0,
            dispatch_id=uuid4(),
        )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.commit_step(lease, "round-2", {"tool_calls": []})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.commit_tool(lease, step, 0, {"ok": True})
    assert s.publish(lease, _ONE_TOOL, step_id=step) is False
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.hand_off(lease)
    assert _run_row(s, r)["state"] == "paused"
    assert s.rebuild(i)["conclusion"] is None


@pytest.mark.parametrize("kind", ["global", "target"])
def test_settling_a_dispatch_reserved_before_the_suspension_is_still_allowed(kind):
    """The one documented exception in ``charge_tool``: settling a dispatch
    this same lease already reserved only lowers the reserved seconds to the
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
            s.charge_tool(
                lease,
                "round-0:1",
                0.0,
                max_operations=10,
                max_tool_seconds=100.0,
                dispatch_id=uuid4(),
            )
        assert _run_row(s, r)["tool_operations_used"] == 1
    finally:
        scope.release()


@pytest.mark.parametrize("kind", ["global", "target"])
def test_a_suspension_cannot_land_inside_an_in_flight_fenced_write(kind):
    """Row-lock contract: every fenced write shares the scope rows
    (``FOR SHARE``) that a suspension takes ``FOR UPDATE``. A suspension
    therefore waits for the in-flight write to commit, and the very next
    write on the same lease is rejected -- there is no window in which the
    pause is committed while a write that read "not suspended" also commits.
    """
    s = _store()
    scope = _Scope(kind, s)
    i, r = _accept(s, target=scope.target)
    lease = s.claim(i, r, uuid4(), {"v": "1"})

    # Hold the same shared lock a fenced write path holds while it works.
    holder = psycopg.connect(DSN, row_factory=dict_row)
    if kind == "global":
        holder.execute(
            "SELECT 1 FROM opspilot_scope_controls WHERE scope_id=1 FOR SHARE"
        )
    else:
        holder.execute(
            "SELECT 1 FROM opspilot_target_suspensions WHERE target_id=%s FOR SHARE",
            (scope.target,),
        )

    done = threading.Event()
    started = time.monotonic()
    elapsed: dict[str, float] = {}

    def suspend():
        scope.suspend(lease)
        elapsed["t"] = time.monotonic() - started
        done.set()

    t = threading.Thread(target=suspend)
    t.start()
    try:
        time.sleep(0.6)
        # The suspension is blocked behind the in-flight write ...
        assert not done.is_set()
        # ... which is still allowed to finish and commit under its lease.
        s.reserve_budget(lease, uuid4(), 1)
        holder.commit()
        holder.close()
        assert done.wait(5.0)
        assert elapsed["t"] >= 0.6
    finally:
        t.join(5.0)
        try:
            # The pause then fences the next write on the same lease.
            with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
                s.reserve_budget(lease, uuid4(), 1)
            assert _run_row(s, r)["budget_reserved"] == 1
        finally:
            scope.release()
