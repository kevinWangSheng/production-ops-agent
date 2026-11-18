"""Workbench entry points: authentication boundary, idempotent intake, SSE cursor
resume, human-control generations, and one end-to-end deterministic Run.

Everything runs against the in-memory doubles; ``tests/integration`` repeats
the end-to-end flow on PostgreSQL. No model HTTP, no network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from opspilot.persistence import PersistenceError
from tests.m1_investigation_support import (
    reply,
    report_from_transcript,
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


# -- SSE cursor resume -------------------------------------------------------------


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
    listing = call(app, "GET", "/?tab=all", headers=basic())
    assert listing.status == 200 and incident in listing.text and other in listing.text
    # Pause remains refused after a conclusion (C3 §10).
    assert (
        _control(
            app,
            incident,
            {
                "action": "pause",
                "expected_generation": "0",
                "idempotency_key": "late",
            },
        ).status
        == 409
    )


def test_cancel_and_new_run_are_accepted_after_a_handoff():
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="park-2").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    first_run = workbench.list_incidents()[0].current_run_id
    investigator = ScriptedInvestigator(
        clock,
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(tool_calls=[tool_call()], finish="stop"),
        ],
    )
    assert workbench.run_once(subject, investigator).handoff is True
    assert workbench.incidents.runs[first_run]["state"] == "waiting_human"
    generation = _renew(app, incident, generation=0)
    assert generation == 2
    assert workbench.incidents.runs[first_run]["state"] == "cancelled"
    assert workbench.list_incidents()[0].current_run_id != first_run
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock))
    assert outcome is not None and outcome.execution == "completed"
    assert workbench.snapshot(subject)["report"] is not None


def test_a_handoff_fenced_by_human_control_is_not_announced_as_a_handoff():
    """Bot review (PR #44): no ``run_handoff`` unless the Run was really parked."""
    app, workbench, clock = build_workbench()
    submit_incident(app, key="fenced-1")
    subject = workbench.list_incidents()[0].incident_id
    run_id = workbench.list_incidents()[0].current_run_id
    inner = ScriptedInvestigator(
        clock,
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            reply(tool_calls=[tool_call()], finish="stop"),
        ],
    )

    class NoteDuringAttempt:
        def investigate(self, context, committer, evidence):
            # A follow_up lands while the attempt runs: the generation moves
            # on, every later commit is fenced and the loop ends in a handoff
            # the attempt may no longer park.
            workbench.incidents.control(
                subject, context.control_generation, "follow_up", "op", {"q": "x"}
            )
            return inner.investigate(context, committer, evidence)

    outcome = workbench.run_once(subject, NoteDuringAttempt())
    assert outcome is not None and outcome.handoff is True
    run = workbench.incidents.runs[run_id]
    assert run["state"] == "queued" and run["owner"] is None
    # The page keeps what the attempt saw as history, but the event never
    # claims a durable park the Run row does not show.
    last = workbench.events.read_after(subject, 0)[-1]
    assert last.kind == "run_handoff" and last.payload["parked"] is False
    # The page shows the row's real state, not a handoff that never landed.
    snapshot = workbench.snapshot(subject)
    assert snapshot["run"]["state"] == "queued"
    assert snapshot["outcome"] is None and snapshot["handoff_report"] is None
    assert [e["kind"] for e in snapshot["events"]][-1] == "run_handoff"
    # Once a human cancels the Run, the fenced attempt's result is history
    # the page may show again (the row is no longer runnable).
    workbench.incidents.control(subject, 1, "cancel", "op")
    assert workbench.snapshot(subject)["run"]["state"] == "cancelled"
    assert workbench.snapshot(subject)["outcome"]["execution"] == outcome.execution


def test_an_unexpected_investigator_error_releases_the_lease_and_hands_off():
    app, workbench, clock = build_workbench()
    submit_incident(app, key="boom")
    subject = workbench.list_incidents()[0].incident_id

    class Exploding:
        def investigate(self, context, committer, evidence):
            raise RuntimeError("investigator bug")

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


