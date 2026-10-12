"""Independent PostgreSQL contract tests for #184 step 1.

These tests deliberately use the public Workbench/IncidentStore seams.  SQL is
used only to inspect the durable contract and to construct explicit race and
terminal-state fixtures; it is never used to implement self-healing behavior.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from opspilot.intake import IntakeEnvelope, IntakeRequest, Principal
from opspilot.observation import ObservationStore
from opspilot.observer import PROFILE_DIRECTORY, canonical_content, load_health_profile
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web import (
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
)
from opspilot.web import __main__ as web_main
from opspilot.web.service import SelfHealOutcome
from opspilot.web.store import TargetIdentity, TargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import login
from tests.target_support import IDENTITY, WORKLOAD

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
VERSIONS = fixture_versions()
SCAN_LIMIT = 10_000


class StaticRegistry(TargetRegistry):
    def __init__(self, *, profile_id: str | None = PROFILE.profile_id) -> None:
        self.profile_id = profile_id
        self.missing = False

    def resolve(self, target_id: str) -> TargetIdentity | None:
        if self.missing:
            return None
        return TargetIdentity(
            resource_uid=target_id,
            health_profile_id=self.profile_id,
            workload=WORKLOAD,
            **IDENTITY,
        )


class FailingIncidentStore(DurableIncidentStore):
    """Public IncidentStore seam for C9's bounded operational-failure test."""

    def authorize_automatic_observation(self, *args: Any, **kwargs: Any) -> Any:
        raise PersistenceError("STORAGE_UNAVAILABLE")


class StalePublishedIncidentStore(DurableIncidentStore):
    """Simulate a publish-generation change between C1 scan and C8 lock."""

    def __init__(self, owner: DurableStore, incident_id: UUID) -> None:
        super().__init__(owner)
        self._owner = owner
        self._stale_incident_id = incident_id

    def authorize_automatic_observation(self, *args: Any, **kwargs: Any) -> Any:
        with self._owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET conclusion=NULL WHERE incident_id=%s",
                (self._stale_incident_id,),
            )
        return super().authorize_automatic_observation(*args, **kwargs)


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


def _workbench(
    owner: DurableStore,
    *,
    profile=PROFILE,
    registry: TargetRegistry | None = None,
) -> Workbench:
    events = DurableEventLog(owner)
    events.install()
    evidence = DurableEvidenceStore(owner)
    evidence.install()
    ledger = DurableWebLedger(owner)
    ledger.install()
    return Workbench(
        incidents=DurableIncidentStore(owner),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=VERSIONS,
        run_seconds=600,
        tool_face=fixture_face(),
        health_profile=profile,
        targets=registry if registry is not None else StaticRegistry(),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _incident(owner: DurableStore) -> tuple[UUID, UUID, UUID, str]:
    incident, run = uuid4(), uuid4()
    resource_uid = f"deployment/checkout-self-heal-{uuid4().hex[:8]}"
    target_id = owner.register_target(resource_uid)
    # Intake initially knows only the immutable resource uid.  Complete the
    # target identity through the same durable target record the workbench
    # later resolves from its registry.
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload=%s WHERE target_id=%s",
            (
                IDENTITY["integration_id"],
                IDENTITY["cluster_uid"],
                IDENTITY["namespace"],
                WORKLOAD,
                target_id,
            ),
        )
    owner.accept(
        incident,
        run,
        f"self-heal-{incident}",
        deadline=_now() + timedelta(hours=1),
        budget_limit=10,
        versions=VERSIONS,
        target_id=target_id,
    )
    return incident, run, target_id, resource_uid


def _publish(owner: DurableStore, incident: UUID, run: UUID) -> dict[str, Any]:
    conclusion: dict[str, Any] = {
        "kind": "conclusion",
        "conclusion": "supported",
        "summary": "the published conclusion is durable",
    }
    lease = owner.claim(incident, run, uuid4(), VERSIONS)
    step_id = owner.commit_step(lease, "conclusion:0", conclusion)
    assert owner.publish(lease, conclusion, step_id=step_id)
    return conclusion


def _incident_row(owner: DurableStore, incident: UUID) -> dict[str, Any]:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT lifecycle,state,mode,control_generation,observation_generation,target_id,current_run_id,conclusion FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _sessions(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT session_id,state,ended_reason,authorized_by,authorization_source,target,health_profile_revision,authorized_at,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds,subject_control_generation,observation_generation FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY created_at,session_id",
                (incident,),
            ).fetchall()
        )


