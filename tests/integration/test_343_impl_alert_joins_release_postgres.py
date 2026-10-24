"""Implementation tests for #343: an alert that arrives after a release
anomaly opened an incident on the same target joins it (U1-A, the claim),
and a closed release incident can be the related one (U2-A).

The independent acceptance tests live on their own branch; these pin the
implementer's own edges at the store level, through the web role, with a
release incident made by the real F11 consumption path. Opt-in like the
other PG tests (``M1_DURABLE_POSTGRES=1``).
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.observation import release_commands
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import fixture_versions
from scripts.m0.postgres_lab import DSN
from tests.integration import test_f11_pr2b_impl_postgres as pr2b
from tests.integration.test_f11_pr4b_impl_postgres import (
    _consume,
    _incident,
    _target,
    world,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
release_harness = pr2b.release_harness
__all__ = ["release_harness", "world"]


@pytest.fixture
def released(world, monkeypatch):
    """A release-opened incident R and its target's resource_uid."""
    workbench, web, _, oid, store = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "created"
    target_id = _target(web, oid)
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT resource_uid FROM opspilot_targets WHERE target_id=%s",
            (target_id,),
        ).fetchone()
    assert row is not None
    return store, receipt.incident_id, target_id, row[0]


def _firing(store, resource_uid, fingerprint, *, priority=2, starts_at=None):
    starts_at = starts_at or datetime.now(timezone.utc).replace(microsecond=0)
    delivery = dict(
        delivery_key=f"{fingerprint}:{starts_at.isoformat()}",
        fingerprint=fingerprint,
        starts_at=starts_at,
        starts_at_raw=starts_at.isoformat(),
        status="firing",
        actor="alertmanager",
        raw_sha256="0" * 64,
        annotations_sha256="0" * 64,
        alert_json="{}",
        truncated=False,
    )
    incident_id = uuid4()
    opening = dict(
        incident_id=incident_id,
        intake_key=f"alert:{incident_id}",
        run_id=uuid4(),
        resource_uid=resource_uid,
        namespace="otel-demo",
        workload="checkout",
        deadline=datetime.now(timezone.utc) + timedelta(hours=1),
        budget_limit=3,
        versions=fixture_versions(),
        input=None,
        ledger=(),
        priority=priority,
    )
    return store.record_alert(delivery, opening=opening)


def _row(incident_id: UUID):
    with psycopg.connect(DSN) as conn:
        incident = conn.execute(
            "SELECT state,lifecycle,mode,control_generation,current_run_id,priority FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
        runs = conn.execute(
            "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s", (incident_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT content FROM opspilot_inputs WHERE incident_id=%s AND kind='event' AND content->>'channel'='alertmanager' ORDER BY sequence",
            (incident_id,),
        ).fetchall()
        identities = conn.execute(
            "SELECT count(*) FROM opspilot_alert_identities WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
    return incident, runs[0], [e[0] for e in events], identities[0]


def _close(incident_id: UUID) -> None:
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            (incident_id,),
        )


def test_first_alert_joins_the_release_incident(released):
    store, rid, _, uid = released
    before, runs, events, _ = _row(rid)
    assert runs == 1 and events == [] and before[5] == 1
    fp = "fp-" + uuid4().hex
    result = _firing(store, uid, fp, priority=0)
    assert result["outcome"] == "attached"
    assert result["incident_id"] == rid and result["run_id"] is None
    assert result["related_incident_id"] is None
    after, runs, events, identities = _row(rid)
    # R4: priority only rises; nothing else of the incident changes.
    assert after[:5] == before[:5] and after[5] == 0
    assert runs == 1 and identities == 0
    assert len(events) == 1
    assert events[0]["fingerprint"] == fp
    assert "release-anomaly incident" in events[0]["text"]
    assert "fired again" not in events[0]["text"]
    # R-C3: a repeat of the same episode adds no event; a new startsAt does.
    assert _firing(store, uid, fp, starts_at=_starts(events[0]))["outcome"] == (
        "attached"
    )
    assert len(_row(rid)[2]) == 1
    later = _starts(events[0]) + timedelta(minutes=5)
    assert _firing(store, uid, fp, starts_at=later)["incident_id"] == rid
    assert len(_row(rid)[2]) == 2


def _starts(event) -> datetime:
    return datetime.fromisoformat(event["starts_at"].replace("Z", "+00:00"))


def test_a_claimed_release_incident_takes_no_other_fingerprint(released):
    store, rid, _, uid = released
    assert _firing(store, uid, "fp-x-" + uuid4().hex)["incident_id"] == rid
    other = _firing(store, uid, "fp-y-" + uuid4().hex)
    assert other["outcome"] == "created" and other["incident_id"] != rid
    assert other["related_incident_id"] is None
    assert len(_row(rid)[2]) == 1


def test_concurrent_first_alerts_claim_once(released):
    store, rid, _, uid = released
    stores = [DurableStore(store.dsn) for _ in range(2)]
    holder = psycopg.connect(DSN)
    try:
        # Both deliveries queue on R's row lock, so they really contend.
        holder.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (rid,),
        )
        with ThreadPoolExecutor(2) as pool:
            started = threading.Barrier(3)

            def deliver(i):
                started.wait()
                return _firing(stores[i], uid, f"fp-{i}-" + uuid4().hex)

            futures = [pool.submit(deliver, i) for i in range(2)]
            started.wait()
            time.sleep(0.5)
            holder.commit()
            results = [f.result(timeout=30) for f in futures]
    finally:
        holder.close()
        for s in stores:
            s.close()
    assert sorted(r["outcome"] for r in results) == ["attached", "created"]
    assert [r["incident_id"] for r in results if r["outcome"] == "attached"] == [rid]
    assert len(_row(rid)[2]) == 1


def test_closed_release_incident_is_the_related_one(released):
    store, rid, _, uid = released
    _close(rid)
    result = _firing(store, uid, "fp-" + uuid4().hex)
    assert result["outcome"] == "created" and result["related_incident_id"] == rid
    assert _row(rid)[0][1] == "closed"


def test_closed_release_incident_claimed_by_another_is_not_related(released):
    store, rid, _, uid = released
    assert _firing(store, uid, "fp-y-" + uuid4().hex)["incident_id"] == rid
    _close(rid)
    result = _firing(store, uid, "fp-x-" + uuid4().hex)
    assert result["outcome"] == "created" and result["related_incident_id"] is None


def test_a_manual_incident_on_the_target_is_not_a_candidate(released):
    store, rid, target_id, uid = released
    _close(rid)
    manual, _ = _incident(target_id)
    result = _firing(store, uid, "fp-" + uuid4().hex)
    assert result["outcome"] == "created"
    assert result["incident_id"] != manual and result["related_incident_id"] == rid
    assert _row(manual)[2] == []


def test_a_release_incident_on_another_target_is_not_a_candidate(released):
    store, rid, _, _ = released
    other = "other-" + uuid4().hex
    result = _firing(store, other, "fp-" + uuid4().hex)
    assert result["outcome"] == "created" and result["related_incident_id"] is None
    assert _row(rid)[2] == []
