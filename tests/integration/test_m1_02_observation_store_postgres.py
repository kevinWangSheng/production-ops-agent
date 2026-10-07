"""Observation sessions and samples on a real PostgreSQL (M1-02 step 2, #84).

Covers the atomic adoption transaction (C3 section 10 "采样与提交"), the
Observer role boundary (section 3, decision D3), lease retries keeping the
logical sequence, deadline/budget exhaustion handing back to ``open``, and
replay from stored rows alone (F6 step 5).

One scratch database per module (migrated to head); every test works on its
own incident and target, except the global-suspension test which restores
the gate before returning.
"""

import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.intake import Target
from opspilot.domain.observation import HealthSample
from opspilot.observation import (
    ObservationStore,
    SampleLease,
    SampleReceipt,
    SignalReading,
)
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=3, timeout=5.0)
PROFILE = "checkout@abc123def456"


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_obs_{uuid4().hex[:12]}"
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
    """A LOGIN role that is a member of ``opspilot_observer`` (set up outside
    the repository in a deployment; here per module, dropped afterwards)."""
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
    store.install()  # SELECT on alembic_version is part of the grant
    yield store
    store.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _target(uid: str) -> Target:
    return Target(
        integration_id="prom-lab",
        cluster_uid="kind-lab",
        namespace="otel-demo",
        resource_uid=uid,
        revision="r1",
    )


def _incident(owner: DurableStore) -> tuple[UUID, UUID, Target]:
    incident, run = uuid4(), uuid4()
    uid = f"deployment/checkout-{uuid4().hex[:8]}"
    target_id = owner.register_target(uid)
    owner.accept(
        incident,
        run,
        f"obs-{incident}",
        deadline=_now() + timedelta(hours=1),
        budget_limit=10,
        versions={"v": "1"},
        target_id=target_id,
    )
    return incident, target_id, _target(uid)


def _authorize(
    controller: ObservationStore,
    incident: UUID,
    target: Target,
    *,
    max_samples: int = 10,
    sustained: int = 60,
    deadline: timedelta = timedelta(hours=1),
    profile: str | None = PROFILE,
) -> UUID:
    return controller.authorize_session(
        incident,
        target=target,
        actor="tester",
        deadline_at=_now() + deadline,
        max_samples=max_samples,
        sample_interval_seconds=15,
        sustained_window_seconds=sustained,
        health_profile_revision=profile,
        first_sample_due_at=_now(),
    )


def _claim(observer: ObservationStore, session: UUID, **kwargs: int) -> SampleLease:
    leases = [
        lease
        for lease in observer.claim_due_samples(uuid4(), **kwargs)
        if lease.session_id == session
    ]
    assert len(leases) == 1, leases
    return leases[0]


def _sample(
    lease: SampleLease,
    window: tuple[datetime, datetime],
    outcome: str = "healthy",
    *,
    required: bool = True,
    **overrides: object,
) -> HealthSample:
    fields: dict[str, object] = {
        "sample_id": str(uuid4()),
        "session_id": str(lease.session_id),
        "sequence": lease.sequence,
        "window": QueryWindow(start=window[0], end=window[1]),
        "outcome": outcome,
        "subject_control_generation": lease.subject_control_generation,
        "observation_generation": lease.observation_generation,
        "health_profile_revision": lease.health_profile_revision,
        "required_signals_present": required,
    }
    fields.update(overrides)
    return HealthSample(**fields)  # type: ignore[arg-type]


def _readings(window: tuple[datetime, datetime]) -> list[SignalReading]:
    return [
        SignalReading(
            signal_name="error_ratio",
            status="ok",
            value=0.001,
            sample_count=12,
            query="sum(rate(http_errors[1m]))/sum(rate(http_requests[1m]))",
            window_start=window[0],
            window_end=window[1],
            source="prometheus",
            raw_sha256="a" * 64,
        ),
        SignalReading(
            signal_name="ready_pods",
            status="no_data",
            query="kube_deployment_status_replicas_ready",
            window_start=window[0],
            window_end=window[1],
            source="prometheus",
        ),
    ]


def _window(start: datetime, seconds: int) -> tuple[datetime, datetime]:
    return start, start + timedelta(seconds=seconds)


def _lifecycle(owner: DurableStore, incident: UUID) -> str:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return str(row["lifecycle"])


def _release_lease(owner: DurableStore, session: UUID) -> None:
    """Simulate a crashed Observer: its lease ran out."""
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_lease_until=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (session,),
        )


