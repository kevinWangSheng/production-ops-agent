"""M1-01 incident control projection on the durable store (C3 §4 line 86).

Same contract as ``tests/test_m1_incident_control_contract.py``, driven on
PostgreSQL where global/target suspension exist (scenarios 5-7 of the task
record's table) and where the stored rows can be compared before and after a
page render. Actions go through ``POST /incidents/{id}/control``,
``DurableStore`` suspension/claim/publish calls and the workbench; pages are
read through ``GET /`` and ``GET /incidents/{id}``.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from tests.integration.test_m1_web_postgres import _build, _DbClock
from tests.m1_incident_control_support import (
    element_text,
    incident_badges,
    list_headers,
    list_row,
    summary_words,
)
from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    call,
    post_form,
    same_origin,
)
from tests.target_support import register

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

IN_PROGRESS_WORDS = {"queued", "running"}


def _submit(app, target):
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": target,
            "question": "Why is checkout erroring?",
            "idempotency_key": f"ctl-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201
    return response.json()["incident_id"]


def _act(app, incident, action, generation, text=None):
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": f"{action}-{uuid4()}",
    }
    if text is not None:
        fields["text"] = text
    response = post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )
    assert response.status == 200, response.text
    return response


def _detail(app, incident):
    response = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert response.status == 200
    return response.text


def _index(app):
    response = call(app, "GET", "/", headers=basic())
    assert response.status == 200
    return response.text


def _rows(store, incident):
    with store.transaction() as conn:
        inc = conn.execute(
            "SELECT * FROM opspilot_incidents WHERE incident_id=%s", (incident,)
        ).fetchone()
        runs = conn.execute(
            "SELECT * FROM opspilot_runs WHERE incident_id=%s ORDER BY run_id",
            (incident,),
        ).fetchall()
    return inc, runs


def _stored_state(store, incident):
    return _rows(store, incident)[0]["state"]


def _run_state(store, incident):
    inc, runs = _rows(store, incident)
    return next(r["state"] for r in runs if r["run_id"] == inc["current_run_id"])


def _claim(workbench, incident):
    subject = UUID(incident)
    run_id = workbench.list_incidents()[0].current_run_id
    assert workbench.list_incidents()[0].incident_id == subject
    return workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, 600)


def _assert_no_incident_state(html):
    assert element_text(html, "incident-state") is None
    assert "state" not in summary_words(html)


def _assert_in_progress(html):
    assert element_text(html, "incident-control") is None
    _assert_no_incident_state(html)
    assert not IN_PROGRESS_WORDS & set(incident_badges(html))
    assert not IN_PROGRESS_WORDS & summary_words(html)


def _assert_list_control(html, incident, expected):
    headers = list_headers(html)
    assert "Control" in headers and "State" not in headers
    row = list_row(html, incident)
    assert row["Control"] == expected


class _Suspension:
    """Public-API suspension of one scope, always released on exit."""

    def __init__(self, store, kind, target_name):
        self.store, self.kind = store, kind
        self.target = register(store, target_name)
        self.generation = None

    def _current(self):
        if self.kind == "global":
            with self.store.transaction() as conn:
                return conn.execute(
                    "SELECT global_generation AS g FROM opspilot_scope_controls WHERE scope_id=1"
                ).fetchone()["g"]
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT generation AS g FROM opspilot_target_suspensions WHERE target_id=%s",
                (self.target,),
            ).fetchone()
        return 0 if row is None else row["g"]

    def _set(self, suspended):
        if self.kind == "global":
            return self.store.set_global_suspension(
                suspended, expected_generation=self._current(), actor="operator"
            )
        return self.store.set_target_suspension(
            self.target,
            suspended,
            expected_generation=self._current(),
            actor="operator",
        )

    def suspend(self):
        self.generation = self._set(True)

    def release(self):
        self._set(False)


@pytest.fixture(params=["target", "global"])
def scope(request):
    app, workbench, store = _build()
    target = f"tgt-{uuid4()}"
    suspension = _Suspension(store, request.param, target)
    # A left-over global suspension from another test would pause our intake.
    if request.param == "global":
        with store.transaction() as conn:
            assert not conn.execute(
                "SELECT global_suspended FROM opspilot_scope_controls WHERE scope_id=1"
            ).fetchone()["global_suspended"]
    try:
        yield app, workbench, store, target, suspension
    finally:
        try:
            suspension.release()
        except Exception:  # noqa: BLE001 - best-effort cleanup of shared lab state
            pass


# -- 5. suspension before the conclusion ---------------------------------------


def test_suspension_of_an_unpublished_incident_shows_control_paused(scope):
    app, workbench, store, target, suspension = scope
    incident = _submit(app, target)
    suspension.suspend()
    assert _stored_state(store, UUID(incident)) == "paused"
    assert _run_state(store, UUID(incident)) == "paused"
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "paused"
    assert element_text(html, "run-state") == "paused"
    assert element_text(html, "concluded") == "no conclusion"
    _assert_no_incident_state(html)
    assert not IN_PROGRESS_WORDS & summary_words(html)
    assert not IN_PROGRESS_WORDS & set(incident_badges(html))
    _assert_list_control(_index(app), incident, "paused")


def test_suspension_of_a_claimed_run_shows_control_paused(scope):
    app, workbench, store, target, suspension = scope
    incident = _submit(app, target)
    _claim(workbench, incident)
    suspension.suspend()
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "paused"
    assert element_text(html, "run-state") == "paused"
    _assert_no_incident_state(html)
    _assert_list_control(_index(app), incident, "paused")


# -- 6. suspension after publish -----------------------------------------------


def test_suspension_does_not_mark_a_published_incident(scope):
    app, workbench, store, target, suspension = scope
    incident = _submit(app, target)
    subject = UUID(incident)
    outcome = workbench.run_once(subject, ScriptedInvestigator(_DbClock(store)))
    assert outcome is not None and outcome.execution == "completed"
    suspension.suspend()
    assert _run_state(store, subject) == "completed"
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "completed"
    assert element_text(html, "concluded") == "concluded"
    assert "open" in summary_words(html)
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


# -- 7. release keeps paused ---------------------------------------------------


def test_releasing_a_suspension_leaves_control_paused_until_a_human_resumes(scope):
    app, workbench, store, target, suspension = scope
    incident = _submit(app, target)
    suspension.suspend()
    suspension.release()
    assert _stored_state(store, UUID(incident)) == "paused"
    html = _detail(app, incident)
    assert element_text(html, "incident-control") == "paused"
    assert element_text(html, "run-state") == "paused"
    _assert_no_incident_state(html)
    _assert_list_control(_index(app), incident, "paused")
    # A human resume is what returns it to progress.
    generation = workbench.list_incidents()[0].control_generation
    _act(app, incident, "resume", generation)
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "queued"
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


# -- the remaining rows on the real store --------------------------------------


def test_publish_handoff_cancel_pause_resume_and_claim_on_postgres():
    app, workbench, store = _build()
    target = f"tgt-{uuid4()}"

    # 9 claim, then 1 publish (marker stays queued in the database).
    incident = _submit(app, target)
    _claim(workbench, incident)
    html = _detail(app, incident)
    assert element_text(html, "run-state") == "running"
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")

    published = _submit(app, target)
    outcome = workbench.run_once(UUID(published), ScriptedInvestigator(_DbClock(store)))
    assert outcome is not None and outcome.execution == "completed"
    html = _detail(app, published)
    assert element_text(html, "run-state") == "completed"
    assert element_text(html, "concluded") == "concluded"
    _assert_in_progress(html)

    # 3 cancel: control marker cancelled, lifecycle open.
    cancelled = _submit(app, target)
    _act(app, cancelled, "cancel", 0)
    html = _detail(app, cancelled)
    assert element_text(html, "incident-control") == "cancelled"
    assert element_text(html, "run-state") == "cancelled"
    _assert_no_incident_state(html)
    words = summary_words(html)
    assert "open" in words and "closed" not in words
    _assert_list_control(_index(app), cancelled, "cancelled")

    # 4 pause of a claimed Run, then 8 resume.
    paused = _submit(app, target)
    _claim(workbench, paused)
    _act(app, paused, "pause", 0)
    html = _detail(app, paused)
    assert element_text(html, "incident-control") == "paused"
    assert element_text(html, "run-state") == "paused"
    _assert_no_incident_state(html)
    _assert_list_control(_index(app), paused, "paused")
    _act(app, paused, "resume", 1)
    assert _stored_state(store, UUID(paused)) == "running"
    assert _run_state(store, UUID(paused)) == "queued"
    html = _detail(app, paused)
    assert element_text(html, "run-state") == "queued"
    _assert_in_progress(html)
    _assert_list_control(_index(app), paused, "")

    # 8 follow_up on a fresh incident.
    noted = _submit(app, target)
    _act(app, noted, "follow_up", 0, text="check the 5xx panel")
    assert _stored_state(store, UUID(noted)) == "running"
    html = _detail(app, noted)
    assert element_text(html, "run-state") == "queued"
    _assert_in_progress(html)


def test_overdue_run_sweep_shows_waiting_human_without_control_marker():
    """Scenario 2 on the real store: the page load sweeps the overdue Run."""
    app, workbench, store = _build()
    incident = _submit(app, f"tgt-{uuid4()}")
    subject = UUID(incident)
    lease = _claim(workbench, incident)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (lease.run_id,),
        )
    html = _detail(app, incident)
    assert _run_state(store, subject) == "waiting_human"
    assert element_text(html, "run-state") == "waiting_human"
    assert element_text(html, "concluded") == "no conclusion"
    assert "open" in summary_words(html)
    assert 'id="handoff-report"' in html or 'id="report-missing"' in html
    _assert_in_progress(html)
    _assert_list_control(_index(app), incident, "")


def test_rendering_pages_does_not_change_the_stored_rows():
    app, workbench, store = _build()
    incident = _submit(app, f"tgt-{uuid4()}")
    _act(app, incident, "pause", 0)
    before = _rows(store, UUID(incident))
    for _ in range(2):
        _detail(app, incident)
        _index(app)
    assert _rows(store, UUID(incident)) == before
