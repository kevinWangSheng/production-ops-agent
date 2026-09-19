"""Workbench entry points: authentication boundary, idempotent intake, SSE cursor
resume, human-control generations, and one end-to-end deterministic Run.

Everything runs against the in-memory doubles; ``tests/integration`` repeats
the end-to-end flow on PostgreSQL. No model HTTP, no network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from opspilot.web import MemoryEventLog
from tests.m1_investigation_support import (
    reply,
    report_from_transcript,
    report_json,
    tool_call,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    UI_PASSWORD,
    MemoryIncidentStore,
    ScriptedInvestigator,
    basic,
    bearer,
    build_workbench,
    call,
    note_reached_investigation,
    post_form,
    post_json,
    same_origin,
    stream,
    submit_incident,
)

# -- authentication boundary ---------------------------------------------------


def test_pages_refuse_missing_credentials_without_revealing_incidents():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    for path in (
        "/",
        f"/incidents/{incident}",
        f"/incidents/{incident}/events",
        f"/incidents/{incident}/evidence/any",
    ):
        response = call(app, "GET", path)
        assert response.status == 401, path
        assert response.json() == {"code": "MISSING_CREDENTIALS"}
        assert response.headers["www-authenticate"].startswith("Basic")
    # A forged proxy identity header is not a credential.
    response = call(app, "GET", "/", headers={"x-forwarded-user": "alice"})
    assert response.status == 401


def test_wrong_password_and_unknown_user_are_refused_without_echo():
    app, _, _ = build_workbench()
    for headers in (basic(password="not it"), basic(user="mallory")):
        response = call(app, "GET", "/", headers=headers)
        assert response.status == 401
        assert response.json() == {"code": "INVALID_CREDENTIALS"}
        assert "not it" not in response.text and UI_PASSWORD not in response.text


def test_each_entry_point_accepts_only_its_own_channel():
    app, _, _ = build_workbench()
    # Event token on the UI channel: refused before anything is parsed.
    response = call(app, "GET", "/", headers=bearer())
    assert response.status == 401
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": "t", "question": "q", "idempotency_key": "k"},
        headers={**bearer(), **same_origin()},
    )
    assert response.status == 401
    # Basic credentials on the event channel: refused too.
    response = post_json(
        app,
        "/intake/events",
        {
            "source": "alertmanager",
            "external_event_id": "e-1",
            "target_id": "t",
            "question": "q",
        },
        headers=basic(),
    )
    assert response.status == 401
    assert "www-authenticate" not in response.headers
    assert EVENT_TOKEN not in response.text
    # Wrong bearer value.
    response = post_json(
        app,
        "/intake/events",
        {
            "source": "alertmanager",
            "external_event_id": "e-1",
            "target_id": "t",
            "question": "q",
        },
        headers=bearer("some-other-token"),
    )
    assert response.status == 401
    assert response.json() == {"code": "INVALID_CREDENTIALS"}


def test_ui_mutations_require_a_trusted_origin():
    app, workbench, _ = build_workbench()
    fields = {"target_id": "checkout-prod", "question": "why", "idempotency_key": "k1"}
    response = post_form(app, "/intake/ui", fields, headers=basic())
    assert response.status == 403 and response.json() == {"code": "ORIGIN_REJECTED"}
    response = post_form(
        app, "/intake/ui", fields, headers={**basic(), "origin": "https://evil.example"}
    )
    assert response.status == 403
    assert workbench.list_incidents() == ()
    # Same-origin fetch metadata is enough when the browser omits Origin.
    response = post_form(
        app, "/intake/ui", fields, headers={**basic(), "sec-fetch-site": "same-origin"}
    )
    assert response.status == 201


# -- idempotent intake ---------------------------------------------------------


def test_ui_intake_replays_the_same_request_and_refuses_a_reused_key():
    app, workbench, _ = build_workbench()
    first = submit_incident(app, key="k-1")
    assert first.status == 201
    assert first.headers["location"] == f"/incidents/{first.json()['incident_id']}"
    again = submit_incident(app, key="k-1")
    assert again.status == 200
    assert again.json()["incident_id"] == first.json()["incident_id"]
    assert again.json()["replayed"] is True
    conflict = submit_incident(app, key="k-1", question="A different question")
    assert conflict.status == 409
    assert conflict.json() == {"code": "INTAKE_KEY_CONFLICT"}
    assert len(workbench.list_incidents()) == 1
    events = workbench.events.read_after(
        first.json()["incident_id"] and workbench.list_incidents()[0].incident_id, 0
    )
    assert [e.kind for e in events] == ["intake_accepted"]


def test_event_intake_deduplicates_on_the_delivery_key():
    app, workbench, _ = build_workbench()
    payload = {
        "source": "alertmanager",
        "external_event_id": "alert-77",
        "target_id": "checkout-prod",
        "question": "CheckoutErrorRateHigh firing",
    }
    first = post_json(app, "/intake/events", payload, headers=bearer())
    assert first.status == 201
    assert first.json()["delivery_key"].startswith("evt:")
    second = post_json(app, "/intake/events", payload, headers=bearer())
    assert second.status == 200 and second.json()["replayed"] is True
    assert second.json()["incident_id"] == first.json()["incident_id"]
    # A resource name alone is not a delivery key (C3 section 6).
    bad = post_json(
        app,
        "/intake/events",
        {
            "source": "alertmanager",
            "object_identity": "checkout",
            "target_id": "t",
            "question": "q",
        },
        headers=bearer(),
    )
    assert bad.status == 400 and bad.json() == {"code": "INVALID_DELIVERY_KEY"}
    assert len(workbench.list_incidents()) == 1
    summary = workbench.list_incidents()[0]
    events = workbench.events.read_after(summary.incident_id, 0)
    assert events[0].payload["channel"] == "event_token"
    assert events[0].payload["actor_id"] == "alertmanager-prod"


def test_intake_rejects_bad_bodies_without_echoing_them():
    app, _, _ = build_workbench()
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": "with space", "question": "q", "idempotency_key": "k"},
        headers={**basic(), **same_origin()},
    )
    assert response.status == 400 and response.json() == {"code": "INVALID_INPUT"}
    assert "with space" not in response.text
    response = call(
        app,
        "POST",
        "/intake/ui",
        headers={**basic(), **same_origin(), "content-type": "application/json"},
        body=b"{}",
    )
    assert response.status == 415
    response = call(
        app,
        "POST",
        "/intake/events",
        headers={**bearer(), "content-type": "application/json"},
        body=b"not json",
    )
    assert response.status == 400 and response.json() == {"code": "INVALID_JSON"}


# -- human control ---------------------------------------------------------------


def _renew(app, incident, *, generation):
    """cancel + new_run from ``generation``; returns the new generation."""
    cancelled = _control(
        app,
        incident,
        {
            "action": "cancel",
            "expected_generation": str(generation),
            "idempotency_key": f"cancel-{generation}",
        },
    )
    assert cancelled.status == 200
    renewed = _control(
        app,
        incident,
        {
            "action": "new_run",
            "expected_generation": str(generation + 1),
            "idempotency_key": f"new-run-{generation}",
        },
    )
    assert renewed.status == 200
    return renewed.json()["generation"]


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def test_control_increments_the_generation_once_per_accepted_action():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    response = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "fu-1",
            "text": "Also check the 5xx panel.",
        },
    )
    assert response.status == 200
    assert response.json()["generation"] == 1 and response.json()["replayed"] is False
    replay = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "fu-1",
            "text": "Also check the 5xx panel.",
        },
    )
    assert replay.status == 200
    assert replay.json()["generation"] == 1 and replay.json()["replayed"] is True
    stale = _control(
        app,
        incident,
        {
            "action": "correct",
            "expected_generation": "0",
            "idempotency_key": "c-1",
            "text": "The target is checkout-canary.",
        },
    )
    assert stale.status == 409
    assert stale.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    reused = _control(
        app,
        incident,
        {
            "action": "cancel",
            "expected_generation": "1",
            "idempotency_key": "fu-1",
        },
    )
    assert reused.status == 409 and reused.json() == {"code": "CONTROL_KEY_CONFLICT"}
    snapshot = workbench.snapshot(workbench.list_incidents()[0].incident_id)
    assert snapshot["incident"].control_generation == 1
    assert [c["action"] for c in snapshot["controls"]] == ["follow_up"]
    assert snapshot["controls"][0]["text"] == "Also check the 5xx panel."
    assert snapshot["controls"][0]["actor_id"] == "alice"
    audit = workbench.incidents.controls
    assert [(a["action"], a["expected"], a["resulting"]) for a in audit] == [
        ("follow_up", 0, 1)
    ]


def test_pause_blocks_follow_up_until_resume_and_cancel_allows_a_new_run():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    paused = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p"},
    )
    assert paused.json()["generation"] == 1
    # PR #31 semantics: a bare pause while paused is refused, but a note with
    # content is recorded as an input without resuming the run.
    blocked = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "1", "idempotency_key": "p2"},
    )
    assert blocked.status == 409 and blocked.json() == {"code": "ILLEGAL_TRANSITION"}
    noted = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "1",
            "idempotency_key": "f",
            "text": "still?",
        },
    )
    assert noted.status == 200 and noted.json()["generation"] == 2
    assert workbench.list_incidents()[0].state == "paused"
    assert workbench.incidents.inputs[-1]["content"]["text"] == "still?"
    resumed = _control(
        app,
        incident,
        {"action": "resume", "expected_generation": "2", "idempotency_key": "r"},
    )
    assert resumed.json()["generation"] == 3
    cancelled = _control(
        app,
        incident,
        {"action": "cancel", "expected_generation": "3", "idempotency_key": "x"},
    )
    assert cancelled.json()["generation"] == 4
    summary = workbench.list_incidents()[0]
    old_run = summary.current_run_id
    assert summary.state == "cancelled"
    renewed = _control(
        app,
        incident,
        {"action": "new_run", "expected_generation": "4", "idempotency_key": "n"},
    )
    assert renewed.status == 200 and renewed.json()["generation"] == 5
    summary = workbench.list_incidents()[0]
    assert summary.state == "queued" and summary.current_run_id != old_run
    assert workbench.incidents.runs[old_run]["state"] == "cancelled"
    # Validation: text is required for follow_up and forbidden elsewhere.
    assert _control(
        app,
        incident,
        {"action": "follow_up", "expected_generation": "5", "idempotency_key": "t1"},
    ).json() == {"code": "TEXT_REQUIRED"}
    assert _control(
        app,
        incident,
        {
            "action": "pause",
            "expected_generation": "5",
            "idempotency_key": "t2",
            "text": "x",
        },
    ).json() == {"code": "TEXT_NOT_ALLOWED"}
    assert (
        _control(
            app,
            incident,
            {"action": "takeover", "expected_generation": "5", "idempotency_key": "t3"},
        ).status
        == 400
    )
    assert workbench.list_incidents()[0].control_generation == 5


# -- SSE cursor resume -------------------------------------------------------------


def test_sse_replays_from_the_cursor_and_asks_for_reload_when_too_old():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "note one",
        },
    )
    _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "1", "idempotency_key": "p"},
    )
    full = stream(app, f"/incidents/{incident}/events", headers=basic(), until_events=3)
    assert full.status == 200
    assert full.headers["content-type"].startswith("text/event-stream")
    assert full.text.startswith("retry: 2000\n\n")
    events = full.sse_events()
    assert [(i, k) for i, k, _ in events] == [
        ("1", "intake_accepted"),
        ("2", "control_applied"),
        ("3", "control_applied"),
    ]
    assert events[1][2]["text"] == "note one"
    resumed = stream(
        app, f"/incidents/{incident}/events?cursor=2", headers=basic(), until_events=1
    )
    assert [(i, k) for i, k, _ in resumed.sse_events()] == [("3", "control_applied")]
    by_header = stream(
        app,
        f"/incidents/{incident}/events",
        headers={**basic(), "last-event-id": "1"},
        until_events=2,
    )
    assert [i for i, _, _ in by_header.sse_events()] == ["2", "3"]
    # Nothing new: the stream idles out and closes; the browser reconnects with its id.
    idle = call(app, "GET", f"/incidents/{incident}/events?cursor=3", headers=basic())
    assert idle.status == 200 and idle.sse_events() == [] and ": idle" in idle.text
    # Retention moved past the cursor: reload instead of a gapped stream.
    log = workbench.events
    assert isinstance(log, MemoryEventLog)
    log.prune_before(workbench.list_incidents()[0].incident_id, 3)
    reload = call(app, "GET", f"/incidents/{incident}/events?cursor=1", headers=basic())
    assert reload.sse_events() == [
        (None, "reload", {"reason": "CURSOR_TOO_OLD", "cursor": 1})
    ]
    assert (
        call(
            app, "GET", f"/incidents/{incident}/events?cursor=x", headers=basic()
        ).status
        == 400
    )
    assert (
        call(app, "GET", "/incidents/not-a-uuid/events", headers=basic()).status == 404
    )


# -- end to end ------------------------------------------------------------------


def test_submitted_incident_is_investigated_and_the_report_is_shown_with_evidence():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="e2e-1").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    investigator = ScriptedInvestigator(clock)
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "completed"
    assert outcome.handoff is False and outcome.report is not None
    assert investigator.contexts[0].question == "Why is checkout erroring?"
    assert investigator.contexts[0].target_id == "checkout-prod"

    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert kinds == [
        "intake_accepted",
        "run_claimed",
        "step_committed",
        "tool_committed",
        "step_committed",
        "run_completed",
    ]
    completed = workbench.events.read_after(subject, 5)[0].payload
    assert completed["published"] is True and completed["evidence_ids"]
    assert completed["report_sha256"] == outcome.report_content_sha256

    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert 'id="run-state">completed' in page.text
    assert 'id="concluded">concluded' in page.text
    assert workbench.list_incidents()[0].concluded is True
    assert "Facts (1)" in page.text and "Hypotheses (0)" in page.text
    assert (
        "Counter-evidence (0)" in page.text and "Rejected hypotheses (0)" in page.text
    )
    assert (
        "Unknown / gaps (1)" in page.text
        and "No HealthProfile was supplied." in page.text
    )
    evidence_id = completed["evidence_ids"][0]
    assert f"/incidents/{incident}/evidence/{evidence_id}" in page.text
    assert outcome.report_content_sha256 in page.text

    record = call(
        app, "GET", f"/incidents/{incident}/evidence/{evidence_id}", headers=basic()
    )
    assert record.status == 200
    body = record.json()
    assert body["hashes_verified"] is True
    assert json.loads(body["raw_utf8"]) == {
        "data": {"result": [{"metric": "checkout", "value": 3}]}
    }
    assert body["view"]["evidence_id"] == evidence_id and body["view"]["content"]
    assert body["raw_sha256"] != body["view_sha256"]
    assert body["status"] == "ok" and body["adopted"] is True
    assert "transport-only-marker" not in record.text

    # Evidence is scoped to the incident that produced it.
    other = submit_incident(app, key="e2e-2").json()["incident_id"]
    assert (
        call(
            app, "GET", f"/incidents/{other}/evidence/{evidence_id}", headers=basic()
        ).status
        == 404
    )
    listing = call(app, "GET", "/", headers=basic())
    assert listing.status == 200 and incident in listing.text and other in listing.text
    # Concluded incidents accept no further control.
    assert (
        _control(
            app,
            incident,
            {
                "action": "follow_up",
                "expected_generation": "0",
                "idempotency_key": "late",
                "text": "more?",
            },
        ).status
        == 409
    )


def test_incomplete_report_is_a_handoff_not_a_conclusion_and_control_stays_open():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="inc-1").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id

    def incomplete(call_):
        evidence = None
        for message in reversed(call_.messages):
            if message.get("role") == "tool":
                evidence = json.loads(message["content"])["evidence_id"]
                break
        return reply(content=report_json(evidence_id=evidence, status="incomplete"))

    investigator = ScriptedInvestigator(
        clock,
        replies=[reply(tool_calls=[tool_call()], finish="tool_calls"), incomplete],
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "completed"
    assert outcome.handoff is True
    assert outcome.handoff_reasons == ("INCOMPLETE_INVESTIGATION",)
    # Not published: the incident has no conclusion and human control is open.
    snapshot = workbench.snapshot(subject)
    assert snapshot["report"] is None and snapshot["incident"].concluded is False
    assert snapshot["handoff_report"]["incomplete"] is True
    last = workbench.events.read_after(subject, 0)[-1]
    assert last.kind == "run_handoff"
    assert last.payload["reasons"] == ["INCOMPLETE_INVESTIGATION"]
    assert last.payload["report_sha256"] == outcome.report_content_sha256
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert 'id="handoff-report"' in page.text
    assert "Incomplete investigation, not an uncertain finding." in page.text
    assert 'conclusion <span class="badge">inconclusive' in page.text
    follow = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "after-incomplete",
            "text": "Also check the dependency.",
        },
    )
    assert follow.status == 200 and follow.json()["generation"] == 1
    # On this branch the loop reuses its round keys, so continuing the same
    # Run after a note is fenced (PR #31 adds per-attempt keys); the
    # supported continuation is cancel + new_run, which reads the note
    # from the durable ledger.
    _renew(app, incident, generation=1)
    again = ScriptedInvestigator(clock)
    outcome = workbench.run_once(subject, again)
    assert outcome is not None and outcome.execution == "completed"
    assert note_reached_investigation(workbench, again, "Also check the dependency.")
    assert workbench.snapshot(subject)["report"]["facts"]


def test_control_replay_requires_identical_intent():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    first = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "shared",
            "text": "note A",
        },
    )
    assert first.status == 200 and first.json()["generation"] == 1
    other_text = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "shared",
            "text": "note B",
        },
    )
    assert other_text.status == 409
    assert other_text.json() == {"code": "CONTROL_KEY_CONFLICT"}
    other_generation = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "1",
            "idempotency_key": "shared",
            "text": "note A",
        },
    )
    assert other_generation.status == 409
    assert workbench.list_incidents()[0].control_generation == 1
    # The page hands out a fresh key per render, never the same one twice.
    one = call(app, "GET", f"/incidents/{incident}", headers=basic()).text
    two = call(app, "GET", f"/incidents/{incident}", headers=basic()).text

    def key_of(html):
        return html.split('name="idempotency_key" value="')[1].split('"')[0]

    assert key_of(one) != key_of(two)


def test_snapshot_survives_event_retention_and_unmapped_storage_errors():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "kept in the ledger",
        },
    )
    log = workbench.events
    assert isinstance(log, MemoryEventLog)
    log.prune_before(subject, 3)
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert (
        "kept in the ledger" in page.text and "Why is checkout erroring?" in page.text
    )
    assert workbench.snapshot(subject)["latest_sequence"] == 2
    _renew(app, incident, generation=1)
    fresh = ScriptedInvestigator(clock)
    outcome = workbench.run_once(subject, fresh)
    assert outcome is not None and outcome.execution == "completed"
    assert note_reached_investigation(workbench, fresh, "kept in the ledger")
    # A storage refusal surfaces as a coded 503, not a bare 500.
    from opspilot.persistence import PersistenceError

    def unavailable(*_a, **_k):
        raise PersistenceError("STORAGE_UNAVAILABLE")

    workbench.incidents.list_incidents = unavailable
    down = call(app, "GET", "/", headers=basic())
    assert down.status == 503 and down.json() == {"code": "STORAGE_UNAVAILABLE"}


def test_an_unexpected_investigator_error_releases_the_lease_and_hands_off():
    app, workbench, clock = build_workbench()
    submit_incident(app, key="boom")
    subject = workbench.list_incidents()[0].incident_id

    class Exploding:
        def investigate(self, context, committer, evidence):
            raise RuntimeError("investigator bug")

    import pytest

    with pytest.raises(RuntimeError):
        workbench.run_once(subject, Exploding())
    last = workbench.events.read_after(subject, 0)[-1]
    assert last.kind == "run_handoff" and last.payload["reasons"] == [
        "UNEXPECTED_ERROR"
    ]
    run = workbench.incidents.runs[workbench.list_incidents()[0].current_run_id]
    assert run["owner"] is None and run["lease_until"] is None
    # Re-claimable immediately: no LEASE_ACTIVE.
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock))
    assert outcome is not None and outcome.execution == "completed"


def test_a_pause_during_the_run_hands_off_and_publishes_nothing():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="pause-1").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id

    def pause_then_answer(call_):
        workbench.control(
            subject,
            actor_id="alice",
            action="pause",
            expected_generation=0,
            idempotency_key="mid-run-pause",
        )
        return report_from_transcript(call_)

    investigator = ScriptedInvestigator(
        clock,
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            pause_then_answer,
        ],
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None
    assert outcome.execution == "failed" and outcome.handoff_reasons == (
        "CONTROL_DENIED",
    )
    snapshot = workbench.snapshot(subject)
    assert snapshot["report"] is None and snapshot["incident"].state == "paused"
    assert snapshot["outcome"]["execution"] == "failed"
    assert [s["status"] for s in snapshot["steps"]] == [
        "tool_result_committed",
        "late_result",
    ]
    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert kinds[-2:] == ["control_applied", "run_handoff"]
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert 'id="report-missing"' in page.text and "history only" in page.text


def test_a_cancelled_incident_cannot_be_claimed():
    app, workbench, clock = build_workbench()
    submit_incident(app, key="cancel-1")
    subject = workbench.list_incidents()[0].incident_id
    workbench.control(
        subject,
        actor_id="alice",
        action="cancel",
        expected_generation=0,
        idempotency_key="c",
    )
    assert workbench.run_once(subject, ScriptedInvestigator(clock)) is None
    last = workbench.events.read_after(subject, 0)[-1]
    assert last.kind == "run_claim_refused" and last.payload["code"] == "CONTROL_DENIED"
    assert workbench.snapshot(subject)["report"] is None


def test_an_applied_but_unconfirmed_control_is_reconciled_from_the_audit():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="unconfirmed").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    real_put = workbench.ledger.put
    calls = {"n": 0}

    def flaky_put(namespace, key, value):
        if namespace == "control":
            calls["n"] += 1
            if calls["n"] == 1:
                from opspilot.persistence import PersistenceError

                raise PersistenceError("STORAGE_UNAVAILABLE")
        return real_put(namespace, key, value)

    workbench.ledger.put = flaky_put
    fields = {
        "action": "follow_up",
        "expected_generation": "0",
        "idempotency_key": "note-1",
        "text": "The dependency was restarted at 09:00.",
    }
    first = _control(app, incident, fields)
    assert first.status == 503
    assert workbench.list_incidents()[0].control_generation == 1
    retry = _control(app, incident, fields)
    assert retry.status == 200
    assert retry.json() == {
        "incident_id": incident,
        "action": "follow_up",
        "generation": 1,
        "replayed": True,
        "sequence": retry.json()["sequence"],
    }
    _renew(app, incident, generation=1)
    investigator = ScriptedInvestigator(clock)
    assert workbench.run_once(subject, investigator).execution == "completed"
    assert note_reached_investigation(
        workbench, investigator, "The dependency was restarted at 09:00."
    )
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert "restarted at 09:00" in page.text


def test_a_refused_control_does_not_poison_its_idempotency_key():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    stale = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "5", "idempotency_key": "p"},
    )
    assert stale.status == 409 and stale.json()["code"] == "CONTROL_CONFLICT"
    fixed = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p"},
    )
    assert fixed.status == 200 and fixed.json()["generation"] == 1


def test_a_stale_form_with_a_fresh_key_is_refused_not_reconciled():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    ok = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "k1",
            "text": "X",
        },
    )
    assert ok.status == 200 and ok.json()["generation"] == 1
    stale = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "k2",
            "text": "Y",
        },
    )
    assert stale.status == 409
    assert stale.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    notes = [n["text"] for n in workbench.snapshot(subject)["controls"]]
    assert notes == ["X"]
    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert kinds.count("control_applied") == 1
    assert workbench.ledger.get("control_intent", f"{subject}:k2") is None


def test_a_note_applied_after_the_claim_fences_the_attempt_instead_of_being_ignored():
    """P1 race: notes must be read under the lease, never before the claim.

    A correction that lands after the claim advances the generation; the
    attempt then must not publish. The renewed attempt sees the note.
    """
    app, workbench, clock = build_workbench()
    submit_incident(app, key="race-1")
    subject = workbench.list_incidents()[0].incident_id

    class CorrectThenInvestigate(ScriptedInvestigator):
        def investigate(self, context, committer, evidence):
            # Lease is held here; the operator corrects the target now.
            workbench.control(
                subject,
                actor_id="alice",
                action="correct",
                expected_generation=context.control_generation,
                idempotency_key="race-correct",
                text="The target is checkout-canary, not checkout-prod.",
            )
            return super().investigate(context, committer, evidence)

    racing = CorrectThenInvestigate(clock)
    outcome = workbench.run_once(subject, racing)
    assert outcome is not None and outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    snapshot = workbench.snapshot(subject)
    assert snapshot["report"] is None and snapshot["incident"].concluded is False
    assert [c["action"] for c in snapshot["controls"]] == ["correct"]
    # The attempt that ran with the stale view never published; the next
    # attempt (fresh Run) receives the correction.
    _renew(app, str(subject), generation=1)
    fresh = ScriptedInvestigator(clock)
    outcome = workbench.run_once(subject, fresh)
    assert outcome is not None and outcome.execution == "completed"
    assert note_reached_investigation(
        workbench, fresh, "The target is checkout-canary, not checkout-prod."
    )


def test_openapi_schema_is_not_served():
    app, _, _ = build_workbench()
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert call(app, "GET", path).status in (401, 404), path
        assert call(app, "GET", path, headers=basic()).status == 404, path


def test_a_held_lease_is_a_quiet_refusal_not_an_event_per_poll():
    app, workbench, clock = build_workbench()
    submit_incident(app, key="held")
    subject = workbench.list_incidents()[0].incident_id
    summary = workbench.list_incidents()[0]
    other = workbench.incidents.claim(
        subject, summary.current_run_id, uuid4(), {"state": "v1"}, 30
    )
    assert other.epoch == 1
    before = workbench.events.latest(subject)
    for _ in range(3):
        assert workbench.run_once(subject, ScriptedInvestigator(clock)) is None
    assert workbench.events.latest(subject) == before
    # A refusal that needs a human (version mismatch) is still recorded.
    workbench.incidents.abandon(other)
    workbench.run_versions = {"state": "v2"}
    assert workbench.run_once(subject, ScriptedInvestigator(clock)) is None
    last = workbench.events.read_after(subject, 0)[-1]
    assert last.kind == "run_claim_refused"
    assert last.payload["code"] == "INCOMPATIBLE_STATE"


# -- lease renewal (PR #35 capability, optional on this base) ------------------


def test_default_lease_is_short_so_takeover_after_a_hard_kill_waits_lease_not_wall():
    from opspilot.web.service import LEASE_SECONDS

    app, workbench, clock = build_workbench()
    assert LEASE_SECONDS < workbench.run_seconds
    submit_incident(app, key="kill")
    subject = workbench.list_incidents()[0].incident_id
    run_id = workbench.list_incidents()[0].current_run_id
    granted: list = []

    class Dies(ScriptedInvestigator):
        def investigate(self, context, committer, evidence):
            # Observe the lease exactly as claim() granted it, then die hard.
            granted.append(workbench.incidents.runs[run_id]["lease_until"])
            raise KeyboardInterrupt

    import pytest

    with pytest.raises(KeyboardInterrupt):
        workbench.run_once(subject, Dies(clock))
    assert granted == [clock.now() + timedelta(seconds=LEASE_SECONDS)]
    # A real kill leaves the lease held: restore what claim() had granted
    # (the BaseException path above released it in-process).
    run = workbench.incidents.runs[run_id]
    run.update(owner=uuid4(), state="running", lease_until=granted[0])
    before = workbench.events.latest(subject)
    assert workbench.run_once(subject, ScriptedInvestigator(clock)) is None
    assert workbench.events.latest(subject) == before
    clock.advance(LEASE_SECONDS + 1)
    assert clock.now() < run["deadline"]
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock))
    assert outcome is not None and outcome.execution == "completed"
    assert workbench.incidents.runs[run_id]["epoch"] == 2


def test_without_the_capability_the_lease_spans_the_run_wall_as_before(monkeypatch):
    app, workbench, clock = build_workbench()
    monkeypatch.setattr(MemoryIncidentStore, "renewal_supported", False)
    submit_incident(app, key="no-renew")
    subject = workbench.list_incidents()[0].incident_id
    run_id = workbench.list_incidents()[0].current_run_id
    granted: list = []

    class Observe(ScriptedInvestigator):
        def investigate(self, context, committer, evidence):
            granted.append(workbench.incidents.runs[run_id]["lease_until"])
            return super().investigate(context, committer, evidence)

    outcome = workbench.run_once(subject, Observe(clock))
    assert outcome is not None and outcome.execution == "completed"
    assert granted == [clock.now() + timedelta(seconds=workbench.run_seconds)]


def test_renewal_before_each_commit_keeps_a_long_attempt_alive(monkeypatch):
    from opspilot.web.service import LEASE_SECONDS

    app, workbench, clock = build_workbench()
    workbench.run_seconds = 4 * LEASE_SECONDS
    submit_incident(app, key="long")
    subject = workbench.list_incidents()[0].incident_id

    def slow_round(call_):
        # One maximal model request: longer than half the lease, shorter
        # than the lease itself, twice over the attempt.
        clock.advance(LEASE_SECONDS - 30)
        return reply(tool_calls=[tool_call()], finish="tool_calls")

    def slow_final(call_):
        clock.advance(LEASE_SECONDS - 30)
        return report_from_transcript(call_)

    investigator = ScriptedInvestigator(clock, replies=[slow_round, slow_final])
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "completed"
    assert workbench.incidents.renewals >= 3
    # A store that claims the capability but does not extend (a broken
    # renew) fences the same attempt by its own lease.
    app2, workbench2, clock2 = build_workbench()
    workbench2.run_seconds = 4 * LEASE_SECONDS
    monkeypatch.setattr(MemoryIncidentStore, "renew_lease", lambda self, lease, s: None)
    submit_incident(app2, key="long-no-renew")
    subject2 = workbench2.list_incidents()[0].incident_id

    def slow_round2(call_):
        clock2.advance(LEASE_SECONDS - 30)
        return reply(tool_calls=[tool_call()], finish="tool_calls")

    def slow_final2(call_):
        clock2.advance(LEASE_SECONDS - 30)
        return report_from_transcript(call_)

    outcome2 = workbench2.run_once(
        subject2, ScriptedInvestigator(clock2, replies=[slow_round2, slow_final2])
    )
    assert outcome2 is not None and outcome2.execution == "failed"
    assert outcome2.handoff_reasons == ("CONTROL_DENIED",)


def test_a_refused_renewal_hands_off_and_keeps_the_late_result_as_history():
    app, workbench, clock = build_workbench()
    submit_incident(app, key="refused")
    subject = workbench.list_incidents()[0].incident_id
    renewals_at_pause: list = []

    def pause_then_answer(call_):
        workbench.control(
            subject,
            actor_id="alice",
            action="pause",
            expected_generation=0,
            idempotency_key="mid-run",
        )
        renewals_at_pause.append(workbench.incidents.renewals)
        return report_from_transcript(call_)

    investigator = ScriptedInvestigator(
        clock,
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            pause_then_answer,
        ],
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None
    assert outcome.execution == "failed" and outcome.handoff_reasons == (
        "CONTROL_DENIED",
    )
    # Renewals happened before the pause and none succeeded after it: the
    # refused renewal fell through to the store's fence, which recorded
    # the late final step as history.
    assert renewals_at_pause[0] >= 2
    assert workbench.incidents.renewals == renewals_at_pause[0]
    snapshot = workbench.snapshot(subject)
    assert snapshot["report"] is None and snapshot["incident"].state == "paused"
    assert [s["status"] for s in snapshot["steps"]] == [
        "tool_result_committed",
        "late_result",
    ]
    assert workbench.events.read_after(subject, 0)[-1].kind == "run_handoff"
    run = workbench.incidents.runs[workbench.list_incidents()[0].current_run_id]
    assert run["owner"] is None and run["lease_until"] is None


# -- review threads on PR #33 -----------------------------------------------------


def test_reconciliation_never_binds_a_different_key_to_a_crashed_decision():
    """Thread 1 (P1): k1 applied then crashed before confirm; k2 (other text)
    crashed after recording its intent. k2's retry must not be confirmed as
    k1's decision."""
    app, workbench, _ = build_workbench()
    submit_incident(app, key="xkey")
    subject = workbench.list_incidents()[0].incident_id
    real_put = workbench.ledger.put

    def crash_before_confirm(namespace, key, value):
        if namespace == "control" and key.endswith(":k1"):
            from opspilot.persistence import PersistenceError

            raise PersistenceError("STORAGE_UNAVAILABLE")
        return real_put(namespace, key, value)

    workbench.ledger.put = crash_before_confirm
    try:
        with __import__("pytest").raises(Exception):
            workbench.control(
                subject,
                actor_id="alice",
                action="follow_up",
                expected_generation=0,
                idempotency_key="k1",
                text="X",
            )
    finally:
        workbench.ledger.put = real_put
    assert workbench.list_incidents()[0].control_generation == 1
    # k2 recorded its intent earlier and crashed before applying.
    workbench.ledger.put(
        "control_intent",
        f"{subject}:k2",
        {
            "action": "follow_up",
            "actor_id": "alice",
            "expected_generation": 0,
            "text": "Y",
        },
    )
    import pytest

    from opspilot.web import WorkbenchError

    with pytest.raises(WorkbenchError) as refused:
        workbench.control(
            subject,
            actor_id="alice",
            action="follow_up",
            expected_generation=0,
            idempotency_key="k2",
            text="Y",
        )
    assert refused.value.code == "CONTROL_CONFLICT"
    assert workbench.ledger.get("control", f"{subject}:k2") is None
    # k1's own retry is the one that reconciles, with its own text.
    result = workbench.control(
        subject,
        actor_id="alice",
        action="follow_up",
        expected_generation=0,
        idempotency_key="k1",
        text="X",
    )
    assert result.replayed is True and result.generation == 1
    assert [n["text"] for n in workbench.snapshot(subject)["controls"]] == ["X"]


