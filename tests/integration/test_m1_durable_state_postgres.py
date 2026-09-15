import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def test_commit_visibility_restart_control_late_and_budget():
    store = DurableStore(DSN)
    store.install()
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    owner = uuid4()
    lease = store.claim(incident, run, owner, {"state": "v1"})
    reservation = uuid4()
    store.reserve_budget(lease, reservation, 7)
    with pytest.raises(PersistenceError, match="BUDGET_EXHAUSTED"):
        store.reserve_budget(lease, uuid4(), 4)
    step = store.commit_step(lease, "round-0", {"role": "assistant", "tool_calls": []})
    assert step
    assert store.rebuild(incident)["steps"][0]["status"] == "response_committed"
    generation = store.control(incident, 0, "cancel", "operator")
    assert generation == 1
    assert store.publish(lease, {"result": "supported"}, step_id=step) is False
    rebuilt = store.rebuild(incident)
    assert rebuilt["control_generation"] == 1
    assert rebuilt["conclusion"] is None


def test_incompatible_versions_block_without_silent_resume():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-incompat-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with pytest.raises(PersistenceError, match="INCOMPATIBLE_STATE"):
        store.claim(incident, run, uuid4(), {"state": "v2"})
    assert store.rebuild(incident)["run"]["state"] == "blocked"


def test_partial_tool_checkpoint_and_lease_fencing():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-tools-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    step = store.commit_step(
        lease, "round-1", {"tool_calls": [{"id": "a"}, {"id": "b"}]}
    )
    store.commit_tool(lease, step, 0, {"ok": True})
    assert store.rebuild(incident)["pending_tools"][0]["ordinal"] == 1


def test_pause_resume_fences_run_and_terminal_incident_cannot_reclaim():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-control-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "pause", "operator") == 1
    assert store.control(incident, 1, "resume", "operator") == 2
    resumed = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert resumed.control_generation == 2
    final_step = store.commit_step(resumed, "final", {"result": "supported"})
    assert store.publish(resumed, {"result": "supported"}, step_id=final_step) is True
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        store.control(incident, 2, "resume", "operator")
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.publish(lease, {"result": "late"}, step_id=uuid4()) is False


def test_expired_lease_cannot_publish_or_reserve():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-expiry-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.reserve_budget(lease, uuid4(), 1)
    assert store.publish(lease, {"result": "expired"}, step_id=uuid4()) is False