def _assert_replay_consistent(controller: ObservationStore, session: UUID) -> None:
    report = controller.replay_session(session)
    assert report.consistent, [
        (item.sequence, item.stored, item.replayed)
        for item in report.samples
        if not item.matches
    ]


# --- authorization primitive


def test_authorize_moves_incident_to_observing_and_schedules_the_first_job(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, target_id, target = _incident(owner)
    assert _lifecycle(owner, incident) == "open"

    session = _authorize(controller, incident, target)

    row = controller.session(session)
    assert _lifecycle(owner, incident) == "observing_recovery"
    assert row["state"] == "authorized"
    assert row["target_id"] == target_id
    assert row["observation_generation"] == 1
    assert row["active_sample_sequence"] == 1
    assert row["active_sample_job_id"] is not None
    assert row["active_sample_owner"] is None
    assert row["health_profile_revision"] == PROFILE
    with pytest.raises(PersistenceError, match="OBSERVATION_ALREADY_AUTHORIZED"):
        _authorize(controller, incident, target)
    with pytest.raises(PersistenceError, match="TARGET_MISMATCH"):
        controller.authorize_session(
            incident,
            target=_target("deployment/other"),
            actor="tester",
            deadline_at=_now() + timedelta(hours=1),
            max_samples=3,
            sample_interval_seconds=15,
            sustained_window_seconds=60,
        )
    with pytest.raises(PersistenceError, match="UNKNOWN_IDENTITY"):
        _authorize(controller, uuid4(), target)


def test_authorize_refuses_a_resolved_or_closed_incident(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            (incident,),
        )
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        _authorize(controller, incident, target)
    assert _lifecycle(owner, incident) == "closed"


# --- Observer role boundary (C3 section 3, D3)


def test_observer_role_cannot_write_investigation_runs_reports_or_evidence(
    observer_dsn: str, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    denied = [
        "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) VALUES(gen_random_uuid(),%(i)s,'queued',0,1,clock_timestamp(),'{}')",
        "UPDATE opspilot_runs SET state='cancelled' WHERE incident_id=%(i)s",
        "UPDATE opspilot_incidents SET conclusion='{}' WHERE incident_id=%(i)s",
        "UPDATE opspilot_incidents SET control_generation=control_generation+1 WHERE incident_id=%(i)s",
        "UPDATE opspilot_incidents SET state='cancelled' WHERE incident_id=%(i)s",
        "UPDATE opspilot_incidents SET current_run_id=NULL WHERE incident_id=%(i)s",
        "SELECT conclusion FROM opspilot_incidents WHERE incident_id=%(i)s",
        "INSERT INTO opspilot_steps(step_id,run_id,logical_key,status,control_generation) SELECT gen_random_uuid(),run_id,'k','response_committed',0 FROM opspilot_runs WHERE incident_id=%(i)s",
        "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at) VALUES('e','r','s','ok',true,'','h','{}','h','p',clock_timestamp())",
        "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor) VALUES(gen_random_uuid(),%(i)s,'cancel',0,1,'observer')",
        "INSERT INTO opspilot_inputs(input_id,incident_id,sequence,kind,content,control_generation) VALUES(gen_random_uuid(),%(i)s,99,'event','{}',0)",
        "UPDATE opspilot_scope_controls SET global_suspended=true",
        "UPDATE opspilot_target_suspensions SET suspended=true",
        # Session parameters are fixed at authorization.
        "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()+interval '1 year' WHERE session_id=%(s)s",
        "UPDATE opspilot_observation_sessions SET subject_control_generation=99 WHERE session_id=%(s)s",
        "UPDATE opspilot_observation_sessions SET health_profile_revision='x' WHERE session_id=%(s)s",
        "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds) SELECT gen_random_uuid(),incident_id,purpose,target_id,target,0,0,'observer',deadline_at,1,1,1 FROM opspilot_observation_sessions WHERE session_id=%(s)s",
        "DELETE FROM opspilot_observation_samples",
        "UPDATE opspilot_observation_samples SET disposition='adopted'",
        "DELETE FROM opspilot_observation_sessions WHERE session_id=%(s)s",
    ]
    for statement in denied:
        with psycopg.connect(observer_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, {"i": incident, "s": session})
    # Nothing leaked through: the incident and session are untouched.
    assert _lifecycle(owner, incident) == "observing_recovery"
    assert controller.session(session)["state"] == "authorized"
    assert owner.recovery_metadata(incident)["control_generation"] == 0


def test_observer_role_runs_the_whole_sampling_path(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """Claim, submit (adopt + lifecycle + next job), sweep, read back: all
    under the Observer role; every negative statement above stays denied."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=30)
    start = _now()

    lease = _claim(observer, session)
    receipt = observer.submit_sample(
        lease, _sample(lease, _window(start, 60)), _readings(_window(start, 60))
    )

    assert receipt.accepted and receipt.transition == "recovery_confirmed"
    assert receipt.incident_lifecycle == "resolved"
    assert _lifecycle(owner, incident) == "resolved"
    history = observer.session_history(session)
    assert history["session"]["state"] == "completed"
    assert history["session"]["ended_reason"] == "recovery_confirmed"
    assert [row["signal_name"] for row in history["samples"][0]["readings"]] == [
        "error_ratio",
        "ready_pods",
    ]
    assert observer.sweep_expired_sessions() == []
    assert observer.replay_session(session).consistent


# --- adoption, watermark, lifecycle, next job in one transaction


def test_sustained_healthy_window_confirms_recovery(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=60)
    t0 = _now()

    first = _claim(observer, session)
    r1 = observer.submit_sample(first, _sample(first, _window(t0, 30)), [])
    assert r1.accepted and r1.confirms_health and r1.transition is None
    assert r1.next_sample_due_at is not None
    row = controller.session(session)
    assert (row["adopted_sequence"], row["adopted_count"]) == (1, 1)
    assert row["healthy_since"] == t0
    assert (
        row["active_sample_sequence"] == 2
        and row["active_sample_job_id"] != first.job_id
    )
    assert _lifecycle(owner, incident) == "observing_recovery"
    # The next job is due one interval later: not claimable yet.
    assert observer.claim_due_samples(uuid4()) == []
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (session,),
        )

    second = _claim(observer, session)
    assert (second.sequence, second.adopted_window_end) == (
        2,
        t0 + timedelta(seconds=30),
    )
    window = (t0 + timedelta(seconds=30), t0 + timedelta(seconds=70))
    r2 = observer.submit_sample(second, _sample(second, window), _readings(window))

    assert r2.transition == "recovery_confirmed"
    assert r2.session_state == "completed"
    assert _lifecycle(owner, incident) == "resolved"
    row = controller.session(session)
    assert row["active_sample_job_id"] is None
    assert row["adopted_window_end"] == window[1]
    assert observer.claim_due_samples(uuid4()) == []
    samples = controller.session_history(session)["samples"]
    assert [s["transition"] for s in samples] == [None, "recovery_confirmed"]
    assert all(s["subject_lifecycle"] == "observing_recovery" for s in samples)
    _assert_replay_consistent(controller, session)


@pytest.mark.parametrize(
    ("outcome", "required"),
    [("no_data", True), ("stale", True), ("failed", True), ("healthy", False)],
)
def test_unknown_or_incomplete_samples_are_adopted_but_never_confirm(
    observer: ObservationStore,
    owner: DurableStore,
    controller: ObservationStore,
    outcome: str,
    required: bool,
) -> None:
    """A legal no_data/stale/failed sample (or healthy without the required
    signals) is adopted as an unknown observation: it counts, it moves the
    watermark, it resets the healthy streak, it never resolves."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=30)
    t0 = _now()
    lease = _claim(observer, session)
    receipt = observer.submit_sample(lease, _sample(lease, _window(t0, 60)), [])
    assert (
        receipt.accepted
        and receipt.confirms_health
        and receipt.transition == "recovery_confirmed"
    )

    # A fresh session: unknown first, then healthy but not yet sustained.
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=30)
    lease = _claim(observer, session)
    window = _window(t0, 60)
    receipt = observer.submit_sample(
        lease, _sample(lease, window, outcome, required=required), _readings(window)
    )
    assert receipt.accepted and not receipt.confirms_health
    assert receipt.transition is None and receipt.reason == "adopted"
    row = controller.session(session)
    assert row["adopted_count"] == 1 and row["healthy_since"] is None
    assert _lifecycle(owner, incident) == "observing_recovery"
    _assert_replay_consistent(controller, session)


def test_a_non_healthy_sample_resets_the_streak(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=50)
    t0 = _now()
    plan = [
        ("healthy", 0, 30, None, t0),
        ("degraded", 30, 60, None, None),
        ("healthy", 60, 90, None, t0 + timedelta(seconds=60)),
        ("healthy", 90, 120, "recovery_confirmed", t0 + timedelta(seconds=60)),
    ]
    for outcome, a, b, transition, since in plan:
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
                (session,),
            )
        lease = _claim(observer, session)
        window = (t0 + timedelta(seconds=a), t0 + timedelta(seconds=b))
        receipt = observer.submit_sample(lease, _sample(lease, window, outcome), [])
        assert receipt.accepted and receipt.transition == transition, (outcome, a)
        assert controller.session(session)["healthy_since"] == since, (outcome, a)
    assert _lifecycle(owner, incident) == "resolved"
    _assert_replay_consistent(controller, session)


def test_without_a_profile_revision_recovery_is_never_confirmed(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=10, profile=None)
    lease = _claim(observer, session)
    assert lease.health_profile_revision is None
    receipt = observer.submit_sample(lease, _sample(lease, _window(_now(), 60)), [])
    assert receipt.accepted and not receipt.confirms_health
    assert _lifecycle(owner, incident) == "observing_recovery"


# --- rejected samples are history only and change nothing


def test_stale_bindings_are_history_only(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    t0 = _now()
    cases = [
        ({"health_profile_revision": "other@000"}, "health_profile_revision_mismatch"),
        ({"observation_generation": 7}, "observation_generation_stale"),
    ]
    for overrides, reason in cases:
        lease = _claim(observer, session)
        receipt = observer.submit_sample(
            lease, _sample(lease, _window(t0, 30), **overrides), []
        )
        assert (receipt.accepted, receipt.reason) == (False, reason)
        row = controller.session(session)
        # Lease released, same job and sequence stay for the retry.
        assert row["active_sample_job_id"] == lease.job_id
        assert row["active_sample_sequence"] == lease.sequence == 1
        assert row["active_sample_owner"] is None and row["adopted_count"] == 0
    # A human decision moved the control generation on (step 3 revokes in
    # the same transaction; here only the generation moves).
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=control_generation+1 WHERE incident_id=%s",
            (incident,),
        )
    lease = _claim(observer, session)
    receipt = observer.submit_sample(lease, _sample(lease, _window(t0, 30)), [])
    assert receipt.reason == "control_generation_stale"
    assert _lifecycle(owner, incident) == "observing_recovery"
    history = controller.session_history(session)
    assert [s["disposition"] for s in history["samples"]] == ["history_only"] * 3
    _assert_replay_consistent(controller, session)


def test_a_sample_from_a_lost_lease_is_history_only_and_the_retry_keeps_the_sequence(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    t0 = _now()
    first = _claim(observer, session)
    _release_lease(owner, session)

    second = _claim(observer, session)
    assert (second.job_id, second.sequence) == (first.job_id, first.sequence)
    assert second.epoch == first.epoch + 1
    late = observer.submit_sample(first, _sample(first, _window(t0, 30)), [])
    assert (late.accepted, late.reason) == (False, "lease_revoked")
    row = controller.session(session)
    assert row["active_sample_owner"] == second.owner and row["adopted_sequence"] == 0

    good = observer.submit_sample(second, _sample(second, _window(t0, 30)), [])
    assert good.accepted and good.reason == "adopted"
    assert controller.session(session)["adopted_sequence"] == 1
    # The consumed lease cannot be reused for the next job either.
    replayed = observer.submit_sample(
        second, _sample(second, _window(t0 + timedelta(seconds=30), 30)), []
    )
    assert replayed.reason == "lease_revoked"
    _assert_replay_consistent(controller, session)


def test_the_sequence_comes_with_the_lease(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        observer.submit_sample(
            lease, _sample(lease, _window(_now(), 30), sequence=5), []
        )
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        observer.submit_sample(
            lease, _sample(lease, _window(_now(), 30), session_id=str(uuid4())), []
        )
    assert controller.session_history(session)["samples"] == []


def test_a_revoked_session_adopts_nothing(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)

    assert controller.revoke_sessions(incident) == [session]

    assert observer.claim_due_samples(uuid4()) == []
    receipt = observer.submit_sample(lease, _sample(lease, _window(_now(), 30)), [])
    assert (receipt.accepted, receipt.reason) == (False, "lease_revoked")
    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("revoked", "authority_revoked")
    assert _lifecycle(owner, incident) == "observing_recovery"
    # A new session can be authorized on the still-observing incident.
    assert controller.authorize_session(
        incident,
        target=target,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=3,
        sample_interval_seconds=15,
        sustained_window_seconds=60,
    )


def test_target_suspension_blocks_claims_and_adoption(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, target_id, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    generation = owner.set_target_suspension(
        target_id, True, expected_generation=0, actor="tester"
    )
    try:
        receipt = observer.submit_sample(lease, _sample(lease, _window(_now(), 30)), [])
        assert (receipt.accepted, receipt.reason) == (False, "suspended")
        assert observer.claim_due_samples(uuid4()) == []
        row = controller.session(session)
        assert row["active_sample_sequence"] == 1 and row["adopted_count"] == 0
    finally:
        owner.set_target_suspension(
            target_id, False, expected_generation=generation, actor="tester"
        )
    retry = _claim(observer, session)
    assert retry.sequence == 1 and retry.epoch == 2
    assert observer.submit_sample(
        retry, _sample(retry, _window(_now(), 30)), []
    ).accepted
    _assert_replay_consistent(controller, session)


def test_global_suspension_blocks_claims(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    before = owner.control_state(incident)["global_generation"]
    generation = owner.set_global_suspension(
        True, expected_generation=before, actor="tester"
    )
    try:
        assert observer.claim_due_samples(uuid4()) == []
    finally:
        owner.set_global_suspension(
            False, expected_generation=generation, actor="tester"
        )
    assert _claim(observer, session).sequence == 1


# --- deadline and budget exhaustion hand back to open


def test_deadline_reached_at_submission_ends_the_session_and_reopens(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (session,),
        )

    receipt = observer.submit_sample(lease, _sample(lease, _window(_now(), 30)), [])

    assert (receipt.accepted, receipt.reason) == (False, "deadline_expired")
    assert receipt.transition == "observation_ended_unconfirmed"
    assert receipt.session_state == "expired"
    assert _lifecycle(owner, incident) == "open"
    row = controller.session(session)
    assert (
        row["ended_reason"] == "deadline_expired"
        and row["active_sample_job_id"] is None
    )
    assert observer.claim_due_samples(uuid4()) == []
    _assert_replay_consistent(controller, session)


def test_sweep_expires_a_silent_session_and_reopens(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    assert observer.sweep_expired_sessions() == []
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (session,),
        )
    assert observer.claim_due_samples(uuid4()) == []

    assert observer.sweep_expired_sessions() == [session]

    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("expired", "deadline_expired")
    assert _lifecycle(owner, incident) == "open"
    assert observer.sweep_expired_sessions() == []


def test_sample_budget_exhausted_without_confirmation_reopens(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, max_samples=2, sustained=600)
    t0 = _now()
    lease = _claim(observer, session)
    assert (
        observer.submit_sample(
            lease, _sample(lease, _window(t0, 30), "degraded"), []
        ).transition
        is None
    )
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (session,),
        )
    lease = _claim(observer, session)
    window = (t0 + timedelta(seconds=30), t0 + timedelta(seconds=60))

    receipt = observer.submit_sample(lease, _sample(lease, window), [])

    assert receipt.accepted and receipt.confirms_health
    assert receipt.transition == "observation_ended_unconfirmed"
    assert _lifecycle(owner, incident) == "open"
    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("expired", "max_samples_exhausted")
    assert row["adopted_count"] == 2 and row["active_sample_job_id"] is None
    _assert_replay_consistent(controller, session)


# --- concurrency: two submissions, one watermark advance


def test_concurrent_submissions_advance_the_watermark_once(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=600)
    lease = _claim(observer, session)
    t0 = _now()
    barrier = threading.Barrier(2)
    receipts: list[SampleReceipt] = []
    errors: list[BaseException] = []

    def submit() -> None:
        sample = _sample(lease, _window(t0, 30))
        try:
            barrier.wait(timeout=10)
            receipts.append(
                observer.submit_sample(sample=sample, lease=lease, readings=[])
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(r.accepted for r in receipts) == [False, True]
    assert {r.reason for r in receipts if not r.accepted} == {"lease_revoked"}
    row = controller.session(session)
    assert (row["adopted_sequence"], row["adopted_count"]) == (1, 1)
    assert row["active_sample_sequence"] == 2
    samples = controller.session_history(session)["samples"]
    assert sorted(s["disposition"] for s in samples) == ["adopted", "history_only"]
    _assert_replay_consistent(controller, session)


# --- replay reads storage only


def test_replay_detects_a_tampered_decision(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=600)
    lease = _claim(observer, session)
    receipt = observer.submit_sample(
        lease, _sample(lease, _window(_now(), 30), "degraded"), []
    )
    assert receipt.accepted and not receipt.confirms_health
    assert controller.replay_session(session).consistent
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_samples SET confirms_health=true WHERE sample_id=%s",
            (receipt.sample_id,),
        )
    report = controller.replay_session(session)
    assert not report.consistent
    assert (
        report.samples[0].stored[2] is True and report.samples[0].replayed[2] is False
    )
