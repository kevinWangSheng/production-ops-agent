"""Deterministic PR-1 implementation checks on the existing PostgreSQL lab."""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from opspilot.persistence.base import PersistenceError
from tests.integration.test_m1_incident_control_postgres import _act, _submit
from tests.integration.test_m1_web_postgres import _build, _DbClock
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


def _setup(terminal):
    app, workbench, store = _build()
    incident = UUID(_submit(app, f"f12-{uuid4()}"))
    old = store.rebuild(incident)["run"]["run_id"]
    if terminal == "completed":
        result = workbench.run_once(incident, ScriptedInvestigator(_DbClock(store)))
        assert result is not None and result.execution == "completed"
    else:
        store.control(incident, 0, "cancel", "operator")
    return app, workbench, store, incident, old


@pytest.mark.parametrize("action", ["follow_up", "correct"])
@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_terminal_note_renews_and_preserves_old_run(action, terminal):
    app, workbench, store, incident, old = _setup(terminal)
    before = store.rebuild(incident)
    generation = before["control_generation"]
    response = _act(
        app, str(incident), action, generation, text="Check payments instead"
    )
    successor = UUID(response.json()["run_id"])
    assert successor != old
    after = store.rebuild(incident)
    assert after["control_generation"] == generation + 1
    assert (
        after["conclusion"] is None
        and workbench.incidents.find_incident(incident).lifecycle == "open"
    )
    assert after["state"] == "running" and after["run"]["state"] == "queued"
    assert after["run"]["budget_limit"] == before["run"]["budget_limit"]
    assert after["run"]["versions"] == before["run"]["versions"]
    assert after["inputs"][-1]["content"]["text"] == "Check payments instead"
    history = store.investigation_history(incident)
    assert {r["run_id"] for r in history} == {old, successor}
    prior = next(row for row in history if row["run_id"] == old)
    assert prior["state"] == terminal
    assert (
        prior["started_at"] is not None
        if terminal == "completed"
        else prior["started_at"] is None
    )
    assert (
        prior["concluded_at"] is not None
        if terminal == "completed"
        else prior["concluded_at"] is None
    )
    snapshot = workbench.snapshot(incident)
    assert len(snapshot["history"]) == 1
    assert snapshot["history"][0]["verified"] is (
        True if terminal == "completed" else None
    )
    assert snapshot["controls"][-1]["decided_at"]
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200 and "Investigation history" in page.text
    assert str(old) in page.text and "Time (UTC)" in page.text
    assert "Concluded at (UTC)" in page.text
    if terminal == "completed":
        report = snapshot["history"][0]["report"]
        assert report["summary"] in page.text
        eid = report["facts"][0]["evidence_ids"][0]
        url = f"/incidents/{incident}/evidence/{eid}"
        assert url in page.text
        json_response = call(app, "GET", url, headers=basic())
        html = call(app, "GET", url, headers={**basic(), "accept": "text/html"})
        assert json_response.json()["hashes_verified"] is True
        assert html.status == 200 and html.headers["content-type"].startswith(
            "text/html"
        )
        for field in (
            eid,
            "raw_sha256",
            "view_sha256",
            "observed_at",
            "projection_revision",
        ):
            assert field in html.text
        # A later published conclusion leaves the earlier one visible.
        result = workbench.run_once(incident, ScriptedInvestigator(_DbClock(store)))
        assert result is not None and result.execution == "completed"
        assert workbench.snapshot(incident)["history"][0]["report"] == report


@pytest.mark.parametrize("action", ["follow_up", "correct"])
@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_human_owned_terminal_note_only_records_input(action, terminal):
    _, workbench, store, incident, _ = _setup(terminal)
    generation = store.rebuild(incident)["control_generation"]
    store.control(incident, generation, "takeover", "operator")
    before = store.rebuild(incident)
    generation += 1
    with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
        store.control(incident, generation, action, "operator")
    assert (
        store.control(incident, generation, action, "operator", {"text": "new fact"})
        == generation + 1
    )
    after = store.rebuild(incident)
    assert after["run"] == before["run"]
    assert after["state"] == before["state"]
    assert after["conclusion"] == before["conclusion"]
    assert workbench.incidents.find_incident(incident).mode == "human_owned"
    assert len(store.investigation_history(incident)) == 1
    assert after["inputs"][-1]["content"] == {"text": "new fact"}
    assert store.control_times(incident)[-1]["resulting_generation"] == generation + 1


