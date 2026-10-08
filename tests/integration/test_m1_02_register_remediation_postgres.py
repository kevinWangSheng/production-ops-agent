"""Registering a human remediation starts an observation (M1-02 step 3, #85).

C3 section 10 "观察主体与授权" and section 4 "人工控制与结果所有权" on a real
PostgreSQL: the registration is one transaction (generation step under
``expected_generation``, earlier authorization withdrawn, new session bound
to the new generation, incident ``observing_recovery``, audit row); pause,
cancel, a renewal and a new Run revoke the authorization in their own
transaction; the session's Target is the registry's identity plus the
revision snapshot; a stale form conflicts and writes nothing; a concurrent
cancel and registration never leave an authorized session on a cancelled
decision. The workbench flow (idempotency key, page) is covered through the
real app at the end.

One scratch database per module (migrated to head); every test works on
its own incident and target.
"""

from __future__ import annotations

import json
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
from opspilot.domain.intake import Target
from opspilot.observation import ObservationStore
from opspilot.observer.health_profile import (
    PROFILE_DIRECTORY,
    HealthProfileError,
    canonical_content,
    load_health_profile,
)
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.persistence.base import _StoreBase
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
)
from tests.target_support import ANY_TARGETS, IDENTITY, WORKLOAD, identity_of

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
VERSIONS = {"state": "v1"}


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_reg_{uuid4().hex[:12]}"
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


@pytest.fixture(scope="module")
def controller(scratch_dsn: str) -> Iterator[ObservationStore]:
    store = ObservationStore(scratch_dsn, pool=POOL)
    store.install()
    yield store
    store.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _incident(owner: DurableStore) -> tuple[UUID, UUID, UUID, str]:
    incident, run = uuid4(), uuid4()
    uid = f"deployment/checkout-{uuid4().hex[:8]}"
    target_id = owner.register_target(uid)
    owner.accept(
        incident,
        run,
        f"reg-{incident}",
        deadline=_now() + timedelta(hours=1),
        budget_limit=10,
        versions=VERSIONS,
        target_id=target_id,
    )
    return incident, run, target_id, uid


# Sentinel: ``_register`` resolves the identity like the workbench would.
_REGISTRY = object()


def _register(
    controller: ObservationStore,
    incident: UUID,
    *,
    expected: int,
    revision: str = "checkout:v2",
    actor: str = "operator",
    session_id: UUID | None = None,
    identity: dict[str, str] | None | object = _REGISTRY,
) -> int:
    """``identity`` defaults to the configured registry's answer for the
    incident's target (what the workbench passes); ``None`` means no registry."""
    if identity is _REGISTRY:
        identity = identity_of(_resource_uid(controller, incident))
    parameters = PROFILE.session
    return controller.register_remediation(
        incident,
        expected_generation=expected,
        actor=actor,
        revision=revision,
        deadline_at=_now() + timedelta(seconds=parameters.deadline_seconds),
        max_samples=parameters.max_samples,
        sample_interval_seconds=parameters.sample_interval_seconds,
        sustained_window_seconds=parameters.sustained_window_seconds,
        health_profile_revision=PROFILE.revision,
        health_profile=canonical_content(PROFILE),
        session_id=session_id,
        identity=identity,  # type: ignore[arg-type]
        payload={"channel": "test"},
    )


def _registered(owner: DurableStore, target_id: UUID) -> dict[str, Any]:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT resource_uid,integration_id,cluster_uid,namespace,workload FROM opspilot_targets WHERE target_id=%s",
            (target_id,),
        ).fetchone()
    assert row is not None
    return row


def _resource_uid(store: _StoreBase, incident: UUID) -> str:
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT t.resource_uid FROM opspilot_incidents i JOIN opspilot_targets t ON t.target_id=i.target_id WHERE i.incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return str(row["resource_uid"])


def _incident_row(owner: DurableStore, incident: UUID) -> dict[str, Any]:
    with owner.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT lifecycle,state,control_generation,observation_generation FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    assert row is not None
    return row