def test_reregistering_committed_evidence_after_a_restart_reuses_it():
    """Thread 2 (P1): same evidence_id, same raw bytes, later observed_at."""
    from dataclasses import replace
    from datetime import timedelta

    from opspilot.web import MemoryEvidenceStore
    from tests.m1_tool_support import build, request

    executor, transport, sink, clock = build()
    from opspilot.tools import TransportResponse
    from tests.m1_tool_support import WINDOW_START, body

    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
    )
    outcome = executor.execute(request())
    record = outcome.evidence
    assert record is not None
    store = MemoryEvidenceStore()
    assert store.register(record) == record.evidence_id
    later_view = dict(record.view, observed_at="2026-09-14T01:06:00+00:00")
    from opspilot.tools.registry import canonical_hash

    again = replace(
        record,
        observed_at=record.observed_at + timedelta(seconds=60),
        view=later_view,
        view_sha256=canonical_hash(later_view),
    )
    assert store.register(again) == record.evidence_id
    kept = store.get(record.evidence_id)
    # The replayed observation is the one the loop consumes; the stored
    # view must be that one, not the never-committed earlier attempt.
    assert kept is not None and kept.observed_at == again.observed_at
    assert kept.view_sha256 == again.view_sha256
    different = replace(
        record,
        raw=b'{"data":{"result":[]}}',
        raw_sha256=__import__("hashlib").sha256(b'{"data":{"result":[]}}').hexdigest(),
    )
    import pytest

    from opspilot.persistence import PersistenceError

    with pytest.raises(PersistenceError, match="IDENTITY_CONFLICT"):
        store.register(different)


