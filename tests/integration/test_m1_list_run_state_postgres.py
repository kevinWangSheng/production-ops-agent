"""M1-01 contract tests for the incident list's authoritative Run badge."""

from __future__ import annotations

import contextlib
import os
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.loop import ModelError
from opspilot.web import DurableClock
from tests.integration.test_m1_web_postgres import _build
from tests.m1_incident_control_support import list_headers, list_row
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


def _submit(app, key: str):
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "list state",
            "idempotency_key": key,
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    return response.json()["incident_id"]


def _index(app) -> str:
    response = call(app, "GET", "/", headers=basic())
    assert response.status == 200
    return response.text


def test_list_run_column_uses_current_run_when_an_incident_has_multiple_runs():
    """Contract 1: the badge follows current_run_id across Run history.

    The public control flow can create a second Run only after cancelling the
    first, and that successor becomes current.  It therefore cannot produce
    a non-latest current Run; the two persisted Run states still differ, which
    exercises the same authoritative-pointer choice permitted by the task.
    """
    app, workbench, store = _build()
    incident = UUID(_submit(app, f"m1-list-multiple-runs-{uuid4()}"))
    first = workbench.incidents.find_incident(incident)
    assert first is not None and first.current_run_id is not None
    first_run = first.current_run_id

    cancel = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": "cancel",
            "expected_generation": "0",
            "idempotency_key": f"cancel-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert cancel.status == 200, cancel.text
    successor = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": "new_run",
            "expected_generation": "1",
            "idempotency_key": f"new-run-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert successor.status == 200, successor.text
    current = workbench.incidents.find_incident(incident)
    assert current is not None and current.current_run_id is not None
    assert current.current_run_id != first_run
    workbench.incidents.claim(
        incident,
        current.current_run_id,
        uuid4(),
        {"state": "v1"},
        600,
    )

    with store.transaction(snapshot=True) as conn:
        states = conn.execute(
            "SELECT run_id,state FROM opspilot_runs WHERE incident_id=%s",
            (incident,),
        ).fetchall()
    assert {row["run_id"]: row["state"] for row in states} == {
        first_run: "cancelled",
        current.current_run_id: "running",
    }
    assert list_row(_index(app), str(incident))["Run"] == "running"


