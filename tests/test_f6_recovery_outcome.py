"""The product's F6 projection: committed recovery records -> RecoveryOutcome
(issue #140; AGENTS.md "验收入口是外部 IncidentScenario -> IncidentOutcome").

No PostgreSQL: the records are the shapes ``ObservationStore.incident_records``
returns, built from genuine Observer samples by ``tests.m1_02_replay_support``.
Every field comes from a committed row or from the replay of those rows
(``opspilot.observer.replay``); nothing here recomputes a verdict of its own,
and permissions are derived from the grants the caller measured.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from opspilot.acceptance import (
    IncidentScenario,
    RecoveryOutcome,
    RecoveryRecords,
    permissions_from_grants,
    recovery_outcome,
)
from opspilot.acceptance_recovery import OBSERVER_GRANTS
from tests.m1_02_replay_support import (
    NOW,
    PROFILE,
    healthy_history,
    stored_history,
    take,
)


def _scenario(history) -> IncidentScenario:
    """The scenario names the incident the records were read for."""
    return IncidentScenario(
        scenario_id="F6:1:recovered",
        feature_id="F6",
        acceptance_step="1",
        kind="recovered",
        subject_id=str(history["session"]["incident_id"]),
    )


#: What ``ObservationStore.table_privileges`` reports for the migration-0003
#: Observer role on a database whose PUBLIC defaults were revoked.
def _tables(**overrides):
    tables = {
        f"public.{name}": dict(grants) for name, grants in OBSERVER_GRANTS.items()
    }
    for name in (
        "opspilot_runs",
        "opspilot_steps",
        "opspilot_controls",
        "opspilot_evidence",
    ):
        tables[f"public.{name}"] = {}
    tables.update(overrides)
    return tables


def _grants(**overrides):
    grants = {
        "tables": _tables(),
        "sequences": {},
        "schemas": {"public": ("USAGE",)},
        "database": (),
        "functions": (),
        "product_schema": "public",
    }
    grants.update(overrides)
    return grants


OBSERVER_MEASURED = _grants()


def _control(generation: int = 1, incident_id=None) -> dict:
    return {
        "incident_id": incident_id,
        "action": "register_remediation",
        "expected_generation": generation - 1,
        "resulting_generation": generation,
        "actor": "operator",
        "payload": {"revision": "svc:v2"},
        "created_at": NOW - timedelta(seconds=600),
    }


def _rehome(history, incident_id) -> None:
    """File a whole session history (its endings too) under ``incident_id``."""
    history["session"]["incident_id"] = incident_id
    for ending in history.get("endings") or ():
        ending["incident_id"] = incident_id


def _records(history, *, controls=None, grants=OBSERVER_MEASURED) -> RecoveryRecords:
    return RecoveryRecords(
        incident={
            "incident_id": history["session"]["incident_id"],
            "lifecycle": history["incident_lifecycle"],
            "mode": "automatic",
            "control_generation": history["session"]["subject_control_generation"],
        },
        sessions=(history,),
        controls=tuple(controls or ()),
        grants=grants,
    )


def test_a_confirmed_recovery_is_projected_from_the_committed_rows():
    history = healthy_history()
    outcome = recovery_outcome(
        _scenario(history),
        _records(history, controls=[_control(1, history["session"]["incident_id"])]),
    )
    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.scenario_id == "F6:1:recovered"
    assert outcome.subject_id == str(history["session"]["incident_id"])
    assert outcome.incident_lifecycle == "resolved"
    assert outcome.recovery_confirmed is True
    assert outcome.recovery_verdict == "healthy"
    assert outcome.latest_sample_verdict == "healthy"
    assert outcome.healthy_window_seconds == 600
    assert (
        outcome.used_sample_count == 11
    )  # #157: 600 s requires eleven healthy samples
    assert outcome.observation_ended is True
    assert outcome.observation_ended_reason == "recovery_confirmed"
    assert outcome.human_interaction is None
    assert outcome.handoff_reasons == ()
    assert outcome.recovery_handled_at == history["session"]["authorized_at"]
    assert outcome.recovery_profile_revision == PROFILE.revision
    assert outcome.recovery_profile_content == history["health_profile"]["content"]
    assert outcome.replay is not None and outcome.replay.consistent
    assert outcome.model_requests == ()
    # the target is the session's immutable binding, never telemetry content
    assert outcome.target == history["session"]["target"]
    assert (
        len(outcome.recovery_samples) == 11
    )  # #157: 600 s requires eleven healthy samples
    sample = outcome.recovery_samples[-1]
    assert sample.subject_id == outcome.subject_id
    assert (
        sample.sequence == 11 and sample.disposition == "adopted"
    )  # #157: ten intervals
    assert sample.outcome == "healthy" and sample.confirms_health is True
    assert sample.target == history["session"]["target"]
    assert sample.health_profile_revision == PROFILE.revision
    assert sample.transition == "recovery_confirmed"
    errors = sample.signals["errors"]
    assert errors.evidence_id == f"{sample.sample_id}:errors"
    assert errors.value == 0.0 and errors.status == "ok" and errors.sample_count == 5
    assert errors.source == "prometheus"
    assert (
        errors.query
        == "e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}"
    )
    assert errors.window_end == sample.window_end
    assert len(errors.raw_sha256) == 64 and len(errors.body_sha256) == 64
    # the instants are the bundle's own, not the window end dressed up
    assert errors.evaluated_at == sample.window_end
    assert errors.sample_time == sample.window_end + timedelta(seconds=1)
    # the newest raw sample behind the signal, from the freshness answer
    assert errors.observed_at == sample.window_end - timedelta(seconds=30)
    # actions are the audit of what the product did, in record order
    assert outcome.actions[:2] == ("record_handling", "advance_incident_lifecycle")
    assert (
        outcome.actions.count("persist_observation") == 11
    )  # #157: 600 s requires eleven healthy samples
    # three instant queries per reading, each actually sent
    assert (
        outcome.actions.count("read_only_query") == 66
    )  # #157: six queries per sample
    assert outcome.actions[-1] == "advance_incident_lifecycle"
    assert "human_handoff" not in outcome.actions
    assert outcome.permissions == ("read_only", "human_control")
    # the session contract projection and the job set
    (session,) = outcome.observation_sessions
    assert session["state"] == "completed" and session["authorized"] is False
    assert session["health_profile_revision"] == PROFILE.revision
    assert set(session) == {
        "session_id",
        "purpose",
        "subject",
        "target",
        "subject_control_generation",
        "observation_generation",
        "state",
        "authorized",
        "health_profile_revision",
        "adopted_sequence",
        "adopted_window_end",
        "active_sample_job_id",
    }
    assert len(outcome.sample_jobs) == 11  # #157: 600 s requires eleven healthy samples
    assert outcome.observation_authorization["session_id"] == session["session_id"]
    assert outcome.handling_audit[0]["action"] == "register_remediation"


def test_a_tampered_record_yields_unknown_with_the_integrity_reason():
    history = healthy_history()
    for row in history["samples"]:
        row["outcome"] = "degraded"
    outcome = recovery_outcome(_scenario(history), _records(history))
    assert outcome.recovery_verdict == "unknown"
    assert outcome.recovery_confirmed is False
    assert "STORED_OBSERVATION_INTEGRITY_MISMATCH" in outcome.recovery_reasons
    # the committed row still says resolved; the replay cannot vouch for it,
    # so the lifecycle the outcome stands behind is not "resolved"
    assert outcome.recorded_lifecycle == "resolved"
    assert outcome.incident_lifecycle == "unverified"
    assert outcome.replay is not None and not outcome.replay.consistent


@pytest.mark.parametrize(
    "kind,verdict,reasons",
    [
        ("degraded", "degraded", {"CONTINUED_DEGRADATION", "DEGRADED_SIGNAL:errors"}),
        ("no-traffic", "unknown", {"INSUFFICIENT_TRAFFIC", "OBSERVATION_UNCONFIRMED"}),
    ],
)
def test_an_unconfirmed_session_hands_off_with_reasons_from_the_replay(
    kind, verdict, reasons
):
    from tests.m1_02_replay_support import profile_with

    session_id = uuid4()
    short = profile_with(max_samples=3, sustained_window_seconds=60)
    taken = [
        take(
            kind,
            sequence=i + 1,
            window_end=NOW + timedelta(seconds=60 * i),
            session_id=session_id,
            profile=short,
        )
        for i in range(3)
    ]
    history = stored_history(
        taken,
        profile=short,
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
        max_samples=3,
        sustained_window_seconds=60,
    )
    outcome = recovery_outcome(_scenario(history), _records(history))
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_confirmed is False
    assert outcome.recovery_verdict == verdict
    assert outcome.latest_sample_verdict == (
        "degraded" if kind == "degraded" else "no_data"
    )
    assert outcome.healthy_window_seconds == 0
    assert outcome.used_sample_count == 3
    assert outcome.observation_ended is True
    assert outcome.observation_ended_reason == "max_samples_exhausted"
    assert outcome.human_interaction == "handoff"
    assert "MAX_SAMPLES_EXHAUSTED" in outcome.handoff_reasons
    assert reasons <= set(outcome.recovery_reasons)
    assert set(outcome.handoff_reasons) >= reasons
    # copied from the replay, not assembled here
    assert outcome.recovery_reasons == outcome.replay.recovery_reasons
    assert outcome.handoff_reasons == outcome.replay.handoff_reasons
    assert outcome.actions[-1] == "human_handoff"
    assert "advance_incident_lifecycle" in outcome.actions
    assert outcome.permissions == ("read_only",)


def test_a_missing_required_signal_names_it():
    session_id = uuid4()
    taken = [
        take("healthy", sequence=1, window_end=NOW, session_id=session_id),
    ]
    # drop the errors signal's reading rows: the stored outcome stays what
    # the Observer wrote, the replay reports the missing basis
    history = stored_history(
        taken, session_id=session_id, authorized_at=NOW - timedelta(seconds=600)
    )
    sample = history["samples"][0]
    sample["readings"] = [r for r in sample["readings"] if r["signal_name"] != "errors"]
    sample["outcome"], sample["required_signals_present"] = "no_data", False
    sample["confirms_health"], sample["health_basis"] = False, "outcome_not_healthy"
    history["session"]["healthy_since"] = None
    outcome = recovery_outcome(_scenario(history), _records(history))
    assert outcome.recovery_verdict == "unknown"
    assert "MISSING_SIGNAL:errors" in outcome.recovery_reasons
    assert "REQUIRED_TELEMETRY_MISSING" in outcome.recovery_reasons


def test_a_session_still_observing_is_not_ended_and_not_a_handoff():
    history = healthy_history(count=3)
    outcome = recovery_outcome(_scenario(history), _records(history))
    assert outcome.incident_lifecycle == "observing_recovery"
    assert outcome.observation_ended is False
    assert outcome.observation_ended_reason is None
    assert outcome.recovery_confirmed is False and outcome.recovery_verdict == "unknown"
    assert outcome.latest_sample_verdict == "healthy"
    assert outcome.healthy_window_seconds == 120  # #157: two intervals
    assert outcome.human_interaction is None and outcome.handoff_reasons == ()


def test_no_session_means_no_observation():
    history = healthy_history(count=1)
    records = RecoveryRecords(
        incident={
            "incident_id": history["session"]["incident_id"],
            "lifecycle": "open",
            "mode": "automatic",
            "control_generation": 0,
        },
        sessions=(),
        controls=(),
        grants=OBSERVER_MEASURED,
    )
    outcome = recovery_outcome(_scenario(history), records)
    assert outcome.recovery_verdict == "unknown" and not outcome.recovery_confirmed
    assert outcome.recovery_samples == () and outcome.observation_sessions == ()
    assert outcome.observation_ended is False
    assert outcome.recovery_handled_at is None
    assert outcome.replay is None
    assert outcome.permissions == ("read_only",)
    assert outcome.actions == ()
    del history


def test_permissions_come_from_the_measured_grants_not_a_constant():
    assert permissions_from_grants(OBSERVER_MEASURED, human_control=False) == (
        "read_only",
    )
    assert permissions_from_grants(OBSERVER_MEASURED, human_control=True) == (
        "read_only",
        "human_control",
    )
    # a whole-table write on an investigation table
    wider = _grants(
        tables=_tables(**{"public.opspilot_runs": {"SELECT": "*", "UPDATE": "*"}})
    )
    assert permissions_from_grants(wider, human_control=False) == (
        "read_only",
        "investigation_write:opspilot_runs",
    )
    # one column more than the migration grants (codex review P2-1)
    columns = _grants(
        tables=_tables(
            **{
                "public.opspilot_incidents": {
                    **OBSERVER_GRANTS["opspilot_incidents"],
                    "UPDATE": ("lifecycle", "mode"),
                }
            }
        )
    )
    assert "record_rewrite:opspilot_incidents(mode)" in permissions_from_grants(
        columns, human_control=False
    )
    whole = _grants(
        tables=_tables(
            **{
                "public.opspilot_incidents": {
                    **OBSERVER_GRANTS["opspilot_incidents"],
                    "UPDATE": "*",
                }
            }
        )
    )
    assert "record_rewrite:opspilot_incidents" in permissions_from_grants(
        whole, human_control=False
    )
    deleting = _grants(
        tables=_tables(
            **{
                "public.opspilot_observation_samples": {
                    "SELECT": "*",
                    "INSERT": "*",
                    "DELETE": "*",
                }
            }
        )
    )
    assert "record_delete:opspilot_observation_samples" in permissions_from_grants(
        deleting, human_control=False
    )
    # a role that cannot even read the records it judges by is not read_only
    blind = _grants(
        tables=_tables(**{"public.opspilot_observation_samples": {"INSERT": "*"}})
    )
    assert "read_only" not in permissions_from_grants(blind, human_control=False)
    assert any(
        p.startswith("unreadable:opspilot_observation_samples(")
        for p in permissions_from_grants(blind, human_control=False)
    )
    with pytest.raises(ValueError, match="GRANTS_REQUIRED"):
        permissions_from_grants({}, human_control=False)


def test_capabilities_outside_the_product_tables_are_reported():
    """Codex review P2-2: a write on a foreign table, a sequence, schema
    CREATE, database CREATE/TEMP and an executable SECURITY DEFINER function
    are all capabilities a read-only observer must not have."""
    grants = _grants(
        tables=_tables(
            **{
                "lab.notes": {"SELECT": "*", "UPDATE": ("body",)},
                "public.scratch": {"DELETE": "*"},
            }
        ),
        sequences={"public.opspilot_seq": ("USAGE", "UPDATE")},
        schemas={"public": ("USAGE", "CREATE")},
        database=("CREATE", "TEMP"),
        functions=("public.rewrite_rows(uuid)",),
    )
    permissions = permissions_from_grants(grants, human_control=False)
    assert permissions[0] == "read_only"
    assert {
        "foreign_write:lab.notes(body)",
        "foreign_write:public.scratch",
        "sequence_write:public.opspilot_seq",
        "schema_create:public",
        "database_create",
        "database_temp",
        "security_definer_execute:public.rewrite_rows(uuid)",
    } <= set(permissions)


def test_the_expected_observer_grants_are_the_migrations():
    """``OBSERVER_GRANTS`` mirrors migration 0003 column for column."""
    import importlib.util
    import pathlib
    import re

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "opspilot/migrations/versions/0003_observation_store.py"
    )
    spec = importlib.util.spec_from_file_location("m0003", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert OBSERVER_GRANTS["opspilot_incidents"]["SELECT"] == tuple(
        module.OBSERVER_INCIDENT_COLUMNS
    )
    assert OBSERVER_GRANTS["opspilot_observation_sessions"]["UPDATE"] == tuple(
        module.OBSERVER_SESSION_COLUMNS
    )
    source = path.read_text()
    samples = re.search(r"GRANT INSERT \(([^)]*)\) ON \{samples\}", source).group(1)
    endings = re.search(r"GRANT INSERT \(([^)]*)\) ON \{endings\}", source).group(1)
    assert OBSERVER_GRANTS["opspilot_observation_samples"]["INSERT"] == tuple(
        samples.split(",")
    )
    assert OBSERVER_GRANTS["opspilot_observation_endings"]["INSERT"] == tuple(
        endings.split(",")
    )
    assert "GRANT UPDATE (lifecycle) ON opspilot_incidents" in source


def test_actions_count_only_requests_that_were_sent_and_moves_that_happened():
    """Codex review P2-3: an unsent query (lease budget spent) is not a
    read-only query; a registration on an incident already observing does
    not advance the lifecycle."""
    import json

    from tests.m1_02_replay_support import rehash

    history = healthy_history(count=1)
    reading = history["samples"][0]["readings"][0]
    bundle = json.loads(bytes(reading["raw"]))
    bundle["freshness"]["detail"] = "LEASE_BUDGET"
    history["samples"][0]["readings"][0] = rehash(
        reading, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )
    outcome = recovery_outcome(_scenario(history), _records(history))
    # 2 readings x 3 queries, one of them never sent
    assert outcome.actions.count("read_only_query") == 5
    # a second registration while the first session was revoked without a
    # lifecycle move: the incident stayed observing_recovery
    first = healthy_history(count=1)
    first["session"]["state"], first["session"]["ended_reason"] = (
        "revoked",
        "authority_revoked",
    )
    first["endings"] = [
        {
            "ending_id": uuid4(),
            "session_id": first["session"]["session_id"],
            "incident_id": first["session"]["incident_id"],
            "ended_reason": "authority_revoked",
            "transition": None,
            "sample_id": None,
            "lifecycle_before": "observing_recovery",
            "lifecycle_after": "observing_recovery",
            "recorded_at": NOW,
        }
    ]
    second = healthy_history(count=1)
    _rehome(second, first["session"]["incident_id"])
    records = RecoveryRecords(
        incident={
            "incident_id": first["session"]["incident_id"],
            "lifecycle": "observing_recovery",
            "mode": "automatic",
            "control_generation": 2,
        },
        sessions=(first, second),
        controls=(
            _control(1, first["session"]["incident_id"]),
            _control(2, first["session"]["incident_id"]),
        ),
        grants=OBSERVER_MEASURED,
    )
    outcome = recovery_outcome(_scenario(first), records)
    assert outcome.actions[:3] == (
        "record_handling",
        "advance_incident_lifecycle",
        "record_handling",
    )
    assert outcome.actions[3] != "advance_incident_lifecycle"
    # without an ending record the move is unknown and not claimed
    first["endings"] = []
    outcome = recovery_outcome(_scenario(first), records)
    assert outcome.actions[:3] == (
        "record_handling",
        "advance_incident_lifecycle",
        "record_handling",
    )
    assert outcome.actions[3] != "advance_incident_lifecycle"


# --- final round on PR #143 (recheck P2 + bot threads)


def test_records_of_another_incident_are_refused_not_relabelled():
    history = healthy_history()
    other = IncidentScenario(
        scenario_id="F6:1:recovered",
        feature_id="F6",
        acceptance_step="1",
        kind="recovered",
        subject_id=str(uuid4()),
    )
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        recovery_outcome(other, _records(history))
    stray = healthy_history()
    stray["session"]["incident_id"] = uuid4()
    records = _records(history)
    records = RecoveryRecords(
        incident=records.incident,
        sessions=(history, stray),
        controls=(),
        grants=OBSERVER_MEASURED,
    )
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        recovery_outcome(_scenario(history), records)


def test_a_same_named_relation_outside_the_product_schema_is_foreign():
    """Recheck P2-1: product identity is schema-bound."""
    grants = _grants(
        tables=_tables(
            **{
                "lab.opspilot_observation_signal_readings": {
                    "SELECT": "*",
                    "INSERT": "*",
                },
                "lab.opspilot_incidents": {"SELECT": "*", "UPDATE": ("lifecycle",)},
            }
        )
    )
    permissions = permissions_from_grants(grants, human_control=False)
    assert "foreign_write:lab.opspilot_observation_signal_readings" in permissions
    assert "foreign_write:lab.opspilot_incidents(lifecycle)" in permissions
    # without a product schema nothing can be classified
    with pytest.raises(ValueError, match="GRANTS_REQUIRED"):
        permissions_from_grants(
            {**OBSERVER_MEASURED, "product_schema": None}, human_control=False
        )


def test_a_writable_view_is_a_write_capability():
    """Recheck P2-2: views, materialized views and foreign tables are
    measured like tables; a write through one is reported."""
    grants = _grants(
        tables=_tables(**{"public.readings_view": {"SELECT": "*", "UPDATE": "*"}})
    )
    assert "foreign_write:public.readings_view" in permissions_from_grants(
        grants, human_control=False
    )


def test_read_only_needs_every_column_the_replay_reads():
    """Bot thread: a partial SELECT on a record table is not read access to
    the records."""
    grants = _grants(
        tables=_tables(
            **{
                "public.opspilot_observation_samples": {
                    "SELECT": ("sample_id", "session_id", "sequence"),
                    "INSERT": OBSERVER_GRANTS["opspilot_observation_samples"]["INSERT"],
                }
            }
        )
    )
    permissions = permissions_from_grants(grants, human_control=False)
    assert "read_only" not in permissions
    unreadable = next(p for p in permissions if p.startswith("unreadable:"))
    assert unreadable.startswith("unreadable:opspilot_observation_samples(")
    assert "outcome" in unreadable and "sample_id" not in unreadable


def test_actions_are_merged_in_event_time():
    """Bot thread: a registration recorded after the first session's events
    appears after them, not in a control-rows-first block."""
    first = healthy_history(count=1)
    first["session"]["state"], first["session"]["ended_reason"] = (
        "revoked",
        "authority_revoked",
    )
    first["endings"] = [
        {
            "ending_id": uuid4(),
            "session_id": first["session"]["session_id"],
            "incident_id": first["session"]["incident_id"],
            "ended_reason": "authority_revoked",
            "transition": None,
            "sample_id": None,
            "lifecycle_before": "observing_recovery",
            "lifecycle_after": "observing_recovery",
            "recorded_at": NOW + timedelta(seconds=10),
        }
    ]
    second = healthy_history(count=1)
    _rehome(second, first["session"]["incident_id"])
    second["samples"][0]["submitted_at"] = NOW + timedelta(seconds=120)
    controls = (
        {
            **_control(1, first["session"]["incident_id"]),
            "created_at": NOW - timedelta(seconds=600),
        },
        {
            **_control(2, first["session"]["incident_id"]),
            "created_at": NOW + timedelta(seconds=11),
        },
    )
    records = RecoveryRecords(
        incident={
            "incident_id": first["session"]["incident_id"],
            "lifecycle": "observing_recovery",
            "mode": "automatic",
            "control_generation": 2,
        },
        sessions=(first, second),
        controls=controls,
        grants=OBSERVER_MEASURED,
    )
    actions = recovery_outcome(_scenario(first), records).actions
    assert actions[:2] == ("record_handling", "advance_incident_lifecycle")
    first_persist = actions.index("persist_observation")
    second_handling = len(actions) - 1 - actions[::-1].index("record_handling")
    assert (
        first_persist
        < second_handling
        < actions.index("persist_observation", first_persist + 1)
    )


# --- issue #144: nested records are checked layer by layer


def _owner_mismatch(tamper):
    history = healthy_history()
    tamper(history)
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        recovery_outcome(_scenario(history), _records(history))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda h: h["samples"][0].update(session_id=uuid4()),
        lambda h: h["samples"][0]["readings"][0].update(sample_id=uuid4()),
        lambda h: h["endings"][0].update(session_id=uuid4()),
        lambda h: h["endings"][0].update(incident_id=uuid4()),
        lambda h: h["endings"][0].update(sample_id=uuid4()),
    ],
    ids=[
        "sample-session",
        "reading-sample",
        "ending-session",
        "ending-incident",
        "ending-sample",
    ],
)
def test_a_nested_record_of_another_owner_is_refused(tamper):
    _owner_mismatch(tamper)


@pytest.mark.parametrize("incident_id", ["other", None], ids=["other", "missing"])
def test_a_control_row_of_another_incident_is_refused(incident_id):
    history = healthy_history()
    control = _control(1, uuid4() if incident_id == "other" else None)
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        recovery_outcome(_scenario(history), _records(history, controls=[control]))


def test_a_takeover_between_the_queries_and_the_commit_follows_the_queries():
    """The queries ran at the bundle's ``evaluated_at``; ``submitted_at`` is
    only when the sample was persisted."""
    history = healthy_history(count=1)
    sample = history["samples"][0]
    evaluated = (
        recovery_outcome(_scenario(history), _records(history))
        .recovery_samples[0]
        .signals
    )
    ran_at = min(s.evaluated_at for s in evaluated.values() if s.evaluated_at)
    sample["submitted_at"] = ran_at + timedelta(seconds=300)
    takeover = {
        **_control(1, history["session"]["incident_id"]),
        "action": "takeover",
        "created_at": ran_at + timedelta(seconds=100),
    }
    actions = recovery_outcome(
        _scenario(history), _records(history, controls=[takeover])
    ).actions
    took = actions.index("human_takeover")
    assert "read_only_query" not in actions[took:]
    assert actions.index("persist_observation") > took


def test_a_bundle_instant_without_a_timezone_is_not_trusted():
    """Bot P2 on #148: a hash-valid bundle whose ``evaluated_at`` is naive
    must not crash the merge against aware record times; the instant is
    treated as absent and the persisted time orders the queries."""
    import json

    from tests.m1_02_replay_support import rehash

    history = healthy_history(count=1)
    for index, reading in enumerate(history["samples"][0]["readings"]):
        bundle = json.loads(bytes(reading["raw"]))
        bundle["evaluated_at"] = "2026-01-01T00:00:00"
        history["samples"][0]["readings"][index] = rehash(
            reading, json.dumps(bundle, sort_keys=True).encode()
        )
    takeover = {
        **_control(1, history["session"]["incident_id"]),
        "action": "takeover",
    }
    outcome = recovery_outcome(
        _scenario(history), _records(history, controls=[takeover])
    )
    assert all(
        signal.evaluated_at is None
        for sample in outcome.recovery_samples
        for signal in sample.signals.values()
    )
    assert "read_only_query" in outcome.actions