def _auth_rows(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT incident_id,control_generation,state,reason,attempts,next_attempt_at,session_id FROM opspilot_self_heal_authorizations WHERE incident_id=%s ORDER BY control_generation",
                (incident,),
            ).fetchall()
        )


def _controls(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT action,expected_generation,resulting_generation,actor,payload FROM opspilot_controls WHERE incident_id=%s ORDER BY audit_id",
                (incident,),
            ).fetchall()
        )


def _published_candidate(
    owner: DurableStore, *, profile=PROFILE, registry: TargetRegistry | None = None
) -> tuple[Workbench, UUID, UUID, UUID, str]:
    resource_uid = f"deployment/checkout-self-heal-{uuid4().hex[:8]}"
    workbench = _workbench(owner, profile=profile, registry=registry)
    submitted = workbench.submit(
        IntakeEnvelope(
            request_id=f"request-{uuid4()}",
            principal=Principal(
                actor_id="test-operator", channel="ui_basic", auth_revision="test-auth"
            ),
            request=IntakeRequest(
                target_id=resource_uid,
                question="Why did checkout fail?",
                idempotency_key=f"self-heal-{uuid4()}",
            ),
            received_at=_now(),
        )
    )
    incident, run = submitted.incident_id, submitted.run_id
    with owner.transaction() as conn:
        target_id = conn.execute(
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()["target_id"]
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload=%s WHERE target_id=%s",
            (
                IDENTITY["integration_id"],
                IDENTITY["cluster_uid"],
                IDENTITY["namespace"],
                WORKLOAD,
                target_id,
            ),
        )
    _publish(owner, incident, run)
    return workbench, incident, run, target_id, resource_uid


def _for_incident(
    outcomes: list[SelfHealOutcome], incident: UUID
) -> list[SelfHealOutcome]:
    return [outcome for outcome in outcomes if outcome.incident_id == incident]


def _pass_for(workbench: Workbench, incident: UUID) -> list[SelfHealOutcome]:
    """Scan past unrelated shared-lab candidates, then isolate this incident."""
    return _for_incident(workbench.self_heal_pass(limit=SCAN_LIMIT), incident)


def _end_unconfirmed_session(
    owner: DurableStore, *, incident: UUID | None = None, session_id: UUID | None = None
) -> None:
    """End a fixture session while satisfying the 0003 job consistency CHECK."""
    assert (incident is None) != (session_id is None)
    predicate = "incident_id=%s" if incident is not None else "session_id=%s"
    value = incident if incident is not None else session_id
    with owner.transaction() as conn:
        conn.execute(
            f"UPDATE opspilot_observation_sessions SET state='expired',ended_reason='max_samples_exhausted',active_sample_job_id=NULL,active_sample_sequence=NULL,active_sample_due_at=NULL,active_sample_owner=NULL,active_sample_lease_until=NULL WHERE {predicate}",
            (value,),
        )


def test_self_heal_loop_default_interval_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", raising=False)
    assert web_main._self_heal_interval() == 0.0


