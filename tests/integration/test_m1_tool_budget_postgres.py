"""The per-Run tool budget on the real DurableStore across execution attempts.

Technical plan section 13: budgets are reserved and settled in PostgreSQL and a
restart must not reset them. Before this seam existed the executor counted only
in instance memory, so every new attempt of the same Run had the frozen
20 operations / 240 s again.

M1-01 (2026-09-28 user decision, docs/tasks/2026-09-28-m1-01-loop-limits.md,
L2): ``MAX_OPERATIONS_PER_RUN``/``MAX_TOOL_SECONDS_PER_RUN`` are no longer a
per-Run ceiling ``charge_tool``/``DurableToolLedger`` refuse on -- the loop's
single anti-loop ceiling is the model-request count (L1). This suite keeps
exercising ``charge_tool``'s durable *accounting* (counts and seconds surviving
a restart, idempotent settlement, reservation-then-settlement, lease fencing)
and, where a test used to prove a refusal at the old cap, now proves the
opposite: the charge still succeeds and the count/total keeps growing past it.
"""

import dataclasses
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools import (
    MAX_OPERATIONS_PER_RUN,
    MAX_TOOL_SECONDS_PER_RUN,
    TransportResponse,
)
from opspilot.tools.ledger import DurableToolLedger
from scripts.m0.postgres_lab import DSN
from tests.m1_tool_support import WINDOW_START, FakeClock, body, build, request

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _accept(store, tag):
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-tool-budget-{tag}-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    return incident, run


def _attempt(store, lease, clock, *, duration):
    ledger = DurableToolLedger(
        store,
        lease,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
    )
    executor, transport, _, _ = build(
        clock=clock,
        ledger=ledger,
        scope_overrides={
            "run_id": str(lease.run_id),
            "subject_id": str(lease.incident_id),
        },
    )
    transport.clock, transport.duration = clock, duration
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
    )
    return executor, transport


def test_second_attempt_of_the_same_run_inherits_used_operations_and_seconds():
    """M1-01 (2026-09-28 user decision, L2) removed the cap this test used to
    prove was hit after a restart; the accounting-continuity invariant it
    also proved -- a new attempt's executor starts counting from the ledger,
    not from zero -- still holds and is what this test now isolates. Charging
    ``MAX_OPERATIONS_PER_RUN`` more operations on top of the 3 the first
    attempt already spent crosses the old 20-operation ceiling with real
    dispatches and none of them are refused.
    """
    store = DurableStore(DSN)
    store.install()
    incident, run = _accept(store, "restart")
    clock = FakeClock()

    first = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    executor, transport = _attempt(store, first, clock, duration=6.0)
    for index in range(3):
        assert executor.execute(request(tool_index=index)).status == "ok"
    assert (executor.operations_used, executor.tool_seconds_used) == (3, 18.0)
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (3, 18.0)

    time.sleep(1.2)  # the first attempt's lease expires without a clean exit
    second = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert second.epoch == first.epoch + 1
    executor, transport = _attempt(store, second, clock, duration=6.0)
    assert (executor.operations_used, executor.tool_seconds_used) == (3, 18.0)

    outcomes = [
        executor.execute(request(step_id="s2", tool_index=index))
        for index in range(MAX_OPERATIONS_PER_RUN)
    ]
    assert all(item.status == "ok" for item in outcomes)
    assert len(transport.requests) == MAX_OPERATIONS_PER_RUN
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == 3 + MAX_OPERATIONS_PER_RUN
    assert row["tool_seconds_used"] == 18.0 + 6.0 * MAX_OPERATIONS_PER_RUN


def test_charge_tool_counts_once_per_operation_and_settles_seconds_upward():
    store = DurableStore(DSN)
    incident, run = _accept(store, "idempotent")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})

    dispatch = uuid4()  # one real read: reserve then settle carry its id
    store.charge_tool(
        lease,
        "step-1:0",
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatch,
    )
    store.charge_tool(
        lease,
        "step-1:0",
        2.5,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatch,
    )
    store.charge_tool(
        lease,
        "step-1:0",
        2.5,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatch,
    )  # a replayed settle changes nothing
    store.charge_tool(
        lease,
        "step-1:0",
        1.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatch,
    )  # never charged downward
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (1, 2.5)

    for bad in ("", 7):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(
                lease,
                bad,
                0.0,
                max_operations=MAX_OPERATIONS_PER_RUN,
                max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
                dispatch_id=uuid4(),
            )
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(
                lease,
                "step-1:1",
                bad,
                max_operations=MAX_OPERATIONS_PER_RUN,
                max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
                dispatch_id=uuid4(),
            )


