"""The F6 projection over real committed rows on PostgreSQL (issue #140).

The Observer loop samples the step 4 suite's scripted Prometheus under the
``opspilot_observer`` login; ``ObservationStore.incident_records`` reads the
committed incident, sessions, samples, readings, endings and control audit
in one snapshot; ``table_privileges`` measures what the Observer login can
actually do; ``recovery_outcome`` projects all of it without recomputing a
verdict of its own.
"""

# ruff: noqa: F811 - the step 4 suite's fixtures are imported by name and
# then named again as test parameters, which is how pytest binds them

import os
import time
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot.acceptance import IncidentScenario, RecoveryRecords, recovery_outcome
from opspilot.acceptance_recovery import OBSERVER_GRANTS
from opspilot.domain.intake import Target
from opspilot.observation import ObservationStore
from opspilot.observer.health_profile import SessionParameters, canonical_content
from opspilot.persistence import DurableStore, PoolConfig
from tests.integration.test_m1_02_observer_postgres import (  # noqa: F401 - fixtures bound by name
    SHIPPED,
    Telemetry,
    _backdate_authorization,
    _due_now,
    _incident,
    _lifecycle,
    _only,
    controller,
    loop,
    observer,
    observer_dsn,
    owner,
    scratch_dsn,
    telemetry,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _scenario(incident) -> IncidentScenario:
    return IncidentScenario(
        scenario_id="F6:1:recovered",
        feature_id="F6",
        acceptance_step="1",
        kind="recovered",
        subject_id=str(incident),
    )


def _authorize(
    controller: ObservationStore,
    incident: object,
    target: Target,
    *,
    sustained: int = 1,
    max_samples: int = 40,
) -> object:
    """A session whose parameters are its frozen profile's (the replay
    checks the row against the profile): the shipped checkout profile with
    the session values this test needs, under its own revision."""
    profile = SHIPPED.model_copy(
        update={
            "session": SessionParameters(
                deadline_seconds=3600,
                max_samples=max_samples,
                sample_interval_seconds=60,
                sustained_window_seconds=sustained,
            )
        }
    )
    return controller.authorize_session(
        incident,  # type: ignore[arg-type]
        revision=target.revision,
        actor="tester",
        deadline_at=controller.current_time() + timedelta(seconds=3600),
        max_samples=max_samples,
        sample_interval_seconds=60,
        sustained_window_seconds=sustained,
        health_profile_revision=profile.revision,
        health_profile=canonical_content(profile),
        first_sample_due_at=controller.current_time(),
    )


def _outcome(controller, observer, incident):
    records = controller.incident_records(incident)
    return recovery_outcome(
        _scenario(incident),
        RecoveryRecords(
            incident=records["incident"],
            sessions=tuple(records["sessions"]),
            controls=tuple(records["controls"]),
            grants=observer.table_privileges(),
        ),
    )


def test_the_observer_login_privileges_are_measured_not_assumed(
    observer: ObservationStore, controller: ObservationStore
) -> None:
    grants = observer.table_privileges()
    tables = grants["tables"]
    # column-exact, as migration 0003 grants them
    for name, expected in OBSERVER_GRANTS.items():
        measured = tables[f"public.{name}"]
        assert set(measured) == set(expected), name
        for kind, grant in expected.items():
            if grant == "*":
                assert measured[kind] == "*", (name, kind)
            else:
                assert set(measured[kind]) == set(grant), (name, kind)
    assert tables["public.opspilot_runs"] == {}
    assert tables["public.opspilot_controls"] == {}
    assert grants["sequences"] == {}
    assert "CREATE" not in grants["schemas"].get("public", ())
    assert grants["functions"] == ()
    assert grants["product_schema"] == "public"
    # the owner connection holds everything: the projection would say so
    assert (
        controller.table_privileges()["tables"]["public.opspilot_runs"]["DELETE"] == "*"
    )


def test_a_confirmed_recovery_projects_the_committed_rows(
    loop: tuple,
    owner: DurableStore,
    controller: ObservationStore,
    observer: ObservationStore,
) -> None:
    observer_loop, state = loop
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    first = _only(observer_loop.poll_once(), session)
    assert first.accepted and first.confirms_health and first.transition is None
    # #157: one healthy sample has zero span, so collect another after >=1 s.
    time.sleep(1.05)
    _due_now(owner, session)
    receipt = _only(observer_loop.poll_once(), session)
    assert receipt.transition == "recovery_confirmed"
    requests = len(state.requests)

    outcome = _outcome(controller, observer, incident)

    assert len(state.requests) == requests, "the projection queries nothing"
    assert outcome.subject_id == str(incident)
    assert outcome.incident_lifecycle == "resolved"
    assert outcome.recovery_confirmed and outcome.recovery_verdict == "healthy"
    assert outcome.latest_sample_verdict == "healthy"
    # #157: duration comes from window ends, never the look-back length.
    assert outcome.healthy_window_seconds >= 1
    elapsed = (
        outcome.recovery_samples[-1].window_end - outcome.recovery_samples[0].window_end
    ).total_seconds()
    assert outcome.healthy_window_seconds == int(elapsed)  # #157: whole seconds
    assert outcome.used_sample_count == 2  # #157
    assert outcome.observation_ended and outcome.human_interaction is None
    assert outcome.replay is not None and outcome.replay.consistent
    assert outcome.target == controller.session(session)["target"]
    assert outcome.target["resource_uid"] == target.resource_uid
    assert (
        outcome.recovery_profile_revision
        == controller.session(session)["health_profile_revision"]
    )
    assert len(outcome.recovery_samples) == 2  # #157
    sample = outcome.recovery_samples[-1]
    assert set(sample.signals) == {s.name for s in SHIPPED.signals}
    assert all(
        sig.evidence_id == f"{sample.sample_id}:{name}"
        for name, sig in sample.signals.items()
    )
    assert all(
        sig.status == "ok" and sig.body_sha256 for sig in sample.signals.values()
    )
    # three instant queries per signal, every one actually sent
    assert outcome.actions.count("read_only_query") == 6 * len(
        SHIPPED.signals
    )  # #157: two samples
    assert outcome.actions.count("persist_observation") == 2  # #157
    assert outcome.actions[-1] == "advance_incident_lifecycle"
    # no human control row on this incident: the authority exercised is the
    # Observer login's, measured on this connection (PUBLIC's default TEMP
    # on the scratch database is reported as it is, see development.md)
    assert set(outcome.permissions) - {"database_temp"} == {"read_only"}
    assert outcome.handling_audit == ()
    (projected,) = outcome.observation_sessions
    assert projected["state"] == "completed" and projected["authorized"] is False


def test_a_handoff_after_budget_exhaustion_is_projected(
    loop: tuple,
    owner: DurableStore,
    controller: ObservationStore,
    observer: ObservationStore,
) -> None:
    observer_loop, state = loop
    state.values["request_rate_per_second"] = 0.0
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1, max_samples=2)
    _backdate_authorization(owner, session)
    _only(observer_loop.poll_once(), session)
    _due_now(owner, session)
    receipt = _only(observer_loop.poll_once(), session)
    assert receipt.transition == "observation_ended_unconfirmed"
    assert _lifecycle(owner, incident) == "open"

    outcome = _outcome(controller, observer, incident)

    assert outcome.incident_lifecycle == "open"
    assert not outcome.recovery_confirmed and outcome.recovery_verdict == "unknown"
    assert outcome.latest_sample_verdict == "no_data"
    assert outcome.used_sample_count == 2
    assert outcome.observation_ended
    assert outcome.observation_ended_reason == "max_samples_exhausted"
    assert outcome.human_interaction == "handoff"
    assert "MAX_SAMPLES_EXHAUSTED" in outcome.handoff_reasons
    assert "INSUFFICIENT_TRAFFIC" in outcome.recovery_reasons
    assert outcome.actions[-1] == "human_handoff"
    assert len(outcome.recovery_samples) == 2
    assert {job["sequence"] for job in outcome.sample_jobs} == {1, 2}