def test_snapshot_shows_the_newest_events_so_sse_resumes_without_a_gap():
    """Thread 3 (P2): more retained events than one page."""
    app, workbench, clock = build_workbench()
    submit_incident(app, key="many")
    subject = workbench.list_incidents()[0].incident_id
    workbench.control(
        subject,
        actor_id="alice",
        action="pause",
        expected_generation=0,
        idempotency_key="p",
    )
    for i in range(1200):
        workbench.events.append(subject, "filler", {"i": i})
    workbench.control(
        subject,
        actor_id="alice",
        action="resume",
        expected_generation=1,
        idempotency_key="r",
    )
    snapshot = workbench.snapshot(subject)
    latest = workbench.events.latest(subject)
    assert snapshot["latest_sequence"] == latest
    assert snapshot["events"][-1]["sequence"] == latest
    # Controls come from the authoritative rows, not the event page.
    assert [c["action"] for c in snapshot["controls"]] == ["pause", "resume"]
    # The outcome of the current run is located even when it lies beyond
    # the first page: run it now and check again.
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock))
    assert outcome is not None and outcome.execution == "completed"
    snapshot = workbench.snapshot(subject)
    assert snapshot["outcome"] is not None and snapshot["outcome"]["published"] is True
    assert snapshot["events"][-1]["sequence"] == workbench.events.latest(subject)


