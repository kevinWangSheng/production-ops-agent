"""Stand-in (no PG) input contracts for round-07 control additions.

Every invalid input must fail closed before any database connection is made.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.step_store import Fence, StepStore


class DeniedLedger:
    _dsn = "host=invalid.invalid port=1 dbname=x user=x"

    def _transaction(self):
        raise AssertionError("no database access expected")


@pytest.fixture
def store():
    return StepStore(DeniedLedger())


def run():
    return RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=1)
    )


@pytest.mark.parametrize(
    "scope,target",
    [("region", None), ("target", None), ("target", ""), ("global", "svc")],
)
def test_pause_and_resume_scope_validation(store, scope, target):
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.pause(scope, target=target)
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.resume(scope, target=target)


def test_pause_reason_is_bounded_text(store):
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.pause("global", reason="x" * 201)
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.pause("global", reason=5)


def test_subject_control_still_rejects_scope_actions(store):
    # pause/resume are scope controls, not generation-fenced subject actions.
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.control(uuid4(), 0, "pause")
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.control(uuid4(), 0, "resume")


def test_accept_target_key_validation(store):
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.accept(run(), "k", {}, {"state": "v3"}, target="")
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.accept(run(), "k", {}, {"state": "v3"}, target=7)


def test_observer_authorization_validation(store):
    now = datetime.now(timezone.utc)
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.authorize_observer(
            uuid4(), run(), window_start=now, window_end=now, query_limit=1
        )
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.authorize_observer(
            uuid4(),
            run(),
            window_start=now,
            window_end=now + timedelta(minutes=1),
            query_limit=0,
        )
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.authorize_observer(
            uuid4(),
            run(),
            window_start=now.replace(tzinfo=None),
            window_end=now + timedelta(minutes=1),
            query_limit=1,
        )
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.observe(uuid4(), run(), "not-a-uuid", lambda: None)
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.observe(
            uuid4(), run(), uuid4(), lambda: None, now=now.replace(tzinfo=None)
        )


def test_observation_and_interruption_validation(store):
    fence = Fence(uuid4(), run(), 0, uuid4(), 0)
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.record_observation(uuid4(), object(), object(), captured_at="now")
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.record_stream_interruption(
            fence, uuid4(), partial_sha256=None, partial_bytes=-1
        )
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.record_stream_interruption(
            fence, "rid", partial_sha256=None, partial_bytes=0
        )