@pytest.mark.parametrize("mode", ["automatic", "human_owned"])
@pytest.mark.parametrize("lifecycle", ["resolved", "closed"])
@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_closed_or_resolved_requires_reopen_before_any_write(mode, lifecycle, action):
    app, _, store, incident, _ = _setup("completed")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle=%s,mode=%s WHERE incident_id=%s",
            (lifecycle, mode, incident),
        )
    before = store.rebuild(incident)
    response = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": action,
            "expected_generation": "0",
            "idempotency_key": str(uuid4()),
            "text": "more",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 409 and response.json()["code"] == "REOPEN_REQUIRED"
    assert store.rebuild(incident) == before
    assert store.control_times(incident) == []


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_renewal_guards_and_scope_suspension(terminal):
    _, workbench, store, incident, old = _setup(terminal)
    before = store.rebuild(incident)
    generation = before["control_generation"]
    for action in ("pause", "resume", "cancel", "follow_up", "correct"):
        with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
            store.control(incident, generation, action, "operator", {"text": "more"})
    deadline = workbench.incidents.now() + timedelta(minutes=5)
    with pytest.raises(PersistenceError, match="^IDENTITY_CONFLICT$"):
        store.control(
            incident,
            generation,
            "correct",
            "operator",
            {"text": "more"},
            renew_run_id=old,
            renew_deadline=deadline,
        )
    assert store.rebuild(incident) == before
    target = store.register_target(f"scope-{uuid4()}")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET target_id=%s WHERE incident_id=%s",
            (target, incident),
        )
    store.set_target_suspension(target, True, expected_generation=0, actor="operator")
    successor = uuid4()
    supplied_input = {"test": "successor input"}
    try:
        store.control(
            incident,
            generation,
            "correct",
            "operator",
            {"text": "more"},
            renew_run_id=successor,
            renew_deadline=deadline,
            renew_input=supplied_input,
        )
        after = store.rebuild(incident)
        assert after["state"] == after["run"]["state"] == "paused"
        assert after["run"]["input"] == supplied_input
        assert (
            next(
                row
                for row in store.investigation_history(incident)
                if row["run_id"] == old
            )["state"]
            == terminal
        )
    finally:
        store.set_target_suspension(
            target, False, expected_generation=1, actor="operator"
        )


def test_history_selection_hash_mismatch_and_pruned_event():
    app, workbench, store, incident, old = _setup("completed")
    original = store.investigation_history(incident)[0]["report"]
    _act(app, str(incident), "follow_up", 0, text="more")
    with store.transaction() as conn:
        # A late conclusion is never the published historical assessment.
        conn.execute(
            "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation) VALUES(%s,%s,999,'f12-late','late_result',%s,0)",
            (
                uuid4(),
                old,
                Jsonb({"kind": "conclusion", "conclusion": {"report_content": "late"}}),
            ),
        )
    assert store.investigation_history(incident)[0]["report"] == original
    assert workbench.snapshot(incident)["history"][0]["verified"] is True
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_subject_events SET payload=jsonb_set(payload,'{report_sha256}',%s) WHERE subject_id=%s AND kind='run_completed'",
            (Jsonb("bad-hash"), incident),
        )
    assert workbench.snapshot(incident)["history"][0]["verified"] is False
    with store.transaction() as conn:
        conn.execute(
            "DELETE FROM opspilot_subject_events WHERE subject_id=%s AND kind='run_completed'",
            (incident,),
        )
    assert workbench.snapshot(incident)["history"][0]["verified"] is None


def test_renewal_identity_survives_confirmation_crash_and_later_renewal(monkeypatch):
    _, workbench, store, incident, old = _setup("completed")
    original_put = workbench.ledger.put

    def crash_confirmation(namespace, key, value):
        if namespace == "control":
            raise RuntimeError("simulated confirmation crash")
        return original_put(namespace, key, value)

    monkeypatch.setattr(workbench.ledger, "put", crash_confirmation)
    with pytest.raises(RuntimeError, match="simulated confirmation crash"):
        workbench.control(
            incident,
            actor_id="operator",
            action="follow_up",
            expected_generation=0,
            idempotency_key="crash",
            text="first question",
        )
    first = store.rebuild(incident)["run"]["run_id"]
    assert first != old
    monkeypatch.setattr(workbench.ledger, "put", original_put)
    # The store applied it, but a later Run can replace it before recovery.
    later = uuid4()
    store.control(incident, 1, "cancel", "operator")
    store.control(
        incident,
        2,
        "follow_up",
        "operator",
        {"text": "later"},
        renew_run_id=later,
        renew_deadline=workbench.incidents.now() + timedelta(minutes=5),
    )
    result = workbench.control(
        incident,
        actor_id="operator",
        action="follow_up",
        expected_generation=0,
        idempotency_key="crash",
        text="first question",
    )
    assert result.replayed is True and result.generation == 1
    assert result.run_id == str(first) and result.run_id != str(later)