# -- review threads on PR #33, round 2 -------------------------------------------


def test_an_applied_but_unconfirmed_note_reaches_the_attempt_on_a_pre_31_store(
    monkeypatch,
):
    """Round 2 thread 1 (P1): worker B claims the generation that A's note
    produced while A's confirm row is still missing; B must still see it."""
    app, workbench, clock = build_workbench()
    monkeypatch.setattr(MemoryIncidentStore, "payload_supported", False)
    submit_incident(app, key="pre31")
    subject = workbench.list_incidents()[0].incident_id
    # A: intent durable, store applied the decision, crash before confirm.
    workbench.ledger.put(
        "control_intent",
        f"{subject}:a1",
        {
            "action": "correct",
            "actor_id": "alice",
            "expected_generation": 0,
            "text": "Target is checkout-canary.",
        },
    )
    assert workbench.incidents.control(subject, 0, "correct", "alice") == 1
    # B: claims and runs on generation 1.
    investigator = ScriptedInvestigator(clock)
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "completed"
    assert "Target is checkout-canary." in investigator.contexts[0].question
    assert [c["text"] for c in workbench.snapshot(subject)["controls"]] == [
        "Target is checkout-canary."
    ]


def test_a_completion_event_lost_after_publish_is_reconciled_from_the_run():
    """Round 2 thread 3 (P2): publish committed, event append crashed."""
    app, workbench, clock = build_workbench()
    submit_incident(app, key="lost-event")
    subject = workbench.list_incidents()[0].incident_id
    real_append = workbench.events.append

    def crash_on_completion(incident_id, kind, payload):
        if kind == "run_completed":
            raise RuntimeError("process died after publish")
        return real_append(incident_id, kind, payload)

    workbench.events.append = crash_on_completion
    import pytest

    with pytest.raises(RuntimeError):
        workbench.run_once(subject, ScriptedInvestigator(clock))
    workbench.events.append = real_append
    assert workbench.list_incidents()[0].concluded is True
    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert "run_completed" not in kinds
    before = workbench.events.latest(subject)
    # The page reconciles from the authoritative completed Run.
    snapshot = workbench.snapshot(subject)
    assert snapshot["outcome"] is not None and snapshot["outcome"]["published"] is True
    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert kinds.count("run_completed") == 1
    # Idempotent: a second look does not duplicate the event.
    workbench.snapshot(subject)
    assert [e.kind for e in workbench.events.read_after(subject, 0)].count(
        "run_completed"
    ) == 1
    # And a stream opened at the old cursor now receives it.
    incident = str(subject)
    resumed = stream(
        app,
        f"/incidents/{incident}/events?cursor={before}",
        headers=basic(),
        until_events=1,
    )
    assert [k for _, k, _ in resumed.sse_events()] == ["run_completed"]