def test_charge_tool_no_longer_refuses_a_new_operation_past_the_old_cap():
    """M1-01 (2026-09-28 user decision, L2) removes the per-Run operation-count
    ceiling: ``charge_tool`` no longer refuses counting a new operation once
    the Run is past the old frozen cap of 20 -- it keeps counting. The row
    lock (``FOR UPDATE`` above the UPDATE) still serializes concurrent
    charges against the same Run, so the accounting itself stays race-free;
    what changed is that there is nothing left to refuse on.
    """

    store = DurableStore(DSN)
    incident, run = _accept(store, "cap-enforced")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})

    for index in range(MAX_OPERATIONS_PER_RUN):
        store.charge_tool(
            lease,
            f"step-1:{index}",
            1.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            dispatch_id=uuid4(),
        )
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN

    store.charge_tool(
        lease,
        "step-1:overflow",
        1.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=uuid4(),
    )
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN + 1
    assert row["tool_seconds_used"] == float(MAX_OPERATIONS_PER_RUN + 1)


def test_charge_tool_settlement_is_not_subject_to_the_cap():
    """The cap only refuses counting a *new* operation. Settling the seconds
    of one already counted must still succeed even once the Run is at the
    cap -- it is not adding a new operation.
    """

    store = DurableStore(DSN)
    incident, run = _accept(store, "cap-settlement")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    dispatches = {}
    for index in range(MAX_OPERATIONS_PER_RUN):
        dispatches[index] = uuid4()
        store.charge_tool(
            lease,
            f"step-1:{index}",
            0.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            dispatch_id=dispatches[index],
        )

    store.charge_tool(
        lease,
        "step-1:0",
        3.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatches[0],
    )  # settling an already-counted dispatch, not counting a new one

    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN
    assert row["tool_seconds_used"] == 3.0


def test_the_durable_ledger_no_longer_enforces_max_operations_at_any_binding():
    """Superseded bot review finding, M1-01 (2026-09-28 user decision, L2):
    before this decision, ``DurableToolLedger`` had to carry the caller's own
    ``max_operations`` through to ``charge_tool`` rather than default to the
    frozen global ceiling, because a narrower per-Run cap had to bind tighter
    than the global one. There is no ceiling left to bind narrower or wider
    any more -- ``max_operations`` stays a required constructor keyword only
    to keep the input-validation shape unchanged (see ``ledger.py``'s
    docstring), and a ledger bound to ``max_operations=1`` charges a second
    operation exactly as freely as one bound to the global default.
    """

    store = DurableStore(DSN)
    incident, run = _accept(store, "ledger-narrow-cap")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    ledger = DurableToolLedger(
        store, lease, max_operations=1, max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN
    )

    ledger.charge("step-1:0", 1.0, dispatch_id=uuid4())
    assert ledger.usage().operations_used == 1

    ledger.charge("step-1:1", 1.0, dispatch_id=uuid4())  # no longer refused at 1
    assert ledger.usage().operations_used == 2


def test_a_re_dispatched_operation_in_a_new_epoch_is_counted_again():
    """Same operation_id, new attempt: the read really went out twice."""
    store = DurableStore(DSN)
    incident, run = _accept(store, "re-dispatch")
    first = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    store.charge_tool(
        first,
        "step-1:0",
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=uuid4(),
    )  # reserved, then the worker died
    time.sleep(1.2)
    second = store.claim(incident, run, uuid4(), {"state": "v1"})
    retry = uuid4()  # the new attempt's own read
    store.charge_tool(
        second,
        "step-1:0",
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=retry,
    )
    store.charge_tool(
        second,
        "step-1:0",
        4.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=retry,
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 4.0)


def test_charge_tool_is_fenced_by_the_lease_like_every_write_path():
    store = DurableStore(DSN)
    incident, run = _accept(store, "fenced")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    store.charge_tool(
        lease,
        "step-1:0",
        1.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=uuid4(),
    )
    assert store.control(incident, 0, "cancel", "operator") == 1

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.charge_tool(
            lease,
            "step-1:1",
            1.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            dispatch_id=uuid4(),
        )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (1, 1.0)


