"""Implementer's checks for the #184 step 1 self-heal pass on a real PostgreSQL.

The acceptance tests are the independent author's
(``test_self_heal_authorization_postgres.py``); these pin the implementation
details the r3 interface leaves to the implementer: the migration's CHECK,
the decision record and its retry arithmetic, the ``SKIP LOCKED`` hand-off
to the next pass, and the pass under the ``opspilot_web`` login role. The
published conclusion is written straight onto the incident row: the pass
reads only that durable state (S1-D2).

One scratch database per module, migrated to head.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.intake import IntakeEnvelope, IntakeRequest, Principal
from opspilot.observation.store import (
    AUTO_OBSERVATION_ACTION,
    AUTO_OBSERVATION_MAX_ATTEMPTS,
    AUTO_OBSERVATION_RETRY_BASE_SECONDS,
    AUTOMATIC,
    ObservationStore,
)
from opspilot.observer.health_profile import PROFILE_DIRECTORY, load_health_profile
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.web import (
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
)
from opspilot.web.store import TargetIdentity, TargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import login
from tests.target_support import ANY_TARGETS, IDENTITY, WORKLOAD

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
VERSIONS = {"state": "v1"}


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_heal_{uuid4().hex[:12]}"
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
def owner(scratch_dsn: str) -> Iterator[DurableStore]:
    store = DurableStore(scratch_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


def _workbench(
    store: DurableStore, *, targets: TargetRegistry | None = ANY_TARGETS
) -> Workbench:
    return Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=VERSIONS,
        targets=targets,
        run_seconds=600,
        health_profile=PROFILE,
    )


def ui_envelope(*, target_id: str, idempotency_key: str) -> IntakeEnvelope:
    return IntakeEnvelope(
        request_id=str(uuid4()),
        principal=Principal(
            actor_id="oncall-1", channel="ui_basic", auth_revision="auth-v1"
        ),
        request=IntakeRequest(
            target_id=target_id,
            question="Why is checkout erroring?",
            idempotency_key=idempotency_key,
        ),
        received_at=datetime.now(timezone.utc),
    )


def _published(workbench: Workbench, owner: DurableStore) -> UUID:
    """A fresh incident whose current Run published a conclusion at the
    incident's generation (what ``publish()`` leaves behind)."""
    result = workbench.submit(
        ui_envelope(
            target_id=f"checkout-{uuid4().hex[:8]}",
            idempotency_key=f"heal-{uuid4()}",
        )
    )
    _publish(owner, result.incident_id)
    return result.incident_id


def _publish(owner: DurableStore, incident: UUID) -> None:
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET conclusion=%s::jsonb WHERE incident_id=%s",
            ('{"kind": "test"}', incident),
        )
        conn.execute(
            "UPDATE opspilot_runs r SET state='completed',control_generation=i.control_generation FROM opspilot_incidents i WHERE i.incident_id=%s AND r.run_id=i.current_run_id",
            (incident,),
        )