# -- review threads on PR #33, round 3 -------------------------------------------


def test_a_note_applied_between_reconcile_and_claim_is_seen_under_the_lease(
    monkeypatch,
):
    """Round 3 thread 1 (P1): reconcile must happen after claim()."""
    app, workbench, clock = build_workbench()
    monkeypatch.setattr(MemoryIncidentStore, "payload_supported", False)
    submit_incident(app, key="toctou")
    subject = workbench.list_incidents()[0].incident_id
    store = workbench.incidents
    real_claim = store.claim

    def note_then_claim(incident_id, run_id, owner, versions, lease_seconds=30):
        # Another operator's note lands after the pre-claim reconcile: the
        # store applies it, the handler dies before its confirm row.
        workbench.ledger.put(
            "control_intent",
            f"{subject}:late-note",
            {
                "action": "correct",
                "actor_id": "bob",
                "expected_generation": 0,
                "text": "Look at the canary.",
            },
        )
        store.control(subject, 0, "correct", "bob")
        store.claim = real_claim
        return real_claim(incident_id, run_id, owner, versions, lease_seconds)

    store.claim = note_then_claim
    investigator = ScriptedInvestigator(clock)
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "completed"
    assert "Look at the canary." in investigator.contexts[0].question


def test_a_committed_evidence_projection_is_not_replaced_by_a_stale_replay():
    """Round 3 thread 2 (P1): replacement stops once the tool result committed."""
    from dataclasses import replace
    from datetime import timedelta

    from opspilot.tools import TransportResponse
    from opspilot.tools.registry import canonical_hash
    from opspilot.web import MemoryEvidenceStore
    from tests.m1_tool_support import WINDOW_START, body, build, request

    executor, transport, _, _ = build()
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
    )
    record = executor.execute(request()).evidence
    assert record is not None

    def at(minutes):
        observed = record.observed_at + timedelta(minutes=minutes)
        view = dict(record.view, observed_at=observed.isoformat())
        return replace(
            record, observed_at=observed, view=view, view_sha256=canonical_hash(view)
        )

    store = MemoryEvidenceStore()
    current, stale = at(1), at(2)
    store.register(current)
    # The current worker commits its tool result: its projection is final.
    store.commit(current.evidence_id, current.view)
    # The expired worker's replay arrives later with the same bytes.
    assert store.register(stale) == record.evidence_id
    kept = store.get(record.evidence_id)
    assert kept is not None and kept.view_sha256 == current.view_sha256
    assert kept.observed_at == current.observed_at
    assert kept.committed is True