def test_charge_tool_refuses_a_lease_whose_incident_and_run_disagree():
    """Bot review finding: the Run lookup matched on ``r.run_id`` alone.

    ``charge_tool`` locks ``lease.incident_id``'s incident row first (the
    module-wide incident-before-Run order), but then joined the Run row back
    to *its own* incident, never checking that it is the same incident. A
    Lease pairing incident A with a Run of incident B therefore locked A and
    charged B whenever B's owner/epoch/generation happened to match --
    mutating the wrong business record and defeating the documented lock
    order at the same time. ``renew_lease()`` and ``lease_current()`` already
    bind both identities; this path now does too.
    """
    store = DurableStore(DSN)
    incident_a, run_a = _accept(store, "cross-a")
    incident_b, run_b = _accept(store, "cross-b")
    owner = uuid4()
    lease_a = store.claim(incident_a, run_a, owner, {"state": "v1"})
    lease_b = store.claim(incident_b, run_b, owner, {"state": "v1"})
    # The two Runs really are indistinguishable on the fields the fence reads.
    assert (lease_a.owner, lease_a.epoch, lease_a.control_generation) == (
        lease_b.owner,
        lease_b.epoch,
        lease_b.control_generation,
    )

    crossed = dataclasses.replace(lease_a, run_id=run_b)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.charge_tool(
            crossed,
            "step-1:0",
            1.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            dispatch_id=uuid4(),
        )

    row = store.rebuild(incident_b)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (0, 0.0)


def test_two_dispatches_of_one_operation_in_one_epoch_are_both_charged():
    """Bot review finding, resolved against C3 §13: "重试计入次数和费用".

    The charge row used to be keyed ``(run, epoch, operation_id)``, and
    ``operation_id`` is stable by construction (C3 §7 line 246: 工具操作 ID
    由步骤 ID 和工具序号稳定生成). A duplicate delivery or a same-epoch retry
    of one tool call therefore found the existing row and settled it: both
    callers really reached ``transport.fetch()``, but the Run was charged one
    operation and ``max(seconds)`` instead of the sum. Measured before the
    fix on this database: two reads of 5.0s and 7.0s -> ``ops=1 secs=7.0``.

    C3 §4 line 258 explicitly declines to promise exactly-once external
    queries, so the contract's answer is not to refuse the second dispatch
    but to charge it. Each dispatch now owns its own charge row.
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "dup-dispatch")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    first, second = uuid4(), uuid4()

    store.charge_tool(
        lease,
        "step-1:0",
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=first,
    )
    store.charge_tool(
        lease,
        "step-1:0",
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=second,
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 0.0)

    store.charge_tool(
        lease,
        "step-1:0",
        5.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=first,
    )
    store.charge_tool(
        lease,
        "step-1:0",
        7.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=second,
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 12.0)

    # Each dispatch settles its own row, so a replayed settle still changes
    # nothing -- the property the per-operation key used to provide.
    store.charge_tool(
        lease,
        "step-1:0",
        5.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=first,
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 12.0)


def test_a_duplicate_dispatch_is_charged_like_any_other_past_the_old_cap():
    """M1-01 (2026-09-28 user decision, L2): a second real dispatch of the
    same stable ``operation_id`` is charged as its own operation (C3 §13,
    "重试计入次数和费用") whether or not the Run is past the old 20-operation
    ceiling -- there is no ceiling left to stop it from counting.
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "dup-cap")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    for index in range(MAX_OPERATIONS_PER_RUN):
        store.charge_tool(
            lease,
            f"step-1:{index}",
            0.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            dispatch_id=uuid4(),
        )

    store.charge_tool(
        lease,
        "step-1:0",  # same stable operation, a second real dispatch
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=uuid4(),
    )
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN + 1