def test_a_refused_renewal_before_a_tool_dispatch_stops_the_tool_from_running(
    monkeypatch,
):
    """Independent review of PR #33 (P3-1): ``StepCommitter.renew()`` must raise.

    The loop renews before each tool dispatch so a fenced attempt halts
    *before* reading production again. A wrapper that swallows the refusal
    lets one duplicate read happen and only then records it as history.
    """
    app, workbench, clock = build_workbench()
    submit_incident(app, key="refused-pre-dispatch")
    subject = workbench.list_incidents()[0].incident_id
    original = MemoryIncidentStore.renew_lease
    paused: list = []

    def pause_before_dispatch(self, lease, seconds):
        run = self.runs[lease.run_id]
        # The step carrying the tool plan is committed and no tool has run:
        # this is the loop's pre-dispatch renewal.
        if not paused and run["steps"] and not run["steps"][-1]["tool_results"]:
            paused.append(True)
            workbench.control(
                subject,
                actor_id="alice",
                action="pause",
                expected_generation=0,
                idempotency_key="pre-dispatch",
            )
        return original(self, lease, seconds)

    monkeypatch.setattr(MemoryIncidentStore, "renew_lease", pause_before_dispatch)
    outcome = workbench.run_once(subject, ScriptedInvestigator(clock))
    assert paused and outcome is not None
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    # No read happened after the refusal: nothing was registered as evidence
    # and no late tool result exists; only the planned step is on record.
    assert outcome.evidence_ids == ()
    assert not workbench.evidence._records
    snapshot = workbench.snapshot(subject)
    assert [s["status"] for s in snapshot["steps"]] == ["response_committed"]
    assert snapshot["incident"].state == "paused"
    assert workbench.events.read_after(subject, 0)[-1].kind == "run_handoff"


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
        _view=later_view,
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
            record, observed_at=observed, _view=view, view_sha256=canonical_hash(view)
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


# -- codex security review on PR #33 -----------------------------------------------


def test_every_response_forbids_framing_so_human_controls_cannot_be_clickjacked():
    """Security P2: a framed workbench page submits control forms with the
    workbench's own Origin, which ``check_origin()`` rightly accepts; the
    defence is to refuse being framed at all, on every response."""
    app, workbench, _ = build_workbench()
    incident = submit_incident(app, key="frame").json()["incident_id"]
    responses = {
        "page": call(app, "GET", f"/incidents/{incident}", headers=basic()),
        "control": _control(
            app,
            incident,
            {"action": "pause", "expected_generation": "0", "idempotency_key": "x"},
        ),
        "auth_refused": call(app, "GET", f"/incidents/{incident}"),
        "sse": call(
            app, "GET", f"/incidents/{incident}/events?cursor=0", headers=basic()
        ),
        "not_found": call(app, "GET", "/no-such-route", headers=basic()),
    }
    assert responses["page"].status == 200
    assert responses["control"].status in (200, 303)
    assert responses["auth_refused"].status == 401
    assert responses["sse"].status == 200
    assert responses["not_found"].status == 404
    for name, response in responses.items():
        assert response.headers.get("x-frame-options") == "DENY", name
        assert (
            response.headers.get("content-security-policy") == "frame-ancestors 'none'"
        ), name


def test_the_sweep_never_overwrites_a_human_decision_that_landed_first():
    app, workbench, clock = build_workbench()
    for action in ("cancel", "pause"):
        incident = submit_incident(app, key=f"sweep-{action}").json()["incident_id"]
        subject = UUID(incident)
        run_id = workbench.list_incidents()[0].current_run_id
        workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, 600)
        clock.advance(601)
        applied = _control(
            app,
            incident,
            {"action": action, "expected_generation": "0", "idempotency_key": action},
        )
        assert applied.status == 200
        before = workbench.incidents.runs[run_id]["state"]
        assert before == ("cancelled" if action == "cancel" else "paused")
        workbench.snapshot(subject)
        assert workbench.incidents.runs[run_id]["state"] == before
        assert not [
            e
            for e in workbench.events.read_after(subject, 0)
            if e.kind == "run_handoff"
        ]


def test_a_sweep_that_cannot_take_its_lock_does_not_fail_the_page_load():
    """Independent review P3-4: the sweep is a repair on the way to a read;
    a lock timeout there is not the page's failure."""
    app, workbench, clock = build_workbench()
    submit_incident(app, key="sweep-outage")
    subject = workbench.list_incidents()[0].incident_id
    run_id = workbench.list_incidents()[0].current_run_id
    workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, 600)
    clock.advance(601)

    def refuse(*, incident_id=None, limit=100):
        raise PersistenceError("LOCK_TIMEOUT")

    workbench.incidents.sweep_expired_runs = refuse
    snapshot = workbench.snapshot(subject)
    assert snapshot["run"]["state"] == "running"
    assert not [
        e for e in workbench.events.read_after(subject, 0) if e.kind == "run_handoff"
    ]
