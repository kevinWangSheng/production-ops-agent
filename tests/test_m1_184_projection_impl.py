"""Implementer's checks for #184 step 2 without PostgreSQL: the F6 and F13
projections and the incident page tell the automatic observation (actor
``system``) from a human action (r6 C6/C7, D2-01..D2-16).

The acceptance tests are the independent author's; these pin the
implementation details: the legacy reading of a session without a source,
the pairing of a registration with the session it authorized (D2-13), the
page's recovery statement and the F13 input sections.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from opspilot.acceptance import IncidentScenario, RecoveryRecords, recovery_outcome
from opspilot.acceptance_recovery import (
    OBSERVER_GRANTS,
    authorization_source,
    is_system_authorization,
)
from opspilot.knowledge.generation import (
    MAX_HUMAN_ACTIONS,
    InputTooLarge,
    build_input,
)
from opspilot.knowledge.store import Watermark
from opspilot.web.service import (
    OBSERVING_NO_CLAIM,
    RECOVERED_NO_REMEDIATION,
    _with_recovery_status,
)
from tests.m1_02_replay_support import NOW, healthy_history
from tests.m1_web_support import basic, call
from tests.test_m1_02_recovery_page import _observing_incident

GRANTS = {
    "tables": {
        f"public.{name}": dict(grants) for name, grants in OBSERVER_GRANTS.items()
    },
    "sequences": {},
    "schemas": {"public": ("USAGE",)},
    "database": (),
    "functions": (),
    "product_schema": "public",
}


def _scenario(incident) -> IncidentScenario:
    return IncidentScenario(
        scenario_id="F6:184:automatic",
        feature_id="F6",
        acceptance_step="1",
        kind="recovered",
        subject_id=str(incident),
    )


def _auto_control(incident, session_id, generation=0) -> dict:
    return {
        "incident_id": incident,
        "action": "auto_authorize_observation",
        "expected_generation": generation,
        "resulting_generation": generation,
        "actor": "system",
        "payload": {
            "authorization_source": "automatic",
            "revision": None,
            "session_id": str(session_id),
        },
        "created_at": NOW - timedelta(seconds=600),
    }


def _registration(incident, session_id, generation=1, *, named=True) -> dict:
    payload: dict = {"revision": "svc:v2"}
    if named:
        payload["session_id"] = str(session_id)
    return {
        "incident_id": incident,
        "action": "register_remediation",
        "expected_generation": generation - 1,
        "resulting_generation": generation,
        "actor": "operator",
        "payload": payload,
        "created_at": NOW - timedelta(seconds=500),
    }


def _records(incident, lifecycle, sessions, controls) -> RecoveryRecords:
    return RecoveryRecords(
        incident={"incident_id": incident, "lifecycle": lifecycle, "mode": "automatic"},
        sessions=tuple(sessions),
        controls=tuple(controls),
        grants=GRANTS,
    )


# --- F6 --------------------------------------------------------------------


def test_an_automatic_confirmed_recovery_is_a_system_authorization():
    history = healthy_history()
    history["session"]["authorization_source"] = "automatic"
    history["session"]["authorized_by"] = "system"
    incident = history["session"]["incident_id"]
    control = _auto_control(incident, history["session"]["session_id"])
    outcome = recovery_outcome(
        _scenario(incident),
        _records(incident, history["incident_lifecycle"], [history], [control]),
    )
    assert outcome.recovery_confirmed and outcome.incident_lifecycle == "resolved"
    assert outcome.authorization_source == "automatic"
    assert outcome.authorization_sources == ("automatic",)
    assert outcome.observation_authorization is not None
    assert outcome.observation_authorization["authorization_source"] == "automatic"
    # no human action, no human control, no human interaction
    assert outcome.permissions == ("read_only",)
    assert outcome.human_interaction is None
    assert "record_handling" not in outcome.actions
    assert not [a for a in outcome.actions if a.startswith("human_")]
    assert all(e.time_source != "created_at" for e in outcome.action_events)
    # the raw audit keeps the row; the projection names it a system one
    assert outcome.handling_audit == (control,)
    assert outcome.system_authorizations == (control,)
    # the session contract keys are unchanged (F6 step 1 assertion)
    assert "authorization_source" not in outcome.observation_sessions[0]


def test_a_session_written_before_0013_reads_as_the_human_registration():
    history = healthy_history()
    assert "authorization_source" not in history["session"]
    assert authorization_source(history["session"]) == "post_remediation"
    assert authorization_source({"purpose": "release_observation"}) is None
    assert (
        authorization_source(
            {"purpose": "incident_recovery", "authorization_source": None}
        )
        == "post_remediation"
    )
    incident = history["session"]["incident_id"]
    outcome = recovery_outcome(
        _scenario(incident),
        _records(
            incident,
            history["incident_lifecycle"],
            [history],
            [_registration(incident, history["session"]["session_id"])],
        ),
    )
    assert outcome.authorization_source == "post_remediation"
    assert outcome.permissions == ("read_only", "human_control")
    assert outcome.actions[:2] == ("record_handling", "advance_incident_lifecycle")
    assert outcome.system_authorizations == ()


def test_only_the_system_actor_makes_the_action_a_system_authorization():
    assert is_system_authorization(
        {"action": "auto_authorize_observation", "actor": "system"}
    )
    assert not is_system_authorization(
        {"action": "auto_authorize_observation", "actor": "operator"}
    )
    assert not is_system_authorization(
        {"action": "register_remediation", "actor": "system"}
    )


@pytest.mark.parametrize("named", [True, False])
def test_a_registration_after_an_automatic_session_pairs_with_its_own_session(
    named: bool,
):
    """D2-13: automatic session (open -> observing_recovery), revoked by a
    human registration while observing; the registration found the incident
    already observing, so it claims no lifecycle move. Positional pairing
    would have taken it for the first session and claimed ``open`` before."""
    incident = uuid4()
    automatic = healthy_history(count=2)
    automatic["session"].update(
        incident_id=incident,
        authorization_source="automatic",
        authorized_by="system",
        state="revoked",
        ended_reason="authority_revoked",
    )
    automatic["endings"] = [
        {
            "ending_id": uuid4(),
            "session_id": automatic["session"]["session_id"],
            "incident_id": incident,
            "ended_reason": "authority_revoked",
            "transition": None,
            "sample_id": None,
            "lifecycle_before": "observing_recovery",
            "lifecycle_after": "observing_recovery",
            "recorded_at": NOW + timedelta(seconds=90),
            "job_id": None,
            "job_sequence": None,
        }
    ]
    human = healthy_history()
    human["session"].update(
        incident_id=incident, authorization_source="post_remediation"
    )
    for ending in human["endings"]:
        ending["incident_id"] = incident
    controls = [
        _auto_control(incident, automatic["session"]["session_id"]),
        {
            **_registration(incident, human["session"]["session_id"], named=named),
            "created_at": NOW + timedelta(seconds=90),
        },
    ]
    outcome = recovery_outcome(
        _scenario(incident),
        _records(incident, "resolved", [automatic, human], controls),
    )
    assert outcome.authorization_sources == ("automatic", "post_remediation")
    assert outcome.authorization_source == "post_remediation"
    assert outcome.permissions == ("read_only", "human_control")
    registered = [
        event for event in outcome.action_events if event.time_source == "created_at"
    ]
    assert [event.action for event in registered] == ["record_handling"]
    # the human session's own confirmation still moves the lifecycle
    assert outcome.actions[-1] == "advance_incident_lifecycle"


# --- F13 -------------------------------------------------------------------


def _raw(controls, *, source=None) -> dict:
    session = {
        "state": "completed",
        "session_id": uuid4(),
        "target": {},
        "authorized_by": "system" if source == "automatic" else "operator",
        "authorized_at": NOW,
        "deadline_at": None,
        "max_samples": 1,
        "sample_interval_seconds": 1,
        "sustained_window_seconds": 1,
        "ended_reason": "recovery_confirmed",
        "health_profile_revision": None,
        "adopted_count": 0,
        "healthy_since": None,
    }
    if source is not None:
        session["authorization_source"] = source
    return {
        "incident": {
            "incident_id": uuid4(),
            "control_generation": 0,
            "observation_generation": 1,
            "intake_key": "i",
            "lifecycle": "resolved",
            "state": "completed",
            "mode": "automatic",
            "target_id": uuid4(),
            "resource_uid": "r",
            "current_run_id": None,
            "created_at": NOW,
            "conclusion": {},
        },
        "watermark": Watermark(1, 1, uuid4(), uuid4(), 1, uuid4(), 1, "h"),
        "observation": {
            "session": session,
            "ending": {
                "ending_id": uuid4(),
                "ended_reason": "recovery_confirmed",
                "recorded_at": NOW,
                "lifecycle_before": "observing_recovery",
                "lifecycle_after": "resolved",
                "transition": "recovery_confirmed",
            },
            "samples": [],
            "readings": [],
        },
        "evidence": [],
        "controls": controls,
        "inputs": [],
        "runs": [],
        "postmortem": None,
        "pending_regeneration_version": None,
        "return_reason": None,
        "published_entries": {},
    }


def test_f13_input_leaves_the_system_authorization_out_of_human_actions():
    incident = uuid4()
    controls = [
        {**_auto_control(incident, uuid4()), "created_at": NOW},
        {**_registration(incident, uuid4()), "created_at": NOW},
    ]
    built = build_input(_raw(controls, source="automatic"), secrets=[])
    actions = [a["action"] for a in built.facts["human_actions"]]
    assert actions == ["register_remediation"]
    recovery = built.facts["recovery"]
    assert recovery["authorization_source"] == "automatic"
    assert recovery["authorized_by"] == "system"
    assert recovery["authorized_at"] == NOW.isoformat()
    events = [e["event"] for e in built.facts["timeline"]]
    assert not [e for e in events if "auto_authorize" in e]
    assert "self_heal" not in built.payload_text


def test_the_system_authorization_does_not_count_toward_the_human_action_limit():
    incident = uuid4()
    controls = [
        {**_registration(incident, uuid4()), "created_at": NOW}
        for _ in range(MAX_HUMAN_ACTIONS)
    ]
    controls.append({**_auto_control(incident, uuid4()), "created_at": NOW})
    built = build_input(_raw(controls, source="automatic"), secrets=[])
    assert len(built.facts["human_actions"]) == MAX_HUMAN_ACTIONS
    with pytest.raises(InputTooLarge):
        build_input(_raw(controls + controls[:1], source="automatic"), secrets=[])


def test_f13_input_reads_a_legacy_session_as_post_remediation():
    built = build_input(_raw([]), secrets=[])
    assert built.facts["recovery"]["authorization_source"] == "post_remediation"


# --- workbench -------------------------------------------------------------


def _view(source, state, *, consistent=True, confirmed=True, available=True):
    return {
        "authorization_source": source,
        "state": state,
        "replay": {
            "available": available,
            "consistent": consistent,
            "recovery_confirmed": confirmed,
        },
    }


def test_the_statement_is_only_for_a_confirmed_automatic_resolution():
    (view,) = _with_recovery_status([_view("automatic", "completed")], "resolved")
    assert view["recovery_status"] == "recovered_no_remediation"
    assert view["recovery_statement"] == RECOVERED_NO_REMEDIATION
    for views, lifecycle in (
        ([_view("automatic", "completed")], "open"),
        ([_view("automatic", "completed", consistent=False)], "resolved"),
        ([_view("automatic", "completed", confirmed=False)], "resolved"),
        ([_view("automatic", "completed", available=False)], "resolved"),
        ([_view("post_remediation", "completed")], "resolved"),
        ([_view("automatic", "expired", confirmed=False)], "open"),
    ):
        (out,) = _with_recovery_status(views, lifecycle)
        assert out["recovery_statement"] is None, (views, lifecycle)
    # an older automatic session never speaks for the latest one
    older, latest = _with_recovery_status(
        [_view("automatic", "completed"), _view("post_remediation", "completed")],
        "resolved",
    )
    assert older["recovery_statement"] is None and latest["recovery_statement"] is None
    (observing,) = _with_recovery_status(
        [_view("automatic", "authorized", confirmed=False)], "observing_recovery"
    )
    assert observing["recovery_status"] == "observing"
    assert observing["recovery_statement"] == OBSERVING_NO_CLAIM


def _automatic(workbench) -> None:
    session = workbench.incidents.sessions[0]
    session["authorization_source"] = "automatic"
    session["authorized_by"] = "system"
    session["target"] = {**session["target"], "revision": None}


def test_the_page_says_recovered_without_a_remediation_for_an_automatic_session():
    app, workbench, incident, _ = _observing_incident()
    _automatic(workbench)
    snapshot = workbench.snapshot(UUID(incident))
    (session,) = snapshot["observation_sessions"]
    assert session["authorization_source"] == "automatic"
    assert session["target_revision"] is None
    (recovery,) = snapshot["recovery"]
    assert recovery["recovery_status"] == "recovered_no_remediation"
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert RECOVERED_NO_REMEDIATION in page.text
    assert '<span class="badge authorization-source">automatic</span>' in page.text
    assert "authorized at" in page.text
    assert "self_heal" not in page.text and "self-heal" not in page.text


def test_the_page_claims_no_recovery_while_the_automatic_session_observes():
    app, workbench, incident, _ = _observing_incident(count=3)
    _automatic(workbench)
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert OBSERVING_NO_CLAIM in page.text
    assert RECOVERED_NO_REMEDIATION not in page.text
    assert "recovery confirmed" not in page.text.replace("not confirmed", "")


def test_the_human_registration_keeps_its_wording():
    app, workbench, incident, _ = _observing_incident()
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert '<span class="badge authorization-source">post_remediation</span>' in (
        page.text
    )
    assert RECOVERED_NO_REMEDIATION not in page.text
    assert "handled at" in page.text
    (recovery,) = workbench.snapshot(UUID(incident))["recovery"]
    assert recovery["recovery_status"] is None
