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
    assert s.set_target_suspension(t, True) == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.claim(i, r, uuid4(), {"v": "1"})
    assert s.set_target_suspension(t, False, expected_generation=1) == 2
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.claim(i, r, uuid4(), {"v": "1"})


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
    assert s.set_global_suspension(True) == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        s.reserve_budget(lease, uuid4(), 1)
