"""#245 PR-2 implementation check: a release anomaly's correlation follows
merge pointers as an alert's does (U5 ②, review on #380)."""

# ruff: noqa: F811 - pytest fixtures imported from the F11 modules
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import timedelta

import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.web import Workbench
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401 - fixtures
    _by_intake,
    _case,
    _compare,
    _db_now,
    _incident,
    _seed,
    _settle,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    Catalog,
    catalog,
    observer,
    observer_dsn,
    owner,
    web,
    web_dsn,
    worker_dsn,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


def test_a_release_relates_to_the_survivor_not_the_newer_merged_source(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    now = _db_now()
    survivor = _seed(
        owner, case.target_id, lifecycle="closed", created_at=now - timedelta(hours=3)
    )
    source = _seed(
        owner, case.target_id, lifecycle="closed", created_at=now - timedelta(hours=1)
    )
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET merged_into_incident_id=%s WHERE incident_id=%s",
            (survivor, source),
        )
    _compare(observer, case)

    bench(case.registry()).release_anomaly_pass()

    (created,) = _by_intake(case.intake_key)
    assert _incident(created["incident_id"])["related_incident_id"] == survivor
