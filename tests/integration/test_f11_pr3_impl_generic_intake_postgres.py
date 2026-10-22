"""F11 PR-3 r5: the release-deployer token opens only the release route (U4).

Its digest sits in the shared ``OPSPILOT_EVENT_TOKENS``, so the generic event
routes must refuse that actor themselves: ``/intake/events`` and
``/intake/alertmanager`` answer 403 and write no incident, Run or alert row,
while other event actors keep using them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest

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
    ALERT_TOKEN,
    OTHER_TOKEN,
    RELEASE_TOKEN,
    TARGET,
    bearer_for,
    build_app,
    call,
    error_code,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

POOL = PoolConfig(min_size=1, max_size=8, timeout=5.0)
WRITTEN = (
    "opspilot_incidents",
    "opspilot_runs",
    "opspilot_alert_identities",
    "opspilot_alert_deliveries",
    "opspilot_web_ledger",
    "opspilot_release_observations",
    "opspilot_release_events",
)


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def app(owner: DurableStore) -> Iterator[Any]:
    with login(DSN, WEB_ROLE) as dsn:
        app, _ = build_app(
            ReleaseStore(dsn, pool=POOL),
            workbench_kwargs={
                "incidents": DurableIncidentStore(owner),
                "events": DurableEventLog(owner),
                "evidence": DurableEvidenceStore(owner),
                "ledger": DurableWebLedger(owner),
                "run_versions": fixture_versions(),
            },
            clock=DurableClock(owner),
        )
        yield app


def _counts() -> dict[str, int]:
    with psycopg.connect(DSN) as conn:
        return {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # type: ignore[index]
            for table in WRITTEN
        }


def _event(app: Any, token: str) -> Any:
    return call(
        app,
        "POST",
        "/intake/events",
        headers={"content-type": "application/json", **bearer_for(token)},
        body=json.dumps(
            {
                "source": "alertmanager",
                "external_event_id": f"e-{uuid4().hex}",
                "target_id": TARGET["resource_uid"],
                "question": "why is checkout failing?",
            }
        ).encode(),
    )


def _alertmanager(app: Any, token: str) -> Any:
    webhook = {
        "receiver": "opspilot",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "Unmatched",
                    "service": f"nobody-{uuid4().hex[:6]}",
                },
                "annotations": {},
                "startsAt": "2026-10-10T10:41:54.365Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": uuid4().hex[:16],
            }
        ],
        "version": "4",
    }
    return call(
        app,
        "POST",
        "/intake/alertmanager",
        headers={"content-type": "application/json", **bearer_for(token)},
        body=json.dumps(webhook).encode(),
    )


@pytest.mark.parametrize("post", [_event, _alertmanager], ids=["events", "alerts"])
def test_release_actor_is_refused_on_generic_intake_and_writes_nothing(
    app: Any, post: Any
) -> None:
    before = _counts()
    response = post(app, RELEASE_TOKEN)
    assert (response.status, error_code(response)) == (
        403,
        "RELEASE_ACTOR_FORBIDDEN",
    )
    assert _counts() == before


def test_release_actor_is_refused_before_the_body_is_read(app: Any) -> None:
    response = call(
        app,
        "POST",
        "/intake/events",
        headers={"content-type": "application/json", **bearer_for(RELEASE_TOKEN)},
        body=b"not json",
    )
    assert (response.status, error_code(response)) == (
        403,
        "RELEASE_ACTOR_FORBIDDEN",
    )


def test_other_event_actors_still_use_generic_intake(app: Any) -> None:
    created = _event(app, OTHER_TOKEN)
    assert created.status == 201, created.text
    alerts = _alertmanager(app, ALERT_TOKEN)
    assert alerts.status == 200, alerts.text
    assert all(r["outcome"] != "failed" for r in alerts.json()["results"])