def test_a_dispatch_past_the_old_cumulative_time_ceiling_is_charged_not_refused():
    """M1-01 (2026-09-28 user decision, L2) removes the per-Run cumulative
    tool-time ceiling: a reservation that would have overrun the old 240 s
    cap is charged and accumulates like any other -- there is no ceiling
    left for two executors racing from the same stale usage snapshot to
    overrun (the row lock still makes each charge's own accounting atomic;
    the reservation-then-settlement mechanism this test used to prove closed
    a race is exercised more directly by
    ``test_settling_releases_the_unused_part_of_a_reservation``).
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "time-reserve")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    cap = 240.0

    spent = uuid4()
    for value in (239.0, 239.0):  # reserve then settle
        store.charge_tool(
            lease,
            "step-1:0",
            value,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=cap,
            dispatch_id=spent,
        )
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 239.0

    first = uuid4()
    store.charge_tool(
        lease,
        "step-1:1",
        1.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=cap,
        dispatch_id=first,
    )
    store.charge_tool(
        lease,
        "step-1:2",
        1.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=cap,
        dispatch_id=uuid4(),
    )
    row = store.rebuild(incident)["run"]
    assert row["tool_seconds_used"] == 241.0
    assert row["tool_operations_used"] == 3


def test_settling_releases_the_unused_part_of_a_reservation():
    """The other half of the contract's 原子预留和结算: a read that finishes
    early gives the budget back, so the ceiling bounds real spend, not intent.
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "time-release")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    dispatch = uuid4()

    def charge(value):
        store.charge_tool(
            lease,
            "step-1:0",
            value,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=240.0,
            dispatch_id=dispatch,
        )

    charge(10.0)  # reservation
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 10.0
    charge(2.5)  # settled at its real cost
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 2.5
    for replayed in (2.5, 1.0):
        charge(replayed)  # replay changes nothing; never lowered below 2.5
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 2.5


def test_a_transport_that_overran_its_reservation_is_recorded_truthfully():
    """The ceiling governs what may be started; the record tells the truth
    about what was spent. A transport that outlasts its bound settles above
    the reservation.
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "time-overrun")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    dispatch = uuid4()

    for value in (5.0, 9.0):
        store.charge_tool(
            lease,
            "step-1:0",
            value,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=240.0,
            dispatch_id=dispatch,
        )
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 9.0


def test_the_durable_ledger_no_longer_reports_a_time_ceiling_signal():
    """Superseded bot review finding, M1-01 (2026-09-28 user decision, L2):
    ``ledger.py`` no longer translates a time-budget code out of
    ``charge_tool`` at all (there is none to translate -- see ``charge()``'s
    updated comment), so a ledger bound to a 2 s ``max_tool_seconds`` charges
    straight past it instead of raising ``ToolTimeBudgetExhausted``.
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "time-signal")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    ledger = DurableToolLedger(
        store, lease, max_operations=MAX_OPERATIONS_PER_RUN, max_tool_seconds=2.0
    )

    ledger.charge("step-1:0", 2.0, dispatch_id=uuid4())
    ledger.charge("step-1:1", 0.5, dispatch_id=uuid4())  # no longer refused past 2 s
    assert ledger.usage().tool_seconds_used == 2.5


def test_a_revoked_lease_may_settle_its_own_reservation_but_not_make_a_new_one():
    """Bot review finding: the lease fence rejected the settlement too, so the
    whole reserved timeout stayed in ``tool_seconds_used`` forever -- a resumed
    attempt inherited the inflated total and could hit TIME_BUDGET_EXHAUSTED
    for seconds nobody ever spent.

    Settling only writes the now-known elapsed time back to the row this same
    lease created; it makes no reservation and adopts nothing. Creating a new
    reservation under a revoked lease is still refused.
    """
    store = DurableStore(DSN)
    incident, run = _accept(store, "revoked-settle")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    dispatch = uuid4()

    store.charge_tool(
        lease,
        "step-1:0",
        30.0,  # reserves the full authorized timeout
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatch,
    )
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 30.0

    assert store.control(incident, 0, "cancel", "operator") == 1

    # The read finished in 2 s; that reconciliation must still land.
    store.charge_tool(
        lease,
        "step-1:0",
        2.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
        dispatch_id=dispatch,
    )
    assert store.rebuild(incident)["run"]["tool_seconds_used"] == 2.0

    # A *new* dispatch under the revoked lease is still refused.
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.charge_tool(
            lease,
            "step-1:1",
            1.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            dispatch_id=uuid4(),
        )
    assert store.rebuild(incident)["run"]["tool_operations_used"] == 1
