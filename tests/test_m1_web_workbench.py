"""Workbench entry points: authentication boundary, idempotent intake, SSE cursor
resume, human-control generations, and one end-to-end deterministic Run.

Everything runs against the in-memory doubles; ``tests/integration`` repeats
the end-to-end flow on PostgreSQL. No model HTTP, no network.
"""

from __future__ import annotations

import json

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
    ScriptedInvestigator,
    basic,
    bearer,
    build_workbench,
    call,
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
    assert (
        _control(
            app,
            incident,
            {"action": "pause", "expected_generation": "0", "idempotency_key": "p"},
        ).json()["generation"]
        == 1
    )
    blocked = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "1",
            "idempotency_key": "f",
            "text": "still?",
        },
    )
    assert blocked.status == 409 and blocked.json() == {"code": "ILLEGAL_TRANSITION"}
    assert (
        _control(
            app,
            incident,
            {"action": "resume", "expected_generation": "1", "idempotency_key": "r"},
        ).json()["generation"]
        == 2
    )
    assert (
        _control(
            app,
            incident,
            {"action": "cancel", "expected_generation": "2", "idempotency_key": "x"},
        ).json()["generation"]
        == 3
    )
    summary = workbench.list_incidents()[0]
    old_run = summary.current_run_id
    assert summary.state == "cancelled"
    renewed = _control(
        app,
        incident,
        {"action": "new_run", "expected_generation": "3", "idempotency_key": "n"},
    )
    assert renewed.status == 200 and renewed.json()["generation"] == 4
    summary = workbench.list_incidents()[0]
    assert summary.state == "queued" and summary.current_run_id != old_run
    assert workbench.incidents.runs[old_run]["state"] == "cancelled"
    # Validation: text is required for follow_up and forbidden elsewhere.
    assert _control(
        app,
        incident,
        {"action": "follow_up", "expected_generation": "4", "idempotency_key": "t1"},
    ).json() == {"code": "TEXT_REQUIRED"}
    assert _control(
        app,
        incident,
        {
            "action": "pause",
            "expected_generation": "4",
            "idempotency_key": "t2",
            "text": "x",
        },
    ).json() == {"code": "TEXT_NOT_ALLOWED"}
    assert (
        _control(
            app,
            incident,
            {"action": "takeover", "expected_generation": "4", "idempotency_key": "t3"},
        ).status
        == 400
    )
    assert workbench.list_incidents()[0].control_generation == 4


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
    assert "Also check the dependency." in again.contexts[0].question
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
    assert "kept in the ledger" in fresh.contexts[0].question
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
    assert "restarted at 09:00" in investigator.contexts[0].question
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
