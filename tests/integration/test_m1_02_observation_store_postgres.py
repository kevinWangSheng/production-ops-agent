"""Observation sessions and samples on a real PostgreSQL (M1-02 step 2, #84).

Covers the atomic adoption transaction (C3 section 10 "采样与提交"), the
Observer role boundary (section 3, decision D3), lease retries keeping the
logical sequence, deadline/budget exhaustion handing back to ``open``, and
replay from stored rows alone (F6 step 5).

One scratch database per module (migrated to head); every test works on its
own incident and target, except the global-suspension test which restores
the gate before returning.
"""

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import ValidationError

from opspilot import schema
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.intake import Target
from opspilot.domain.observation import HealthSample
from opspilot.observation import (
    ObservationStore,
    SampleLease,
    SampleReceipt,
    SignalReading,
    profile_revision,
    required_signals,
)
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.persistence.base import _StoreBase
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=3, timeout=5.0)
PROFILE_CONTENT = json.dumps(
    {
        "profile_id": "checkout",
        "signals": [
            {
                "name": "error_ratio",
                "query": "sum(rate(http_errors[1m]))/sum(rate(http_requests[1m]))",
                "coverage_query": "count_over_time(http_requests[5m])",
                "healthy": {"max": 0.01},
            },
            {
                "name": "debug_only",
                "required": False,
                "query": "up",
                "coverage_query": "count_over_time(up[5m])",
                "healthy": {"min": 0},
            },
        ],
    },
    sort_keys=True,
    separators=(",", ":"),
)
PROFILE = profile_revision("checkout", PROFILE_CONTENT)


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


# Sample windows must end by the database clock (+30 s skew), so the tests
# build them in the recent past and backdate the authorization below them.
BACKDATE = timedelta(hours=1)


def _t0() -> datetime:
    return _now() - timedelta(minutes=50)


def _backdate_authorization(owner: _StoreBase, session: UUID) -> datetime:
    with owner.transaction() as conn:
        row = conn.execute(
            "UPDATE opspilot_observation_sessions SET authorized_at=authorized_at-%s WHERE session_id=%s RETURNING authorized_at",
            (BACKDATE, session),
        ).fetchone()
    assert row is not None
    return row["authorized_at"]


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
    session = controller.authorize_session(
        incident,
        target=target,
        actor="tester",
        deadline_at=_now() + deadline,
        max_samples=max_samples,
        sample_interval_seconds=15,
        sustained_window_seconds=sustained,
        health_profile_revision=profile,
        health_profile=PROFILE_CONTENT if profile == PROFILE else None,
        first_sample_due_at=_now(),
    )
    _backdate_authorization(controller, session)
    return session


def _claimable(observer: ObservationStore, session: UUID) -> list[SampleLease]:
    """Leases for this session only: the module shares one database, other
    tests' sessions may be due as well."""
    return [
        lease
        for lease in observer.claim_due_samples(uuid4())
        if lease.session_id == session
    ]


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


def _readings_for(sample: HealthSample) -> list[SignalReading]:
    """Reading rows that say what the sample says: the profile's required
    signal is ok when ``required_signals_present``, missing otherwise."""
    return [
        SignalReading(
            signal_name="error_ratio",
            status="ok" if sample.required_signals_present else "no_data",
            value=0.0 if sample.required_signals_present else None,
            query="q",
            window_start=sample.window.start,
            window_end=sample.window.end,
            source="prometheus",
        )
    ]


def _submit(
    observer: ObservationStore, lease: SampleLease, sample: HealthSample
) -> SampleReceipt:
    return observer.submit_sample(lease, sample, _readings_for(sample))


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


def _due_now(owner: DurableStore, session: UUID) -> None:
    """Skip the sampling interval: make the active job due immediately."""
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
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
        "UPDATE opspilot_observation_endings SET transition=NULL",
        # timestamps come from the database clock only
        "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,recorded_at) VALUES(gen_random_uuid(),%(s)s,%(i)s,'authority_revoked',clock_timestamp()-interval '1 day')",
        "INSERT INTO opspilot_observation_samples(sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,required_signals_present,subject_control_generation,observation_generation,disposition,reason,confirms_health,health_basis,subject_lifecycle,incident_control_generation,incident_observation_generation,scope_suspended,global_generation,target_generation,within_deadline,lease_valid,lease_stamps_match,readings_consistent,submitted_at) VALUES(gen_random_uuid(),%(s)s,gen_random_uuid(),1,1,clock_timestamp()-interval '1 minute',clock_timestamp(),'healthy',true,0,1,'adopted','adopted',false,'not_adopted','observing_recovery',0,1,false,0,0,true,true,true,true,clock_timestamp())",
        "DELETE FROM opspilot_observation_endings",
        "INSERT INTO opspilot_health_profiles(health_profile_revision,profile_id,content_sha256,content) VALUES('x@000000000000','x',repeat('0',64),'{}')",
        "UPDATE opspilot_health_profiles SET content='{}'",
        "DELETE FROM opspilot_health_profiles",
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
    start = _t0()

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
    t0 = _t0()

    first = _claim(observer, session)
    r1 = _submit(observer, first, _sample(first, _window(t0, 30)))
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
    assert _claimable(observer, session) == []
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
    assert _claimable(observer, session) == []
    samples = controller.session_history(session)["samples"]
    assert [s["transition"] for s in samples] == [None, "recovery_confirmed"]
    assert all(s["subject_lifecycle"] == "observing_recovery" for s in samples)
    _assert_replay_consistent(controller, session)


