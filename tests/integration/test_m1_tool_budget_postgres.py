"""The per-Run tool budget on the real DurableStore across execution attempts.

Technical plan section 13: budgets are reserved and settled in PostgreSQL and a
restart must not reset them. Before this seam existed the executor counted only
in instance memory, so every new attempt of the same Run had the frozen
20 operations / 240 s again.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools import MAX_OPERATIONS_PER_RUN, TransportResponse
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

    store.charge_tool(lease, "step-1:0", 0.0, max_operations=MAX_OPERATIONS_PER_RUN)
    store.charge_tool(lease, "step-1:0", 2.5, max_operations=MAX_OPERATIONS_PER_RUN)
    store.charge_tool(
        lease, "step-1:0", 2.5, max_operations=MAX_OPERATIONS_PER_RUN
    )  # a replayed settle changes nothing
    store.charge_tool(
        lease, "step-1:0", 1.0, max_operations=MAX_OPERATIONS_PER_RUN
    )  # never charged downward
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (1, 2.5)

    for bad in ("", 7):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(lease, bad, 0.0, max_operations=MAX_OPERATIONS_PER_RUN)
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(
                lease, "step-1:1", bad, max_operations=MAX_OPERATIONS_PER_RUN
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
            lease, f"step-1:{index}", 1.0, max_operations=MAX_OPERATIONS_PER_RUN
        )
    row = store.rebuild(incident)["run"]
    assert row["tool_operations_used"] == MAX_OPERATIONS_PER_RUN

    with pytest.raises(PersistenceError, match="OPERATION_BUDGET_EXHAUSTED"):
        store.charge_tool(
            lease, "step-1:overflow", 1.0, max_operations=MAX_OPERATIONS_PER_RUN
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
    for index in range(MAX_OPERATIONS_PER_RUN):
        store.charge_tool(
            lease, f"step-1:{index}", 0.0, max_operations=MAX_OPERATIONS_PER_RUN
        )

    store.charge_tool(
        lease, "step-1:0", 3.0, max_operations=MAX_OPERATIONS_PER_RUN
    )  # settling an already-counted operation, not counting a new one

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
    """

    store = DurableStore(DSN)
    incident, run = _accept(store, "ledger-narrow-cap")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    ledger = DurableToolLedger(store, lease, max_operations=1)

    ledger.charge("step-1:0", 1.0)
    assert ledger.usage().operations_used == 1

    with pytest.raises(PersistenceError, match="OPERATION_BUDGET_EXHAUSTED"):
        ledger.charge("step-1:1", 1.0)  # refused at 1, not the global cap of 20
    assert ledger.usage().operations_used == 1


def test_a_re_dispatched_operation_in_a_new_epoch_is_counted_again():
    """Same operation_id, new attempt: the read really went out twice."""
    store = DurableStore(DSN)
    incident, run = _accept(store, "re-dispatch")
    first = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    store.charge_tool(
        first, "step-1:0", 0.0, max_operations=MAX_OPERATIONS_PER_RUN
    )  # reserved, then the worker died
    time.sleep(1.2)
    second = store.claim(incident, run, uuid4(), {"state": "v1"})
    store.charge_tool(second, "step-1:0", 0.0, max_operations=MAX_OPERATIONS_PER_RUN)
    store.charge_tool(second, "step-1:0", 4.0, max_operations=MAX_OPERATIONS_PER_RUN)
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (2, 4.0)


def test_charge_tool_is_fenced_by_the_lease_like_every_write_path():
    store = DurableStore(DSN)
    incident, run = _accept(store, "fenced")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    store.charge_tool(lease, "step-1:0", 1.0, max_operations=MAX_OPERATIONS_PER_RUN)
    assert store.control(incident, 0, "cancel", "operator") == 1

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.charge_tool(lease, "step-1:1", 1.0, max_operations=MAX_OPERATIONS_PER_RUN)
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (1, 1.0)