def test_commit_tool_pins_the_consumed_evidence_view():
    """The emitting committer couples the evidence projection to the commit."""
    app, workbench, clock = build_workbench()
    submit_incident(app, key="pin")
    subject = workbench.list_incidents()[0].incident_id
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock))
    assert outcome is not None and outcome.execution == "completed"
    evidence_id = outcome.evidence_ids[0]
    stored = workbench.evidence.get(evidence_id)
    assert stored is not None and stored.committed is True
    rebuilt = workbench.incidents.rebuild(subject)
    committed_view = rebuilt["steps"][0]["tool_results"][0]["result"]
    from opspilot.tools.registry import canonical_hash

    assert stored.view_sha256 == canonical_hash(committed_view)


# -- review threads on PR #33, round 4 -------------------------------------------


def test_a_lost_intake_event_is_repaired_on_replay_and_on_reconcile():
    """Round 4 thread (P2): accept() committed, intake_accepted append crashed."""
    app, workbench, _ = build_workbench()
    real_append = workbench.events.append

    def crash_on_intake(incident_id, kind, payload):
        if kind == "intake_accepted":
            raise RuntimeError("process died after accept")
        return real_append(incident_id, kind, payload)

    import pytest

    workbench.events.append = crash_on_intake
    with pytest.raises(RuntimeError):
        submit_incident(app, key="lost-intake")
    workbench.events.append = real_append
    subject = workbench.list_incidents()[0].incident_id
    assert workbench.events.read_after(subject, 0) == ()
    # Replay repairs the projection and reports the true sequence.
    again = submit_incident(app, key="lost-intake")
    assert again.status == 200 and again.json()["replayed"] is True
    events = workbench.events.read_after(subject, 0)
    assert [e.kind for e in events] == ["intake_accepted"]
    assert again.json()["sequence"] == events[0].sequence
    assert events[0].payload["question"] == "Why is checkout erroring?"
    # Idempotent: a further replay and a page load add nothing.
    submit_incident(app, key="lost-intake")
    workbench.snapshot(subject)
    assert [e.kind for e in workbench.events.read_after(subject, 0)] == [
        "intake_accepted"
    ]
    # A page load alone repairs it too (no replay needed).
    app2, workbench2, _ = build_workbench()
    real_append2 = workbench2.events.append
    workbench2.events.append = crash_on_intake
    with pytest.raises(RuntimeError):
        submit_incident(app2, key="lost-intake-2")
    workbench2.events.append = real_append2
    subject2 = workbench2.list_incidents()[0].incident_id
    snapshot = workbench2.snapshot(subject2)
    assert [e["kind"] for e in snapshot["events"]] == ["intake_accepted"]
    assert snapshot["question"] == "Why is checkout erroring?"