@pytest.mark.parametrize(
    ("outcome", "required"),
    [("no_data", True), ("stale", True), ("failed", True)],
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
    t0 = _t0()
    lease = _claim(observer, session)
    receipt = _submit(observer, lease, _sample(lease, _window(t0, 60)))
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
    # Readings say what the sample says (a healthy sample whose required
    # signals are missing has no ok reading for them).
    receipt = _submit(
        observer, lease, _sample(lease, window, outcome, required=required)
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
    t0 = _t0()
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
        receipt = _submit(observer, lease, _sample(lease, window, outcome))
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
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 60)))
    assert receipt.accepted and not receipt.confirms_health
    assert _lifecycle(owner, incident) == "observing_recovery"


# --- rejected samples are history only and change nothing


def test_a_recoverable_rejection_postpones_the_same_job(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """A window that regresses behind the adopted watermark is the Observer's
    windowing, not a stale session: the job is kept (same sequence) and
    retried one interval later, not immediately and not forever."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=600)
    t0 = _t0()
    lease = _claim(observer, session)
    assert _submit(observer, lease, _sample(lease, _window(t0, 60))).accepted
    _due_now(owner, session)
    lease = _claim(observer, session)
    receipt = _submit(observer, lease, _sample(lease, (t0 - timedelta(seconds=60), t0)))
    assert (receipt.accepted, receipt.reason) == (False, "window_regressed")
    assert receipt.next_sample_due_at is not None
    row = controller.session(session)
    assert row["active_sample_job_id"] == lease.job_id
    assert row["active_sample_sequence"] == 2 and row["active_sample_owner"] is None
    assert row["active_sample_due_at"] == receipt.next_sample_due_at
    assert row["active_sample_due_at"] > _now() + timedelta(seconds=10)
    assert _claimable(observer, session) == []
    _due_now(owner, session)
    retry = _claim(observer, session)
    assert (retry.sequence, retry.epoch) == (2, 2)
    assert controller.session(session)["adopted_count"] == 1
    _assert_replay_consistent(controller, session)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            "UPDATE opspilot_incidents SET observation_generation=observation_generation+1 WHERE incident_id=%s",
            "observation_generation_stale",
        ),
        (
            "UPDATE opspilot_incidents SET control_generation=control_generation+1 WHERE incident_id=%s",
            "control_generation_stale",
        ),
    ],
)
def test_a_stale_binding_is_history_only_and_ends_the_session(
    observer: ObservationStore,
    owner: DurableStore,
    controller: ObservationStore,
    mutate: str,
    reason: str,
) -> None:
    """A moved control/observation generation cannot be retried into
    validity: the sample is history, the session ends as ``binding_stale``
    (lifecycle untouched, step 3 decides it) and no job is claimable again."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    with owner.transaction() as conn:
        conn.execute(mutate, (incident,))

    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30)))

    assert (receipt.accepted, receipt.reason) == (False, reason)
    assert receipt.session_state == "revoked" and receipt.transition is None
    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("revoked", "binding_stale")
    assert row["active_sample_job_id"] is None and row["adopted_count"] == 0
    assert _lifecycle(owner, incident) == "observing_recovery"
    assert _claimable(observer, session) == []
    assert observer.sweep_expired_sessions() == []
    assert [
        s["disposition"] for s in controller.session_history(session)["samples"]
    ] == ["history_only"]
    _assert_replay_consistent(controller, session)


def test_a_sample_from_a_lost_lease_is_history_only_and_the_retry_keeps_the_sequence(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    t0 = _t0()
    first = _claim(observer, session)
    _release_lease(owner, session)

    second = _claim(observer, session)
    assert (second.job_id, second.sequence) == (first.job_id, first.sequence)
    assert second.epoch == first.epoch + 1
    late = _submit(observer, first, _sample(first, _window(t0, 30)))
    assert (late.accepted, late.reason) == (False, "lease_revoked")
    row = controller.session(session)
    assert row["active_sample_owner"] == second.owner and row["adopted_sequence"] == 0

    good = _submit(observer, second, _sample(second, _window(t0, 30)))
    assert good.accepted and good.reason == "adopted"
    assert controller.session(session)["adopted_sequence"] == 1
    # The consumed lease cannot be reused for the next job either.
    replayed = _submit(
        observer, second, _sample(second, _window(t0 + timedelta(seconds=30), 30))
    )
    assert replayed.reason == "lease_revoked"
    _assert_replay_consistent(controller, session)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 5},
        {"subject_control_generation": 9},
        {"observation_generation": 9},
        {"health_profile_revision": "other@000000000000"},
        {"health_profile_revision": None},
    ],
)
def test_a_sample_with_other_stamps_than_its_lease_is_history_only(
    observer: ObservationStore,
    owner: DurableStore,
    controller: ObservationStore,
    overrides: dict[str, object],
) -> None:
    """Sequence, generations and profile revision are the lease's; a sample
    stamped otherwise is an invalid identity: kept as history with its
    readings (C3 section 10), never judged against the session (so the
    session is not ended as stale), the lease released and the same job
    retried one interval later."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)

    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30), **overrides))

    assert (receipt.accepted, receipt.reason) == (False, "lease_stamp_mismatch")
    assert receipt.session_state == "authorized" and receipt.transition is None
    assert receipt.next_sample_due_at is not None
    history = controller.session_history(session)
    assert [
        (s["disposition"], s["reason"], s["lease_valid"], s["lease_stamps_match"])
        for s in history["samples"]
    ] == [("history_only", "lease_stamp_mismatch", True, False)]
    assert [r["signal_name"] for r in history["samples"][0]["readings"]] == [
        "error_ratio"
    ]
    row = controller.session(session)
    assert row["state"] == "authorized" and row["adopted_count"] == 0
    assert row["active_sample_job_id"] == lease.job_id
    assert row["active_sample_owner"] is None
    assert row["active_sample_due_at"] == receipt.next_sample_due_at
    assert _claimable(observer, session) == []
    _due_now(owner, session)
    retry = _claim(observer, session)
    assert (retry.job_id, retry.sequence, retry.epoch) == (lease.job_id, 1, 2)
    assert _submit(observer, retry, _sample(retry, _window(_t0(), 30))).accepted
    _assert_replay_consistent(controller, session)


def test_a_sample_for_another_session_is_refused_outright(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """The session is the lease's; a different one is not a sample of this
    session at all (nothing to file it under), so it is refused as input."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        _submit(
            observer,
            lease,
            _sample(lease, _window(_t0(), 30), session_id=str(uuid4())),
        )
    assert controller.session_history(session)["samples"] == []
    row = controller.session(session)
    assert row["state"] == "authorized" and row["active_sample_owner"] == lease.owner