def test_migration_exposes_source_checks_authorization_table_and_legacy_backfill(
    owner: DurableStore,
) -> None:
    with owner.transaction(snapshot=True) as conn:
        column = conn.execute(
            "SELECT is_nullable,data_type,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='opspilot_observation_sessions' AND column_name='authorization_source'"
        ).fetchone()
        assert column is not None
        assert (
            column["is_nullable"] == "YES"
        )  # purpose-specific CHECK supplies nullability
        assert column["data_type"] == "text"
        assert "post_remediation" in (column["column_default"] or "")
        assert conn.execute(
            "SELECT 1 FROM pg_class WHERE relname='opspilot_self_heal_authorizations'"
        ).fetchone()
        primary = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='opspilot_self_heal_authorizations'::regclass AND contype='p'"
        ).fetchone()
        assert (
            primary is not None
            and "incident_id" in primary["pg_get_constraintdef"]
            and "control_generation" in primary["pg_get_constraintdef"]
        )
        states = {
            row["state"]
            for row in conn.execute(
                "SELECT state FROM opspilot_self_heal_authorizations"
            )
        }
        assert states <= {"authorized", "skipped", "retrying", "handed_off"}
        assert (
            conn.execute(
                "SELECT count(*) FROM opspilot_observation_sessions WHERE purpose='incident_recovery' AND authorization_source='post_remediation'"
            ).fetchone()["count"]
            >= 0
        )

    incident, _, target_id, _ = _incident(owner)
    values = {
        "incident_id": incident,
        "target_id": target_id,
        "target": Jsonb({"resource_uid": str(target_id), "revision": "test"}),
        "subject_control_generation": 0,
        "observation_generation": 0,
        "authorized_by": "tester",
        "authorized_global_generation": 0,
        "authorized_target_generation": 0,
        "deadline_at": _now() + timedelta(hours=1),
        "max_samples": 1,
        "sample_interval_seconds": 1,
        "sustained_window_seconds": 1,
    }
    for purpose, source in (
        ("incident_recovery", None),
        ("incident_recovery", "bogus"),
        ("release_observation", "automatic"),
    ):
        with pytest.raises(errors.CheckViolation):
            with psycopg.connect(DSN) as conn:
                conn.execute(
                    "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,authorization_source,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds) VALUES(%(session_id)s,%(incident_id)s,%(purpose)s,%(target_id)s,%(target)s,%(subject_control_generation)s,%(observation_generation)s,%(authorized_by)s,%(source)s,%(authorized_global_generation)s,%(authorized_target_generation)s,%(deadline_at)s,%(max_samples)s,%(sample_interval_seconds)s,%(sustained_window_seconds)s)",
                    {
                        **values,
                        "session_id": uuid4(),
                        "purpose": purpose,
                        "source": source,
                    },
                )
    with pytest.raises(errors.CheckViolation):
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "INSERT INTO opspilot_self_heal_authorizations(incident_id,control_generation,state) VALUES(%s,%s,%s)",
                (incident, 0, "invalid"),
            )
    with psycopg.connect(DSN) as conn:
        legacy = conn.execute(
            "SELECT count(*) FROM opspilot_observation_sessions WHERE purpose='incident_recovery' AND (authorization_source IS NULL OR authorization_source NOT IN ('post_remediation','automatic'))"
        ).fetchone()[0]
        release_non_null = conn.execute(
            "SELECT count(*) FROM opspilot_observation_sessions WHERE purpose='release_observation' AND authorization_source IS NOT NULL"
        ).fetchone()[0]
    assert legacy == 0
    assert release_non_null == 0


def test_human_register_remediation_persists_post_remediation_source(
    owner: DurableStore,
) -> None:
    incident, _, target_id, resource_uid = _incident(owner)
    controller = ObservationStore(DSN, pool=POOL)
    try:
        generation = controller.register_remediation(
            incident,
            expected_generation=0,
            actor="operator",
            revision="checkout:v-human",
            deadline_at=_now() + timedelta(hours=1),
            max_samples=PROFILE.session.max_samples,
            sample_interval_seconds=PROFILE.session.sample_interval_seconds,
            sustained_window_seconds=PROFILE.session.sustained_window_seconds,
            health_profile_revision=PROFILE.revision,
            health_profile=canonical_content(PROFILE),
            session_id=uuid4(),
            identity={
                **IDENTITY,
                "resource_uid": resource_uid,
                "workload": WORKLOAD,
            },
        )
        assert generation == 1
        (session,) = _sessions(owner, incident)
        assert session["authorization_source"] == "post_remediation"
        assert session["authorized_by"] == "operator"
        assert _incident_row(owner, incident)["target_id"] == target_id
    finally:
        controller.close()