def test_concurrent_intake_retries_announce_exactly_one_intake_event():
    """Round 5 thread (P2): two retries pass the ledger check before either appends.

    The rival retry runs to completion inside the first call's window between
    ``ledger.get("intake_event")`` and ``events.append``; the durable stream
    must still carry a single ``intake_accepted``.
    """
    app, workbench, _ = build_workbench()
    real_get = workbench.ledger.get
    armed = [True]
    rival = []

    def racing_get(namespace, key):
        value = real_get(namespace, key)
        if namespace == "intake_event" and value is None and armed[0]:
            armed[0] = False
            rival.append(submit_incident(app, key="race"))
        return value

    workbench.ledger.get = racing_get
    first = submit_incident(app, key="race")
    workbench.ledger.get = real_get
    assert first.status == 201 and first.json()["replayed"] is False
    assert rival[0].status == 200 and rival[0].json()["replayed"] is True
    subject = workbench.list_incidents()[0].incident_id
    events = workbench.events.read_after(subject, 0)
    assert [e.kind for e in events] == ["intake_accepted"]
    assert first.json()["sequence"] == events[0].sequence == 1
    assert rival[0].json()["sequence"] == 1
    assert workbench.ledger.get("intake_event", str(subject)) == {"sequence": 1}
    # A later replay and a page-load reconcile add nothing either.
    submit_incident(app, key="race")
    workbench.snapshot(subject)
    assert workbench.events.latest(subject) == 1


