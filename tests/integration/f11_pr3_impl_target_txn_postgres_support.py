"""F11 PR-3 r4: a release delivery registers its target in its own transaction.

Contract behaviour 10 ("no half subject"): a refused or failed delivery for a
resource_uid seen for the first time leaves no ``opspilot_targets`` or
``opspilot_target_suspensions`` row; a created one registers both, and a
replay reuses them. Driven through ``POST /intake/version-changes`` with the
release store connected as the real web role.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
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
