"""Wiring for the RecoverySession renewal seam (lease-wire.md §7b) against a
real ``DurableStore``.

``DurableStore.renew_lease`` was delivered by PR #35 (``fix/lease-renewal``)
and is present on the current ``main`` base. ``opspilot/worker.py`` reaches
it through ``getattr(store, "renew_lease", None)`` for compatibility with
minimal store doubles, while these tests exercise the real method whenever
the explicit PostgreSQL opt-in is enabled.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1"
    or not hasattr(DurableStore, "renew_lease"),
    reason="explicit PG opt-in + DurableStore.renew_lease required",
)


def _loop_step(*calls):
    """A step exactly as the investigation loop commits it (``_commit_step``)."""
    return {
        "assistant": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
                for index, name in enumerate(calls)
            ],
        },
        "finish_reason": "tool_calls",
        "response_model": "deepseek-flash",
        "usage": {"total_tokens": 10},
        "request_sha256": "0" * 64,
        "response_id": "resp-1",
    }


def test_resume_renews_the_lease_between_executing_and_committing_each_tool():
    """Without the renewal, a short claim lease would expire mid-session and
    ``commit_tool`` would reject the second tool result with CONTROL_DENIED.
    """
    store = DurableStore(DSN)
    store.install()
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-lease-wire-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    dead = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    store.commit_step(dead, "round-1", _loop_step("metrics.range_query", "logs.search"))
    time.sleep(1.2)

    executed = []

    def execute(item):
        executed.append(item["ordinal"])
        # Longer than the claim's own lease_seconds=2 below; only a renewal
        # between execute and commit keeps the second commit_tool call fenced
        # by a still-live lease.
        time.sleep(1.5)
        return {"ok": True, "evidence_id": f"e{item['ordinal']}"}

    session = Worker.create(store, {"state": "v1"}).resume(
        incident, lease_seconds=2, renew_seconds=30
    )
    assert session.execute_pending(execute) == 2
    assert executed == [0, 1]

    rebuilt = store.rebuild(incident)
    assert rebuilt["pending_tools"] == []
    results = {
        item["ordinal"]: item["result"] for item in rebuilt["steps"][0]["tool_results"]
    }
    assert results == {
        0: {"ok": True, "evidence_id": "e0"},
        1: {"ok": True, "evidence_id": "e1"},
    }
    # The lease was extended well past the original 2s claim.
    assert store.lease_current(session.lease)


def test_human_control_between_execute_and_commit_stops_the_commit():
    """Renewal shares the commit fence: a revoked lease is rejected before
    the tool result it protects is ever written.
    """
    store = DurableStore(DSN)
    store.install()
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-lease-wire-revoke-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    dead = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    step = store.commit_step(dead, "round-1", _loop_step("logs.search"))
    time.sleep(1.2)

    session = Worker.create(store, {"state": "v1"}).resume(
        incident, lease_seconds=30, renew_seconds=30
    )

    def execute(item):
        # A human cancels the incident while the tool call is in flight, so
        # the lease is revoked by the time renewal runs before the commit.
        store.control(incident, session.lease.control_generation, "cancel", "operator")
        return {"ok": True}

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        session.execute_pending(execute)

    rebuilt = store.rebuild(incident)
    assert rebuilt["run"]["state"] == "cancelled"
    # The tool result was never committed: the pending tool call is still
    # outstanding on the (now cancelled) step.
    step_tools = [s for s in rebuilt["steps"] if s["step_id"] == step][0]
    assert step_tools["tool_results"] == []


def test_the_lease_is_renewed_before_each_tool_is_dispatched():
    """The lease must cover the in-flight call, not only the commit after it.

    With renewal only between ``execute`` and ``commit``, a callback longer
    than the lease remaining at entry runs unprotected: the lease lapses
    mid-call, a competing worker can claim the Run and repeat the same
    operation, and this session then loses its own result to the fence.
    """
    store = DurableStore(DSN)
    store.install()
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-lease-predispatch-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    dead = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    store.commit_step(dead, "round-1", _loop_step("logs.search"))
    time.sleep(1.2)

    def execute(item):
        # Outlives the 2s initial claim below; only a renewal taken *before*
        # dispatch keeps the Run held for the whole call.
        time.sleep(2.5)
        with pytest.raises(PersistenceError, match="LEASE_ACTIVE"):
            store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=5)
        return {"ok": True, "evidence_id": "e0"}

    session = Worker.create(store, {"state": "v1"}).resume(
        incident, lease_seconds=2, renew_seconds=30
    )
    assert session.execute_pending(execute) == 1

    rebuilt = store.rebuild(incident)
    assert rebuilt["pending_tools"] == []
    assert rebuilt["steps"][0]["tool_results"] == [
        {"ordinal": 0, "result": {"ok": True, "evidence_id": "e0"}}
    ]
