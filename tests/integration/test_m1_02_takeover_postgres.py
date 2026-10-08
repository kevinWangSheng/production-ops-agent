"""Takeover (human_owned) on a real PostgreSQL (M1-02 step 3b, #121).

C3 section 10: "暂停、接管、取消当前调查 ... 在同一事务中增加相关版本并撤销旧
观察任务" and "接管默认停止自动观察；人工可在 human_owned 状态下单独授权观察，
但该授权不恢复 Agent 自动调查". One transaction steps the generation, revokes
the observation and parks the Run; afterwards the Observer leases nothing
and a lease taken before is history; the worker cannot claim; a remediation
registered under human ownership authorizes a session without a new Run;
a takeover racing a registration is serialized by the row lock.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.observation import HealthSample
from opspilot.observation import ObservationStore, SignalReading
from opspilot.observer.health_profile import (
    PROFILE_DIRECTORY,
    canonical_content,
    load_health_profile,
)
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from scripts.m0.postgres_lab import DSN
from tests.target_support import identity_of

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
VERSIONS = {"state": "v1"}


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_tko_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    schema.migrate(dsn, pg_dump=PG_DUMP)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def observer_dsn(scratch_dsn: str) -> Iterator[str]:
    login = f"opspilot_observer_login_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
    try:
        yield make_conninfo(scratch_dsn, user=login)
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.fixture(scope="module")
def owner(scratch_dsn: str) -> Iterator[DurableStore]:
    store = DurableStore(scratch_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def controller(scratch_dsn: str) -> Iterator[ObservationStore]:
    store = ObservationStore(scratch_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def observer(observer_dsn: str) -> Iterator[ObservationStore]:
    store = ObservationStore(observer_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _incident(owner: DurableStore) -> tuple[UUID, UUID, str]:
    incident, run = uuid4(), uuid4()
    uid = f"deployment/checkout-{uuid4().hex[:8]}"
    owner.accept(
        incident,
        run,
        f"tko-{incident}",
        deadline=_now() + timedelta(hours=1),
        budget_limit=10,
        versions=VERSIONS,
        target_id=owner.register_target(uid),
    )
    return incident, run, uid


def _register(
    controller: ObservationStore, incident: UUID, uid: str, *, expected: int
) -> int:
    parameters = PROFILE.session
    return controller.register_remediation(
        incident,
        expected_generation=expected,
        actor="operator",
        revision="checkout:v2",
        deadline_at=_now() + timedelta(seconds=parameters.deadline_seconds),
        max_samples=parameters.max_samples,
        sample_interval_seconds=parameters.sample_interval_seconds,
        sustained_window_seconds=parameters.sustained_window_seconds,
        health_profile_revision=PROFILE.revision,
        health_profile=canonical_content(PROFILE),
        identity=identity_of(uid),
    )


def _incident_row(owner: DurableStore, incident: UUID) -> dict[str, Any]:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT lifecycle,state,mode,control_generation,observation_generation,current_run_id FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return row


def _run_row(owner: DurableStore, run: UUID) -> dict[str, Any]:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT state,owner,lease_until,control_generation FROM opspilot_runs WHERE run_id=%s",
            (run,),
        ).fetchone()
    assert row is not None
    return row


def _sessions(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT session_id,state,ended_reason,subject_control_generation,active_sample_job_id FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY created_at",
                (incident,),
            ).fetchall()
        )


def _runs(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT run_id,state FROM opspilot_runs WHERE incident_id=%s",
                (incident,),
            ).fetchall()
        )


def _audit(owner: DurableStore, incident: UUID) -> list[str]:
    with owner.transaction(snapshot=True) as conn:
        return [
            str(row["action"])
            for row in conn.execute(
                "SELECT action FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
                (incident,),
            ).fetchall()
        ]


def _due_now(owner: DurableStore, session: UUID) -> None:
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (session,),
        )


def test_takeover_steps_the_generation_revokes_observation_and_parks_the_run(
    owner: DurableStore, controller: ObservationStore, observer: ObservationStore
) -> None:
    incident, run, uid = _incident(owner)
    owner.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    assert _register(controller, incident, uid, expected=0) == 1
    (session,) = _sessions(owner, incident)
    _due_now(owner, session["session_id"])
    lease = next(
        item
        for item in observer.claim_due_samples(uuid4())
        if item.session_id == session["session_id"]
    )

    before = _incident_row(owner, incident)
    assert owner.control(incident, 1, "takeover", "operator") == 2

    row = _incident_row(owner, incident)
    assert (row["mode"], row["control_generation"]) == ("human_owned", 2)
    # The control mirror and the lifecycle are not the takeover's to change.
    assert (row["state"], row["lifecycle"]) == (before["state"], "observing_recovery")
    (session,) = _sessions(owner, incident)
    assert (session["state"], session["ended_reason"]) == (
        "revoked",
        "authority_revoked",
    )
    assert session["active_sample_job_id"] is None
    parked = _run_row(owner, run)
    assert (parked["state"], parked["owner"], parked["lease_until"]) == (
        "waiting_human",
        None,
        None,
    )
    assert parked["control_generation"] == 2
    assert _audit(owner, incident) == ["register_remediation", "takeover"]
    # The Observer leases nothing more, and the lease it took before is history.
    assert [
        item
        for item in observer.claim_due_samples(uuid4())
        if item.incident_id == incident
    ] == []
    now = _now()
    receipt = observer.submit_sample(
        lease,
        HealthSample(
            sample_id=str(uuid4()),
            session_id=str(lease.session_id),
            sequence=lease.sequence,
            window=QueryWindow(
                start=now - timedelta(minutes=5), end=now - timedelta(minutes=1)
            ),
            outcome="no_data",
            subject_control_generation=lease.subject_control_generation,
            observation_generation=lease.observation_generation,
            health_profile_revision=lease.health_profile_revision,
            required_signals_present=False,
        ),
        [
            SignalReading(
                signal_name=name,
                status="no_data",
                query="q",
                window_start=now - timedelta(minutes=5),
                window_end=now - timedelta(minutes=1),
                source="prometheus",
            )
            for name in ("deployment_available_replicas",)
        ],
    )
    assert receipt.accepted is False and receipt.disposition == "history_only"
    assert receipt.incident_lifecycle == "observing_recovery"
    # The worker cannot claim, resume and a new Run are refused, a second
    # takeover is an illegal transition.
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        owner.claim(incident, run, uuid4(), VERSIONS)
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        owner.control(incident, 2, "resume", "operator")
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        owner.control(incident, 2, "takeover", "operator")
    assert owner.claimable_incidents(limit=100).count(incident) == 0
    assert _incident_row(owner, incident)["control_generation"] == 2


def test_a_remediation_under_human_ownership_observes_without_a_new_run(
    owner: DurableStore, controller: ObservationStore, observer: ObservationStore
) -> None:
    incident, run, uid = _incident(owner)
    assert owner.control(incident, 0, "takeover", "operator") == 1
    runs_before = _runs(owner, incident)

    assert _register(controller, incident, uid, expected=1) == 2

    row = _incident_row(owner, incident)
    assert (row["mode"], row["lifecycle"], row["control_generation"]) == (
        "human_owned",
        "observing_recovery",
        2,
    )
    (session,) = _sessions(owner, incident)
    assert (
        session["state"] == "authorized" and session["subject_control_generation"] == 2
    )
    assert _runs(owner, incident) == runs_before
    assert _run_row(owner, run)["state"] == "waiting_human"
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        owner.claim(incident, run, uuid4(), VERSIONS)
    assert owner.claimable_incidents(limit=100).count(incident) == 0
    # The Observer may sample the human-authorized session.
    _due_now(owner, session["session_id"])
    assert any(
        item.session_id == session["session_id"]
        for item in observer.claim_due_samples(uuid4())
    )
    # A note is recorded for the human; the Run stays parked, no renewal.
    assert (
        owner.control(
            incident,
            2,
            "follow_up",
            "operator",
            {"text": "handled", "channel": "web"},
            renew_run_id=uuid4(),
            renew_deadline=_now() + timedelta(minutes=5),
        )
        == 3
    )
    assert _runs(owner, incident) == runs_before
    assert _run_row(owner, run)["state"] == "waiting_human"
    # A new Run after cancel is still refused under human ownership.
    assert owner.control(incident, 3, "cancel", "operator") == 4
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        owner.new_run(
            incident,
            uuid4(),
            expected_generation=4,
            deadline=_now() + timedelta(minutes=5),
            budget_limit=10,
            versions=VERSIONS,
            actor="operator",
        )


def test_a_takeover_racing_a_registration_is_serialized(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """Same expected generation; the incident row lock lets exactly one
    apply. A winning takeover leaves no authorized session; a winning
    registration leaves one that the conflicting takeover did not revoke."""
    for _ in range(6):
        incident, _, uid = _incident(owner)
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def act(name: str, fn: Any) -> None:
            barrier.wait()
            try:
                fn()
                results[name] = "applied"
            except PersistenceError as exc:
                results[name] = str(exc)

        threads = [
            threading.Thread(
                target=act,
                args=(
                    "register",
                    lambda: _register(controller, incident, uid, expected=0),
                ),
            ),
            threading.Thread(
                target=act,
                args=("takeover", lambda: owner.control(incident, 0, "takeover", "op")),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results.values()) == ["CONTROL_CONFLICT", "applied"], results
        winner = next(name for name, value in results.items() if value == "applied")
        row = _incident_row(owner, incident)
        assert row["control_generation"] == 1
        sessions = _sessions(owner, incident)
        if winner == "takeover":
            assert row["mode"] == "human_owned" and sessions == []
        else:
            assert row["mode"] == "automatic"
            assert [s["state"] for s in sessions] == ["authorized"]