def test_published_automatic_matching_profile_is_authorized_once(
    owner: DurableStore,
) -> None:
    workbench, incident, _, _, _ = _published_candidate(owner)
    with owner.transaction(snapshot=True) as conn:
        before = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    outcomes = workbench.self_heal_pass(limit=SCAN_LIMIT)
    with owner.transaction(snapshot=True) as conn:
        after = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]

    matching = [item for item in outcomes if item.incident_id == incident]
    assert len(matching) == 1
    outcome = matching[0]
    assert (outcome.incident_id, outcome.control_generation, outcome.state) == (
        incident,
        0,
        "authorized",
    )
    assert outcome.reason is None
    assert outcome.session_id is not None
    row = _incident_row(owner, incident)
    assert (
        row["lifecycle"],
        row["control_generation"],
        row["observation_generation"],
    ) == (
        "observing_recovery",
        0,
        1,
    )
    (session,) = _sessions(owner, incident)
    assert session["authorization_source"] == "automatic"
    assert session["authorized_by"] == "system"
    assert session["target"]["revision"] is None
    assert session["health_profile_revision"] == PROFILE.revision
    assert session["max_samples"] == PROFILE.session.max_samples
    assert session["sample_interval_seconds"] == PROFILE.session.sample_interval_seconds
    assert (
        session["sustained_window_seconds"] == PROFILE.session.sustained_window_seconds
    )
    assert (
        abs(
            (session["deadline_at"] - session["authorized_at"]).total_seconds()
            - PROFILE.session.deadline_seconds
        )
        < 1.0
    )
    assert before <= session["authorized_at"] <= after
    (audit,) = [
        item for item in _controls(owner, incident) if item["actor"] == "system"
    ]
    assert audit["expected_generation"] == audit["resulting_generation"] == 0
    assert audit["action"] == "auto_authorize_observation"
    assert audit["payload"]["authorization_source"] == "automatic"
    (auth,) = _auth_rows(owner, incident)
    assert auth["state"] == "authorized"
    assert auth["reason"] is None
    assert auth["attempts"] == 0
    assert auth["session_id"] == session["session_id"]


def test_no_published_conclusion_creates_no_authorization_row(
    owner: DurableStore,
) -> None:
    incident, _, _, _ = _incident(owner)
    workbench = _workbench(owner)
    assert _pass_for(workbench, incident) == []
    assert _auth_rows(owner, incident) == []


def test_handoff_without_published_conclusion_creates_no_authorization_row(
    owner: DurableStore,
) -> None:
    incident, run, _, _ = _incident(owner)
    lease = owner.claim(incident, run, uuid4(), VERSIONS)
    owner.hand_off(lease)
    workbench = _workbench(owner)
    outcomes = _pass_for(workbench, incident)
    assert outcomes == []
    assert _auth_rows(owner, incident) == []


@pytest.mark.parametrize(
    "condition",
    [
        "no_conclusion",
        "run_not_completed",
        "run_generation_mismatch",
        "lifecycle_not_candidate",
        "handoff_no_run",
    ],
)
def test_c1_non_candidates_create_no_record_and_no_outcome(
    owner: DurableStore, condition: str
) -> None:
    """C1 is a scan gate: failure here must not become a skipped decision."""
    if condition == "no_conclusion":
        incident, _, _, _ = _incident(owner)
        workbench = _workbench(owner)
    else:
        workbench, incident, run, _, _ = _published_candidate(owner)
        if condition == "run_not_completed":
            with owner.transaction() as conn:
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued' WHERE run_id=%s", (run,)
                )
        elif condition == "run_generation_mismatch":
            with owner.transaction() as conn:
                conn.execute(
                    "UPDATE opspilot_runs SET control_generation=control_generation+1 WHERE run_id=%s",
                    (run,),
                )
        elif condition == "lifecycle_not_candidate":
            with owner.transaction() as conn:
                conn.execute(
                    "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
                    (incident,),
                )
        elif condition == "handoff_no_run":
            with owner.transaction() as conn:
                conn.execute(
                    "UPDATE opspilot_incidents SET current_run_id=NULL WHERE incident_id=%s",
                    (incident,),
                )
    assert _pass_for(workbench, incident) == []
    assert _auth_rows(owner, incident) == []


def test_self_heal_pass_is_idempotent_and_new_run_opens_new_generation(
    owner: DurableStore,
) -> None:
    workbench, incident, run, _, _ = _published_candidate(owner)
    first = _pass_for(workbench, incident)
    second = _pass_for(workbench, incident)
    assert first[0].state == "authorized"
    assert second == []
    assert len(_auth_rows(owner, incident)) == 1
    assert len(_sessions(owner, incident)) == 1

    # End the first observation and reopen the incident to model the Observer
    # finishing before a subsequent Run is published.
    _end_unconfirmed_session(owner, incident=incident)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='open',state='cancelled' WHERE incident_id=%s",
            (incident,),
        )
    generation = owner.new_run(
        incident,
        uuid4(),
        expected_generation=0,
        deadline=_now() + timedelta(hours=1),
        budget_limit=10,
        versions=VERSIONS,
        actor="operator",
    )
    assert generation == 1
    current = _incident_row(owner, incident)
    assert current["conclusion"] is None
    new_run = current["current_run_id"]
    _publish(owner, incident, new_run)
    third = _pass_for(workbench, incident)
    assert len(third) == 1 and third[0].state == "authorized"
    rows = _auth_rows(owner, incident)
    assert [(row["control_generation"], row["state"]) for row in rows] == [
        (0, "authorized"),
        (1, "authorized"),
    ]
    assert run != new_run


