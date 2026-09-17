"""C3 M1-01 control completion contracts."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _store():
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


def _accept(s, *, target=None):
    i, r = uuid4(), uuid4()
    s.accept(
        i,
        r,
        "completion-" + str(i),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=5,
        versions={"v": "1"},
        target_id=target,
    )
    return i, r


def test_scope_suspension_fences_claim_and_release_does_not_resume_old_run():
    s = _store()
    t = s.register_target("target-" + str(uuid4()))
    i, r = _accept(s, target=t)
    assert (
        s.set_target_suspension(t, True, expected_generation=0, actor="operator") == 1
    )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.claim(i, r, uuid4(), {"v": "1"})
    assert (
        s.set_target_suspension(t, False, expected_generation=1, actor="operator") == 2
    )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.claim(i, r, uuid4(), {"v": "1"})


def test_claim_allows_a_fresh_never_suspended_target_at_generation_zero():
    """A registered target that was never suspended keeps target_generation
    at 0 (see set_target_suspension: the first suspend call always bumps 0 to
    1 in the same statement as flipping suspended to True). claim() must not
    treat generation 0 by itself as a denial signal — only the live
    global/target suspended flags do. This is the scenario the removed
    ``target_generation == 0 and target_suspended`` clause in claim() would
    have misread as meaningful; it never fires because target_suspended is
    False here, and claim() must still succeed."""
    s = _store()
    t = s.register_target("target-" + str(uuid4()))
    i, r = _accept(s, target=t)
    lease = s.claim(i, r, uuid4(), {"v": "1"})
    assert lease.target_suspension_generation == 0


def test_follow_up_payload_is_durable_and_readable():
    s = _store()
    i, r = _accept(s)
    assert s.control(i, 0, "follow_up", "operator", {"question": "why"}) == 1
    rows = s.read_inputs(i)
    assert rows[0]["kind"] == "follow_up" and rows[0]["content"] == {"question": "why"}


def test_global_suspension_blocks_budget_and_publish_via_lease_fence():
    s = _store()
    i, r = _accept(s)
    lease = s.claim(i, r, uuid4(), {"v": "1"})
    with s.transaction() as conn:
        generation = conn.execute(
            "SELECT global_generation FROM opspilot_scope_controls WHERE scope_id=1"
        ).fetchone()["global_generation"]
    assert (
        s.set_global_suspension(True, expected_generation=generation, actor="operator")
        == generation + 1
    )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.reserve_budget(lease, uuid4(), 1)
    assert (
        s.set_global_suspension(
            False, expected_generation=generation + 1, actor="operator"
        )
        == generation + 2
    )


def test_new_run_created_while_suspended_persists_paused_on_the_run_row_too():
    """new_run() puts a suspended successor's Incident in 'paused', but used to
    leave the Run row itself at the hardcoded 'queued' it always inserted with
    -- so rebuild_plan() (opspilot/recovery.py) treated it as a live recovery
    candidate and Worker.resume() kept trying a claim the scope fence rejects.
    The Run row must agree with the Incident it belongs to."""
    from opspilot.recovery import rebuild_plan

    s = _store()
    t = s.register_target("target-" + str(uuid4()))
    i, r, next_run = uuid4(), uuid4(), uuid4()
    s.accept(
        i,
        r,
        "new-run-suspended-" + str(i),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=5,
        versions={"v": "1"},
        target_id=t,
    )
    assert s.control(i, 0, "cancel", "operator") == 1
    assert (
        s.set_target_suspension(t, True, expected_generation=0, actor="operator") == 1
    )
    generation = s.new_run(
        i,
        next_run,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=5,
        versions={"v": "1"},
        actor="operator",
    )
    assert generation == 2
    rebuilt = s.rebuild(i)
    assert rebuilt["state"] == "paused"
    assert rebuilt["run"]["run_id"] == next_run
    assert rebuilt["run"]["state"] == "paused"
    assert rebuild_plan(rebuilt).candidate is False
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.claim(i, next_run, uuid4(), {"v": "1"})