def test_a_registered_remediation_is_human_control_in_the_audit(
    owner: DurableStore, controller: ObservationStore, observer: ObservationStore
) -> None:
    incident, _, target = _incident(owner)
    generation = controller.register_remediation(
        incident,
        expected_generation=0,
        actor="operator",
        revision="checkout:v2",
        deadline_at=controller.current_time()
        + timedelta(seconds=SHIPPED.session.deadline_seconds),
        max_samples=SHIPPED.session.max_samples,
        sample_interval_seconds=SHIPPED.session.sample_interval_seconds,
        sustained_window_seconds=SHIPPED.session.sustained_window_seconds,
        health_profile_revision=SHIPPED.revision,
        health_profile=canonical_content(SHIPPED),
        session_id=uuid4(),
        identity={
            "integration_id": "prom-lab",
            "cluster_uid": "kind-lab",
            "namespace": "otel-demo",
            "resource_uid": target.resource_uid,
            "workload": "checkout",
        },
    )
    assert generation == 1

    outcome = _outcome(controller, observer, incident)

    assert outcome.incident_lifecycle == "observing_recovery"
    assert set(outcome.permissions) - {"database_temp"} == {
        "read_only",
        "human_control",
    }
    assert outcome.actions[:2] == ("record_handling", "advance_incident_lifecycle")
    assert outcome.handling_audit[0]["action"] == "register_remediation"
    assert outcome.handling_audit[0]["resulting_generation"] == 1
    assert outcome.observation_authorization["authorized"] is True
    assert outcome.observation_authorization["subject_control_generation"] == 1
    assert outcome.target["revision"] == "checkout:v2"
    assert outcome.recovery_handled_at is not None
    assert not outcome.observation_ended and outcome.recovery_verdict == "unknown"