@pytest.mark.parametrize(
    "condition",
    [
        "human_owned",
        "paused",
        "cancelled",
        "active_session",
        "lifecycle_observing",
        "global_suspension",
        "target_suspension",
        "no_identity",
        "profile_mismatch",
        "no_profile",
    ],
)
def test_c8_locked_refusals_are_skipped_but_generation_advancing_controls_are_not_candidates(
    owner: DurableStore, condition: str
) -> None:
    """C8 runs after C1. Takeover and human registration advance generation,
    so they are tested as C1 no-record cases; the other fixtures preserve the
    published generation and must leave one terminal skipped decision.
    """
    registry: TargetRegistry = StaticRegistry()
    workbench, incident, _, target_id, _ = _published_candidate(
        owner, registry=registry
    )
    expect_skipped = True
    if condition == "human_owned":
        # C10: takeover is a public control and advances control_generation.
        assert owner.control(incident, 0, "takeover", "operator") == 1
        expect_skipped = False
    elif condition == "paused":
        # C10 makes pause unavailable after publish; this direct state fixture
        # exercises C8's locked paused branch without inventing a public action.
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET state='paused' WHERE incident_id=%s",
                (incident,),
            )
    elif condition == "cancelled":
        # C10 likewise leaves cancellation unavailable after publication.
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET state='cancelled' WHERE incident_id=%s",
                (incident,),
            )
    elif condition == "active_session":
        controller = ObservationStore(DSN, pool=POOL)
        try:
            controller.authorize_session(
                incident,
                revision="manual-v1",
                actor="operator",
                deadline_at=_now() + timedelta(hours=1),
                max_samples=PROFILE.session.max_samples,
                sample_interval_seconds=PROFILE.session.sample_interval_seconds,
                sustained_window_seconds=PROFILE.session.sustained_window_seconds,
                health_profile_revision=PROFILE.revision,
                health_profile=canonical_content(PROFILE),
                session_id=uuid4(),
            )
        finally:
            controller.close()
    elif condition == "lifecycle_observing":
        # C1 admits observing_recovery; C8 then records lifecycle-not-open.
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='observing_recovery' WHERE incident_id=%s",
                (incident,),
            )
    elif condition == "global_suspension":
        with owner.transaction(snapshot=True) as conn:
            scope_generation = conn.execute(
                "SELECT global_generation FROM opspilot_scope_controls WHERE scope_id=1"
            ).fetchone()["global_generation"]
        owner.set_global_suspension(
            True, expected_generation=scope_generation, actor="operator"
        )
    elif condition == "target_suspension":
        with owner.transaction(snapshot=True) as conn:
            target_generation = conn.execute(
                "SELECT generation FROM opspilot_target_suspensions WHERE target_id=%s",
                (target_id,),
            ).fetchone()["generation"]
        owner.set_target_suspension(
            target_id, True, expected_generation=target_generation, actor="operator"
        )
    elif condition == "no_identity":
        workbench.targets = StaticRegistry()
        workbench.targets.missing = True
    elif condition == "profile_mismatch":
        workbench.targets = StaticRegistry(profile_id="unrelated-profile")
    elif condition == "no_profile":
        workbench.health_profile = None
    before = _incident_row(owner, incident)

    try:
        outcomes = _pass_for(workbench, incident)
        if not expect_skipped:
            assert outcomes == []
            assert _auth_rows(owner, incident) == []
            return
        assert len(outcomes) == 1
        assert outcomes[0].state == "skipped"
        assert outcomes[0].reason
        after = _incident_row(owner, incident)
        assert after["lifecycle"] == before["lifecycle"]
        assert after["control_generation"] == before["control_generation"]
        assert [
            item for item in _controls(owner, incident) if item["actor"] == "system"
        ] == []
        assert _auth_rows(owner, incident)[0]["state"] == "skipped"
        if condition == "global_suspension":
            with owner.transaction(snapshot=True) as conn:
                released_generation = conn.execute(
                    "SELECT global_generation FROM opspilot_scope_controls WHERE scope_id=1"
                ).fetchone()["global_generation"]
            owner.set_global_suspension(
                False, expected_generation=released_generation, actor="operator"
            )
            assert _pass_for(workbench, incident) == []
        elif condition == "target_suspension":
            with owner.transaction(snapshot=True) as conn:
                released_generation = conn.execute(
                    "SELECT generation FROM opspilot_target_suspensions WHERE target_id=%s",
                    (target_id,),
                ).fetchone()["generation"]
            owner.set_target_suspension(
                target_id,
                False,
                expected_generation=released_generation,
                actor="operator",
            )
            assert _pass_for(workbench, incident) == []
    finally:
        if condition == "global_suspension":
            with owner.transaction(snapshot=True) as conn:
                scope_generation = conn.execute(
                    "SELECT global_generation FROM opspilot_scope_controls WHERE scope_id=1"
                ).fetchone()["global_generation"]
            owner.set_global_suspension(
                False, expected_generation=scope_generation, actor="operator"
            )
        elif condition == "target_suspension":
            with owner.transaction(snapshot=True) as conn:
                target_generation = conn.execute(
                    "SELECT generation FROM opspilot_target_suspensions WHERE target_id=%s",
                    (target_id,),
                ).fetchone()["generation"]
            owner.set_target_suspension(
                target_id,
                False,
                expected_generation=target_generation,
                actor="operator",
            )


