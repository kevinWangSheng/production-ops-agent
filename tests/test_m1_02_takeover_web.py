"""Takeover through the workbench without PostgreSQL (M1-02 step 3b, #121).

The in-memory ``IncidentStore`` mirrors the durable semantics the PG test
proves (``tests/integration/test_m1_02_takeover_postgres.py``); this file
covers the web contract: the action is accepted with the usual key /
generation / audit path, the page shows ``human_owned``, the observation is
withdrawn, automatic investigation stays stopped, and a remediation can
still be registered under human ownership without a new Run.
"""

from __future__ import annotations

from datetime import timedelta

from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)
from tests.target_support import ANY_TARGETS


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def _register(app, incident, generation, key):
    return _control(
        app,
        incident,
        {
            "action": "register_remediation",
            "expected_generation": str(generation),
            "idempotency_key": key,
            "revision": "checkout:v2",
        },
    )


def test_takeover_withdraws_observation_and_stops_automatic_investigation():
    app, workbench, clock = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    assert _register(app, incident, 0, "rem-1").status == 200
    (session,) = workbench.incidents.sessions
    assert session["state"] == "authorized"

    taken = _control(
        app,
        incident,
        {"action": "takeover", "expected_generation": "1", "idempotency_key": "t-1"},
    )
    assert taken.status == 200, taken.text
    assert taken.json()["generation"] == 2 and taken.json()["replayed"] is False
    replay = _control(
        app,
        incident,
        {"action": "takeover", "expected_generation": "1", "idempotency_key": "t-1"},
    )
    assert replay.status == 200 and replay.json()["replayed"] is True
    again = _control(
        app,
        incident,
        {"action": "takeover", "expected_generation": "2", "idempotency_key": "t-2"},
    )
    assert (again.status, again.json()["code"]) == (409, "ILLEGAL_TRANSITION")

    summary = workbench.list_incidents()[0]
    assert summary.mode == "human_owned" and summary.control_generation == 2
    assert (session["state"], session["ended_reason"]) == (
        "revoked",
        "authority_revoked",
    )
    run = workbench.incidents.runs[summary.current_run_id]
    assert run["state"] == "waiting_human"
    # The Agent does not investigate a human-owned incident.
    assert workbench.run_once(summary.incident_id, ScriptedInvestigator(clock)) is None
    assert run["state"] == "waiting_human"
    refused = _control(
        app,
        incident,
        {"action": "new_run", "expected_generation": "2", "idempotency_key": "nr-1"},
    )
    assert (refused.status, refused.json()["code"]) == (409, "ILLEGAL_TRANSITION")
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert 'id="incident-mode">human_owned' in page.text
    assert "authority_revoked" in page.text
    assert [c["action"] for c in workbench.incidents.controls] == [
        "register_remediation",
        "takeover",
    ]


def test_a_remediation_under_human_ownership_observes_without_investigating():
    app, workbench, clock = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    assert (
        _control(
            app,
            incident,
            {
                "action": "takeover",
                "expected_generation": "0",
                "idempotency_key": "t-1",
            },
        ).status
        == 200
    )
    runs_before = set(workbench.incidents.runs)
    registered = _register(app, incident, 1, "rem-1")
    assert registered.status == 200, registered.text
    assert registered.json()["generation"] == 2
    summary = workbench.list_incidents()[0]
    assert summary.mode == "human_owned" and summary.lifecycle == "observing_recovery"
    (session,) = workbench.incidents.sessions
    assert (
        session["state"] == "authorized" and session["subject_control_generation"] == 2
    )
    # No new Run, nothing claimable, the parked Run untouched.
    assert set(workbench.incidents.runs) == runs_before
    assert workbench.incidents.runs[summary.current_run_id]["state"] == "waiting_human"
    assert workbench.run_once(summary.incident_id, ScriptedInvestigator(clock)) is None
    # A note is recorded for the human but starts nothing.
    noted = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "2",
            "idempotency_key": "f-1",
            "text": "handled by the on-call engineer",
        },
    )
    assert noted.status == 200 and noted.json()["generation"] == 3
    assert workbench.incidents.runs[summary.current_run_id]["state"] == "waiting_human"
    assert (
        workbench.incidents.inputs[-1]["content"]["text"]
        == "handled by the on-call engineer"
    )


def test_pause_then_takeover_is_lifted_by_resume_and_notes_outlive_the_deadline():
    """Review of PR #123, items 1 and 2 through the workbench."""
    app, workbench, clock = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    for action, generation, key in (("pause", 0, "p-1"), ("takeover", 1, "t-1")):
        assert (
            _control(
                app,
                incident,
                {
                    "action": action,
                    "expected_generation": str(generation),
                    "idempotency_key": key,
                },
            ).status
            == 200
        )
    blocked = _register(app, incident, 2, "rem-1")
    assert (blocked.status, blocked.json()["code"]) == (409, "ILLEGAL_TRANSITION")
    resumed = _control(
        app,
        incident,
        {"action": "resume", "expected_generation": "2", "idempotency_key": "r-1"},
    )
    assert resumed.status == 200 and resumed.json()["generation"] == 3
    summary = workbench.list_incidents()[0]
    assert (summary.state, summary.mode) == ("running", "human_owned")
    run = workbench.incidents.runs[summary.current_run_id]
    assert run["state"] == "waiting_human"
    assert _register(app, incident, 3, "rem-2").status == 200
    assert workbench.run_once(summary.incident_id, ScriptedInvestigator(clock)) is None
    # Old Run past its deadline: the note is still recorded, nothing renewed.
    run["deadline"] = clock.now() - timedelta(seconds=1)
    runs_before = set(workbench.incidents.runs)
    noted = _control(
        app,
        incident,
        {
            "action": "correct",
            "expected_generation": "4",
            "idempotency_key": "c-1",
            "text": "human record",
        },
    )
    assert noted.status == 200 and noted.json()["generation"] == 5
    assert set(workbench.incidents.runs) == runs_before
    assert run["state"] == "waiting_human"
    assert workbench.incidents.inputs[-1]["content"]["text"] == "human record"