def _sessions(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT session_id,state,ended_reason,subject_control_generation,observation_generation,target,health_profile_revision,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds,active_sample_job_id FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY created_at,session_id",
                (incident,),
            ).fetchall()
        )


def _endings(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT session_id,ended_reason,transition,lifecycle_before,lifecycle_after FROM opspilot_observation_endings WHERE incident_id=%s ORDER BY recorded_at",
                (incident,),
            ).fetchall()
        )


def _audit(owner: DurableStore, incident: UUID) -> list[dict[str, Any]]:
    with owner.transaction(snapshot=True) as conn:
        return list(
            conn.execute(
                "SELECT action,expected_generation,resulting_generation,actor,payload FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
                (incident,),
            ).fetchall()
        )


# --- the registration transaction


def test_registration_steps_the_generation_authorizes_and_observes(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target_id, uid = _incident(owner)
    before = _incident_row(owner, incident)
    assert (before["lifecycle"], before["control_generation"]) == ("open", 0)
    assert _registered(owner, target_id)["integration_id"] is None

    assert _register(controller, incident, expected=0, revision="checkout:v2") == 1

    after = _incident_row(owner, incident)
    assert after["lifecycle"] == "observing_recovery"
    assert after["control_generation"] == 1
    assert after["observation_generation"] == 1
    # The human-control mirror is untouched: observation is not a Run state.
    assert after["state"] == before["state"]
    (session,) = _sessions(owner, incident)
    assert session["state"] == "authorized"
    assert session["subject_control_generation"] == 1
    assert session["observation_generation"] == 1
    assert session["health_profile_revision"] == PROFILE.revision
    assert session["active_sample_job_id"] is not None
    # Parameters are the profile's, not a caller's choice.
    assert session["max_samples"] == PROFILE.session.max_samples
    assert session["sample_interval_seconds"] == PROFILE.session.sample_interval_seconds
    assert (
        session["sustained_window_seconds"] == PROFILE.session.sustained_window_seconds
    )
    assert session["deadline_at"] <= _now() + timedelta(
        seconds=PROFILE.session.deadline_seconds
    )
    # The Target is the registry's identity plus the revision snapshot; the
    # operator never passed an identity. Intake registered the uid alone;
    # this first registration completed the row from the configured registry.
    assert Target(**session["target"]) == Target(
        resource_uid=uid, revision="checkout:v2", **IDENTITY
    )
    assert _registered(owner, target_id) == {
        "resource_uid": uid,
        "workload": WORKLOAD,
        **IDENTITY,
    }
    (audit,) = _audit(owner, incident)
    assert (audit["action"], audit["expected_generation"]) == (
        "register_remediation",
        0,
    )
    assert audit["resulting_generation"] == 1 and audit["actor"] == "operator"
    assert audit["payload"]["revision"] == "checkout:v2"
    assert audit["payload"]["session_id"] == str(session["session_id"])
    assert audit["payload"]["health_profile_revision"] == PROFILE.revision
    assert audit["payload"]["channel"] == "test"
    # The profile content is stored behind its revision for the replay.
    assert controller.health_profile(PROFILE.revision)["content"] == canonical_content(
        PROFILE
    )


def test_a_stale_generation_conflicts_and_writes_nothing(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, _, _ = _incident(owner)
    assert _register(controller, incident, expected=0) == 1
    with pytest.raises(PersistenceError, match="CONTROL_CONFLICT"):
        _register(controller, incident, expected=0)
    with pytest.raises(PersistenceError, match="CONTROL_CONFLICT"):
        _register(controller, incident, expected=5)
    row = _incident_row(owner, incident)
    assert (row["control_generation"], row["observation_generation"]) == (1, 1)
    assert len(_sessions(owner, incident)) == 1
    assert len(_audit(owner, incident)) == 1


def test_a_second_registration_supersedes_the_first_session(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """Another remediation (another revision) is a new decision: the old
    authorization ends as revoked in the same transaction, the new session
    carries the new generations and the new revision, the incident keeps
    observing. The identity is the registry's both times."""
    incident, _, _, uid = _incident(owner)
    assert _register(controller, incident, expected=0, revision="checkout:v2") == 1
    assert _register(controller, incident, expected=1, revision="checkout:v3") == 2

    row = _incident_row(owner, incident)
    assert row["lifecycle"] == "observing_recovery"
    assert (row["control_generation"], row["observation_generation"]) == (2, 2)
    first, second = _sessions(owner, incident)
    assert (first["state"], first["ended_reason"]) == ("revoked", "authority_revoked")
    assert first["active_sample_job_id"] is None
    assert second["state"] == "authorized"
    assert (second["subject_control_generation"], second["observation_generation"]) == (
        2,
        2,
    )
    assert Target(**second["target"]).revision == "checkout:v3"
    assert Target(**first["target"]).model_copy(update={"revision": "checkout:v3"}) == (
        Target(**second["target"])
    )
    (ending,) = _endings(owner, incident)
    assert ending["session_id"] == first["session_id"]
    assert (ending["ended_reason"], ending["transition"]) == ("authority_revoked", None)
    assert (ending["lifecycle_before"], ending["lifecycle_after"]) == (
        "observing_recovery",
        "observing_recovery",
    )
    second_audit = _audit(owner, incident)[1]
    assert second_audit["payload"]["revoked_sessions"] == [str(first["session_id"])]


def test_registration_is_refused_while_the_scope_is_suspended(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target_id, _ = _incident(owner)
    owner.set_target_suspension(target_id, True, expected_generation=0, actor="op")
    try:
        with pytest.raises(PersistenceError, match="SCOPE_SUSPENDED"):
            _register(controller, incident, expected=0)
    finally:
        owner.set_target_suspension(target_id, False, expected_generation=1, actor="op")
    row = _incident_row(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 0)
    assert _sessions(owner, incident) == []
    assert _audit(owner, incident) == []
    # Released: the control mirror stays paused until a human resumes
    # (C3 section 4), so the registration is still refused; after the
    # explicit resume it goes through under the new generation.
    assert _incident_row(owner, incident)["state"] == "paused"
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        _register(controller, incident, expected=0)
    assert owner.control(incident, 0, "resume", "operator") == 1
    assert _register(controller, incident, expected=1) == 2


def test_registration_is_refused_on_a_resolved_or_closed_incident(
    owner: DurableStore, controller: ObservationStore
) -> None:
    for lifecycle in ("resolved", "closed"):
        incident, _, _, _ = _incident(owner)
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                (lifecycle, incident),
            )
        with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
            _register(controller, incident, expected=0)
        row = _incident_row(owner, incident)
        assert (row["lifecycle"], row["control_generation"]) == (lifecycle, 0)
        assert _sessions(owner, incident) == []


def test_registration_refuses_a_registry_row_that_no_longer_matches(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """The identity every earlier session recorded must still be the
    registry's; a registry row edited underneath is TARGET_MISMATCH and the
    generation does not move (user decision 2026-10-07)."""
    incident, _, target_id, uid = _incident(owner)
    assert _register(controller, incident, expected=0) == 1
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets SET namespace='elsewhere' WHERE target_id=%s",
            (target_id,),
        )
    try:
        with pytest.raises(PersistenceError, match="TARGET_MISMATCH"):
            _register(controller, incident, expected=1, revision="checkout:v3")
    finally:
        with owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_targets SET namespace=%s WHERE target_id=%s",
                (IDENTITY["namespace"], target_id),
            )
    row = _incident_row(owner, incident)
    assert row["control_generation"] == 1
    (session,) = _sessions(owner, incident)
    assert session["state"] == "authorized"
    assert len(_audit(owner, incident)) == 1


# --- revocation by the human-control paths (C3 section 10)


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_pause_and_cancel_revoke_the_authorization_in_their_transaction(
    owner: DurableStore, controller: ObservationStore, action: str
) -> None:
    incident, run, _, _ = _incident(owner)
    assert _register(controller, incident, expected=0) == 1
    if action == "pause":
        # pause needs a running/waiting Run; claim one.
        owner.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)

    assert owner.control(incident, 1, action, "operator") == 2

    (session,) = _sessions(owner, incident)
    assert (session["state"], session["ended_reason"]) == (
        "revoked",
        "authority_revoked",
    )
    assert session["active_sample_job_id"] is None
    (ending,) = _endings(owner, incident)
    assert (ending["lifecycle_before"], ending["lifecycle_after"]) == (
        "observing_recovery",
        "observing_recovery",
    )
    row = _incident_row(owner, incident)
    # The lifecycle is not the control path's to change; the incident stays
    # observing without a session until a human registers again (a new
    # registration is a fresh decision under the new generation).
    assert row["lifecycle"] == "observing_recovery"
    assert row["state"] == ("paused" if action == "pause" else "cancelled")
    generation = 2
    if action == "pause":
        # A paused incident takes no new authorization (review P1): the
        # registration waits for an explicit resume.
        with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
            _register(controller, incident, expected=2, revision="checkout:v3")
        assert owner.control(incident, 2, "resume", "operator") == 3
        generation = 3
    assert (
        _register(controller, incident, expected=generation, revision="checkout:v3")
        == generation + 1
    )
    sessions = _sessions(owner, incident)
    assert [s["state"] for s in sessions] == ["revoked", "authorized"]


def test_a_renewal_on_an_overdue_run_revokes_and_reopens_in_one_transaction(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """The step 2 risk: the renewal path writes the lifecycle back to open;
    it must end the session in the same transaction, with the ending record
    taken before the lifecycle moved."""
    incident, run, _, _ = _incident(owner)
    assert _register(controller, incident, expected=0) == 1
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )
    fresh = uuid4()
    assert (
        owner.control(
            incident,
            1,
            "follow_up",
            "operator",
            {"text": "also check the rollout", "channel": "web"},
            renew_run_id=fresh,
            renew_deadline=_now() + timedelta(minutes=5),
        )
        == 2
    )
    row = _incident_row(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 2)
    (session,) = _sessions(owner, incident)
    assert (session["state"], session["ended_reason"]) == (
        "revoked",
        "authority_revoked",
    )
    (ending,) = _endings(owner, incident)
    assert (ending["lifecycle_before"], ending["lifecycle_after"]) == (
        "observing_recovery",
        "observing_recovery",
    )


def test_a_new_run_after_cancel_revokes_a_session_registered_in_between(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, _, _ = _incident(owner)
    assert owner.control(incident, 0, "cancel", "operator") == 1
    assert _register(controller, incident, expected=1) == 2
    assert _incident_row(owner, incident)["lifecycle"] == "observing_recovery"
    assert (
        owner.new_run(
            incident,
            uuid4(),
            expected_generation=2,
            deadline=_now() + timedelta(minutes=5),
            budget_limit=10,
            versions=VERSIONS,
            actor="operator",
        )
        == 3
    )
    row = _incident_row(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 3)
    (session,) = _sessions(owner, incident)
    assert (session["state"], session["ended_reason"]) == (
        "revoked",
        "authority_revoked",
    )


def test_a_registration_racing_a_cancel_never_leaves_a_live_session_behind(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """Both decisions carry the same expected generation; the row lock
    serializes them, exactly one applies, and the outcome is consistent
    either way: a winning cancel leaves no authorized session, a winning
    registration leaves one bound to the generation the cancel then
    conflicts on. Repeated to let either order happen."""
    outcomes: set[str] = set()
    for _ in range(8):
        incident, _, _, _ = _incident(owner)
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
                args=("register", lambda: _register(controller, incident, expected=0)),
            ),
            threading.Thread(
                target=act,
                args=("cancel", lambda: owner.control(incident, 0, "cancel", "op")),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results.values()) == ["CONTROL_CONFLICT", "applied"], results
        winner = next(name for name, value in results.items() if value == "applied")
        outcomes.add(winner)
        row = _incident_row(owner, incident)
        assert row["control_generation"] == 1
        sessions = _sessions(owner, incident)
        if winner == "cancel":
            assert row["lifecycle"] == "open" and sessions == []
        else:
            assert row["lifecycle"] == "observing_recovery"
            assert [s["state"] for s in sessions] == ["authorized"]
            assert sessions[0]["subject_control_generation"] == 1
    # Not asserted: that both orders occurred in 8 runs (timing); each
    # outcome that did occur satisfied the invariant.


# --- through the workbench (idempotency key, page)


def _build(dsn: str) -> tuple[Any, Workbench, DurableStore]:
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    ledger = DurableWebLedger(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=VERSIONS,
        targets=ANY_TARGETS,
        run_seconds=600,
        health_profile=PROFILE,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, workbench, store


def test_the_workbench_action_is_idempotent_and_the_page_shows_the_session(
    scratch_dsn: str, owner: DurableStore
) -> None:
    app, workbench, store = _build(scratch_dsn)
    try:
        submitted = post_form(
            app,
            "/intake/ui",
            {
                "target_id": f"checkout-{uuid4().hex[:8]}",
                "question": "Why is checkout erroring?",
                "idempotency_key": f"reg-web-{uuid4()}",
            },
            headers={**basic(), **same_origin()},
        )
        assert submitted.status == 201
        incident = UUID(submitted.json()["incident_id"])
        fields = {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "rem-1",
            "revision": "checkout:v2",
        }
        first = post_form(
            app,
            f"/incidents/{incident}/control",
            fields,
            headers={**basic(), **same_origin()},
        )
        assert first.status == 200, first.text
        assert first.json()["generation"] == 1 and first.json()["replayed"] is False
        replay = post_form(
            app,
            f"/incidents/{incident}/control",
            fields,
            headers={**basic(), **same_origin()},
        )
        assert replay.status == 200
        assert replay.json()["generation"] == 1 and replay.json()["replayed"] is True
        assert len(_sessions(owner, incident)) == 1, "same key, one session"
        stale = post_form(
            app,
            f"/incidents/{incident}/control",
            {**fields, "idempotency_key": "rem-2"},
            headers={**basic(), **same_origin()},
        )
        assert stale.status == 409
        assert stale.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
        missing = post_form(
            app,
            f"/incidents/{incident}/control",
            {
                "action": "register_remediation",
                "expected_generation": "1",
                "idempotency_key": "rem-3",
            },
            headers={**basic(), **same_origin()},
        )
        assert (missing.status, missing.json()["code"]) == (400, "REVISION_REQUIRED")
        page = call(app, "GET", f"/incidents/{incident}", headers=basic())
        assert page.status == 200
        assert "observing_recovery" in page.text
        assert 'id="observation-sessions"' in page.text
        assert "checkout:v2" in page.text and PROFILE.revision in page.text
        snapshot = workbench.snapshot(incident)
        (view,) = snapshot["observation_sessions"]
        assert (
            view["state"] == "authorized" and view["target_revision"] == "checkout:v2"
        )
        assert [c["action"] for c in snapshot["controls"]] == ["register_remediation"]
        assert snapshot["controls"][0]["revision"] == "checkout:v2"
        assert _incident_row(owner, incident)["lifecycle"] == "observing_recovery"
    finally:
        store.close()


def test_a_paused_incident_takes_no_registration(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """Independent review of PR #120, P1 (its reproduction: pause, then
    register under the new generation): refused, nothing written, the pause
    stands; after an explicit resume the registration goes through."""
    incident, run, _, _ = _incident(owner)
    owner.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
    assert owner.control(incident, 0, "pause", "operator") == 1
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        _register(controller, incident, expected=1)
    row = _incident_row(owner, incident)
    assert (row["state"], row["lifecycle"]) == ("paused", "open")
    assert (row["control_generation"], row["observation_generation"]) == (1, 0)
    assert _sessions(owner, incident) == []
    assert [a["action"] for a in _audit(owner, incident)] == ["pause"]
    # The storage primitive refuses as well, not only the human action.
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        controller.authorize_session(
            incident,
            revision="checkout:v2",
            actor="tester",
            deadline_at=_now() + timedelta(hours=1),
            max_samples=3,
            sample_interval_seconds=15,
            sustained_window_seconds=60,
        )
    assert owner.control(incident, 1, "resume", "operator") == 2
    assert _register(controller, incident, expected=2) == 3
    row = _incident_row(owner, incident)
    assert (row["state"], row["lifecycle"]) == ("running", "observing_recovery")


def test_a_bare_registry_row_without_a_configured_identity_refuses_registration(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """Intake registered the uid alone (M1-01 contract, unchanged); without a
    configured identity to complete it, the registration fails closed and
    writes nothing -- the generation, the lifecycle, the row."""
    incident, _, target_id, uid = _incident(owner)
    with pytest.raises(PersistenceError, match="TARGET_IDENTITY_MISSING"):
        _register(controller, incident, expected=0, identity=None)
    row = _incident_row(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 0)
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []
    assert _registered(owner, target_id) == {
        "resource_uid": uid,
        "integration_id": None,
        "cluster_uid": None,
        "namespace": None,
        "workload": None,
    }
    # The storage primitive is fail-closed too.
    with pytest.raises(PersistenceError, match="TARGET_IDENTITY_MISSING"):
        controller.authorize_session(
            incident,
            revision="checkout:v2",
            actor="tester",
            deadline_at=_now() + timedelta(hours=1),
            max_samples=3,
            sample_interval_seconds=15,
            sustained_window_seconds=60,
        )
    # With the identity the same registration goes through under the same
    # generation, and the row is complete from then on.
    assert _register(controller, incident, expected=0) == 1
    assert _registered(owner, target_id) == {
        "resource_uid": uid,
        "workload": WORKLOAD,
        **IDENTITY,
    }


def test_a_completed_identity_never_changes_in_place(
    owner: DurableStore, controller: ObservationStore
) -> None:
    incident, _, target_id, uid = _incident(owner)
    assert _register(controller, incident, expected=0) == 1
    other = {**identity_of(uid), "namespace": "elsewhere"}
    with pytest.raises(PersistenceError, match="TARGET_MISMATCH"):
        _register(controller, incident, expected=1, revision="v3", identity=other)
    with pytest.raises(PersistenceError, match="TARGET_MISMATCH"):
        _register(
            controller,
            incident,
            expected=1,
            revision="v3",
            identity={**identity_of(uid), "resource_uid": "another"},
        )
    assert _registered(owner, target_id) == {
        "resource_uid": uid,
        "workload": WORKLOAD,
        **IDENTITY,
    }
    assert _incident_row(owner, incident)["control_generation"] == 1
    assert len(_sessions(owner, incident)) == 1
    # Without a registry entry a complete row is still usable.
    assert (
        _register(controller, incident, expected=1, revision="v3", identity=None) == 2
    )


def test_the_profile_authorizes_only_the_workload_its_subject_names(
    owner: DurableStore, controller: ObservationStore
) -> None:
    """Second recheck of PR #120, P1 reproduction: an identity for
    ``payment-prod`` in namespace ``payments-prod`` declared under the checkout
    profile. The store compares the profile's ``subject`` (namespace,
    service) with the registry row (namespace, workload) under the incident
    lock: refused, nothing written -- also for a target in the profile's
    namespace that is another workload, and for the storage primitive."""
    incident, _, target_id, uid = _incident(owner)
    payment = {**identity_of(uid, workload="payment"), "namespace": "payments-prod"}
    with pytest.raises(PersistenceError, match="HEALTH_PROFILE_TARGET_MISMATCH"):
        _register(controller, incident, expected=0, identity=payment)
    other, _, other_target, other_uid = _incident(owner)
    with pytest.raises(PersistenceError, match="HEALTH_PROFILE_TARGET_MISMATCH"):
        _register(
            controller,
            other,
            expected=0,
            identity=identity_of(other_uid, workload="payment"),
        )
    for subject, tid in ((incident, target_id), (other, other_target)):
        row = _incident_row(owner, subject)
        assert (row["lifecycle"], row["control_generation"]) == ("open", 0)
        assert _sessions(owner, subject) == [] and _audit(owner, subject) == []
        # The transaction rolled back: the registry row is still bare.
        assert _registered(owner, tid)["workload"] is None
    # The checkout workload in the profile's namespace is authorized.
    assert _register(controller, incident, expected=0) == 1
    # The primitive is bound too: a complete row for another workload.
    foreign, _, foreign_target, _ = _incident(owner)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload='payment' WHERE target_id=%s",
            (
                IDENTITY["integration_id"],
                IDENTITY["cluster_uid"],
                IDENTITY["namespace"],
                foreign_target,
            ),
        )
    with pytest.raises(PersistenceError, match="HEALTH_PROFILE_TARGET_MISMATCH"):
        controller.authorize_session(
            foreign,
            revision="payment:v2",
            actor="tester",
            deadline_at=_now() + timedelta(hours=1),
            max_samples=3,
            sample_interval_seconds=15,
            sustained_window_seconds=60,
            health_profile_revision=PROFILE.revision,
            health_profile=canonical_content(PROFILE),
        )
    assert _incident_row(owner, foreign)["lifecycle"] == "open"
    assert _sessions(owner, foreign) == []


def test_a_profile_whose_queries_select_another_workload_cannot_start_observation(
    owner: DurableStore, controller: ObservationStore, tmp_path, monkeypatch
) -> None:
    """Issue #122 end to end on the authorization path. A payment incident
    in ``payments-prod`` has two candidate profiles: the shipped checkout
    profile with only its ``subject`` edited to payment (its queries still
    select checkout) and the shipped profile itself. The first never
    becomes a ``HealthProfile``: the loader refuses it and the workbench
    start-up (``OPSPILOT_HEALTH_PROFILE``) exits on it, so no registration
    can carry it. The second is refused by the store's subject check under
    the incident lock. Either way the incident stays ``open`` with no
    session and no audit row: checkout's readings cannot resolve a payment
    incident."""
    from opspilot.web.__main__ import _health_profile

    payload = json.loads((PROFILE_DIRECTORY / "otel-demo-checkout.json").read_text())
    payload["subject"] = {"service": "payment", "kubernetes_namespace": "payments-prod"}
    mismatched = tmp_path / "payment.json"
    mismatched.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HealthProfileError) as refused:
        load_health_profile(mismatched)
    assert refused.value.code == "PROFILE_INVALID"
    assert "SCOPE_SELECTOR_MISMATCH signals/0/query" in refused.value.detail
    monkeypatch.setenv("OPSPILOT_HEALTH_PROFILE", str(mismatched))
    with pytest.raises(SystemExit, match="SCOPE_SELECTOR_MISMATCH"):
        _health_profile()

    incident, _, target_id, uid = _incident(owner)
    payment = {**identity_of(uid, workload="payment"), "namespace": "payments-prod"}
    with pytest.raises(PersistenceError, match="HEALTH_PROFILE_TARGET_MISMATCH"):
        _register(controller, incident, expected=0, identity=payment)
    row = _incident_row(owner, incident)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 0)
    assert _sessions(owner, incident) == [] and _audit(owner, incident) == []
    assert _registered(owner, target_id)["workload"] is None
    # No stored profile carries the edited subject (the shipped one from the
    # other tests of this module may be there).
    with owner.transaction(snapshot=True) as conn:
        stored = conn.execute(
            "SELECT content FROM opspilot_health_profiles WHERE content LIKE %s",
            ("%payments-prod%",),
        ).fetchall()
    assert stored == []
