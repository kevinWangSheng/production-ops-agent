"""F11 PR-3 r4: a release delivery registers its target in its own transaction.

Contract behaviour 10 ("no half subject"): a refused or failed delivery for a
resource_uid seen for the first time leaves no ``opspilot_targets`` or
``opspilot_target_suspensions`` row; a created one registers both, and a
replay reuses them. Driven through ``POST /intake/version-changes`` with the
release store connected as the real web role.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.persistence.grants import WEB_ROLE
from opspilot.tools.fixture import fixture_versions
from opspilot.web import (
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
)
from scripts.m0.postgres_lab import DSN
from tests.f11_pr3_support import (
    PROFILE,
    TARGET,
    build_app,
    error_code,
    iso,
    post_release,
    release_payload,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

POOL = PoolConfig(min_size=1, max_size=8, timeout=5.0)


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def web() -> Iterator[ReleaseStore]:
    with login(DSN, WEB_ROLE) as dsn:
        yield ReleaseStore(dsn, pool=POOL)


class Env:
    """A registry with two never-registered uids: checkout and payment."""

    def __init__(self, owner: DurableStore, web: ReleaseStore) -> None:
        suffix = uuid4().hex[:10]
        self.uid = f"deployment/checkout-txn-{suffix}"
        self.payment_uid = f"deployment/payment-txn-{suffix}"
        self.source = f"kind-harness-{suffix}"
        entry = {
            "integration_id": TARGET["integration_id"],
            "cluster_uid": TARGET["cluster_uid"],
            "namespace": TARGET["namespace"],
            "workload": TARGET["workload"],
            "health_profile_id": PROFILE.profile_id,
        }
        self.app, _ = build_app(
            web,
            registry={
                self.uid: entry,
                self.payment_uid: {**entry, "workload": "payment"},
            },
            workbench_kwargs={
                "incidents": DurableIncidentStore(owner),
                "events": DurableEventLog(owner),
                "evidence": DurableEvidenceStore(owner),
                "ledger": DurableWebLedger(owner),
                "run_versions": fixture_versions(),
            },
            clock=DurableClock(owner),
        )

    def payload(self, uid: str | None = None, **overrides: Any) -> dict[str, Any]:
        target = {**TARGET, "resource_uid": uid or self.uid}
        if uid == self.payment_uid:
            target["workload"] = "payment"
        return release_payload(
            source=self.source,
            release_id=f"rel-{uuid4().hex[:12]}",
            target=target,
            **overrides,
        )

    def post(self, payload: dict[str, Any]) -> Any:
        return post_release(self.app, payload)


@pytest.fixture
def env(owner: DurableStore, web: ReleaseStore) -> Env:
    return Env(owner, web)


def _registered(uid: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        targets = conn.execute(
            "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s", (uid,)
        ).fetchall()
        suspensions = conn.execute(
            "SELECT s.target_id,s.suspended FROM opspilot_target_suspensions s "
            "JOIN opspilot_targets t USING (target_id) WHERE t.resource_uid=%s",
            (uid,),
        ).fetchall()
    return targets, suspensions


def _db_now() -> Any:
    with psycopg.connect(DSN) as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]


def test_first_delivery_registers_target_and_replay_reuses_it(env: Env) -> None:
    payload = env.payload()
    created = env.post(payload)
    assert created.status == 201, created.text
    [target], [suspension] = _registered(env.uid)
    assert suspension == {"target_id": target["target_id"], "suspended": False}

    replay = env.post(payload)
    assert replay.status == 200, replay.text
    assert replay.json()["outcome"] == "replayed"
    another = env.post(env.payload())
    assert another.status == 201, another.text
    assert _registered(env.uid) == ([target], [suspension])
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT DISTINCT target_id FROM opspilot_release_observations "
            "WHERE source=%s",
            (env.source,),
        ).fetchall() == [(target["target_id"],)]


def test_future_release_leaves_no_target(env: Env) -> None:
    payload = env.payload(released_at=None)
    payload["released_at"] = iso(_db_now() + timedelta(hours=1))
    response = env.post(payload)
    assert (response.status, error_code(response)) == (422, "RELEASE_TIME_IN_FUTURE")
    assert _registered(env.uid) == ([], [])


def test_identity_conflict_on_a_new_target_leaves_no_target(env: Env) -> None:
    payload = env.payload()
    assert env.post(payload).status == 201
    changed = env.payload(env.payment_uid)
    changed["source_release_id"] = payload["source_release_id"]
    response = env.post(changed)
    assert (response.status, error_code(response)) == (
        409,
        "RELEASE_IDENTITY_CONFLICT",
    )
    assert _registered(env.payment_uid) == ([], [])


def test_storage_failure_leaves_no_target_and_retry_registers_once(
    env: Env,
) -> None:
    payload = env.payload()
    # The audit insert hits the store's lock_timeout after the release row
    # (and the target registration) were written in the same transaction.
    with psycopg.connect(DSN) as blocker:
        blocker.execute("LOCK TABLE opspilot_release_events IN ACCESS EXCLUSIVE MODE")
        try:
            failed = env.post(payload)
        finally:
            blocker.rollback()
    assert (failed.status, error_code(failed)) == (503, "STORAGE_UNAVAILABLE")
    assert _registered(env.uid) == ([], [])

    retry = env.post(payload)
    assert retry.status == 201, retry.text
    targets, suspensions = _registered(env.uid)
    assert len(targets) == len(suspensions) == 1


def test_concurrent_first_deliveries_share_one_registration(env: Env) -> None:
    payloads = [env.payload() for _ in range(4)]
    barrier = threading.Barrier(len(payloads))
    statuses: list[int] = []

    def deliver(payload: dict[str, Any]) -> None:
        barrier.wait()
        statuses.append(env.post(payload).status)

    threads = [threading.Thread(target=deliver, args=(p,)) for p in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert statuses == [201] * len(payloads)
    targets, suspensions = _registered(env.uid)
    assert len(targets) == len(suspensions) == 1