def test_a_window_reaching_into_the_future_is_refused(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """``[now, now+900]`` with sustained 600 would otherwise resolve on one
    sample; a window may end at most 30 s after the database clock."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=600)
    lease = _claim(observer, session)
    now = _now()
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        _submit(observer, lease, _sample(lease, (now, now + timedelta(seconds=900))))
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        observer.submit_sample(
            lease,
            _sample(lease, (now - timedelta(seconds=60), now + timedelta(seconds=60))),
            [],
        )
    assert controller.session_history(session)["samples"] == []
    assert _lifecycle(owner, incident) == "observing_recovery"
    receipt = _submit(
        observer, lease, _sample(lease, (now - timedelta(seconds=60), now))
    )
    assert receipt.accepted and receipt.transition is None


def test_a_revoked_session_adopts_nothing(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)

    assert controller.revoke_sessions(incident) == [session]

    assert _claimable(observer, session) == []
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30)))
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


def test_target_suspension_ends_the_session_rather_than_pausing_it(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """While suspended nothing is leased; a lease taken before the suspension
    submits as ``suspended`` history and the session ends (scope_suspended).
    Lifting the suspension does not resume it: step 3 authorizes anew."""
    incident, target_id, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    generation = owner.set_target_suspension(
        target_id, True, expected_generation=0, actor="tester"
    )
    try:
        assert _claimable(observer, session) == []
        receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30)))
        assert (receipt.accepted, receipt.reason) == (False, "suspended")
        assert receipt.session_state == "revoked" and receipt.transition is None
        row = controller.session(session)
        assert (row["state"], row["ended_reason"]) == ("revoked", "scope_suspended")
        assert row["active_sample_job_id"] is None and row["adopted_count"] == 0
    finally:
        owner.set_target_suspension(
            target_id, False, expected_generation=generation, actor="tester"
        )
    assert _claimable(observer, session) == []
    assert _lifecycle(owner, incident) == "observing_recovery"
    endings = controller.session_history(session)["endings"]
    assert [(e["ended_reason"], e["transition"]) for e in endings] == [
        ("scope_suspended", None)
    ]
    _assert_replay_consistent(controller, session)
    # The incident is still observing: a new session can be authorized.
    assert _authorize(controller, incident, target)


def test_a_lifted_suspension_never_resumes_an_old_authorization(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """Suspend and release with no submission in between: the next claim
    finds the scope generation moved and ends the session instead of
    leasing it (C3 section 4)."""
    incident, target_id, target = _incident(owner)
    session = _authorize(controller, incident, target)
    generation = owner.set_target_suspension(
        target_id, True, expected_generation=0, actor="tester"
    )
    owner.set_target_suspension(
        target_id, False, expected_generation=generation, actor="tester"
    )

    assert _claimable(observer, session) == []

    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("revoked", "scope_suspended")
    assert _claimable(observer, session) == []
    assert _lifecycle(owner, incident) == "observing_recovery"


def test_global_suspension_blocks_claims_and_ends_sessions_on_release(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    before = owner.control_state(incident)["global_generation"]
    generation = owner.set_global_suspension(
        True, expected_generation=before, actor="tester"
    )
    try:
        assert _claimable(observer, session) == []
        assert controller.session(session)["state"] == "authorized"
    finally:
        owner.set_global_suspension(
            False, expected_generation=generation, actor="tester"
        )
    assert _claimable(observer, session) == []
    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("revoked", "scope_suspended")


def test_claim_limit_is_not_used_up_by_suspended_sessions(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """``limit`` suspended-target sessions are due first; the unsuspended
    one behind them is still leased (the filter is in the query)."""
    suspended = []
    for _ in range(2):
        incident, target_id, target = _incident(owner)
        session = _authorize(controller, incident, target)
        owner.set_target_suspension(target_id, True, expected_generation=0, actor="t")
        suspended.append(session)
    incident, _, target = _incident(owner)
    wanted = _authorize(controller, incident, target)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp()-interval '1 hour' WHERE session_id = ANY(%s)",
            (suspended,),
        )

    leases = observer.claim_due_samples(uuid4(), limit=2)

    assert [lease.session_id for lease in leases if lease.session_id == wanted] == [
        wanted
    ]
    assert {lease.session_id for lease in leases} & set(suspended) == set()
    for session in suspended:
        assert controller.session(session)["state"] == "authorized"


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

    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30)))

    assert (receipt.accepted, receipt.reason) == (False, "deadline_expired")
    assert receipt.transition == "observation_ended_unconfirmed"
    assert receipt.session_state == "expired"
    assert _lifecycle(owner, incident) == "open"
    row = controller.session(session)
    assert (
        row["ended_reason"] == "deadline_expired"
        and row["active_sample_job_id"] is None
    )
    assert _claimable(observer, session) == []
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
    assert _claimable(observer, session) == []

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
    t0 = _t0()
    lease = _claim(observer, session)
    assert (
        _submit(observer, lease, _sample(lease, _window(t0, 30), "degraded")).transition
        is None
    )
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s",
            (session,),
        )
    lease = _claim(observer, session)
    window = (t0 + timedelta(seconds=30), t0 + timedelta(seconds=60))

    receipt = _submit(observer, lease, _sample(lease, window))

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
    t0 = _t0()
    barrier = threading.Barrier(2)
    receipts: list[SampleReceipt] = []
    errors: list[BaseException] = []

    def submit() -> None:
        sample = _sample(lease, _window(t0, 30))
        try:
            barrier.wait(timeout=10)
            receipts.append(_submit(observer, lease, sample))
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
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30), "degraded"))
    assert receipt.accepted and not receipt.confirms_health
    assert controller.replay_session(session).consistent
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_samples SET confirms_health=true,health_basis='confirmed' WHERE sample_id=%s",
            (receipt.sample_id,),
        )
    report = controller.replay_session(session)
    assert not report.consistent
    assert report.samples[0].replayed[3] == "outcome_not_healthy"
    assert (
        report.samples[0].stored[2] is True and report.samples[0].replayed[2] is False
    )


# --- independent review of PR #114: P1-1 / P1-2 / P2-1 / P2-2, raw payloads


def test_a_lease_claimed_before_a_pause_is_history_after_the_resume(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """Pause then resume between claim and submit: the in-flight result is
    history (C3 section 4) even though nothing is suspended at submit time,
    nothing of it enters the healthy window, and the session is over."""
    incident, target_id, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=60)
    t0 = _t0()
    first = _claim(observer, session)
    assert _submit(observer, first, _sample(first, _window(t0, 30))).confirms_health
    _due_now(owner, session)
    second = _claim(observer, session)
    generation = owner.set_target_suspension(
        target_id, True, expected_generation=0, actor="tester"
    )
    owner.set_target_suspension(
        target_id, False, expected_generation=generation, actor="tester"
    )

    late = _window(t0 + timedelta(minutes=10), 60)
    receipt = _submit(observer, second, _sample(second, late))

    assert (receipt.accepted, receipt.reason) == (False, "suspended")
    assert (
        receipt.transition is None
        and _lifecycle(owner, incident) == "observing_recovery"
    )
    row = controller.session(session)
    assert row["adopted_sequence"] == 1 and row["healthy_since"] == t0
    assert (row["state"], row["ended_reason"]) == ("revoked", "scope_suspended")
    assert _claimable(observer, session) == []
    _assert_replay_consistent(controller, session)


def test_a_gap_between_adopted_windows_restarts_the_healthy_streak(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=60)
    t0 = _t0()
    lease = _claim(observer, session)
    assert _submit(observer, lease, _sample(lease, _window(t0, 30))).confirms_health
    _due_now(owner, session)
    lease = _claim(observer, session)
    # 30 s healthy, 40 minutes unobserved, 30 s healthy: 60 s of healthy
    # data do not make a 60 s sustained window.
    gap = _window(t0 + timedelta(minutes=40), 30)

    receipt = _submit(observer, lease, _sample(lease, gap))

    assert receipt.accepted and receipt.confirms_health and receipt.transition is None
    assert controller.session(session)["healthy_since"] == gap[0]
    assert _lifecycle(owner, incident) == "observing_recovery"
    _due_now(owner, session)
    lease = _claim(observer, session)
    more = (gap[1], gap[1] + timedelta(seconds=30))
    receipt = _submit(observer, lease, _sample(lease, more))
    assert receipt.transition == "recovery_confirmed"
    _assert_replay_consistent(controller, session)


def test_data_from_before_the_authorization_never_confirms_health(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """Profile-shaped parameters (window 300 s, sustained 600 s, interval
    60 s): the Observer samples every 60 s with a 300 s look-back. Windows
    that start before the authorization are adopted but never healthy; the
    streak starts with the first window inside the authorization and the
    confirmation comes when 600 s after it are covered (the sample taken
    600 s after authorization), never earlier."""
    incident, _, target = _incident(owner)
    session = controller.authorize_session(
        incident,
        target=target,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=20,
        sample_interval_seconds=60,
        sustained_window_seconds=600,
        health_profile_revision=PROFILE,
        health_profile=PROFILE_CONTENT,
        first_sample_due_at=_now(),
    )
    authorized_at = _backdate_authorization(owner, session)
    outcomes = []
    for k in range(1, 11):
        _due_now(owner, session)
        lease = _claim(observer, session)
        taken = authorized_at + timedelta(seconds=60 * k)
        window = (taken - timedelta(seconds=300), taken)
        receipt = observer.submit_sample(
            lease, _sample(lease, window), _readings(window)
        )
        outcomes.append(
            (receipt.confirms_health, receipt.health_basis, receipt.transition)
        )
        assert receipt.accepted, k
        if k < 10:
            assert receipt.transition is None, k
            assert _lifecycle(owner, incident) == "observing_recovery", k
    assert outcomes[:4] == [(False, "window_before_authorization", None)] * 4
    assert outcomes[4:9] == [(True, "confirmed", None)] * 5
    assert outcomes[9] == (True, "confirmed", "recovery_confirmed")
    row = controller.session(session)
    assert row["healthy_since"] == authorized_at and row["adopted_count"] == 10
    assert _lifecycle(owner, incident) == "resolved"
    _assert_replay_consistent(controller, session)


def test_a_single_pre_authorization_window_cannot_resolve(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """The review's reproduction: sustained 60 s, first window [now-300, now]."""
    incident, _, target = _incident(owner)
    session = controller.authorize_session(
        incident,
        target=target,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=10,
        sample_interval_seconds=15,
        sustained_window_seconds=60,
        health_profile_revision=PROFILE,
        health_profile=PROFILE_CONTENT,
        first_sample_due_at=_now(),
    )
    lease = _claim(observer, session)
    now = _now()
    receipt = _submit(
        observer, lease, _sample(lease, (now - timedelta(seconds=300), now))
    )
    assert receipt.accepted and not receipt.confirms_health
    assert receipt.health_basis == "window_before_authorization"
    assert (
        receipt.transition is None
        and _lifecycle(owner, incident) == "observing_recovery"
    )


def test_observer_role_may_only_move_lifecycle_along_its_two_edges(
    observer_dsn: str, owner: DurableStore, controller: ObservationStore
) -> None:
    """The column grant allows any value; the trigger allows only
    observing_recovery -> resolved | open for members of the Observer role."""
    observing, _, target = _incident(owner)
    _authorize(controller, observing, target)
    open_incident, _, _ = _incident(owner)
    closed, _, _ = _incident(owner)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            (closed,),
        )
    denied = [
        ("closed", observing),
        ("open", open_incident),  # no-op values are fine; a change is not
        ("resolved", open_incident),
        ("open", closed),
        ("observing_recovery", open_incident),
    ]
    for value, incident in denied:
        if _lifecycle(owner, incident) == value:
            continue
        with psycopg.connect(observer_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
                conn.execute(
                    "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                    (value, incident),
                )
            assert "opspilot_observer may not move lifecycle" in str(refused.value)
    assert _lifecycle(owner, observing) == "observing_recovery"
    assert _lifecycle(owner, open_incident) == "open"
    assert _lifecycle(owner, closed) == "closed"
    # A legal edge passes the edge guard but, as raw SQL with no recorded
    # observation ending, is refused at COMMIT by the evidence trigger; the
    # owner is not restricted.
    with psycopg.connect(observer_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='open' WHERE incident_id=%s",
            (observing,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.commit()
    assert _lifecycle(owner, observing) == "observing_recovery"
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
            (open_incident,),
        )
    assert _lifecycle(owner, open_incident) == "resolved"


def test_raw_payloads_are_stored_with_their_hash_and_verified_on_replay(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=600)
    lease = _claim(observer, session)
    window = _window(_t0(), 30)
    raw = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
    reading = SignalReading(
        signal_name="error_ratio",
        status="ok",
        value=0.0,
        query="q",
        window_start=window[0],
        window_end=window[1],
        source="prometheus",
        raw=raw,
    )
    assert reading.raw_sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValidationError):
        SignalReading(**{**reading.model_dump(), "raw_sha256": "0" * 64})
    with pytest.raises(ValidationError):
        SignalReading(**{**reading.model_dump(), "raw": b"x" * (131072 + 1)})

    receipt = observer.submit_sample(lease, _sample(lease, window), [reading])

    stored = controller.session_history(session)["samples"][0]["readings"][0]
    assert bytes(stored["raw"]) == raw and stored["raw_sha256"] == reading.raw_sha256
    assert controller.replay_session(session).consistent
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=%s WHERE sample_id=%s",
            (b"{}", receipt.sample_id),
        )
    report = controller.replay_session(session)
    assert not report.consistent
    assert report.samples[0].raw_mismatches == ("error_ratio",)


