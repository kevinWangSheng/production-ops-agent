"""M1-01 incident control projection (C3 §4 line 86; task record 2026-09-29).

The persisted incident ``state`` is only a human-control mirror. The workbench
shows it as ``id="incident-control"`` (``paused`` / ``cancelled`` only) and as
the list page's ``Control`` column; execution progress is the Run badge, the
conclusion is ``id="concluded"``, the lifecycle is its own badge. Scenario
numbers below are the rows of the task record's scenario table.

Everything reads the public HTML (``GET /``, ``GET /incidents/{id}``) after
driving the workbench through its public paths. Scenarios 5-7 (global/target
suspension) need the durable store and live in
``tests/integration/test_m1_incident_control_postgres.py``.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest

from tests.m1_incident_control_support import (
    element_text,
    incident_badges,
    list_headers,
    list_row,
    summary_words,
)
from tests.m1_investigation_support import reply, tool_call
from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)

IN_PROGRESS_WORDS = {"queued", "running"}


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def _act(app, incident, action, generation, text=None):
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": f"{action}-{generation}",
    }
    if text is not None:
        fields["text"] = text
    response = _control(app, incident, fields)
    assert response.status == 200, response.text
    return response


def _detail(app, incident) -> str:
    response = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert response.status == 200
    return response.text


def _index(app) -> str:
    response = call(app, "GET", "/", headers=basic())
    assert response.status == 200
    return response.text


def _setup(key="ctl"):
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key=key).json()["incident_id"]
    subject = UUID(incident)
    run_id = workbench.incidents.incidents[subject]["current_run_id"]
    return app, workbench, clock, incident, subject, run_id


def _claim(workbench, subject, run_id, seconds=600):
    return workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, seconds)


def _assert_no_incident_state(html):
    """Old incident-level State element and label are gone."""
    assert element_text(html, "incident-state") is None
    assert "state" not in summary_words(html)


def _assert_in_progress(html):
    """No control marker, and no queued/running at incident level."""
    assert element_text(html, "incident-control") is None
    _assert_no_incident_state(html)
    assert not IN_PROGRESS_WORDS & set(incident_badges(html))
    assert not IN_PROGRESS_WORDS & summary_words(html)


def _assert_list_control(html, incident, expected):
    headers = list_headers(html)
    assert "Control" in headers and "State" not in headers
    assert [h for h in headers if h != "Control"] == [
        "Incident",
        "Run",
        "Lifecycle",
        "Generation",
        "Concluded",
        "Created",
    ]
    row = list_row(html, incident)
    assert row["Control"] == expected
    assert not IN_PROGRESS_WORDS & {c.lower() for c in row.values()}


# -- 1. after publish ----------------------------------------------------------


@pytest.mark.parametrize("marker", ["queued", "running"])
def test_published_incident_shows_completed_run_and_no_control_marker(marker):
    """Scenario 1: stored marker queued or running, Run completed, conclusion."""
    app, workbench, clock, incident, subject, run_id = _setup()
    if marker == "running":
        # follow_up writes ``running`` on the incident row, Run re-queued.
        _act(app, incident, "follow_up", 0, text="check the 5xx panel")
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock, model_requests=4))
    assert outcome is not None and outcome.execution == "completed"
    assert outcome.handoff is False
    assert workbench.incidents.runs[run_id]["state"] == "completed"
    assert workbench.incidents.incidents[subject]["state"] == marker
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "completed"
    assert element_text(html, "concluded") == "concluded"
    assert "open" in summary_words(html)
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")
    assert list_row(_index(app), incident)["Concluded"] == "yes"


# -- 2. handoff or timeout sweep -----------------------------------------------


@pytest.mark.parametrize("marker", ["queued", "running"])
def test_handoff_shows_waiting_human_without_control_marker(marker):
    """Scenario 2 (loop handoff): Run waiting_human, no conclusion, no marker."""
    app, workbench, clock, incident, subject, run_id = _setup()
    if marker == "running":
        _act(app, incident, "follow_up", 0, text="look at the dependency")
    investigator = ScriptedInvestigator(
        clock,
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(tool_calls=[tool_call()], finish="stop"),
        ],
        model_requests=4,
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.handoff is True
    assert workbench.incidents.runs[run_id]["state"] == "waiting_human"
    assert workbench.incidents.incidents[subject]["state"] == marker
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "waiting_human"
    assert element_text(html, "concluded") == "no conclusion"
    assert "open" in summary_words(html)
    assert 'id="handoff-report"' in html or 'id="report-missing"' in html
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")
    assert list_row(_index(app), incident)["Concluded"] == "no"


@pytest.mark.parametrize("marker", ["queued", "running"])
def test_timeout_sweep_shows_waiting_human_without_control_marker(marker):
    """Scenario 2 (deadline sweep on page load)."""
    app, workbench, clock, incident, subject, run_id = _setup()
    if marker == "running":
        _act(app, incident, "follow_up", 0, text="look at the dependency")
    _claim(workbench, subject, run_id)
    clock.advance(601)
    html = _detail(app, incident)  # the page load reconciles the overdue Run
    assert workbench.incidents.runs[run_id]["state"] == "waiting_human"
    assert workbench.incidents.incidents[subject]["state"] == marker
    assert element_text(html, "run-state") == "waiting_human"
    assert element_text(html, "concluded") == "no conclusion"
    assert "open" in summary_words(html)
    assert 'id="handoff-report"' in html or 'id="report-missing"' in html
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


# -- 3. cancel ------------------------------------------------------------------


def test_cancel_shows_control_cancelled_and_lifecycle_stays_open():
    """Scenario 3."""
    app, workbench, _, incident, subject, run_id = _setup()
    _act(app, incident, "cancel", 0)
    assert workbench.incidents.runs[run_id]["state"] == "cancelled"
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "cancelled"
    assert element_text(html, "run-state") == "cancelled"
    _assert_no_incident_state(html)
    words = summary_words(html)
    assert "open" in words and "closed" not in words
    assert not IN_PROGRESS_WORDS & words
    assert workbench.incidents.incidents[subject]["lifecycle"] == "open"
    _assert_list_control(_index(app), incident, "cancelled")
    assert list_row(_index(app), incident)["Lifecycle"] == "open"


def test_cancel_after_claim_shows_control_cancelled():
    """Scenario 3 from a running Run."""
    app, workbench, _, incident, subject, run_id = _setup()
    _claim(workbench, subject, run_id)
    _act(app, incident, "cancel", 0)
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "cancelled"
    assert element_text(html, "run-state") == "cancelled"
    _assert_no_incident_state(html)
    assert "closed" not in summary_words(html)


def test_new_run_after_cancel_clears_the_control_marker():
    """A cancelled marker is not sticky once a human starts a new Run."""
    app, workbench, _, incident, subject, _ = _setup()
    _act(app, incident, "cancel", 0)
    _act(app, incident, "new_run", 1)
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "queued"
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


# -- 4. manual pause ------------------------------------------------------------


def test_manual_pause_shows_control_paused_and_paused_run():
    """Scenario 4."""
    app, workbench, _, incident, subject, run_id = _setup()
    _claim(workbench, subject, run_id)
    _act(app, incident, "pause", 0)
    assert workbench.incidents.runs[run_id]["state"] == "paused"
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "paused"
    assert element_text(html, "run-state") == "paused"
    _assert_no_incident_state(html)
    assert not IN_PROGRESS_WORDS & summary_words(html)
    assert not IN_PROGRESS_WORDS & set(incident_badges(html))
    _assert_list_control(_index(app), incident, "paused")


def test_note_while_paused_keeps_control_paused():
    """A note recorded during a pause does not resume anything (PR #31)."""
    app, workbench, _, incident, subject, run_id = _setup()
    _claim(workbench, subject, run_id)
    _act(app, incident, "pause", 0)
    _act(app, incident, "follow_up", 1, text="still paused?")
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "paused"
    assert element_text(html, "run-state") == "paused"
    _assert_list_control(_index(app), incident, "paused")


def test_pause_before_any_claim_shows_control_paused():
    """Scenario 4 for a Run nobody claimed yet. The stores pause only running or
    waiting_human Runs, so this Run may stay ``queued``; the contract's
    ``run-state`` = paused is therefore not asserted here (open question)."""
    app, workbench, _, incident, subject, _ = _setup()
    _act(app, incident, "pause", 0)
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "paused"
    _assert_no_incident_state(html)
    assert not IN_PROGRESS_WORDS & summary_words(html)
    _assert_list_control(_index(app), incident, "paused")


# -- 8. resume / follow_up / correct -------------------------------------------


@pytest.mark.parametrize("action", ["resume", "follow_up", "correct"])
def test_resume_follow_up_and_correct_show_queued_run_and_no_control_marker(action):
    """Scenario 8: the row is stored as ``running`` but the Run is only queued."""
    app, workbench, _, incident, subject, run_id = _setup()
    generation = 0
    if action == "resume":
        _act(app, incident, "pause", 0)
        generation = 1
    _act(
        app,
        incident,
        action,
        generation,
        text=None if action == "resume" else "the target is checkout-canary",
    )
    assert workbench.incidents.incidents[subject]["state"] == "running"
    assert workbench.incidents.runs[run_id]["state"] == "queued"
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "queued"
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


# -- 9. claim -------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["queued", "running"])
def test_claim_shows_running_run_and_no_control_marker(marker):
    """Scenario 9."""
    app, workbench, _, incident, subject, run_id = _setup()
    if marker == "running":
        _act(app, incident, "follow_up", 0, text="more context")
    _claim(workbench, subject, run_id)
    assert workbench.incidents.incidents[subject]["state"] == marker
    assert workbench.incidents.runs[run_id]["state"] == "running"
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "running"
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


def test_freshly_accepted_incident_is_in_progress_with_queued_run():
    app, _, _, incident, _, _ = _setup()
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "queued"
    assert element_text(html, "concluded") == "no conclusion"
    assert "open" in summary_words(html)
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


# -- stored values other than paused/cancelled are "in progress" ---------------


@pytest.mark.parametrize("stored", ["queued", "running", "some-future-value"])
def test_any_stored_value_other_than_paused_or_cancelled_is_in_progress(stored):
    app, workbench, _, incident, subject, _ = _setup()
    workbench.incidents.incidents[subject]["state"] = stored
    html = _detail(app, incident)
    assert element_text(html, "incident-control") is None
    _assert_no_incident_state(html)
    assert stored not in incident_badges(html)
    _assert_list_control(_index(app), incident, "")


@pytest.mark.parametrize("stored", ["paused", "cancelled"])
def test_stored_paused_or_cancelled_is_shown_verbatim(stored):
    app, workbench, _, incident, subject, _ = _setup()
    workbench.incidents.incidents[subject]["state"] = stored
    assert element_text(_detail(app, incident), "incident-control") == stored
    _assert_list_control(_index(app), incident, stored)


# -- list page with several incidents ------------------------------------------


def test_list_control_column_is_per_incident():
    app, workbench, clock = build_workbench()
    ids = {}
    for name in ("plain", "paused", "cancelled", "done"):
        ids[name] = submit_incident(app, key=f"list-{name}").json()["incident_id"]
    _act(app, ids["paused"], "pause", 0)
    _act(app, ids["cancelled"], "cancel", 0)
    done = UUID(ids["done"])
    workbench.incidents.claim(
        done,
        workbench.incidents.incidents[done]["current_run_id"],
        uuid4(),
        {"state": "v1"},
        600,
    )
    html = _index(app)
    assert list_row(html, ids["plain"])["Control"] == ""
    assert list_row(html, ids["paused"])["Control"] == "paused"
    assert list_row(html, ids["cancelled"])["Control"] == "cancelled"
    assert list_row(html, ids["done"])["Control"] == ""
    headers = list_headers(html)
    assert "Control" in headers and "State" not in headers
    # Run status is now a separate projection; Control remains independent.


# -- read-only projection ------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "cancel", "follow_up"])
def test_rendering_pages_does_not_change_stored_rows(action):
    app, workbench, _, incident, subject, run_id = _setup()
    _act(app, incident, action, 0, text="x" if action == "follow_up" else None)
    before = (
        dict(workbench.incidents.incidents[subject]),
        dict(workbench.incidents.runs[run_id]),
    )
    for _ in range(2):
        _detail(app, incident)
        _index(app)
    after = (
        dict(workbench.incidents.incidents[subject]),
        dict(workbench.incidents.runs[run_id]),
    )
    assert after == before


def test_control_endpoint_response_does_not_expose_an_incident_state():
    """The POST JSON stays as it was: no state field appears."""
    app, _, _, incident, _, _ = _setup()
    body = _act(app, incident, "pause", 0).json()
    assert "state" not in body and "incident-control" not in str(body)


def test_detail_page_never_labels_a_state_anywhere_in_the_summary():
    app, workbench, _, incident, subject, run_id = _setup()
    for step in ("accepted", "claimed", "paused", "cancelled"):
        if step == "claimed":
            _claim(workbench, subject, run_id)
        elif step == "paused":
            _act(app, incident, "pause", 0)
        elif step == "cancelled":
            _act(app, incident, "cancel", 1)
        html = _detail(app, incident)
        assert not re.search(r'id="incident-state"', html), step
        assert "state" not in summary_words(html), step
