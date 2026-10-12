"""Independent #184 step 2 acceptance checks.

The tests use the public Workbench/HTTP/projection seams from step 1.  SQL is
read-only here except for deterministic fixture state (for example, making a
published incident's observer result terminal); no product call order is
asserted.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

from opspilot.acceptance import (
    IncidentScenario,
    RecoveryRecords,
    postmortem_outcome,
    postmortem_records,
    recovery_outcome,
)
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.jobs import GenerationStore
from opspilot.observation import ObservationStore
from opspilot.observer import canonical_content
from opspilot.persistence import DurableStore
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    PostmortemReview,
    Workbench,
    create_app,
    hash_password,
)
from opspilot.web import __main__ as web_main
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import (
    T0,
    CountingModel,
    Harness,
    output,
    seed_upstream,
)
from tests.integration.test_self_heal_authorization_postgres import (
    POOL,
    PROFILE,
    WORKLOAD,
    _controls,
    _end_unconfirmed_session,
    _incident,
    _incident_row,
    _now,
    _pass_for,
    _published_candidate,
    _sessions,
)
from tests.m1_web_support import ORIGIN, UI_PASSWORD, UI_USER, basic, call
from tests.target_support import IDENTITY

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)


def _scenario(incident) -> IncidentScenario:
    return IncidentScenario(
        scenario_id="F6:step2:automatic-recovery",
        feature_id="F6",
        acceptance_step="2",
        kind="recovered",
        subject_id=str(incident),
    )


@pytest.fixture
def observer():
    store = ObservationStore(DSN, pool=POOL)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture(scope="module")
def owner():
    store = DurableStore(DSN, pool=POOL)
    store.install()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture(scope="module")
def f13_harness(owner):
    knowledge = KnowledgeStore(DSN)
    jobs = GenerationStore(DSN)
    events = DurableEventLog(owner)
    evidence = DurableEvidenceStore(owner)
    ledger = DurableWebLedger(owner)
    knowledge.install()
    jobs.install()
    events.install()
    evidence.install()
    ledger.install()
    bench = Workbench(
        incidents=DurableIncidentStore(owner),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    auth = Authenticator(
        AuthConfig(
            ui_users={UI_USER: hash_password(UI_PASSWORD)},
            event_tokens={},
            auth_revision="f13-step2",
            allowed_origins=frozenset({ORIGIN}),
        )
    )
    app = create_app(
        bench,
        auth,
        DurableClock(owner),
        review=PostmortemReview(knowledge=knowledge, events=events),
    )
    yield Harness(app, knowledge, jobs, owner, DSN, events)
    jobs.close()
    knowledge.close()


def _seed_f13_source(dsn: str, *, source: str) -> object:
    incident = seed_upstream(dsn, ended_at=T0)
    with psycopg.connect(dsn) as conn:
        session = conn.execute(
            "SELECT session_id FROM opspilot_observation_sessions WHERE incident_id=%s",
            (incident,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE opspilot_observation_sessions SET authorization_source=%s, authorized_by=%s, target=jsonb_set(target,'{revision}','null'::jsonb) WHERE session_id=%s",
            (source, "system" if source == "automatic" else "operator", session),
        )
        conn.execute(
            "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(gen_random_uuid(),%s,%s,0,0,%s,%s)",
            (
                incident,
                "auto_authorize_observation"
                if source == "automatic"
                else "register_remediation",
                "system" if source == "automatic" else "operator",
                json.dumps({"authorization_source": source}),
            ),
        )
    return incident


def _automatic_records(workbench, observer, incident):
    controller = ObservationStore(DSN, pool=POOL)
    try:
        rows = controller.incident_records(incident)
        return recovery_outcome(
            _scenario(incident),
            RecoveryRecords(
                incident=rows["incident"],
                sessions=tuple(rows["sessions"]),
                controls=tuple(rows["controls"]),
                grants=observer.table_privileges(),
            ),
        )
    finally:
        controller.close()


def test_automatic_projection_has_neutral_source_and_no_human_capability(
    owner, observer
) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    (authorized,) = _pass_for(workbench, incident)
    assert authorized.state == "authorized"

    sessions = _sessions(owner, incident)
    assert len(sessions) == 1
    assert sessions[0]["authorization_source"] == "automatic"
    assert sessions[0]["authorized_by"] == "system"
    assert sessions[0]["target"]["revision"] is None

    projected = _automatic_records(workbench, observer, incident)
    assert projected.authorization_sources == ("automatic",)
    assert projected.authorization_source == "automatic"
    assert projected.observation_authorization["authorization_source"] == "automatic"
    assert projected.observation_authorization["target"]["revision"] is None
    assert projected.system_authorizations[0]["action"] == "auto_authorize_observation"
    assert "human_control" not in projected.permissions
    assert projected.human_interaction is None
    assert all(
        "auto_authorize_observation" not in action for action in projected.actions
    )


def test_automatic_control_is_system_audit_without_generation_advance(owner) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    before = _incident_row(owner, incident)
    _pass_for(workbench, incident)
    after = _incident_row(owner, incident)
    assert after["control_generation"] == before["control_generation"]
    assert after["observation_generation"] == before["observation_generation"] + 1
    controls = [row for row in _controls(owner, incident) if row["actor"] == "system"]
    assert len(controls) == 1
    assert controls[0]["action"] == "auto_authorize_observation"
    assert controls[0]["expected_generation"] == controls[0]["resulting_generation"]


def test_recovery_projection_keeps_automatic_session_and_f6_existing_shape(
    owner, observer
) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    _pass_for(workbench, incident)
    result = _automatic_records(workbench, observer, incident)
    assert result.subject_id == str(incident)
    assert result.authorization_sources == ("automatic",)
    assert result.authorization_source == "automatic"
    assert result.recovery_profile_revision == PROFILE.revision
    assert result.recovery_confirmed is False  # observing is not recovery confirmed
    assert result.actions == tuple(result.actions)  # stable public sequence


def test_workbench_snapshot_exposes_source_and_unknown_revision_without_internal_names(
    owner,
) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    _pass_for(workbench, incident)
    snapshot = workbench.snapshot(incident)
    text = repr(snapshot).lower()
    assert "self_heal" not in text
    assert "self_healing" not in text
    recovery = snapshot["recovery"]
    sessions = snapshot["observation_sessions"]
    assert recovery[0]["authorization_source"] == "automatic"
    assert recovery[0]["recovery_status"] in {"authorized", "observing", "resolved"}
    assert "recovery_statement" in recovery[0]
    assert sessions[0]["authorization_source"] == "automatic"


def test_no_health_profile_has_no_automatic_projection(owner) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    workbench.health_profile = None
    assert _pass_for(workbench, incident)[0].state == "skipped"
    assert _sessions(owner, incident) == []
    assert not [row for row in _controls(owner, incident) if row["actor"] == "system"]


def test_human_registration_remains_post_remediation(owner) -> None:
    # Existing step-1 contract is retained: this is the comparison point for
    # all source-aware F6/F13 projections.
    incident, _run, target_id, resource_uid = _incident(owner)
    controller = ObservationStore(DSN, pool=POOL)
    try:
        controller.register_remediation(
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
            identity={**IDENTITY, "resource_uid": resource_uid, "workload": WORKLOAD},
        )
    finally:
        controller.close()
    assert _sessions(owner, incident)[0]["authorization_source"] == "post_remediation"


def test_mixed_automatic_then_human_history_pairs_f6_by_session_source(
    owner, observer
) -> None:
    workbench, incident, _run, _target_id, resource_uid = _published_candidate(owner)
    _pass_for(workbench, incident)
    controller = ObservationStore(DSN, pool=POOL)
    try:
        generation = _incident_row(owner, incident)["control_generation"]
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
    assert [row["authorization_source"] for row in sessions] == [
        "automatic",
        "post_remediation",
    ]
    assert sessions[0]["state"] == "revoked"
    _end_unconfirmed_session(owner, session_id=sessions[1]["session_id"])
    result = _automatic_records(workbench, observer, incident)
    assert result.actions.count("record_handling") == 1
    assert "human_takeover" not in result.actions
    assert result.human_interaction == "handoff"


@pytest.mark.parametrize(
    ("source", "actor"),
    [("automatic", "system"), ("post_remediation", "operator")],
)
def test_f13_recovery_input_classifies_system_vs_human_authorization(
    f13_harness, source, actor
) -> None:
    incident = _seed_f13_source(DSN, source=source)
    model = CountingModel(output(f"ev-{incident}"))
    view = f13_harness.generate(incident, model)
    assert view["status"] == "under_review"
    records = postmortem_records(f13_harness.knowledge, incident)
    result = postmortem_outcome(
        IncidentScenario(
            scenario_id="F13:step2",
            feature_id="F13",
            acceptance_step="2",
            kind="incident",
            subject_id=str(incident),
        ),
        records,
    )
    version = result.versions[-1]
    actions = version.sections["human_actions"]
    recovery = version.sections["recovery"]
    if source == "automatic":
        assert all(
            item.get("action") != "auto_authorize_observation" for item in actions
        )
    else:
        assert any(item.get("action") == "register_remediation" for item in actions)
    assert recovery["authorization_source"] == source
    assert recovery["authorized_by"] == actor


def test_default_loop_interval_is_thirty_and_zero_still_disables(monkeypatch) -> None:
    monkeypatch.delenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", raising=False)
    assert web_main._self_heal_interval() == 30.0
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", "0")
    assert web_main._self_heal_interval() == 0.0
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", "7.5")
    assert web_main._self_heal_interval() == 7.5
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", "bad")
    with pytest.raises(SystemExit):
        web_main._self_heal_interval()


def test_incident_page_uses_recovered_no_remediation_recorded_and_neutral_source(owner):
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    _pass_for(workbench, incident)
    events = DurableEventLog(owner)
    evidence = DurableEvidenceStore(owner)
    ledger = DurableWebLedger(owner)
    events.install()
    evidence.install()
    ledger.install()
    app = create_app(
        Workbench(
            incidents=DurableIncidentStore(owner),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions={"state": "v1"},
            run_seconds=600,
        ),
        Authenticator(
            AuthConfig(
                ui_users={UI_USER: hash_password(UI_PASSWORD)},
                event_tokens={},
                auth_revision="step2",
                allowed_origins=frozenset({ORIGIN}),
            )
        ),
        DurableClock(owner),
    )
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert "self_heal" not in page.text.lower()
    assert "self_healing" not in page.text.lower()
    # The automatic session is observing, so the page must not claim recovery
    # before an independent healthy observation has ended.
    assert "recovered, no remediation recorded" not in page.text.lower()


def test_c1_existing_published_incident_is_scanned_after_loop_enable(owner) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    assert _sessions(owner, incident) == []
    assert _pass_for(workbench, incident)[0].state == "authorized"


def test_duplicate_scan_does_not_duplicate_session_or_system_audit(owner) -> None:
    workbench, incident, _run, _target_id, _resource_uid = _published_candidate(owner)
    assert len(_pass_for(workbench, incident)) == 1
    assert _pass_for(workbench, incident) == []
    assert len(_sessions(owner, incident)) == 1
    assert (
        len([row for row in _controls(owner, incident) if row["actor"] == "system"])
        == 1
    )