def test_replay_compares_the_final_session_state_and_the_lifecycle(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    # Ended by the sweep (no sample carries the ending): still consistent.
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    _submit(observer, lease, _sample(lease, _window(_t0(), 30), "degraded"))
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (session,),
        )
    assert observer.sweep_expired_sessions() == [session]
    report = controller.replay_session(session)
    assert report.consistent
    assert (report.replayed_session_state, report.stored_session_state) == (
        "authorized",
        "expired",
    )
    assert (report.expected_lifecycle, report.recorded_lifecycle) == (
        None,
        "observing_recovery",
    )

    # A resolved incident later closed by a human: the old session's replay
    # is about what its samples recorded, not about the incident today.
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=30)
    lease = _claim(observer, session)
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 60)))
    assert receipt.transition == "recovery_confirmed"
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            (incident,),
        )
    report = controller.replay_session(session)
    assert report.consistent
    assert (report.expected_lifecycle, report.recorded_lifecycle) == (
        "resolved",
        "resolved",
    )
    # A tampered record: the sample says the incident was open when it
    # confirmed recovery.
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_samples SET subject_lifecycle='open' WHERE sample_id=%s",
            (receipt.sample_id,),
        )
    report = controller.replay_session(session)
    assert not report.lifecycle_consistent and not report.consistent
    assert report.recorded_lifecycle == "open"
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET state='authorized',ended_reason=NULL WHERE session_id=%s",
            (session,),
        )
    assert not controller.replay_session(session).session_consistent