def test_list_run_column_projects_each_current_run_state_and_empty_current_run():
    """Contract 1: GET / shows the current Run state, including no Run."""
    app, workbench, store = _build()
    queued = _submit(app, f"m1-list-queued-{uuid4()}")
    running = _submit(app, f"m1-list-running-{uuid4()}")
    waiting = _submit(app, f"m1-list-waiting-{uuid4()}")
    completed = _submit(app, f"m1-list-completed-{uuid4()}")
    cancelled = _submit(app, f"m1-list-cancelled-{uuid4()}")
    no_run = _submit(app, f"m1-list-no-run-{uuid4()}")

    running_summary = workbench.incidents.find_incident(UUID(running))
    assert running_summary is not None and running_summary.current_run_id is not None
    workbench.incidents.claim(
        UUID(running),
        running_summary.current_run_id,
        uuid4(),
        {"state": "v1"},
        600,
    )
    waiting_subject = UUID(waiting)
    waiting_result = workbench.run_once(
        waiting_subject,
        ScriptedInvestigator(
            DurableClock(store), replies=[ModelError("MODEL_UNAVAILABLE")]
        ),
    )
    assert waiting_result is not None and waiting_result.handoff is True
    completed_subject = UUID(completed)
    completed_result = workbench.run_once(
        completed_subject, ScriptedInvestigator(DurableClock(store))
    )
    assert completed_result is not None and completed_result.execution == "completed"
    cancel = post_form(
        app,
        f"/incidents/{cancelled}/control",
        {
            "action": "cancel",
            "expected_generation": "0",
            "idempotency_key": f"cancel-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert cancel.status == 200, cancel.text

    # No public intake path creates an incident without a current Run.  This
    # represents that persisted contract state solely to exercise the GET view.
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=NULL WHERE incident_id=%s",
            (UUID(no_run),),
        )

    html = _index(app)
    assert list_headers(html)[1] == "Run"
    expected = {
        queued: "queued",
        running: "running",
        waiting: "waiting_human",
        completed: "completed",
        cancelled: "cancelled",
        no_run: "",
    }
    for incident, state in expected.items():
        assert list_row(html, incident)["Run"] == state


def test_list_get_reads_authoritative_run_and_does_not_mutate_rows():
    """Contract 2: rendering GET / is a read-only projection of Run rows."""
    app, workbench, store = _build()
    incident = UUID(_submit(app, f"m1-list-readonly-{uuid4()}"))
    summary = workbench.incidents.find_incident(incident)
    assert summary is not None and summary.current_run_id is not None
    run_id = summary.current_run_id
    with store.transaction(snapshot=True) as conn:
        before = tuple(
            conn.execute(
                "SELECT current_run_id,state,control_generation,lifecycle,conclusion FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            )
            .fetchone()
            .values()
        ) + tuple(
            conn.execute(
                "SELECT run_id,state,owner,lease_until,control_generation FROM opspilot_runs WHERE run_id=%s",
                (run_id,),
            )
            .fetchone()
            .values()
        )
    _index(app)
    with store.transaction(snapshot=True) as conn:
        after = tuple(
            conn.execute(
                "SELECT current_run_id,state,control_generation,lifecycle,conclusion FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            )
            .fetchone()
            .values()
        ) + tuple(
            conn.execute(
                "SELECT run_id,state,owner,lease_until,control_generation FROM opspilot_runs WHERE run_id=%s",
                (run_id,),
            )
            .fetchone()
            .values()
        )
    assert after == before


def test_list_query_count_does_not_grow_with_incident_count():
    """Contract 3: one GET's total query count is independent of row count."""
    app, workbench, store = _build()
    original = store.transaction
    total_queries = 0

    def traced(*, snapshot=False):
        @contextlib.contextmanager
        def run():
            nonlocal total_queries
            with original(snapshot=snapshot) as conn:

                class ConnectionProxy:
                    def execute(self, *args, **kwargs):
                        nonlocal total_queries
                        total_queries += 1
                        return conn.execute(*args, **kwargs)

                    def __getattr__(self, name):
                        return getattr(conn, name)

                yield ConnectionProxy()

        return run()

    store.transaction = traced
    _submit(app, f"m1-list-count-a-{uuid4()}")
    before = total_queries
    _index(app)
    first = total_queries - before
    for _ in range(5):
        _submit(app, f"m1-list-count-{uuid4()}")
    before = total_queries
    _index(app)
    second = total_queries - before
    assert second == first


def test_list_control_column_keeps_paused_cancelled_semantics():
    """Contract 4: existing Control behavior remains independent of Run."""
    app, _, _ = _build()
    paused = _submit(app, f"m1-list-paused-{uuid4()}")
    cancelled = _submit(app, f"m1-list-control-cancelled-{uuid4()}")
    for incident, action in ((paused, "pause"), (cancelled, "cancel")):
        response = post_form(
            app,
            f"/incidents/{incident}/control",
            {
                "action": action,
                "expected_generation": "0",
                "idempotency_key": f"{action}-{uuid4()}",
            },
            headers={**basic(), **same_origin()},
        )
        assert response.status == 200, response.text
    html = _index(app)
    assert list_row(html, paused)["Control"] == "paused"
    assert list_row(html, cancelled)["Control"] == "cancelled"


def test_list_run_column_fails_closed_for_a_run_from_another_incident():
    """A mismatched current_run_id must not leak another incident's state."""
    app, workbench, store = _build()
    first = UUID(_submit(app, f"m1-list-mismatched-run-first-{uuid4()}"))
    second = UUID(_submit(app, f"m1-list-mismatched-run-second-{uuid4()}"))
    second_summary = workbench.incidents.find_incident(second)
    assert second_summary is not None and second_summary.current_run_id is not None

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (second_summary.current_run_id, first),
        )

    assert list_row(_index(app), str(first))["Run"] == ""
