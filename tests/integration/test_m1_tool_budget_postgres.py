"""The per-Run tool budget on the real DurableStore across execution attempts.

Technical plan section 13: budgets are reserved and settled in PostgreSQL and a
restart must not reset them. Before this seam existed the executor counted only
in instance memory, so every new attempt of the same Run had the frozen
20 operations / 240 s again.
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
    ToolBudgetExhausted,
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
    ledger = DurableToolLedger(store, lease, max_operations=MAX_OPERATIONS_PER_RUN)
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
    dispatched = [item for item in outcomes if item.status == "ok"]
    refused = [item for item in outcomes if item.status != "ok"]
    assert len(dispatched) == MAX_OPERATIONS_PER_RUN - 3
    assert {(item.status, item.reason) for item in refused} == {
        ("denied", "OPERATION_BUDGET_EXHAUSTED")
    }
    assert len(transport.requests) == MAX_OPERATIONS_PER_RUN - 3
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN
    assert row["tool_seconds_used"] == 6.0 * MAX_OPERATIONS_PER_RUN


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
        dispatch_id=dispatch,
    )
    store.charge_tool(
        lease,
        "step-1:0",
        2.5,
        max_operations=MAX_OPERATIONS_PER_RUN,
        dispatch_id=dispatch,
    )
    store.charge_tool(
        lease,
        "step-1:0",
        2.5,
        max_operations=MAX_OPERATIONS_PER_RUN,
        dispatch_id=dispatch,
    )  # a replayed settle changes nothing
    store.charge_tool(
        lease,
        "step-1:0",
        1.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
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
                dispatch_id=uuid4(),
            )
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(
                lease,
                "step-1:1",
                bad,
                max_operations=MAX_OPERATIONS_PER_RUN,
                dispatch_id=uuid4(),
            )


def test_charge_tool_refuses_a_new_operation_once_the_cap_is_reached():
    """Bot review finding: counting a new operation was unconditional once
    past the lease/control checks, so two executors racing from the same
    stale ``operations_used`` snapshot (e.g. both read 19, both pass their
    local ``< 20`` check) could both increment after serializing on the row
    lock, taking the durable count past the frozen cap of 20. The cap must
    be enforced inside the same locked transaction that does the increment,
    not only in the application code that decides whether to attempt it.
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
            dispatch_id=uuid4(),
        )
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN

    with pytest.raises(PersistenceError, match="OPERATION_BUDGET_EXHAUSTED"):
        store.charge_tool(
            lease,
            "step-1:overflow",
            1.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            dispatch_id=uuid4(),
        )
    row = store.rebuild(incident)["run"]
    # Refused, not just rejected after already incrementing: the count stays
    # exactly at the cap, and the charge row from the rolled-back attempt
    # does not survive either.
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN
    assert row["tool_seconds_used"] == float(MAX_OPERATIONS_PER_RUN)


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
            dispatch_id=dispatches[index],
        )

    store.charge_tool(
        lease,
        "step-1:0",
        3.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        dispatch_id=dispatches[0],
    )  # settling an already-counted dispatch, not counting a new one

    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN
    assert row["tool_seconds_used"] == 3.0


def test_the_durable_ledger_binds_the_cap_it_was_constructed_with_not_the_global_default():
    """Bot review finding: ``DurableToolLedger`` defaulted ``max_operations``
    to the frozen global ceiling (20) regardless of what a Run's own
    ``QueryScope.max_operations`` actually authorized. A Run issued a
    narrower per-Run cap would still have its durable charges enforced
    against the wider global cap -- the ledger adapter, not just
    ``charge_tool`` itself, has to carry the caller's real cap through.
    ``max_operations`` is now a required keyword; this proves binding it to
    something narrower than the global default actually changes what the
    durable ledger enforces, not just what ``charge_tool`` accepts directly.

    Also proves the real translation from ``charge_tool``'s
    ``PersistenceError("OPERATION_BUDGET_EXHAUSTED")`` into the abstract
    ``ToolUsageLedger`` contract's ``ToolBudgetExhausted`` (a later, separate
    bot review finding) -- not just the in-memory test double's behaviour.
    """

    store = DurableStore(DSN)
    incident, run = _accept(store, "ledger-narrow-cap")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    ledger = DurableToolLedger(store, lease, max_operations=1)

    ledger.charge("step-1:0", 1.0, dispatch_id=uuid4())
    assert ledger.usage().operations_used == 1

    with pytest.raises(ToolBudgetExhausted, match="OPERATION_BUDGET_EXHAUSTED"):
        ledger.charge(
            "step-1:1", 1.0, dispatch_id=uuid4()
        )  # refused at 1, not the global cap of 20
    assert ledger.usage().operations_used == 1


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
        dispatch_id=retry,
    )
    store.charge_tool(
        second,
        "step-1:0",
        4.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
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
        dispatch_id=uuid4(),
    )
    assert store.control(incident, 0, "cancel", "operator") == 1

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.charge_tool(
            lease,
            "step-1:1",
            1.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
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
        lease, "step-1:0", 0.0, max_operations=MAX_OPERATIONS_PER_RUN, dispatch_id=first
    )
    store.charge_tool(
        lease,
        "step-1:0",
        0.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        dispatch_id=second,
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 0.0)

    store.charge_tool(
        lease, "step-1:0", 5.0, max_operations=MAX_OPERATIONS_PER_RUN, dispatch_id=first
    )
    store.charge_tool(
        lease,
        "step-1:0",
        7.0,
        max_operations=MAX_OPERATIONS_PER_RUN,
        dispatch_id=second,
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 12.0)

    # Each dispatch settles its own row, so a replayed settle still changes
    # nothing -- the property the per-operation key used to provide.
    store.charge_tool(
        lease, "step-1:0", 5.0, max_operations=MAX_OPERATIONS_PER_RUN, dispatch_id=first
    )
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 12.0)


def test_a_duplicate_dispatch_still_cannot_exceed_the_operation_cap():
    """Charging every dispatch must not let duplicates outrun the cap: the
    second dispatch is a new operation and is refused once the Run is full.
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
            dispatch_id=uuid4(),
        )

    with pytest.raises(PersistenceError, match="OPERATION_BUDGET_EXHAUSTED"):
        store.charge_tool(
            lease,
            "step-1:0",  # same stable operation, a second real dispatch
            0.0,
            max_operations=MAX_OPERATIONS_PER_RUN,
            dispatch_id=uuid4(),
        )
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN
