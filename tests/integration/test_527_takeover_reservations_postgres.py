"""#527: a lease takeover settles the reservations the old attempt left open.

Contract: docs/tasks/2026-11-22-next-batches-b3-prereview.md B3-R1..R4 and
categories 2/3. When ``claim()`` takes an expired lease over with a new epoch,
every ``state='reserved'`` row of that Run becomes ``unknown`` with
``seconds = reserved_seconds``; the amount moves from ``budget_reserved`` to
``budget_unknown`` so the request total and the model seconds stay the same.
The revoked attempt can no longer settle it. A claim that does not take over
(version mismatch -> blocked) leaves reservations alone (B3-R2).

Driven only through the public ``DurableStore`` API; the one direct write is
moving ``lease_until`` into the past on the DB clock, standing in for a worker
that was killed and stopped renewing.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

VERSIONS = {"state": "v1"}


@pytest.fixture
def store():
    store = DurableStore(DSN, pool=PoolConfig(min_size=0, max_size=2))
    store.install()
    return store


def _accept(store: DurableStore) -> tuple[UUID, UUID]:
    incident, run = uuid4(), uuid4()
    with store.transaction() as conn:
        now = store._db_now(conn)
    store.accept(
        incident,
        run,
        f"issue-527-{incident}",
        deadline=now + timedelta(minutes=30),
        budget_limit=5,
        versions=VERSIONS,
    )
    return incident, run


def _expire_lease(store: DurableStore, run: UUID) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )


def _reservation(store: DurableStore, reservation_id: UUID) -> dict:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT state,seconds,reserved_seconds,amount FROM opspilot_budget_reservations WHERE reservation_id=%s",
            (reservation_id,),
        ).fetchone()


def _budget(store: DurableStore, run: UUID) -> dict:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT budget_reserved,budget_spent,budget_unknown FROM opspilot_runs WHERE run_id=%s",
            (run,),
        ).fetchone()


def test_takeover_settles_orphaned_reservation_as_unknown(store):
    incident, run = _accept(store)
    old = store.claim(incident, run, uuid4(), VERSIONS, 600)

    # One call finished before the crash, one was in flight when it was killed.
    finished, in_flight = uuid4(), uuid4()
    store.reserve_budget(old, finished, 1, seconds=30.0)
    store.settle_budget(old, finished, "spent", seconds=3.0)
    store.reserve_budget(old, in_flight, 1, seconds=42.0)

    before = _budget(store, run)
    assert (
        before["budget_reserved"],
        before["budget_spent"],
        before["budget_unknown"],
    ) == (1, 1, 0)
    usage_before = store.run_usage(run)

    _expire_lease(store, run)
    new = store.claim(incident, run, uuid4(), VERSIONS, 600)
    assert new.epoch == old.epoch + 1

    orphan = _reservation(store, in_flight)
    assert orphan["state"] == "unknown"
    assert orphan["seconds"] == orphan["reserved_seconds"] == 42.0

    settled = _reservation(store, finished)
    assert (settled["state"], settled["seconds"]) == ("spent", 3.0)

    after = _budget(store, run)
    assert (
        after["budget_reserved"],
        after["budget_spent"],
        after["budget_unknown"],
    ) == (0, 1, 1)
    assert sum(after.values()) == sum(before.values())
    usage_after = store.run_usage(run)
    assert (
        usage_after["model_requests_used"] == usage_before["model_requests_used"] == 2
    )
    assert (
        usage_after["model_seconds_used"] == usage_before["model_seconds_used"] == 45.0
    )

    # The revoked attempt cannot settle the row the takeover already settled.
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.settle_budget(old, in_flight, "spent", seconds=1.0)
    assert _reservation(store, in_flight)["state"] == "unknown"
    assert _budget(store, run) == after


def test_claim_that_blocks_on_version_mismatch_leaves_reservations(store):
    incident, run = _accept(store)
    old = store.claim(incident, run, uuid4(), VERSIONS, 600)
    in_flight = uuid4()
    store.reserve_budget(old, in_flight, 1, seconds=42.0)

    _expire_lease(store, run)
    with pytest.raises(PersistenceError, match="INCOMPATIBLE_STATE"):
        store.claim(incident, run, uuid4(), {"state": "v2"}, 600)

    row = _reservation(store, in_flight)
    assert (row["state"], row["seconds"]) == ("reserved", None)
    budget = _budget(store, run)
    assert (
        budget["budget_reserved"],
        budget["budget_spent"],
        budget["budget_unknown"],
    ) == (1, 0, 0)