def test_the_profile_content_is_stored_once_per_revision_and_checked(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """A revision is only a hash; the content it names is kept so a replay
    can read the coverage queries and thresholds (F6 step 5)."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    stored = observer.health_profile(PROFILE)
    assert stored["content"] == PROFILE_CONTENT
    assert stored["profile_id"] == "checkout"
    assert (
        stored["content_sha256"] == hashlib.sha256(PROFILE_CONTENT.encode()).hexdigest()
    )
    assert PROFILE == f"checkout@{stored['content_sha256'][:12]}"
    history = observer.session_history(session)
    assert history["health_profile"]["content"] == PROFILE_CONTENT
    assert json.loads(history["health_profile"]["content"])["signals"][0][
        "coverage_query"
    ].startswith("count_over_time")
    # Same revision again: deduplicated, not duplicated.
    other, _, other_target = _incident(owner)
    _authorize(controller, other, other_target)
    with owner.transaction(snapshot=True) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (PROFILE,),
        ).fetchone() == {"n": 1}
    # Content that does not hash to the revision, a revision without
    # content, or content without a revision: refused before any write.
    third, _, third_target = _incident(owner)
    for kwargs in (
        {"health_profile_revision": PROFILE, "health_profile": PROFILE_CONTENT + " "},
        {
            "health_profile_revision": "checkout@000000000000",
            "health_profile": PROFILE_CONTENT,
        },
        {"health_profile_revision": "no-separator", "health_profile": PROFILE_CONTENT},
        {"health_profile_revision": PROFILE, "health_profile": None},
        {"health_profile_revision": None, "health_profile": PROFILE_CONTENT},
    ):
        with pytest.raises(
            PersistenceError, match="HEALTH_PROFILE_REVISION_MISMATCH|INVALID_INPUT"
        ):
            controller.authorize_session(
                third,
                target=third_target,
                actor="tester",
                deadline_at=_now() + timedelta(hours=1),
                max_samples=3,
                sample_interval_seconds=15,
                sustained_window_seconds=60,
                **kwargs,  # type: ignore[arg-type]
            )
    assert _lifecycle(owner, third) == "open"
    with pytest.raises(PersistenceError, match="UNKNOWN_IDENTITY"):
        observer.health_profile("checkout@000000000000")


def test_an_old_session_replays_consistently_after_a_new_authorization(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    old = _authorize(controller, incident, target)
    lease = _claim(observer, old)
    _submit(observer, lease, _sample(lease, _window(_t0(), 30), "degraded"))
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (old,),
        )
    assert observer.sweep_expired_sessions() == [old]
    assert _lifecycle(owner, incident) == "open"

    new = _authorize(controller, incident, target, sustained=30)
    lease = _claim(observer, new)
    assert _submit(observer, lease, _sample(lease, _window(_t0(), 60))).transition
    assert _lifecycle(owner, incident) == "resolved"

    assert controller.replay_session(old).consistent
    assert controller.replay_session(new).consistent


def test_an_indirect_member_of_the_observer_role_is_guarded_too(
    scratch_dsn: str, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    _authorize(controller, incident, target)
    team = f"opspilot_observer_team_{uuid4().hex[:8]}"
    login = f"opspilot_observer_nested_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(team)
            )
        )
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(
                sql.Identifier(login), sql.Identifier(team)
            )
        )
    try:
        with psycopg.connect(make_conninfo(scratch_dsn, user=login)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
                conn.execute(
                    "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
                    (incident,),
                )
            assert "may not move lifecycle" in str(refused.value)
        with psycopg.connect(make_conninfo(scratch_dsn, user=login)) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='open' WHERE incident_id=%s",
                (incident,),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as at_commit:
                conn.commit()
            assert "without a recorded observation ending" in str(at_commit.value)
        assert _lifecycle(owner, incident) == "observing_recovery"
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(team)))


# --- @codex review triage: lifecycle evidence, replay basis, structure


def test_observer_lifecycle_change_without_a_recorded_ending_fails_at_commit(
    observer_dsn: str, owner: DurableStore, controller: ObservationStore
) -> None:
    """The column grant and the edge guard allow ``observing_recovery ->
    resolved``; the deferred evidence trigger refuses the COMMIT unless this
    transaction recorded the session ending (and, for a confirmation, the
    adopted sample that confirmed it)."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    for value in ("resolved", "open"):
        with psycopg.connect(observer_dsn) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                (value, incident),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
                conn.commit()
            assert "without a recorded observation ending" in str(refused.value)
        assert _lifecycle(owner, incident) == "observing_recovery"
    # An ending whose transition does not match the edge is not evidence.
    with psycopg.connect(observer_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,transition) VALUES(gen_random_uuid(),%s,%s,'deadline_expired','observation_ended_unconfirmed')",
            (session, incident),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
            (incident,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.commit()
    assert _lifecycle(owner, incident) == "observing_recovery"
    assert controller.session_history(session)["endings"] == []
    # An ending row that names this incident but a session of another
    # incident is not evidence either.
    other, _, other_target = _incident(owner)
    other_session = _authorize(controller, other, other_target)
    with psycopg.connect(observer_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,transition) VALUES(gen_random_uuid(),%s,%s,'deadline_expired','observation_ended_unconfirmed')",
            (other_session, incident),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='open' WHERE incident_id=%s",
            (incident,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.commit()
    assert _lifecycle(owner, incident) == "observing_recovery"
    # The real paths still commit: a sweep ending reopens, a confirmed
    # sample resolves.
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (session,),
        )
    observer_store = ObservationStore(observer_dsn, pool=POOL)
    try:
        assert observer_store.sweep_expired_sessions() == [session]
        assert _lifecycle(owner, incident) == "open"
        endings = controller.session_history(session)["endings"]
        assert [
            (e["ended_reason"], e["transition"], e["sample_id"]) for e in endings
        ] == [("deadline_expired", "observation_ended_unconfirmed", None)]
        session = _authorize(controller, incident, target, sustained=30)
        lease = _claim(observer_store, session)
        receipt = _submit(observer_store, lease, _sample(lease, _window(_t0(), 60)))
        assert receipt.transition == "recovery_confirmed"
        assert _lifecycle(owner, incident) == "resolved"
        endings = controller.session_history(session)["endings"]
        assert [
            (e["ended_reason"], e["transition"], e["sample_id"]) for e in endings
        ] == [("recovery_confirmed", "recovery_confirmed", receipt.sample_id)]
    finally:
        observer_store.close()


def test_replay_uses_the_ending_sample_not_a_late_duplicate(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=30)
    lease = _claim(observer, session)
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 60)))
    assert receipt.transition == "recovery_confirmed"
    # The same lease delivered again after the session completed: filed as
    # history, it is the last sample row but not the one that ended things.
    late = _submit(observer, lease, _sample(lease, _window(_t0(), 60)))
    assert (late.accepted, late.reason) == (False, "lease_revoked")
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            (incident,),
        )
    report = controller.replay_session(session)
    assert report.consistent
    assert (report.expected_lifecycle, report.recorded_lifecycle) == (
        "resolved",
        "resolved",
    )


