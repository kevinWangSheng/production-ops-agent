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
    ledger = DurableToolLedger(store, lease)
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

    store.charge_tool(lease, "step-1:0", 0.0)
    store.charge_tool(lease, "step-1:0", 2.5)
    store.charge_tool(lease, "step-1:0", 2.5)  # a replayed settle changes nothing
    store.charge_tool(lease, "step-1:0", 1.0)  # never charged downward
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (1, 2.5)

    for bad in ("", 7):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(lease, bad, 0.0)
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            store.charge_tool(lease, "step-1:1", bad)


def test_charge_tool_is_fenced_by_the_lease_like_every_write_path():
    store = DurableStore(DSN)
    incident, run = _accept(store, "fenced")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    store.charge_tool(lease, "step-1:0", 1.0)
    assert store.control(incident, 0, "cancel", "operator") == 1

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.charge_tool(lease, "step-1:1", 1.0)
    row = store.rebuild(incident)["run"]
    assert (row["tool_operations_used"], row["tool_seconds_used"]) == (1, 1.0)