def test_concurrent_pointer_change_does_not_change_decisions_run_id(monkeypatch):
    _, workbench, store, incident, _ = _setup("completed")
    original = workbench.incidents.control
    created = []

    def apply_then_replace(*args, **kwargs):
        generation = original(*args, **kwargs)
        created.append(store.rebuild(incident)["run"]["run_id"])
        store.control(incident, generation, "cancel", "other")
        store.control(
            incident,
            generation + 1,
            "follow_up",
            "other",
            {"text": "later"},
            renew_run_id=uuid4(),
            renew_deadline=workbench.incidents.now() + timedelta(minutes=5),
        )
        return generation

    monkeypatch.setattr(workbench.incidents, "control", apply_then_replace)
    result = workbench.control(
        incident,
        actor_id="operator",
        action="correct",
        expected_generation=0,
        idempotency_key="race",
        text="correction",
    )
    assert result.run_id == str(created[0])
    assert result.run_id != str(store.rebuild(incident)["run"]["run_id"])


def test_evidence_html_escapes_raw_text(monkeypatch):
    from dataclasses import replace

    app, workbench, store, incident, _ = _setup("completed")
    report = workbench.snapshot(incident)["report"]
    eid = report["facts"][0]["evidence_ids"][0]
    record = workbench.evidence_for(incident, eid)
    assert record is not None
    monkeypatch.setattr(
        workbench,
        "evidence_for",
        lambda *_: replace(record, raw=b'<script>alert("raw")</script>'),
    )
    response = call(
        app,
        "GET",
        f"/incidents/{incident}/evidence/{eid}",
        headers={**basic(), "accept": "text/html"},
    )
    assert response.status == 200
    assert '<script>alert("raw")</script>' not in response.text
    assert "&lt;script&gt;alert" in response.text


def test_history_uses_selected_conclusion_time_and_first_step_order():
    app, workbench, store, incident, old = _setup("completed")
    original = store.investigation_history(incident)[0]["report"]
    _act(app, str(incident), "follow_up", 0, text="more")
    base = workbench.incidents.now().astimezone(timezone.utc)
    early, empty_a, empty_b, empty_later_generation = [uuid4() for _ in range(4)]
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_steps SET observed_at=%s WHERE run_id=%s", (base, old)
        )
        for run_id, generation in [
            (early, 9),
            (empty_a, 7),
            (empty_b, 7),
            (empty_later_generation, 8),
        ]:
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) SELECT %s,incident_id,'queued',%s,budget_limit,deadline,versions FROM opspilot_runs WHERE run_id=%s",
                (run_id, generation, old),
            )
        for run_id, sequence, status, observed_at in [
            (early, 0, "response_committed", base - timedelta(minutes=1)),
            (old, 100, "response_committed", base + timedelta(seconds=20)),
            (old, 101, "late_result", base + timedelta(seconds=30)),
        ]:
            conn.execute(
                "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation,observed_at) VALUES(%s,%s,%s,%s,%s,%s,0,%s)",
                (
                    uuid4(),
                    run_id,
                    sequence,
                    f"time-{sequence}",
                    status,
                    Jsonb(original),
                    observed_at,
                ),
            )
    rows = workbench.incidents.investigation_history(incident)
    by_id = {row["run_id"]: row for row in rows}
    assert [row["run_id"] for row in rows[:2]] == [early, old]
    assert by_id[old]["started_at"] == base
    assert by_id[old]["concluded_at"] == base + timedelta(seconds=20)
    assert by_id[old]["report"] == original
    assert by_id[early]["started_at"] == base - timedelta(minutes=1)
    assert by_id[early]["report"] is None and by_id[early]["concluded_at"] is None
    assert all(
        by_id[run_id]["started_at"] is None and by_id[run_id]["concluded_at"] is None
        for run_id in (empty_a, empty_b, empty_later_generation)
    )
    assert [row["run_id"] for row in rows[-3:]] == [
        *sorted((empty_a, empty_b)),
        empty_later_generation,
    ]
    snapshot = workbench.snapshot(incident)
    prior = next(row for row in snapshot["history"] if row["run_id"] == str(old))
    assert prior["started_at"] == base.isoformat()
    assert prior["concluded_at"] == (base + timedelta(seconds=20)).isoformat()
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert "Concluded at (UTC)" in page.text and prior["concluded_at"] in page.text
    assert "creation time" not in page.text
    assert workbench.incidents.control_times(incident) == store.control_times(incident)