def test_replay_checks_the_required_signal_readings_of_a_healthy_sample(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """Structural check from the stored profile (the threshold replay is
    #87): a healthy sample needs an ok reading row for every required
    signal, and ``required_signals_present`` must agree with the rows."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=600)
    lease = _claim(observer, session)
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30)))
    assert receipt.confirms_health
    assert controller.replay_session(session).consistent
    with owner.transaction() as conn:
        conn.execute(
            "DELETE FROM opspilot_observation_signal_readings WHERE sample_id=%s AND signal_name='error_ratio'",
            (receipt.sample_id,),
        )
    report = controller.replay_session(session)
    assert not report.consistent
    assert report.samples[0].signal_mismatches == ("error_ratio",)
    # A sample that says the required signals were missing while the rows
    # say they were fine is inconsistent the other way round.
    _due_now(owner, session)
    lease = _claim(observer, session)
    receipt = observer.submit_sample(
        lease,
        _sample(
            lease, _window(_t0() + timedelta(seconds=30), 30), "no_data", required=False
        ),
        _readings_for(_sample(lease, _window(_t0() + timedelta(seconds=30), 30))),
    )
    assert (receipt.accepted, receipt.reason) == (False, "readings_inconsistent")
    report = controller.replay_session(session)
    assert report.samples[1].signal_mismatches == ("required_signals_present",)
    assert report.samples[1].matches


def test_claim_never_waits_for_a_locked_incident_when_ending_a_session(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """Every ending path locks the incident row before the session row; the
    claim already holds the session row when it finds the scope moved, so it
    takes the incident only if free (no wait, no deadlock) and otherwise
    leaves the session for the next claim or sweep."""
    incident, target_id, target = _incident(owner)
    session = _authorize(controller, incident, target)
    generation = owner.set_target_suspension(
        target_id, True, expected_generation=0, actor="tester"
    )
    owner.set_target_suspension(
        target_id, False, expected_generation=generation, actor="tester"
    )
    with psycopg.connect(controller.dsn) as holder:
        # Another writer holds the incident row (as submit/sweep/revoke do).
        holder.execute(
            "SELECT incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (incident,),
        )
        started = _now()
        assert _claimable(observer, session) == []
        assert (_now() - started) < timedelta(seconds=3), "claim must not wait"
        assert controller.session(session)["state"] == "authorized"
    assert _claimable(observer, session) == []
    row = controller.session(session)
    assert (row["state"], row["ended_reason"]) == ("revoked", "scope_suspended")


STEP1_SHAPED_PROFILE = json.dumps(
    {
        "format_version": 1,
        "profile_id": "otel-demo-checkout",
        "signals": [
            {"name": "deployment_available_replicas", "required": True, "query": "q1"},
            {"name": "request_rate_per_second", "query": "q2"},
            {"name": "latency_p95_milliseconds", "required": False, "query": "q3"},
        ],
    },
    sort_keys=True,
    separators=(",", ":"),
)


def test_required_signals_follow_the_health_profile_shape() -> None:
    assert required_signals(STEP1_SHAPED_PROFILE) == (
        "deployment_available_replicas",
        "request_rate_per_second",
    )
    for broken in (
        "not json",
        "[]",
        json.dumps({"profile_id": "x"}),
        json.dumps({"signals": "none"}),
        json.dumps({"signals": []}),
        json.dumps({"signals": [{"name": "a", "required": False}]}),
        json.dumps({"signals": [{"query": "q"}]}),
        json.dumps({"signals": [{"name": "a", "required": "yes"}]}),
    ):
        with pytest.raises(PersistenceError, match="HEALTH_PROFILE_UNREADABLE"):
            required_signals(broken)


def test_replay_fails_closed_when_the_profile_yields_no_required_signals(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    content = json.dumps({"profile_id": "bare"}, separators=(",", ":"))
    revision = profile_revision("bare", content)
    incident, _, target = _incident(owner)
    session = controller.authorize_session(
        incident,
        target=target,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=5,
        sample_interval_seconds=15,
        sustained_window_seconds=600,
        health_profile_revision=revision,
        health_profile=content,
        first_sample_due_at=_now(),
    )
    _backdate_authorization(owner, session)
    lease = _claim(observer, session)
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 30), "degraded"))
    # Fail closed at submit too: nothing is adopted under such a profile.
    assert (receipt.accepted, receipt.reason) == (False, "readings_inconsistent")
    report = controller.replay_session(session)
    assert report.consistent
    assert report.samples[0].signal_mismatches == ("health_profile_unreadable",)
    assert report.samples[0].stored == report.samples[0].replayed


def test_a_healthy_claim_without_the_required_readings_is_history_only(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    """An Observer that submits ``healthy`` with ``required_signals_present``
    and no (or non-ok) reading rows for the profile's required signals is
    filed as history at submit time, never adopted, never confirming: the
    readings are the record the verdict must be reconstructed from."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=30)
    lease = _claim(observer, session)
    cases: list[tuple[list[SignalReading], HealthSample]] = [
        ([], _sample(lease, _window(_t0(), 60))),
        (
            _readings_for(_sample(lease, _window(_t0(), 60), required=False)),
            _sample(lease, _window(_t0(), 60)),
        ),
        ([], _sample(lease, _window(_t0(), 60), required=False)),
        ([], _sample(lease, _window(_t0(), 60), "degraded")),
    ]
    for readings, sample in cases:
        receipt = observer.submit_sample(lease, sample, readings)
        assert (receipt.accepted, receipt.reason) == (False, "readings_inconsistent")
        assert receipt.transition is None and not receipt.confirms_health
        assert receipt.session_state == "authorized"
        assert _lifecycle(owner, incident) == "observing_recovery"
        _due_now(owner, session)
        lease = _claim(observer, session)
        assert lease.sequence == 1
    history = controller.session_history(session)
    assert [s["readings_consistent"] for s in history["samples"]] == [False] * 4
    assert controller.session(session)["adopted_count"] == 0
    # Consistent readings: adopted and, with a 60 s window, confirmed.
    receipt = _submit(observer, lease, _sample(lease, _window(_t0(), 60)))
    assert receipt.transition == "recovery_confirmed"
    _assert_replay_consistent(controller, session)


def test_replay_checks_the_ending_record_of_a_session_ended_without_a_sample(
    observer: ObservationStore, owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    lease = _claim(observer, session)
    _submit(observer, lease, _sample(lease, _window(_t0(), 30), "degraded"))
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
            (session,),
        )
    assert observer.sweep_expired_sessions() == [session]
    report = controller.replay_session(session)
    assert report.consistent
    assert report.recorded_ending == (
        "deadline_expired",
        "observation_ended_unconfirmed",
        None,
    )
    # The record says something else than the session.
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_endings SET ended_reason='authority_revoked' WHERE session_id=%s",
            (session,),
        )
    report = controller.replay_session(session)
    assert not report.ending_consistent and not report.consistent
    # No record at all.
    with owner.transaction() as conn:
        conn.execute(
            "DELETE FROM opspilot_observation_endings WHERE session_id=%s", (session,)
        )
    report = controller.replay_session(session)
    assert report.recorded_ending is None
    assert not report.session_consistent and not report.consistent
    # A revoked session with its record is fine; the state alone is not.
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target)
    assert controller.revoke_sessions(incident) == [session]
    assert controller.replay_session(session).consistent
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET state='expired',ended_reason='deadline_expired' WHERE session_id=%s",
            (session,),
        )
    assert not controller.replay_session(session).consistent