def _rows(owner: DurableStore, query: str, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return conn.execute(query, (incident,)).fetchall()


def _incident(owner: DurableStore, incident: UUID) -> dict[str, Any]:
    (row,) = _rows(
        owner,
        "SELECT lifecycle,mode,state,control_generation,observation_generation FROM opspilot_incidents WHERE incident_id=%s",
        incident,
    )
    return row


def _sessions(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    return _rows(
        owner,
        "SELECT session_id,authorization_source,authorized_by,state,target,subject_control_generation,observation_generation,health_profile_revision,deadline_at,authorized_at,max_samples,sample_interval_seconds,sustained_window_seconds FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY created_at",
        incident,
    )


def _audit(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    return _rows(
        owner,
        "SELECT action,actor,expected_generation,resulting_generation,payload FROM opspilot_controls WHERE incident_id=%s ORDER BY created_at",
        incident,
    )


def _decisions(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    return _rows(
        owner,
        "SELECT control_generation,state,reason,attempts,next_attempt_at,session_id,updated_at FROM opspilot_self_heal_authorizations WHERE incident_id=%s ORDER BY control_generation",
        incident,
    )


def _mine(outcomes: list[Any], incident: UUID) -> list[Any]:
    return [o for o in outcomes if o.incident_id == incident]


def test_a_published_incident_is_authorized_once_with_the_profile_parameters(
    scratch_dsn: str, owner: DurableStore
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    before = _incident(owner, incident)

    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)

    assert (outcome.state, outcome.reason) == ("authorized", None)
    assert outcome.control_generation == before["control_generation"] == 0
    (session,) = _sessions(owner, incident)
    assert session["session_id"] == outcome.session_id
    assert (session["authorization_source"], session["authorized_by"]) == (
        AUTOMATIC,
        "system",
    )
    # C4: no remediation recorded, so no authoritative target revision.
    assert session["target"]["revision"] is None
    (view,) = workbench.snapshot(incident)["observation_sessions"]
    assert view["target_revision"] is None
    assert session["health_profile_revision"] == PROFILE.revision
    parameters = PROFILE.session
    assert (
        session["max_samples"],
        session["sample_interval_seconds"],
        session["sustained_window_seconds"],
    ) == (
        parameters.max_samples,
        parameters.sample_interval_seconds,
        parameters.sustained_window_seconds,
    )
    # The window starts at the authorizing transaction's clock (A6).
    window = session["deadline_at"] - session["authorized_at"]
    assert (
        timedelta(seconds=parameters.deadline_seconds - 2)
        <= window
        <= timedelta(seconds=parameters.deadline_seconds)
    )
    after = _incident(owner, incident)
    assert after["lifecycle"] == "observing_recovery"
    assert after["control_generation"] == 0, "S1-D3: control generation unchanged"
    assert after["observation_generation"] == before["observation_generation"] + 1
    assert session["subject_control_generation"] == 0
    (audit,) = _audit(owner, incident)
    assert (audit["actor"], audit["expected_generation"]) == ("system", 0)
    assert audit["resulting_generation"] == 0
    assert audit["action"] == AUTO_OBSERVATION_ACTION == "auto_authorize_observation"
    assert audit["payload"]["authorization_source"] == AUTOMATIC
    assert audit["payload"]["session_id"] == str(outcome.session_id)
    assert audit["payload"]["revision"] is None
    (decision,) = _decisions(owner, incident)
    assert (decision["state"], decision["session_id"]) == (
        "authorized",
        outcome.session_id,
    )
    # A first observation on a bare registry row completed it (no earlier
    # human registration).
    (target,) = _rows(
        owner,
        "SELECT t.integration_id,t.cluster_uid,t.namespace,t.workload FROM opspilot_targets t JOIN opspilot_incidents i ON i.target_id=t.target_id WHERE i.incident_id=%s",
        incident,
    )
    assert target == {**IDENTITY, "workload": WORKLOAD}
    # Idempotent: the next pass finds nothing to decide for this generation.
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []
    assert len(_sessions(owner, incident)) == 1 and len(_audit(owner, incident)) == 1


def test_an_unpublished_incident_is_not_a_candidate(owner: DurableStore) -> None:
    workbench = _workbench(owner)
    result = workbench.submit(
        ui_envelope(target_id="checkout-unpublished", idempotency_key=f"u-{uuid4()}")
    )
    assert _mine(workbench.self_heal_pass(limit=50), result.incident_id) == []
    assert _decisions(owner, result.incident_id) == []


def test_the_human_registration_records_post_remediation(owner: DurableStore) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    workbench.control(
        incident,
        actor_id="operator",
        action="register_remediation",
        expected_generation=0,
        idempotency_key="rem-1",
        revision="checkout:v2",
    )
    (session,) = _sessions(owner, incident)
    assert session["authorization_source"] == "post_remediation"
    assert session["authorized_by"] == "operator"
    # The active human session makes the incident ineligible.
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []


def test_a_suspended_target_is_skipped_for_good(owner: DurableStore) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    (target_id,) = (
        row["target_id"]
        for row in _rows(
            owner,
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
            incident,
        )
    )
    owner.set_target_suspension(target_id, True, expected_generation=0, actor="op")
    try:
        (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    finally:
        owner.set_target_suspension(target_id, False, expected_generation=1, actor="op")
    assert (outcome.state, outcome.reason, outcome.session_id) == (
        "skipped",
        "SCOPE_SUSPENDED",
        None,
    )
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []
    # Lifted: not reopened (C3 section 4; terminal skip).
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []
    assert [d["state"] for d in _decisions(owner, incident)] == ["skipped"]


@dataclass
class _NoIdentity:
    def resolve(self, target_id: str) -> TargetIdentity | None:
        return None


@dataclass
class _OtherProfile:
    def resolve(self, target_id: str) -> TargetIdentity | None:
        return TargetIdentity(
            resource_uid=target_id,
            health_profile_id="another-profile",
            workload=WORKLOAD,
            **IDENTITY,
        )


@pytest.mark.parametrize(
    ("targets", "reason"),
    [
        (None, "TARGET_IDENTITY_MISSING"),
        (_NoIdentity(), "TARGET_IDENTITY_MISSING"),
        (_OtherProfile(), "HEALTH_PROFILE_TARGET_MISMATCH"),
    ],
)
def test_a_target_without_identity_or_profile_is_skipped(
    owner: DurableStore, targets: TargetRegistry | None, reason: str
) -> None:
    workbench = _workbench(owner, targets=targets)
    incident = _published(workbench, owner)
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert (outcome.state, outcome.reason) == ("skipped", reason)
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []
    assert _incident(owner, incident)["lifecycle"] == "open"


def test_a_human_owned_incident_is_not_a_candidate(owner: DurableStore) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    workbench.control(
        incident,
        actor_id="operator",
        action="takeover",
        expected_generation=0,
        idempotency_key="take-1",
    )
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []
    assert _sessions(owner, incident) == []


def test_a_locked_incident_is_left_to_the_next_pass(
    scratch_dsn: str, owner: DurableStore
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    with psycopg.connect(scratch_dsn) as holder:
        holder.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (incident,),
        )
        assert _mine(workbench.self_heal_pass(limit=50), incident) == []
        assert _decisions(owner, incident) == []
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert outcome.state == "authorized"


class _Failing(DurableIncidentStore):
    """Authorization always fails operationally; the rest is the real store."""

    def authorize_automatic_observation(self, *args: Any, **kwargs: Any) -> Any:
        raise PersistenceError("STORAGE_UNAVAILABLE")


def test_operational_failures_back_off_then_hand_off_once(
    owner: DurableStore,
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    failing = _workbench(owner)
    failing.incidents = _Failing(owner)

    def due() -> None:
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_self_heal_authorizations SET next_attempt_at=clock_timestamp() WHERE incident_id=%s AND state='retrying'",
                (incident,),
            )

    for attempt in range(1, AUTO_OBSERVATION_MAX_ATTEMPTS):
        (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
        assert (outcome.state, outcome.reason) == ("retrying", "STORAGE_UNAVAILABLE")
        (decision,) = _decisions(owner, incident)
        assert decision["attempts"] == attempt
        delay = decision["next_attempt_at"] - decision["updated_at"]
        expected = AUTO_OBSERVATION_RETRY_BASE_SECONDS * 2 ** (attempt - 1)
        assert timedelta(seconds=expected - 1) <= delay <= timedelta(seconds=expected)
        # Not due yet: the next pass leaves it alone.
        assert _mine(failing.self_heal_pass(limit=50), incident) == []
        due()
    (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
    assert outcome.state == "handed_off"
    (decision,) = _decisions(owner, incident)
    assert (decision["attempts"], decision["next_attempt_at"]) == (
        AUTO_OBSERVATION_MAX_ATTEMPTS,
        None,
    )
    row = _incident(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 0)
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []
    events = [
        e
        for e in DurableEventLog(owner).read_after(incident, 0)
        if e.kind == "auto_observation_handed_off"
    ]
    assert len(events) == 1 and events[0].payload["control_generation"] == 0
    # Terminal: neither the failing nor a healthy pass touches it again.
    assert _mine(failing.self_heal_pass(limit=50), incident) == []
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []


def test_a_due_retry_that_became_stale_is_skipped(owner: DurableStore) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    failing = _workbench(owner)
    failing.incidents = _Failing(owner)
    (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
    assert outcome.state == "retrying"
    workbench.control(
        incident,
        actor_id="operator",
        action="takeover",
        expected_generation=0,
        idempotency_key="take-2",
    )
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_self_heal_authorizations SET next_attempt_at=clock_timestamp() WHERE incident_id=%s",
            (incident,),
        )
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert (outcome.control_generation, outcome.state, outcome.reason) == (
        0,
        "skipped",
        "CONTROL_GENERATION_CHANGED",
    )
    assert _decisions(owner, incident)[0]["attempts"] == 1


def test_the_pass_runs_under_the_web_role(
    scratch_dsn: str, owner: DurableStore
) -> None:
    seed = _workbench(owner)
    incident = _published(seed, owner)
    with login(scratch_dsn, "opspilot_web") as web_dsn:
        store = DurableStore(web_dsn, pool=POOL)
        try:
            store.install()
            (outcome,) = _mine(_workbench(store).self_heal_pass(limit=50), incident)
        finally:
            store.close()
    assert outcome.state == "authorized"
    (session,) = _sessions(owner, incident)
    assert session["authorization_source"] == AUTOMATIC


def test_the_worker_role_cannot_write_decisions(
    scratch_dsn: str, owner: DurableStore
) -> None:
    with login(scratch_dsn, "opspilot_worker") as worker_dsn:
        with psycopg.connect(worker_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO opspilot_self_heal_authorizations(incident_id,control_generation,state,reason) VALUES(%s,0,'skipped','x')",
                    (uuid4(),),
                )


def test_the_source_check_follows_the_purpose(
    scratch_dsn: str, owner: DurableStore
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    with psycopg.connect(scratch_dsn) as conn:
        for change in (
            "authorization_source=NULL",
            "authorization_source='manual'",
            "purpose='release_observation'",
            # P2-08 (F11 PR-2 contract): a release session must bind a release
            # subject, so an incident-bound session cannot become one
            "purpose='release_observation',authorization_source=NULL",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        f"UPDATE opspilot_observation_sessions SET {change} WHERE session_id=%s",
                        (outcome.session_id,),
                    )


def test_a_conclusion_without_a_completed_run_at_the_generation_is_not_a_candidate(
    owner: DurableStore,
) -> None:
    workbench = _workbench(owner)
    result = workbench.submit(
        ui_envelope(target_id="checkout-queued", idempotency_key=f"q-{uuid4()}")
    )
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET conclusion='{}'::jsonb WHERE incident_id=%s",
            (result.incident_id,),
        )
    assert _mine(workbench.self_heal_pass(limit=50), result.incident_id) == []


def test_a_human_registration_after_the_automatic_one_is_not_a_publish(
    owner: DurableStore,
) -> None:
    """r5: publish at G, automatic observation at G, a human registers a
    remediation (G+1), that observation ends unconfirmed: the incident is
    ``open`` again with its conclusion, yet nothing was published at G+1, so
    no new automatic observation is authorized."""
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    (first,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert first.state == "authorized"
    workbench.control(
        incident,
        actor_id="operator",
        action="register_remediation",
        expected_generation=0,
        idempotency_key="rem-r5",
        revision="checkout:v2",
    )
    automatic, human = _sessions(owner, incident)
    assert automatic["state"] != "authorized" and human["state"] == "authorized"
    assert human["authorization_source"] == "post_remediation"
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp() - interval '1 second' WHERE session_id=%s",
            (human["session_id"],),
        )
    observer = ObservationStore(owner.dsn, pool=POOL)
    try:
        assert human["session_id"] in observer.sweep_expired_sessions(limit=50)
    finally:
        observer.close()
    row = _incident(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 1)
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []
    assert [d["control_generation"] for d in _decisions(owner, incident)] == [0]
    assert len(_sessions(owner, incident)) == 2


def test_both_paths_name_the_source_without_the_column_default(
    scratch_dsn: str, owner: DurableStore
) -> None:
    """The default is only the legacy reading: with it dropped, an automatic
    and a human authorization still write, each with its own source."""
    workbench = _workbench(owner)
    automatic = _published(workbench, owner)
    human = _published(workbench, owner)
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            "ALTER TABLE opspilot_observation_sessions ALTER COLUMN authorization_source DROP DEFAULT"
        )
    try:
        workbench.control(
            human,
            actor_id="operator",
            action="register_remediation",
            expected_generation=0,
            idempotency_key="rem-nodefault",
            revision="checkout:v2",
        )
        (outcome,) = _mine(workbench.self_heal_pass(limit=50), automatic)
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(
                "ALTER TABLE opspilot_observation_sessions ALTER COLUMN authorization_source SET DEFAULT 'post_remediation'"
            )
    assert outcome.state == "authorized"
    assert [s["authorization_source"] for s in _sessions(owner, automatic)] == [
        AUTOMATIC
    ]
    assert [s["authorization_source"] for s in _sessions(owner, human)] == [
        "post_remediation"
    ]


def test_a_registry_row_completed_with_another_identity_is_skipped(
    owner: DurableStore,
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets t SET integration_id='other',cluster_uid='other',namespace='otel-demo',workload='checkout' FROM opspilot_incidents i WHERE i.incident_id=%s AND t.target_id=i.target_id",
            (incident,),
        )
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert (outcome.state, outcome.reason) == ("skipped", "TARGET_MISMATCH")
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []


def test_only_an_automatic_observation_may_leave_the_revision_unknown(
    owner: DurableStore,
) -> None:
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    store = ObservationStore(owner.dsn, pool=POOL)
    try:
        with pytest.raises(PersistenceError, match="INVALID_INPUT"):
            with store.transaction() as conn:
                store.authorize_session_in(
                    conn,
                    incident,
                    revision=None,
                    actor="operator",
                    authorization_source="post_remediation",
                    deadline_at=store.current_time() + timedelta(hours=1),
                    max_samples=1,
                    sample_interval_seconds=60,
                    sustained_window_seconds=60,
                )
    finally:
        store.close()
    assert _sessions(owner, incident) == []


def test_a_session_already_authorized_at_the_generation_is_skipped(
    owner: DurableStore,
) -> None:
    """C8: an active session (here granted through the storage primitive,
    which does not step the generation) keeps the incident a C1 candidate;
    the locked re-check records the skip."""
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    store = ObservationStore(owner.dsn, pool=POOL)
    try:
        resource_uid = _rows(
            owner,
            "SELECT t.resource_uid FROM opspilot_targets t JOIN opspilot_incidents i ON i.target_id=t.target_id WHERE i.incident_id=%s",
            incident,
        )[0]["resource_uid"]
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload=%s WHERE resource_uid=%s",
                (*IDENTITY.values(), WORKLOAD, resource_uid),
            )
        store.authorize_session(
            incident,
            revision="checkout:v1",
            actor="operator",
            deadline_at=store.current_time() + timedelta(hours=1),
            max_samples=1,
            sample_interval_seconds=60,
            sustained_window_seconds=60,
        )
    finally:
        store.close()
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert (outcome.state, outcome.reason) == (
        "skipped",
        "OBSERVATION_ALREADY_AUTHORIZED",
    )
    assert len(_sessions(owner, incident)) == 1 and _audit(owner, incident) == []


def test_human_ownership_found_under_the_lock_is_skipped(owner: DurableStore) -> None:
    """C8: ``mode`` is not a scan filter; a candidate that is human-owned
    when locked (a takeover racing the scan) is recorded skipped."""
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET mode='human_owned' WHERE incident_id=%s",
            (incident,),
        )
    (outcome,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert (outcome.state, outcome.reason) == ("skipped", "HUMAN_OWNED")
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []


def test_a_failed_handoff_event_rolls_the_handoff_back_and_it_lands_once(
    owner: DurableStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C9: the ``handed_off`` decision and its event are one transaction.
    The event append fails once: the hand-off rolls back with it (the
    decision stays ``retrying``); the next due pass hands off with exactly
    one event."""
    from opspilot.observation import store as observation_store

    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    failing = _workbench(owner)
    failing.incidents = _Failing(owner)

    def due() -> None:
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_self_heal_authorizations SET next_attempt_at=clock_timestamp() WHERE incident_id=%s AND state='retrying'",
                (incident,),
            )

    for _ in range(1, AUTO_OBSERVATION_MAX_ATTEMPTS):
        (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
        assert outcome.state == "retrying"
        due()

    real = observation_store.append_once_in
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return real(*args, **kwargs)

    monkeypatch.setattr(observation_store, "append_once_in", flaky)

    def handed_off_events() -> list[Any]:
        return [
            e
            for e in DurableEventLog(owner).read_after(incident, 0)
            if e.kind == "auto_observation_handed_off"
        ]

    with pytest.raises(PersistenceError, match="STORAGE_UNAVAILABLE"):
        failing.self_heal_pass(limit=50)
    (decision,) = _decisions(owner, incident)
    assert (decision["state"], decision["attempts"]) == (
        "retrying",
        AUTO_OBSERVATION_MAX_ATTEMPTS - 1,
    )
    assert handed_off_events() == []

    (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
    assert outcome.state == "handed_off"
    assert calls["n"] == 2
    (event,) = handed_off_events()
    assert event.payload == {
        "control_generation": 0,
        "reason": "STORAGE_UNAVAILABLE",
        "attempts": AUTO_OBSERVATION_MAX_ATTEMPTS,
    }
    # Settled: further passes neither hand off again nor add an event.
    assert _mine(failing.self_heal_pass(limit=50), incident) == []
    assert len(handed_off_events()) == 1


def test_a_retry_of_a_superseded_generation_is_retired_before_it_is_due(
    owner: DurableStore,
) -> None:
    """C9: a failure at G leaves the decision ``retrying``; a human registers
    a remediation (G+1); the next pass retires the G decision as skipped
    without waiting for its retry time and changes nothing else."""
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    failing = _workbench(owner)
    failing.incidents = _Failing(owner)
    (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
    assert outcome.state == "retrying"
    workbench.control(
        incident,
        actor_id="operator",
        action="register_remediation",
        expected_generation=0,
        idempotency_key="rem-stale-retry",
        revision="checkout:v2",
    )
    before = _incident(owner, incident)
    audit_before = _audit(owner, incident)
    (retired,) = _mine(workbench.self_heal_pass(limit=50), incident)
    assert (retired.control_generation, retired.state, retired.reason) == (
        0,
        "skipped",
        "CONTROL_GENERATION_CHANGED",
    )
    (decision,) = _decisions(owner, incident)
    assert (decision["state"], decision["attempts"], decision["next_attempt_at"]) == (
        "skipped",
        1,
        None,
    )
    assert _incident(owner, incident) == before
    assert _audit(owner, incident) == audit_before
    assert [s["authorization_source"] for s in _sessions(owner, incident)] == [
        "post_remediation"
    ]
    assert not [
        e
        for e in DurableEventLog(owner).read_after(incident, 0)
        if e.kind == "auto_observation_handed_off"
    ]
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []


def test_a_failure_recorded_after_a_human_registration_is_a_stale_skip(
    owner: DurableStore,
) -> None:
    """Independent review P1: the authorization at G fails while a human
    registers a remediation (G+1). With four failures already counted, the
    fifth would hand off; recorded after the human decision, it settles the
    G decision ``skipped`` instead -- no attempt, no hand-off, no event."""
    workbench = _workbench(owner)
    incident = _published(workbench, owner)
    failing = _workbench(owner)
    failing.incidents = _Failing(owner)

    def due() -> None:
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_self_heal_authorizations SET next_attempt_at=clock_timestamp() WHERE incident_id=%s AND state='retrying'",
                (incident,),
            )

    for _ in range(1, AUTO_OBSERVATION_MAX_ATTEMPTS):
        (outcome,) = _mine(failing.self_heal_pass(limit=50), incident)
        assert outcome.state == "retrying"
        due()
    assert _decisions(owner, incident)[0]["attempts"] == (
        AUTO_OBSERVATION_MAX_ATTEMPTS - 1
    )

    class _RacedByHuman(DurableIncidentStore):
        def authorize_automatic_observation(self, *args: Any, **kwargs: Any) -> Any:
            workbench.control(
                incident,
                actor_id="operator",
                action="register_remediation",
                expected_generation=0,
                idempotency_key="rem-race",
                revision="checkout:v2",
            )
            raise PersistenceError("STORAGE_UNAVAILABLE")

    racing = _workbench(owner)
    racing.incidents = _RacedByHuman(owner)
    (outcome,) = _mine(racing.self_heal_pass(limit=50), incident)
    assert (outcome.control_generation, outcome.state, outcome.reason) == (
        0,
        "skipped",
        "CONTROL_GENERATION_CHANGED",
    )
    (decision,) = _decisions(owner, incident)
    assert (decision["state"], decision["attempts"], decision["next_attempt_at"]) == (
        "skipped",
        AUTO_OBSERVATION_MAX_ATTEMPTS - 1,
        None,
    )
    assert not [
        e
        for e in DurableEventLog(owner).read_after(incident, 0)
        if e.kind == "auto_observation_handed_off"
    ]
    row = _incident(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("observing_recovery", 1)
    assert [s["authorization_source"] for s in _sessions(owner, incident)] == [
        "post_remediation"
    ]
    assert [a["action"] for a in _audit(owner, incident)] == ["register_remediation"]
    assert _mine(workbench.self_heal_pass(limit=50), incident) == []
