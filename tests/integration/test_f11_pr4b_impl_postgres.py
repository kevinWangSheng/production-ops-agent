"""Implementation tests for F11 PR-4b: anomaly consumption, links, v6 input.

The independent acceptance tests live on their own branch; these pin the
implementer's own edges (lock and guard behaviour, wait bound, replay and
rebuild) against a real PostgreSQL through the product roles.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

from opspilot.investigation.context import (
    INPUT_VERSION_RELEASE,
    RELEASE_OBSERVATION,
    InvestigationInput,
)
from opspilot.observation import release_commands
from opspilot.observation.store import ObservationStore
from opspilot.observer.release_replay import replay_release_history
from opspilot.persistence import DurableStore
from opspilot.persistence.base import PersistenceError
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import Workbench
from opspilot.web.store import DurableIncidentStore, DurableWebLedger
from scripts.m0.postgres_lab import DSN
from tests.integration import test_f11_pr2b_impl_postgres as pr2b
from tests.integration.test_f11_pr4a_comparison_impl_postgres import _anomalous, _drain
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
release_harness = pr2b.release_harness


@pytest.fixture
def world(release_harness):
    web, observer, oid, _, _ = release_harness
    _anomalous(release_harness)
    store = DurableStore(web.dsn)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        releases=web,
    )
    yield workbench, web, observer, oid, store


def _consume(workbench, oid):
    for receipt in workbench.release_anomaly_pass(limit=500):
        if receipt.observation_id == oid:
            return receipt
    return None


def _target(web, oid):
    return web.get_release(oid).target_id


def _incident(target_id, *, lifecycle="open"):
    store = DurableStore(DSN)
    iid, rid = uuid4(), uuid4()
    store.accept(
        iid,
        rid,
        f"intake:pre-{iid}",
        deadline=store_now() + timedelta(hours=1),
        budget_limit=3,
        versions=fixture_versions(),
        target_id=target_id,
    )
    if lifecycle != "open":
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                (lifecycle, iid),
            )
    return iid, rid


def store_now():
    with psycopg.connect(DSN) as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        return row[0]


def test_waits_for_the_comparison_then_creates_once(world, monkeypatch):
    workbench, web, observer, oid, _ = world
    # The comparison is not recorded yet: within the wait the ending stays.
    assert _consume(workbench, oid) is None
    assert web.release_link(oid) is None
    _drain(observer)  # files this ending's comparison (zero queries)
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "created"
    assert not receipt.replayed and receipt.run_id is not None
    assert web.get_release(oid).incident_id == receipt.incident_id
    with psycopg.connect(DSN) as conn:
        run = conn.execute(
            "SELECT state,input FROM opspilot_runs WHERE run_id=%s", (receipt.run_id,)
        ).fetchone()
        priority = conn.execute(
            "SELECT priority FROM opspilot_incidents WHERE incident_id=%s",
            (receipt.incident_id,),
        ).fetchone()
    assert run is not None and run[0] == "queued" and priority == (1,)
    parsed = InvestigationInput.from_json(run[1])
    assert parsed.version == INPUT_VERSION_RELEASE
    fact = parsed.scope_facts[RELEASE_OBSERVATION]
    assert fact["observation_id"] == str(oid)
    assert fact["comparison"]["status"] != "unavailable" or fact["comparison"][
        "reason_codes"
    ] != ["COMPARISON_NOT_RECORDED"]
    policy = parsed.evidence_context["time_policies"][0]  # type: ignore[index]
    assert policy["anchor_rule"] == "release_released_at"
    # A second pass and a direct retry open nothing more.
    assert _consume(workbench, oid) is None
    again = web.consume_anomaly(
        _ending_of(web, oid), opening=workbench._release_opening
    )
    assert (
        again is not None
        and again.replayed
        and again.incident_id == receipt.incident_id
    )


def _ending_of(web, oid):
    with web.transaction(snapshot=True) as conn:
        return web._anomaly_ending_in(conn, oid)


def test_wait_bound_consumes_with_comparison_unavailable(world, monkeypatch):
    workbench, web, _, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "created"
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT r.input,l.comparison_id FROM opspilot_runs r JOIN opspilot_release_incident_links l ON l.run_id=r.run_id WHERE l.observation_id=%s",
            (oid,),
        ).fetchone()
    assert row is not None and row[1] is None
    fact = row[0]["scope_facts"][RELEASE_OBSERVATION]
    assert fact["comparison"]["status"] == "unavailable"
    assert fact["comparison"]["reason_codes"] == ["COMPARISON_NOT_RECORDED"]


def test_open_incident_takes_one_event_and_nothing_else(world, monkeypatch):
    workbench, web, _, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    iid, rid = _incident(_target(web, oid), lifecycle="resolved")
    with psycopg.connect(DSN) as conn:
        before = conn.execute(
            "SELECT control_generation,mode,lifecycle,current_run_id,priority,state FROM opspilot_incidents WHERE incident_id=%s",
            (iid,),
        ).fetchone()
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "attached"
    assert receipt.incident_id == iid and receipt.run_id is None
    with psycopg.connect(DSN) as conn:
        after = conn.execute(
            "SELECT control_generation,mode,lifecycle,current_run_id,priority,state FROM opspilot_incidents WHERE incident_id=%s",
            (iid,),
        ).fetchone()
        runs = conn.execute(
            "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s", (iid,)
        ).fetchone()
        events = conn.execute(
            "SELECT input_id,content,actor FROM opspilot_inputs WHERE incident_id=%s AND kind='event'",
            (iid,),
        ).fetchall()
    assert before == after and runs == (1,)
    assert [e[0] for e in events] == [receipt.input_id]
    assert events[0][1]["channel"] == "release_observation"
    assert events[0][2] == "release_observation"


def test_closed_incident_relates_a_new_one(world, monkeypatch):
    workbench, web, _, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    old, _ = _incident(_target(web, oid), lifecycle="closed")
    newer, _ = _incident(_target(web, oid), lifecycle="closed")
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "created_related"
    assert receipt.related_incident_id == newer and receipt.incident_id not in (
        old,
        newer,
    )
    with psycopg.connect(DSN) as conn:
        lifecycles = conn.execute(
            "SELECT lifecycle FROM opspilot_incidents WHERE incident_id = ANY(%s)",
            ([old, newer],),
        ).fetchall()
    assert lifecycles == [("closed",), ("closed",)]


def test_unresolvable_target_is_handed_off(world, monkeypatch):
    workbench, web, _, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE opspilot_targets SET workload='elsewhere' WHERE target_id=%s",
            (_target(web, oid),),
        )
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.disposition == "handoff_created"
    assert receipt.handoff_reason == "RELEASE_TARGET_UNRESOLVED"
    assert receipt.run_id is None
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT state,current_run_id,target_id FROM opspilot_incidents WHERE incident_id=%s",
            (receipt.incident_id,),
        ).fetchone()
    assert row == ("waiting_human", None, None)


def test_guards_refuse_unlinked_writes_and_other_roles(world, monkeypatch):
    workbench, web, _, oid, _ = world
    iid, _ = _incident(_target(web, oid))
    # The web role cannot name an incident without a link in the transaction.
    with pytest.raises(PersistenceError) as refused, web.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_release_observations SET incident_id=%s WHERE observation_id=%s",
            (iid, oid),
        )
    assert refused.value.args[0] == "INTEGRITY_REFUSED"
    with login(DSN, "opspilot_observer") as observer_dsn:
        with psycopg.connect(observer_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE opspilot_release_observations SET incident_id=%s WHERE observation_id=%s",
                    (iid, oid),
                )
        with psycopg.connect(observer_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT 1 FROM opspilot_release_incident_links")
    with login(DSN, "opspilot_worker") as worker_dsn:
        with psycopg.connect(worker_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT 1 FROM opspilot_release_incident_links")
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    receipt = _consume(workbench, oid)
    assert receipt is not None
    # Linked once: neither the link nor the release edge moves again.
    with pytest.raises(PersistenceError), web.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_release_incident_links SET handoff_reason='x' WHERE observation_id=%s",
            (oid,),
        )
    with pytest.raises(PersistenceError), web.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_release_observations SET incident_id=%s WHERE observation_id=%s",
            (iid, oid),
        )


def test_replay_checks_the_link(world, monkeypatch):
    workbench, web, observer, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    receipt = _consume(workbench, oid)
    assert receipt is not None
    owner = ObservationStore(DSN)
    session = web.get_release(oid)
    with owner.transaction(snapshot=True) as conn:
        sid = conn.execute(
            "SELECT session_id FROM opspilot_observation_sessions WHERE release_observation_id=%s",
            (oid,),
        ).fetchone()["session_id"]
    history = owner.session_history(sid)
    assert history["link"]["incident_id"] == receipt.incident_id
    assert "LINK_MISMATCH" not in replay_release_history(history).integrity
    for field, value in (
        ("incident_id", uuid4()),
        ("ending_id", uuid4()),
        ("anomaly_key", "release-anomaly:" + str(uuid4())),
    ):
        tampered = {**history, "link": {**history["link"], field: value}}
        replay = replay_release_history(tampered)
        assert not replay.consistent and "LINK_MISMATCH" in replay.integrity
    missing = {**history, "link": None}
    assert "LINK_MISMATCH" in replay_release_history(missing).integrity
    assert session.status == "anomalous"


def test_successor_keeps_the_release_fact_and_refuses_a_tampered_one(
    world, monkeypatch
):
    workbench, web, _, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    receipt = _consume(workbench, oid)
    assert receipt is not None and receipt.run_id is not None
    summary = workbench.incidents.find_incident(receipt.incident_id)
    assert summary is not None
    deadline = store_now() + timedelta(minutes=10)
    successor = workbench._successor_input(summary, uuid4(), deadline)
    assert successor is not None
    with psycopg.connect(DSN) as conn:
        first = conn.execute(
            "SELECT input FROM opspilot_runs WHERE run_id=%s", (receipt.run_id,)
        ).fetchone()[0]
    assert successor["version"] == INPUT_VERSION_RELEASE
    assert (
        successor["scope_facts"][RELEASE_OBSERVATION]
        == first["scope_facts"][RELEASE_OBSERVATION]
    )
    tampered = dict(first)
    tampered["scope_facts"] = {
        **first["scope_facts"],
        RELEASE_OBSERVATION: {
            **first["scope_facts"][RELEASE_OBSERVATION],
            "source_release_id": "forged",
        },
    }
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE opspilot_runs SET input=%s WHERE run_id=%s",
            (psycopg.types.json.Jsonb(tampered), receipt.run_id),
        )
    with pytest.raises(PersistenceError) as refused:
        workbench._successor_input(summary, uuid4(), deadline)
    assert refused.value.args[0] == "INCOMPATIBLE_STATE"


def test_concurrent_passes_link_once(world, monkeypatch):
    import threading

    workbench, web, _, oid, store = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    ending = _ending_of(web, oid)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def consume() -> None:
        barrier.wait()
        results.append(web.consume_anomaly(ending, opening=workbench._release_opening))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(r.replayed for r in results) == [False, True]  # type: ignore[attr-defined]
    assert len({r.incident_id for r in results}) == 1  # type: ignore[attr-defined]
    with psycopg.connect(DSN) as conn:
        count = conn.execute(
            "SELECT count(*) FROM opspilot_incidents WHERE intake_key=%s",
            ("intake:release-anomaly:" + str(oid),),
        ).fetchone()
    assert count == (1,)


def test_suspended_target_still_opens_the_incident_paused(world, monkeypatch):
    workbench, web, _, oid, _ = world
    monkeypatch.setattr(release_commands, "comparison_wait_seconds", lambda: 0)
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_target_suspensions SET suspended=true,generation=generation+1 WHERE target_id=%s",
            (_target(web, oid),),
        )
    try:
        receipt = _consume(workbench, oid)
        assert receipt is not None and receipt.disposition == "created"
        with psycopg.connect(DSN) as conn:
            state = conn.execute(
                "SELECT state FROM opspilot_runs WHERE run_id=%s", (receipt.run_id,)
            ).fetchone()
        # Like the alert intake (R10): created, fenced until the target resumes.
        assert state == ("paused",)
    finally:
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_target_suspensions SET suspended=false,generation=generation+1 WHERE target_id=%s",
                (_target(web, oid),),
            )