# -- review threads on PR #33, round 6 -------------------------------------------


def test_a_control_confirmed_after_a_lost_marker_announces_one_control_applied():
    """Round 6 thread 1 (P2): ``control_applied`` appended, ``control`` row lost.

    The retry sees the generation applied but no marker, confirms from the
    audit, and must reuse the published event instead of appending a twin.
    """
    from opspilot.persistence import PersistenceError

    app, workbench, _ = build_workbench()
    incident = submit_incident(app, key="twin-control").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    real_put = workbench.ledger.put
    armed = [True]

    def lose_marker(namespace, key, value):
        if namespace == "control" and armed[0]:
            armed[0] = False
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return real_put(namespace, key, value)

    workbench.ledger.put = lose_marker
    fields = {
        "action": "follow_up",
        "expected_generation": "0",
        "idempotency_key": "note-twin",
        "text": "Only once, please.",
    }
    assert _control(app, incident, fields).status == 503
    applied = [
        e
        for e in workbench.events.read_after(subject, 0)
        if e.kind == "control_applied"
    ]
    assert [e.payload["generation"] for e in applied] == [1]
    retry = _control(app, incident, fields)
    assert retry.status == 200 and retry.json()["replayed"] is True
    assert retry.json()["sequence"] == applied[0].sequence
    applied = [
        e
        for e in workbench.events.read_after(subject, 0)
        if e.kind == "control_applied"
    ]
    assert [e.payload["generation"] for e in applied] == [1]
    assert workbench.ledger.get("control", f"{subject}:note-twin") == {
        "action": "follow_up",
        "generation": 1,
        "sequence": applied[0].sequence,
    }
    # A page-load reconcile and a further replay add nothing.
    workbench.snapshot(subject)
    _control(app, incident, fields)
    assert [e.kind for e in workbench.events.read_after(subject, 0)].count(
        "control_applied"
    ) == 1
    # Later decisions (cancel, new_run) are new generations and still
    # publish their own events.
    _renew(app, incident, generation=1)
    assert [
        e.payload["generation"]
        for e in workbench.events.read_after(subject, 0)
        if e.kind == "control_applied"
    ] == [1, 2, 3]


def test_a_completion_confirmed_after_a_lost_marker_announces_one_run_completed():
    """Round 6 thread 2 (P2): ``run_completed`` appended, ``completion`` row lost.

    The next reconcile sees no marker; it must repair the marker from the
    published event rather than announce the conclusion twice. The worker's
    exit path still appends its ``run_handoff`` after the publication (as
    it does today); the page must keep showing the published conclusion.
    """
    app, workbench, clock = build_workbench()
    submit_incident(app, key="twin-completion")
    subject = workbench.list_incidents()[0].incident_id
    real_put = workbench.ledger.put
    armed = [True]

    def lose_marker(namespace, key, value):
        if namespace == "completion" and armed[0]:
            armed[0] = False
            raise RuntimeError("process died after run_completed")
        return real_put(namespace, key, value)

    workbench.ledger.put = lose_marker
    import pytest

    with pytest.raises(RuntimeError):
        workbench.run_once(subject, ScriptedInvestigator(clock))
    assert workbench.list_incidents()[0].concluded is True
    completed = [
        e for e in workbench.events.read_after(subject, 0) if e.kind == "run_completed"
    ]
    assert len(completed) == 1
    run_id = completed[0].payload["run_id"]
    assert workbench.ledger.get("completion", run_id) is None
    before = workbench.events.latest(subject)
    snapshot = workbench.snapshot(subject)
    assert snapshot["outcome"] is not None and snapshot["outcome"]["published"] is True
    # The outcome is the worker's own record, not a reconciled stub.
    assert snapshot["outcome"].get("reconciled") is None
    assert [e.kind for e in workbench.events.read_after(subject, 0)].count(
        "run_completed"
    ) == 1
    assert workbench.ledger.get("completion", run_id) == {
        "sequence": completed[0].sequence
    }
    # Nothing was appended by the repair, nor by a further page load, so a
    # reconnecting stream at the old cursor has no twin to deliver.
    workbench.snapshot(subject)
    assert workbench.events.latest(subject) == before
    assert workbench.events.read_after(subject, before) == ()


def test_a_completion_lost_after_the_stream_opened_is_repaired_while_following():
    """Round 7 thread (P2): the stream reconciled once, then the worker
    published and died before announcing. The open stream must repair the
    projection itself instead of leaving the page on the running state."""
    import pytest

    app, workbench, clock = build_workbench(sse_idle=0.5, sse_repair=0.05)
    submit_incident(app, key="late-loss")
    subject = workbench.list_incidents()[0].incident_id
    cursor = workbench.events.latest(subject)
    real_append = workbench.events.append
    real_read = workbench.events.read_after
    armed = [True]

    def crash_on_completion(incident_id, kind, payload):
        if kind == "run_completed":
            raise RuntimeError("process died after publish")
        return real_append(incident_id, kind, payload)

    def publish_and_die_inside_the_first_poll(subject_id, after, *, limit=100):
        # The stream's first poll: the endpoint's reconcile already ran, so
        # this loss lands after it.
        if armed[0]:
            armed[0] = False
            workbench.events.append = crash_on_completion
            with pytest.raises(RuntimeError):
                workbench.run_once(subject, ScriptedInvestigator(clock))
            workbench.events.append = real_append
        return real_read(subject_id, after, limit=limit)

    workbench.events.read_after = publish_and_die_inside_the_first_poll
    # Read until the stream ends on idle: the repair tick (0.05 s of silence)
    # comes well before the idle cut (0.5 s), so a repaired stream carries
    # the completion and an unrepaired one ends without it.
    lost = stream(
        app,
        f"/incidents/{subject}/events?cursor={cursor}",
        headers=basic(),
        until_events=10_000,
    )
    workbench.events.read_after = real_read
    kinds = [k for _, k, _ in lost.sse_events()]
    assert kinds[-1] == "run_completed", kinds
    assert kinds.count("run_completed") == 1
    assert workbench.list_incidents()[0].concluded is True
    # Exactly one run_completed was appended; the marker was written with it.
    retained = [e for e in workbench.events.read_after(subject, 0)]
    completed = [e for e in retained if e.kind == "run_completed"]
    assert len(completed) == 1
    assert workbench.ledger.get("completion", completed[0].payload["run_id"]) == {
        "sequence": completed[0].sequence
    }