def test_human_registration_after_auto_authorization_revokes_auto_session(
    owner: DurableStore,
) -> None:
    workbench, incident, _, _, resource_uid = _published_candidate(owner)
    matching = _pass_for(workbench, incident)
    assert matching[0].state == "authorized"
    controller = ObservationStore(DSN, pool=POOL)
    try:
        with owner.transaction(snapshot=True) as conn:
            generation = conn.execute(
                "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()["control_generation"]
        assert (
            controller.register_remediation(
                incident,
                expected_generation=generation,
                actor="operator",
                revision="checkout:v-human-after",
                deadline_at=_now() + timedelta(hours=1),
                max_samples=PROFILE.session.max_samples,
                sample_interval_seconds=PROFILE.session.sample_interval_seconds,
                sustained_window_seconds=PROFILE.session.sustained_window_seconds,
                health_profile_revision=PROFILE.revision,
                health_profile=canonical_content(PROFILE),
                session_id=uuid4(),
                identity={
                    **IDENTITY,
                    "resource_uid": resource_uid,
                    "workload": WORKLOAD,
                },
            )
            == generation + 1
        )
    finally:
        controller.close()
    sessions = _sessions(owner, incident)
    assert sessions[0]["authorization_source"] == "automatic"
    assert sessions[0]["state"] == "revoked"
    assert sessions[1]["authorization_source"] == "post_remediation"
    # C10: after the human G+1 observation ends without a recovery verdict,
    # C1 still cannot select it because the Run remains at the older generation.
    _end_unconfirmed_session(owner, session_id=sessions[1]["session_id"])
    assert _pass_for(workbench, incident) == []
    assert [
        (row["control_generation"], row["state"]) for row in _auth_rows(owner, incident)
    ] == [(generation, "authorized")]


def test_c8_rechecks_published_generation_under_lock(owner: DurableStore) -> None:
    workbench, incident, _, _, _ = _published_candidate(owner)
    workbench.incidents = StalePublishedIncidentStore(owner, incident)
    outcomes = _pass_for(workbench, incident)
    assert len(outcomes) == 1
    assert outcomes[0].state == "skipped"
    assert outcomes[0].reason == "NOT_PUBLISHED_AT_GENERATION"
    assert _auth_rows(owner, incident)[0]["state"] == "skipped"
    assert _controls(owner, incident) == []


def test_human_registration_first_advances_generation_and_is_not_a_candidate(
    owner: DurableStore,
) -> None:
    workbench, incident, _, _, resource_uid = _published_candidate(owner)
    controller = ObservationStore(DSN, pool=POOL)
    try:
        assert (
            controller.register_remediation(
                incident,
                expected_generation=0,
                actor="operator",
                revision="checkout:v-human-first",
                deadline_at=_now() + timedelta(hours=1),
                max_samples=PROFILE.session.max_samples,
                sample_interval_seconds=PROFILE.session.sample_interval_seconds,
                sustained_window_seconds=PROFILE.session.sustained_window_seconds,
                health_profile_revision=PROFILE.revision,
                health_profile=canonical_content(PROFILE),
                session_id=uuid4(),
                identity={
                    **IDENTITY,
                    "resource_uid": resource_uid,
                    "workload": WORKLOAD,
                },
            )
            == 1
        )
    finally:
        controller.close()
    # C10: register_remediation advances control_generation, so C1 excludes
    # this incident instead of creating a skipped automatic decision.
    matching = _pass_for(workbench, incident)
    assert matching == []
    assert _auth_rows(owner, incident) == []


def test_runtime_failure_retries_five_times_then_hands_off_with_event(
    owner: DurableStore,
) -> None:
    workbench, incident, _, _, _ = _published_candidate(owner)
    workbench.incidents = FailingIncidentStore(owner)
    seen: list[SelfHealOutcome] = []
    retry_rows: list[dict[str, Any]] = []
    for attempt in range(1, 6):
        seen.extend(_pass_for(workbench, incident))
        if attempt < 5:
            row = _auth_rows(owner, incident)[0]
            retry_rows.append(row)
            assert row["state"] == "retrying"
            assert row["attempts"] == attempt
            assert row["next_attempt_at"] is not None
            with owner.transaction() as conn:
                conn.execute(
                    "UPDATE opspilot_self_heal_authorizations SET next_attempt_at=clock_timestamp() WHERE incident_id=%s AND control_generation=0",
                    (incident,),
                )
    assert [item.state for item in seen] == ["retrying"] * 4 + ["handed_off"]
    row = _auth_rows(owner, incident)[0]
    assert row["attempts"] == 5
    assert row["state"] == "handed_off"
    assert row["next_attempt_at"] is None
    assert _incident_row(owner, incident)["lifecycle"] == "open"
    events = workbench.events.read_after(incident, 0, limit=100)
    assert any(event.kind == "auto_observation_handed_off" for event in events)
    assert [item["attempts"] for item in retry_rows] == [1, 2, 3, 4]
    assert [
        item for item in _controls(owner, incident) if item["actor"] == "system"
    ] == []


def test_runtime_roles_cannot_write_self_heal_or_session_source(
    owner: DurableStore,
) -> None:
    with (
        login(DSN, "opspilot_worker") as worker_dsn,
        login(DSN, "opspilot_web") as web_dsn,
    ):
        with psycopg.connect(DSN) as conn:
            existing = conn.execute(
                "SELECT session_id FROM opspilot_observation_sessions LIMIT 1"
            ).fetchone()
        with psycopg.connect(worker_dsn) as conn:
            assert not conn.execute(
                "SELECT has_column_privilege(current_user,'opspilot_self_heal_authorizations','incident_id','INSERT') AS allowed"
            ).fetchone()[0]
            assert not conn.execute(
                "SELECT has_column_privilege(current_user,'opspilot_self_heal_authorizations','attempts','UPDATE') AS allowed"
            ).fetchone()[0]
            assert not conn.execute(
                "SELECT has_column_privilege(current_user,'opspilot_observation_sessions','authorization_source','UPDATE') AS allowed"
            ).fetchone()[0]
            if existing is not None:
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute(
                        "UPDATE opspilot_observation_sessions SET authorization_source='automatic' WHERE session_id=%s",
                        (existing[0],),
                    )
        with psycopg.connect(web_dsn) as conn:
            assert conn.execute(
                "SELECT has_table_privilege(current_user,'opspilot_self_heal_authorizations','SELECT') AS allowed"
            ).fetchone()[0]
            assert conn.execute(
                "SELECT has_column_privilege(current_user,'opspilot_self_heal_authorizations','incident_id','INSERT') AS allowed"
            ).fetchone()[0]
            assert conn.execute(
                "SELECT has_column_privilege(current_user,'opspilot_self_heal_authorizations','attempts','UPDATE') AS allowed"
            ).fetchone()[0]
            assert conn.execute(
                "SELECT has_column_privilege(current_user,'opspilot_observation_sessions','authorization_source','INSERT') AS allowed"
            ).fetchone()[0]