def test_a_temporary_pg_roles_cannot_impersonate_a_superuser(
    observer_dsn: str, owner: DurableStore, controller: ObservationStore
) -> None:
    """Both triggers pin ``search_path`` to ``pg_catalog`` and qualify every
    catalog name, so a temp table named ``pg_roles`` that calls the Observer
    a superuser changes nothing."""
    incident, _, target = _incident(owner)
    _authorize(controller, incident, target)
    with psycopg.connect(observer_dsn) as conn:
        conn.execute(
            "CREATE TEMP TABLE pg_roles AS SELECT rolname, true AS rolsuper FROM pg_catalog.pg_roles"
        )
        conn.execute(
            "CREATE TEMP TABLE pg_class AS SELECT oid, 0::oid AS relowner FROM pg_catalog.pg_class"
        )
        assert conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (True,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
                (incident,),
            )
        assert "may not move lifecycle" in str(refused.value)
    with psycopg.connect(observer_dsn) as conn:
        conn.execute(
            "CREATE TEMP TABLE pg_roles AS SELECT rolname, true AS rolsuper FROM pg_catalog.pg_roles"
        )
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='open' WHERE incident_id=%s",
            (incident,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as at_commit:
            conn.commit()
        assert "without a recorded observation ending" in str(at_commit.value)
    assert _lifecycle(owner, incident) == "observing_recovery"