def test_a_wider_login_is_reported_as_wider(
    scratch_dsn: str, owner: DurableStore, controller: ObservationStore
) -> None:
    """A login that can also write investigation rows is not read-only
    plus own records; the projection says which table."""
    login = f"opspilot_wide_login_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
        conn.execute(
            sql.SQL("GRANT UPDATE ON opspilot_runs TO {}").format(sql.Identifier(login))
        )
        conn.execute(
            sql.SQL("GRANT UPDATE (mode) ON opspilot_incidents TO {}").format(
                sql.Identifier(login)
            )
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lab_notes (id int PRIMARY KEY, body text)"
        )
        conn.execute(
            sql.SQL("GRANT SELECT, UPDATE ON lab_notes TO {}").format(
                sql.Identifier(login)
            )
        )
        # an updatable view over a table the login cannot write directly
        conn.execute(
            "CREATE OR REPLACE VIEW lab_notes_view AS SELECT id, body FROM lab_notes"
        )
        conn.execute(
            sql.SQL("GRANT SELECT, UPDATE ON lab_notes_view TO {}").format(
                sql.Identifier(login)
            )
        )
        # a same-named table in another schema is not the product's
        conn.execute("CREATE SCHEMA IF NOT EXISTS lab")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lab.opspilot_observation_signal_readings (id int PRIMARY KEY)"
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA lab TO {}").format(sql.Identifier(login))
        )
        conn.execute(
            sql.SQL(
                "GRANT SELECT, INSERT ON lab.opspilot_observation_signal_readings TO {}"
            ).format(sql.Identifier(login))
        )
    wide = ObservationStore(
        make_conninfo(scratch_dsn, user=login), pool=PoolConfig(1, 2, 5.0)
    )
    try:
        incident, _, target = _incident(owner)
        _authorize(controller, incident, target, sustained=1)
        outcome = _outcome(controller, wide, incident)
        assert {
            "read_only",
            "investigation_write:opspilot_runs",
            "record_rewrite:opspilot_incidents(mode)",
            "foreign_write:public.lab_notes",
            "foreign_write:public.lab_notes_view",
            "foreign_write:lab.opspilot_observation_signal_readings",
        } <= set(outcome.permissions)
        assert "record_rewrite:opspilot_incidents(lifecycle)" not in outcome.permissions
    finally:
        wide.close()
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL(
                    "REVOKE ALL ON opspilot_runs, opspilot_incidents, lab_notes, lab_notes_view, lab.opspilot_observation_signal_readings FROM {}"
                ).format(sql.Identifier(login))
            )
            conn.execute("DROP VIEW IF EXISTS lab_notes_view")
            conn.execute("DROP TABLE IF EXISTS lab_notes")
            conn.execute("DROP SCHEMA IF EXISTS lab CASCADE")
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))
