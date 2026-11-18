"""M1-01 incident control projection on the durable store (C3 §4 line 86).

Same contract as ``tests/test_m1_incident_control_contract.py``, driven on
PostgreSQL where global/target suspension exist (scenarios 5-7 of the task
record's table) and where the stored rows can be compared before and after a
page render. Actions go through ``POST /incidents/{id}/control``,
``DurableStore`` suspension/claim/publish calls and the workbench; pages are
read through ``GET /`` and ``GET /incidents/{id}``.
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest

from opspilot.web.incident_list import status_of
from opspilot.web.incident_view import page_view
from opspilot.web.service import available_actions
from tests.integration.test_m1_web_postgres import _build, _DbClock
from tests.m1_incident_control_support import (
    list_headers,
    list_row,
)
from tests.m1_investigation_support import ModelError, reply, report_json, tool_call
from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    call,
    post_form,
    same_origin,
)

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
    response = call(app, "GET", "/?tab=all", headers=basic())
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


def _assert_list_control(html, incident, expected):
    headers = list_headers(html)
    assert "Control" in headers and "State" not in headers
    row = list_row(html, incident)
    assert row["Control"] == expected


class _Suspension:
    """Public-API suspension of one scope, always released on exit."""

    def __init__(self, store, kind, target_name):
        self.store, self.kind = store, kind
        self.target = store.register_target(target_name)
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
    assert _detail(app, incident)
    _assert_list_control(_index(app), incident, "paused")


def test_suspension_of_a_claimed_run_shows_control_paused(scope):
    app, workbench, store, target, suspension = scope
    incident = _submit(app, target)
    _claim(workbench, incident)
    suspension.suspend()
    assert _detail(app, incident)
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
    assert _detail(app, incident)
    _assert_list_control(_index(app), incident, "")


# -- 7. release keeps paused ---------------------------------------------------


def test_releasing_a_suspension_leaves_control_paused_until_a_human_resumes(scope):
    app, workbench, store, target, suspension = scope
    incident = _submit(app, target)
    suspension.suspend()
    suspension.release()
    assert _stored_state(store, UUID(incident)) == "paused"
    assert _detail(app, incident)
    _assert_list_control(_index(app), incident, "paused")
    # A human resume is what returns it to progress.
    generation = workbench.list_incidents()[0].control_generation
    _act(app, incident, "resume", generation)
    assert _detail(app, incident)
    _assert_list_control(_index(app), incident, "")


# -- the remaining rows on the real store --------------------------------------


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
    assert _detail(app, incident)
    assert _run_state(store, subject) == "waiting_human"
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


# -- redesign A8: one status rule for the page header and the list tabs ---------


#: (lifecycle, incident state, mode, current Run state, concluded, merged):
#: every A8 rule and the ties between neighbouring rules.
_A8_ROWS = (
    ("closed", "queued", "automatic", "completed", True, True),  # merged
    ("closed", "cancelled", "automatic", "cancelled", False, False),  # closed
    ("resolved", "paused", "automatic", "completed", True, False),  # resolved
    ("open", "cancelled", "automatic", "cancelled", False, False),  # canceled
    ("open", "paused", "human_owned", "paused", False, False),  # paused
    ("open", "queued", "automatic", "paused", False, False),  # suspended Run
    ("open", "queued", "human_owned", "waiting_human", True, False),  # human
    ("open", "queued", "automatic", "queued", True, False),  # investigating
    ("observing_recovery", "running", "automatic", "running", True, False),
    ("observing_recovery", "queued", "automatic", "completed", True, False),
    ("open", "queued", "automatic", "completed", True, False),  # root cause
    ("observing_recovery", "queued", "automatic", "waiting_human", False, False),
    ("open", "queued", "automatic", "waiting_human", False, False),
    ("open", "queued", "automatic", "blocked", False, False),
)


def test_list_tab_sql_matches_the_shared_status_rule_for_every_a8_row():
    app, workbench, store = _build()
    target = f"tgt-{uuid4()}"
    survivor = UUID(_submit(app, target))
    seeded = []
    for lifecycle, state, mode, run_state, concluded, merged in _A8_ROWS:
        incident = UUID(_submit(app, target))
        with store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_runs SET state=%s WHERE run_id="
                "(SELECT current_run_id FROM opspilot_incidents WHERE incident_id=%s)",
                (run_state, incident),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle=%s,state=%s,mode=%s,"
                'conclusion=CASE WHEN %s THEN \'{"kind":"conclusion"}\'::jsonb END,'
                "merged_into_incident_id=CASE WHEN %s THEN %s::uuid END"
                " WHERE incident_id=%s",
                (lifecycle, state, mode, concluded, merged, survivor, incident),
            )
        seeded.append(incident)
    groups = workbench.incidents.page_groups()
    items = {
        item.summary.incident_id: item
        for item in workbench.incidents.list_incident_items(limit=100_000)
    }
    seen = {}
    for incident in seeded:
        status = status_of(items[incident])
        seen[status["status"].key] = True
        assert groups[incident] == status["group"], (incident, status)
    # Every A8 outcome was produced by at least one seeded row.
    assert set(seen) == {
        "merged",
        "closed",
        "resolved",
        "canceled",
        "paused",
        "human_owned",
        "investigating",
        "watching",
        "root_cause",
        "needs_attention",
    }


def test_an_earlier_incomplete_report_stays_shown_after_a_later_run_without_one():
    """A8/D7a look-back: Run 1 ends with a validated report the Agent marked
    incomplete; Run 2 ends with none. The page keeps Run 1's report as
    Incomplete and the message box stays open (D7a, not D7b)."""

    def incomplete(call):
        evidence = None
        for message in reversed(call.messages):
            if message.get("role") == "tool":
                evidence = json.loads(message["content"])["evidence_id"]
                break
        return reply(content=report_json(evidence_id=evidence, status="incomplete"))

    app, workbench, store = _build()
    incident = _submit(app, f"tgt-{uuid4()}")
    subject = UUID(incident)
    first = ScriptedInvestigator(_DbClock(store))
    first.replies = [
        reply(tool_calls=[tool_call()], finish="tool_calls"),
        incomplete,
    ]
    workbench.run_once(subject, first)
    snapshot = workbench.snapshot(subject)
    assert snapshot["outcome"]["reasons"] == ["INCOMPLETE_INVESTIGATION"]
    view = page_view(snapshot, None, available_actions(snapshot))
    assert view["report"]["kind"] == "incomplete"

    # Run 2 (cancel, then a new Run) ends without any report.
    _act(app, incident, "cancel", 0)
    _act(app, incident, "new_run", 1)
    workbench.run_once(
        subject,
        ScriptedInvestigator(
            _DbClock(store), replies=[ModelError("MODEL_UNAVAILABLE")]
        ),
    )
    snapshot = workbench.snapshot(subject)
    assert snapshot["report"] is None
    assert snapshot["outcome"]["reasons"] != ["INCOMPLETE_INVESTIGATION"]
    view = page_view(snapshot, None, available_actions(snapshot))
    assert view["status"].key == "needs_attention"
    assert view["report"] is not None and view["report"]["kind"] == "incomplete"
    assert view["report"]["summary"] == (
        "Visible evidence is insufficient to support a cause."
    )
    assert view["composer"]["accepts"] is True
    # A9: the earlier Run's report keeps that Run's evidence on the page.
    (fact,) = view["report"]["facts"]
    assert fact["refs"] and fact["refs"][0]["call"] is not None

    # D12: a message taken while Run 1 of another incident was queued, then
    # that Run ends without reading it or any validated report (D7b).
    other = _submit(app, f"tgt-{uuid4()}")
    taken = post_form(
        app,
        f"/incidents/{other}/control",
        {
            "action": "message",
            "expected_generation": "0",
            "idempotency_key": f"msg-{uuid4()}",
            "text": "check the payment DB pool",
        },
        headers={**basic(), **same_origin()},
    )
    assert taken.status == 200, taken.text
    workbench.run_once(
        UUID(other),
        ScriptedInvestigator(
            _DbClock(store), replies=[ModelError("MODEL_UNAVAILABLE")]
        ),
    )
    snapshot = workbench.snapshot(UUID(other))
    view = page_view(
        snapshot, None, available_actions(snapshot), message_available=True
    )
    (said,) = [e for e in view["log"]["entries"] if e["kind"] == "human"]
    assert said["status"].startswith("Not read — the investigation ended")

    # D8: a human-owned incident that is also paused takes a note, never a
    # promise that the Agent reads it after resume.
    owned = _submit(app, f"tgt-{uuid4()}")
    _act(app, owned, "takeover", 0)
    _act(app, owned, "pause", 1)
    snapshot = workbench.snapshot(UUID(owned))
    view = page_view(
        snapshot, None, available_actions(snapshot), message_available=True
    )
    assert view["status"].key == "paused"
    assert view["composer"]["label"] == "Add a note"
    assert "not investigating" in view["composer"]["hint"]
