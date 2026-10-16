"""F12 acceptance on real PostgreSQL: the four ``feature_list.json`` steps.

Fresh-context acceptance tests (AGENTS.md 验证与汇报 / 独立审查), written from
``docs/tasks/2026-10-15-f12-contract.md`` (r1+r2: U1-U7, G1-G6, section 6
"验收计划·确定性"), the PR-1 record ``2026-10-15-f12-followup-history.md`` and
the PR-2 record ``2026-10-15-f12-close-reopen.md`` (C1-C6), against public
interfaces only:

* ``POST /intake/ui``, ``POST /intake/alertmanager``,
  ``POST /incidents/{id}/control``, ``GET /incidents/{id}`` (badges, buttons,
  "Human decisions", "Investigation history", history-only rows) and
  ``GET /incidents/{id}/evidence/{evidence_id}``;
* ``Workbench.snapshot``, ``DurableStore.rebuild`` / ``claimable_incidents`` /
  ``claim`` / ``read_inputs``, ``outcome_from_durable`` (``opspilot/acceptance.py``).

Conclusions are produced by the product worker (``WorkerLoop`` +
``InvestigationRunner``) with a scripted model and the product fixture tool;
no model HTTP, no network, no kind cluster. "No model request" is the
scripted model's call list staying empty.

Rows touched by hand: none. Read-only SQL inspects ``opspilot_runs``,
``opspilot_controls`` and ``opspilot_incidents`` (the audit time column the
page must show, and Run rows the page does not list).

Each scenario is one ``IncidentScenario`` (``feature_id="F12"``,
``acceptance_step`` 1-4); see ``docs/testing/f12-acceptance-tests.md``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from opspilot.acceptance import IncidentScenario, outcome_from_durable
from opspilot.observer.health_profile import PROFILE_DIRECTORY, load_health_profile
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    REGISTRY as ALERT_REGISTRY,
)
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    _alert,
    _webhook,
)
from tests.integration.test_m1_web_worker_postgres import _tool_round, _worker
from tests.m1_investigation_support import reply, report_from_transcript
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_form,
    post_json,
    same_origin,
)
from tests.target_support import ANY_TARGETS

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

TARGET = "checkout-prod"
QUESTION = "Why is checkout erroring?"
PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
STEP = {
    "1": "Use the authenticated interface to inspect progress and evidence, ask a follow-up question, add a fact and receive an updated investigation",
    "2": "Correct an incorrect incident association or hypothesis and verify the correction and prior history are retained",
    "3": "Pause, cancel, take over, close and reopen an incident and verify explicit status with authorized actor and timestamps",
    "4": "After a user takes over, deliver new evidence and verify it respects the incident state and does not erase the handoff",
}
_NOT_INSTRUCTIONS = re.compile(r"not\s+(an?\s+)?instructions?", re.IGNORECASE)
_BADGE = re.compile(
    r'id="(incident-control|incident-lifecycle|incident-mode|control-generation|run-state|concluded)">([^<]*)<'
)
_BUTTON = re.compile(r'name="action" value="([a-z_]+)"')


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------


def _build(*, alerts: bool = False, observation: bool = False):
    """The workbench + app as the product composes them, on the lab DB.

    ``alerts``: Alertmanager intake with the M1-04 registry (``checkout-prod``
    is bound by the checkout labels). ``observation``: the shipped checkout
    HealthProfile and a registry resolving any target id, so
    ``register_remediation`` can authorize an observation session.
    """
    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    extra = {}
    if alerts:
        extra["targets"] = MappingTargetRegistry(ALERT_REGISTRY)
    if observation:
        extra["targets"] = ANY_TARGETS
        extra["health_profile"] = PROFILE
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=None if observation else fixture_face(),
        **extra,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(store),
        sse_poll_seconds=0.02,
        sse_idle_seconds=0.3,
    )
    return SimpleNamespace(
        app=app, workbench=workbench, store=store, events=events, evidence=evidence
    )


def _submitted(tag: str, *, target: str = TARGET, **build):
    env = _build(**build)
    response = post_form(
        env.app,
        "/intake/ui",
        {
            "target_id": target,
            "question": QUESTION,
            "idempotency_key": f"f12-acc-{tag}-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    env.subject = UUID(response.json()["incident_id"])
    env.old_run = UUID(response.json()["run_id"])
    return env


def _scenario(env, step: str, scenario_id: str) -> IncidentScenario:
    return IncidentScenario(
        scenario_id=scenario_id,
        feature_id="F12",
        acceptance_step=STEP[step],
        kind="incident",
        subject_id=str(env.subject),
    )


def _project(env, scenario):
    snapshot = env.store.rebuild(env.subject)
    controls = tuple(
        asdict(row)
        for row in DurableIncidentStore(env.store).control_audit(env.subject)
    )
    handoffs = tuple(
        dict(event.payload)
        for event in env.events.read_after(env.subject, 0, limit=1000)
        if event.kind == "run_handoff"
    )
    return outcome_from_durable(
        scenario, snapshot, controls=controls, handoff_events=handoffs
    )


# --------------------------------------------------------------------------
# scripted worker
# --------------------------------------------------------------------------


def _reporter(summary: str, hypothesis: str, *, before=None):
    """Final-round reply: the cited fixture report with a distinct summary and
    one hypothesis claim. ``before`` runs while the model call is in flight."""

    def build(model_call):
        if before is not None:
            before()
        payload = json.loads(report_from_transcript(model_call).content)
        payload["summary"] = summary
        fact = payload["claims"][0]
        payload["claims"].append({**fact, "kind": "hypothesis", "text": hypothesis})
        return reply(content=json.dumps(payload, ensure_ascii=False))

    return build


def _poll(env, replies):
    loop, model = _worker(
        env.store, env.events, env.evidence, replies, subject=env.subject
    )
    return [(i, o.status) for i, o in loop.poll_once()], model


def _publish(env, summary: str, hypothesis: str) -> None:
    results, _ = _poll(env, [_tool_round(), _reporter(summary, hypothesis)])
    assert results == [(env.subject, "published")], results


def _concluded(tag: str, **build):
    env = _submitted(tag, **build)
    marker = uuid4().hex[:10]
    env.old_summary = f"Checkout errors rose in the window, prior finding {marker}."
    env.old_hypothesis = f"Payments dependency latency is the likely cause {marker}."
    _publish(env, env.old_summary, env.old_hypothesis)
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["conclusion"] is not None
    assert rebuilt["run"]["run_id"] == env.old_run
    assert rebuilt["run"]["state"] == "completed"
    return env


def _assert_no_automatic_work(env, run_id) -> None:
    """No claimable Run, a direct claim is refused, and a polling worker makes
    no model request."""
    assert env.subject not in env.store.claimable_incidents(limit=1000)
    with pytest.raises(PersistenceError):
        env.store.claim(
            env.subject, run_id, uuid4(), fixture_versions(), lease_seconds=30
        )
    results, model = _poll(env, [_tool_round(), report_from_transcript])
    assert results == []
    assert model.calls == []


# --------------------------------------------------------------------------
# control + readers
# --------------------------------------------------------------------------


def _act(env, action, generation, text=None, *, headers=None, revision=None):
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": f"{action}-{uuid4()}",
    }
    if text is not None:
        fields["text"] = text
    if revision is not None:
        fields["revision"] = revision
    return post_form(
        env.app,
        f"/incidents/{env.subject}/control",
        fields,
        headers={**basic(), **same_origin()} if headers is None else headers,
    )


def _ok(response, generation):
    assert response.status == 200, response.text
    assert response.json()["generation"] == generation
    return response


def _refused(response, code):
    assert response.status == 409, response.text
    assert response.json()["code"] == code, response.text


def _page(env) -> str:
    response = call(env.app, "GET", f"/incidents/{env.subject}", headers=basic())
    assert response.status == 200
    return response.text


def _badges(page: str) -> dict[str, str]:
    return dict(_BADGE.findall(page))


def _buttons(page: str) -> set[str]:
    return set(_BUTTON.findall(page))


def _section(page: str, heading: str) -> str:
    found = re.search(re.escape(heading), page, re.IGNORECASE)
    assert found is not None, f"no {heading!r} section on the page"
    end = page.find("<h2", found.end())
    return page[found.start() : end if end != -1 else len(page)]


def _runs(env):
    with env.store.transaction(snapshot=True) as conn:
        return {
            row["run_id"]: row
            for row in conn.execute(
                "SELECT run_id,state,epoch,control_generation FROM opspilot_runs WHERE incident_id=%s",
                (env.subject,),
            ).fetchall()
        }


def _controls(env):
    with env.store.transaction(snapshot=True) as conn:
        return conn.execute(
            "SELECT action,expected_generation,resulting_generation,actor,payload,created_at "
            "FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
            (env.subject,),
        ).fetchall()


def _incident(env):
    with env.store.transaction(snapshot=True) as conn:
        return conn.execute(
            "SELECT state,lifecycle,mode,control_generation,conclusion,current_run_id "
            "FROM opspilot_incidents WHERE incident_id=%s",
            (env.subject,),
        ).fetchone()


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _rendered_forms(moment: datetime) -> set[str]:
    forms = set()
    for dt in (moment, moment.astimezone(timezone.utc)):
        for sep in (" ", "T"):
            forms.add(dt.strftime(f"%Y-%m-%d{sep}%H:%M:%S"))
    return forms


def _assert_decision_shown(env, page: str, action: str, generation: int) -> None:
    """G1 / step 3: the decision is listed with its authenticated actor, its
    generation and the database time of its audit row."""
    audit = [r for r in _controls(env) if r["resulting_generation"] == generation]
    assert len(audit) == 1, audit
    assert audit[0]["action"] == action
    assert audit[0]["actor"] == UI_USER  # the Basic-Auth principal
    db_time = audit[0]["created_at"]

    rows = [
        r
        for r in env.workbench.snapshot(env.subject)["controls"]
        if r["generation"] == generation
    ]
    assert len(rows) == 1, rows
    assert rows[0]["action"] == action
    assert rows[0]["actor_id"] == UI_USER
    assert _as_datetime(rows[0]["decided_at"]) == db_time

    decisions = _section(page, "Human decisions")
    matching = [
        tr
        for tr in decisions.split("<tr>")
        if f"<code>{action}</code>" in tr and f"→ {generation}<" in tr
    ]
    assert len(matching) == 1, decisions
    assert f"<code>{UI_USER}</code>" in matching[0]
    assert any(f in matching[0] for f in _rendered_forms(db_time)), (
        db_time,
        matching[0],
    )


def _evidence_ids(snapshot) -> list[str]:
    found: list[str] = []
    for step in snapshot["steps"]:
        if step["status"] == "late_result":
            continue
        for entry in step["tool_results"] or ():
            evidence_id = entry["result"].get("evidence_id")
            if (
                isinstance(evidence_id, str)
                and evidence_id
                and evidence_id not in found
            ):
                found.append(evidence_id)
    return found


def _late_rows(snapshot):
    return [s for s in snapshot["steps"] if s["status"] == "late_result"]


# ==========================================================================
# Step 1: inspect, follow up, add a fact, receive an updated investigation
# ==========================================================================


@pytest.mark.parametrize(
    "action,text",
    [
        ("follow_up", "Check the payments dependency as well."),
        ("correct", "Added fact: payments was redeployed at 10:02."),
    ],
)
def test_step1_note_after_conclusion_yields_updated_investigation(action, text):
    env = _concluded(f"s1-{action}")
    scenario = _scenario(env, "1", f"f12-s1-{action}-after-conclusion")

    # Inspect progress and evidence through the authenticated page.
    page = _page(env)
    assert env.old_summary in page
    evidence_ids = _evidence_ids(env.store.rebuild(env.subject))
    assert evidence_ids
    assert f"/incidents/{env.subject}/evidence/{evidence_ids[0]}" in page
    read_back = call(
        env.app,
        "GET",
        f"/incidents/{env.subject}/evidence/{evidence_ids[0]}",
        headers=basic(),
    )
    assert read_back.status == 200
    assert read_back.json()["evidence_id"] == evidence_ids[0]
    assert 'id="progress"' in page and "run_completed" in page

    # Ask / add a fact: 200, generation +1, a new queued Run, old Run completed.
    _ok(_act(env, action, 0, text), 1)
    rebuilt = env.store.rebuild(env.subject)
    new_run = rebuilt["run"]["run_id"]
    assert new_run != env.old_run
    assert rebuilt["run"]["state"] == "queued"
    assert rebuilt["control_generation"] == 1
    runs = _runs(env)
    assert set(runs) == {env.old_run, new_run}
    assert runs[env.old_run]["state"] == "completed"
    assert env.subject in env.store.claimable_incidents(limit=1000)
    assert [
        (i["kind"], i["content"].get("text"), i["control_generation"])
        for i in env.store.read_inputs(env.subject)
    ] == [(action, text, 1)]

    # The worker publishes again: the page shows the new conclusion and the
    # investigation history keeps the old one.
    new_summary = f"Updated assessment after the human note {uuid4().hex[:10]}."
    _publish(env, new_summary, "Payments redeploy is the more likely cause.")
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["run"]["run_id"] == new_run
    assert rebuilt["run"]["state"] == "completed"
    assert _runs(env)[env.old_run]["state"] == "completed"
    page = _page(env)
    assert env.workbench.snapshot(env.subject)["report"]["summary"] == new_summary
    assert new_summary in page
    history = _section(page, "Investigation history")
    assert str(env.old_run) in history and env.old_summary in history

    outcome = _project(env, scenario)
    assert outcome.final_state == "completed"
    assert outcome.report_available is True
    assert outcome.decision == "report_available"


def test_step1_control_requires_authentication_and_same_origin():
    env = _concluded("s1-auth")
    text = "Check the payments dependency as well."
    unauthenticated = _act(env, "follow_up", 0, text, headers=same_origin())
    assert unauthenticated.status == 401
    cross_origin = _act(
        env,
        "follow_up",
        0,
        text,
        headers={**basic(), "origin": "https://evil.example.test"},
    )
    assert cross_origin.status == 403
    assert call(env.app, "GET", f"/incidents/{env.subject}").status == 401
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["control_generation"] == 0
    assert rebuilt["run"]["run_id"] == env.old_run
    assert rebuilt["conclusion"] is not None
    assert _controls(env) == []
    assert env.store.read_inputs(env.subject) == []


# ==========================================================================
# Step 2: correct a hypothesis; correction and prior history retained
# (U6-A: incident-association correction is a known limitation)
# ==========================================================================


def test_step2_correcting_a_hypothesis_keeps_correction_and_prior_history():
    env = _concluded("s2-hypothesis")
    scenario = _scenario(env, "2", "f12-s2-correct-hypothesis")
    text = (
        "Correction: the payments latency hypothesis is wrong; payments p99 was "
        "flat. Added fact: a checkout config push landed at 10:01."
    )
    _ok(_act(env, "correct", 0, text), 1)

    # Audit row and input row.
    audit = _controls(env)
    assert [(r["action"], r["actor"], r["resulting_generation"]) for r in audit] == [
        ("correct", UI_USER, 1)
    ]
    assert (audit[0]["payload"] or {}).get("text") == text
    assert [
        (i["kind"], i["content"].get("text"))
        for i in env.store.read_inputs(env.subject)
    ] == [("correct", text)]

    # The successor Run's stored input carries the prior conclusion labelled
    # as earlier context, not instructions (U5-A).
    rebuilt = env.store.rebuild(env.subject)
    new_run = rebuilt["run"]["run_id"]
    assert new_run != env.old_run
    question = rebuilt["run"]["input"]["question"]
    assert env.old_summary in question and env.old_hypothesis in question
    assert _NOT_INSTRUCTIONS.search(question), question

    # What the successor Run actually sends to the model: its first request
    # contains the prior hypothesis and, after it, the correction (U5-A "人的
    # 纠正排在其后").
    new_summary = f"Config push is the likely cause {uuid4().hex[:10]}."
    results, model = _poll(
        env,
        [
            _tool_round(),
            _reporter(new_summary, "The 10:01 checkout config push broke PlaceOrder."),
        ],
    )
    assert results == [(env.subject, "published")], results
    first_request = "\n".join(
        str(m.get("content") or "") for m in model.calls[0].messages
    )
    assert env.old_hypothesis in first_request
    assert text in first_request
    assert first_request.index(env.old_hypothesis) < first_request.index(text)

    # The old conclusion and its hypothesis stay readable in the history; the
    # correction stays in the human decisions with its text.
    page = _page(env)
    assert new_summary in page
    history = _section(page, "Investigation history")
    assert str(env.old_run) in history
    assert env.old_summary in history
    assert env.old_hypothesis in history
    decisions = _section(page, "Human decisions")
    assert "<code>correct</code>" in decisions
    assert "the payments latency hypothesis is wrong" in decisions
    assert _runs(env)[env.old_run]["state"] == "completed"

    outcome = _project(env, scenario)
    assert outcome.report_available is True


# ==========================================================================
# Step 3: pause, resume, cancel, take over, close, reopen
# ==========================================================================


def test_step3_each_control_shows_status_actor_time_and_generation():
    env = _submitted("s3-chain")
    scenario = _scenario(env, "3", "f12-s3-pause-resume-cancel-takeover-close-reopen")
    run = env.old_run

    def check(action, generation, *, lifecycle, mode, control=None, run_state=None):
        page = _page(env)
        badges = _badges(page)
        assert badges["incident-lifecycle"] == lifecycle, badges
        assert badges["incident-mode"] == mode, badges
        assert badges["control-generation"] == str(generation), badges
        if control is not None:
            assert badges.get("incident-control") == control, badges
        if run_state is not None:
            assert badges["run-state"] == run_state, badges
        _assert_decision_shown(env, page, action, generation)
        row = _incident(env)
        assert (row["lifecycle"], row["mode"], row["control_generation"]) == (
            lifecycle,
            mode,
            generation,
        )
        return page

    _ok(_act(env, "pause", 0), 1)
    check("pause", 1, lifecycle="open", mode="automatic", control="paused")
    assert env.subject not in env.store.claimable_incidents(limit=1000)

    _ok(_act(env, "resume", 1), 2)
    page = check("resume", 2, lifecycle="open", mode="automatic")
    assert _badges(page).get("incident-control") != "paused"
    assert env.subject in env.store.claimable_incidents(limit=1000)

    _ok(_act(env, "cancel", 2), 3)
    check(
        "cancel",
        3,
        lifecycle="open",
        mode="automatic",
        control="cancelled",
        run_state="cancelled",
    )
    assert _runs(env)[run]["state"] == "cancelled"
    outcome = _project(env, scenario)
    assert outcome.final_state == "cancelled"
    assert outcome.human_interaction == "cancel"

    _ok(_act(env, "takeover", 3), 4)
    check("takeover", 4, lifecycle="open", mode="human_owned")

    _ok(_act(env, "close", 4), 5)
    page = check("close", 5, lifecycle="closed", mode="human_owned")
    assert "reopen" in _buttons(page) and "close" not in _buttons(page)

    _ok(_act(env, "reopen", 5), 6)
    page = check("reopen", 6, lifecycle="open", mode="human_owned")
    assert "close" in _buttons(page) and "reopen" not in _buttons(page)
    assert set(_runs(env)) == {run}  # reopen opened no Run
    assert env.subject not in env.store.claimable_incidents(limit=1000)

    assert [
        (r["action"], r["actor"], r["expected_generation"], r["resulting_generation"])
        for r in _controls(env)
    ] == [
        ("pause", UI_USER, 0, 1),
        ("resume", UI_USER, 1, 2),
        ("cancel", UI_USER, 2, 3),
        ("takeover", UI_USER, 3, 4),
        ("close", UI_USER, 4, 5),
        ("reopen", UI_USER, 5, 6),
    ]
    times = [r["created_at"] for r in _controls(env)]
    assert times == sorted(times)


def test_step3_close_stops_a_running_run_and_its_late_publish_is_history_only():
    env = _submitted("s3-close-inflight")
    scenario = _scenario(env, "3", "f12-s3-close-while-running")

    def close_now():
        _ok(_act(env, "close", 0), 1)

    summary = f"Report written after the close {uuid4().hex[:10]}."
    results, model = _poll(
        env,
        [_tool_round(), _reporter(summary, "Late hypothesis.", before=close_now)],
    )
    assert len(model.calls) == 2  # the worker really was in flight
    assert [i for i, _ in results] == [env.subject]
    assert results[0][1] != "published"

    row = _incident(env)
    assert row["lifecycle"] == "closed"
    assert row["control_generation"] == 1
    assert row["conclusion"] is None
    assert row["current_run_id"] == env.old_run
    assert _runs(env)[env.old_run]["state"] == "cancelled"  # U2-A
    assert row["state"] == "cancelled"  # C2

    snapshot = env.store.rebuild(env.subject)
    late = _late_rows(snapshot)
    assert late, "the fenced publish left no history row"
    assert all(s["control_generation"] == 0 for s in late)
    outcome = _project(env, scenario)
    assert outcome.report_available is False
    assert "late_result_rejected" in outcome.actions
    assert "STALE_CONTROL_GENERATION" in outcome.handoff_reasons

    page = _page(env)
    assert summary not in _section(page, "Human control")
    assert "history only" in page
    assert _badges(page)["incident-lifecycle"] == "closed"
    assert _badges(page)["run-state"] == "cancelled"
    assert env.workbench.snapshot(env.subject)["report"] is None
    _assert_decision_shown(env, page, "close", 1)
    _assert_no_automatic_work(env, env.old_run)


def test_step3_close_revokes_observation_and_reopen_restores_neither_run_nor_it():
    env = _submitted(
        "s3-close-observation",
        target=f"checkout-{uuid4().hex[:8]}",
        observation=True,
    )
    scenario = _scenario(env, "3", "f12-s3-close-revokes-observation")
    _ok(_act(env, "register_remediation", 0, revision="checkout:v2"), 1)
    (session,) = env.workbench.snapshot(env.subject)["observation_sessions"]
    assert session["state"] == "authorized"
    assert _incident(env)["lifecycle"] == "observing_recovery"
    page = _page(env)
    assert "close" in _buttons(page) and "reopen" not in _buttons(page)  # G4

    _ok(_act(env, "close", 1), 2)
    row = _incident(env)
    assert (row["lifecycle"], row["control_generation"]) == ("closed", 2)
    assert _runs(env)[env.old_run]["state"] == "cancelled"
    (closed_session,) = env.workbench.snapshot(env.subject)["observation_sessions"]
    assert closed_session["session_id"] == session["session_id"]
    assert closed_session["state"] == "revoked", closed_session
    page = _page(env)
    assert _badges(page)["incident-lifecycle"] == "closed"
    assert "reopen" in _buttons(page) and "close" not in _buttons(page)  # G4
    _assert_decision_shown(env, page, "close", 2)
    assert _project(env, scenario).final_state == "cancelled"

    _ok(_act(env, "reopen", 2), 3)
    row = _incident(env)
    assert (row["lifecycle"], row["control_generation"]) == ("open", 3)
    assert row["current_run_id"] == env.old_run
    assert set(_runs(env)) == {env.old_run}  # no Run
    sessions = env.workbench.snapshot(env.subject)["observation_sessions"]
    assert [(s["session_id"], s["state"]) for s in sessions] == [
        (session["session_id"], "revoked")
    ]  # not restored, no new one
    page = _page(env)
    assert _badges(page)["incident-lifecycle"] == "open"
    assert "close" in _buttons(page) and "reopen" not in _buttons(page)  # G4
    _assert_decision_shown(env, page, "reopen", 3)
    _assert_no_automatic_work(env, env.old_run)


def test_step3_close_after_takeover_cancels_the_waiting_human_run():
    """User decision 2026-10-16 (PR-2 close semantics, option A): closing a
    human-owned incident cancels the ``waiting_human`` Run the takeover left,
    the same as ``cancel``."""
    env = _submitted("s3-takeover-close")
    scenario = _scenario(env, "3", "f12-s3-takeover-then-close")
    _ok(_act(env, "takeover", 0), 1)
    assert _runs(env)[env.old_run]["state"] == "waiting_human"
    assert _incident(env)["mode"] == "human_owned"

    _ok(_act(env, "close", 1), 2)
    row = _incident(env)
    assert (row["lifecycle"], row["mode"], row["control_generation"]) == (
        "closed",
        "human_owned",
        2,
    )
    assert row["current_run_id"] == env.old_run
    assert set(_runs(env)) == {env.old_run}
    assert _runs(env)[env.old_run]["state"] == "cancelled"
    assert env.store.rebuild(env.subject)["run"]["state"] == "cancelled"
    page = _page(env)
    badges = _badges(page)
    assert badges["incident-lifecycle"] == "closed"
    assert badges["incident-mode"] == "human_owned"
    assert badges["run-state"] == "cancelled"
    _assert_decision_shown(env, page, "close", 2)
    assert ("takeover", UI_USER, 1) in [
        (r["action"], r["actor"], r["resulting_generation"]) for r in _controls(env)
    ]
    assert _project(env, scenario).final_state == "cancelled"
    _assert_no_automatic_work(env, env.old_run)


@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_step3_closed_incident_needs_reopen_before_a_new_run(action):
    env = _concluded(f"s3-reopen-required-{action}")
    _ok(_act(env, "close", 0), 1)
    row = _incident(env)
    assert row["lifecycle"] == "closed"
    assert row["conclusion"] is not None  # C2: the conclusion is kept
    assert _runs(env)[env.old_run]["state"] == "completed"

    text = "Is checkout still erroring after the rollback?"
    _refused(_act(env, action, 1, text), "REOPEN_REQUIRED")
    assert _incident(env)["control_generation"] == 1
    assert set(_runs(env)) == {env.old_run}
    assert [r["action"] for r in _controls(env)] == ["close"]
    assert env.store.read_inputs(env.subject) == []

    _ok(_act(env, "reopen", 1), 2)
    assert _incident(env)["lifecycle"] == "open"
    assert set(_runs(env)) == {env.old_run}  # reopen opens no Run
    assert env.subject not in env.store.claimable_incidents(limit=1000)

    _ok(_act(env, action, 2, text), 3)
    rebuilt = env.store.rebuild(env.subject)
    new_run = rebuilt["run"]["run_id"]
    assert new_run != env.old_run and rebuilt["run"]["state"] == "queued"
    assert _runs(env)[env.old_run]["state"] == "completed"
    assert env.subject in env.store.claimable_incidents(limit=1000)
    assert [r["action"] for r in _controls(env)] == ["close", "reopen", action]


# ==========================================================================
# Step 4: new evidence after a takeover respects the incident state and
# keeps the handoff (U7-A, three stimuli)
# ==========================================================================


def _assert_handoff_kept(env, takeover_generation: int, run_state: str) -> None:
    row = _incident(env)
    assert row["mode"] == "human_owned"
    assert row["current_run_id"] == env.old_run
    audit = _controls(env)
    assert ("takeover", UI_USER, takeover_generation) in [
        (r["action"], r["actor"], r["resulting_generation"]) for r in audit
    ]
    assert set(_runs(env)) == {env.old_run}
    assert _runs(env)[env.old_run]["state"] == run_state
    page = _page(env)
    assert _badges(page)["incident-mode"] == "human_owned"
    assert _badges(page)["run-state"] == run_state
    assert "<code>takeover</code>" in _section(page, "Human decisions")
    _assert_no_automatic_work(env, env.old_run)


def test_step4_worker_dispatched_before_takeover_publishes_into_history_only():
    env = _submitted("s4-late-worker")
    scenario = _scenario(env, "4", "f12-s4-late-worker-after-takeover")

    def takeover_now():
        _ok(_act(env, "takeover", 0), 1)

    summary = f"Report the worker wrote after the takeover {uuid4().hex[:10]}."
    results, model = _poll(
        env,
        [_tool_round(), _reporter(summary, "Late hypothesis.", before=takeover_now)],
    )
    assert len(model.calls) == 2
    assert results[0][1] != "published"

    row = _incident(env)
    assert row["conclusion"] is None
    assert row["control_generation"] == 1
    snapshot = env.store.rebuild(env.subject)
    late = _late_rows(snapshot)
    assert late and all(s["control_generation"] == 0 for s in late)
    assert env.workbench.snapshot(env.subject)["report"] is None
    page = _page(env)
    assert "history only" in page
    outcome = _project(env, scenario)
    assert outcome.report_available is False
    assert "late_result_rejected" in outcome.actions
    _assert_handoff_kept(env, 1, "waiting_human")


def test_step4_the_same_alert_firing_again_is_recorded_without_a_new_investigation():
    env = _build(alerts=True)
    alert = _alert()
    first = post_json(
        env.app, "/intake/alertmanager", _webhook(alert), headers=bearer()
    )
    assert first.status == 200, first.text
    [created] = first.json()["results"]
    assert created["outcome"] == "created"
    env.subject = UUID(created["incident_id"])
    env.old_run = UUID(created["run_id"])
    scenario = _scenario(env, "4", "f12-s4-alert-refires-after-takeover")

    _ok(_act(env, "takeover", 0), 1)
    controls_before = _controls(env)
    inputs_before = env.store.read_inputs(env.subject)

    again = post_json(
        env.app,
        "/intake/alertmanager",
        _webhook(dict(alert, annotations={"summary": "error ratio now 72%"})),
        headers=bearer(),
    )
    assert again.status == 200, again.text
    [replayed] = again.json()["results"]
    assert replayed["outcome"] == "replayed"
    assert (replayed["incident_id"], replayed["run_id"]) == (
        str(env.subject),
        str(env.old_run),
    )
    row = _incident(env)
    assert row["control_generation"] == 1  # a delivery is not a human decision
    assert _controls(env) == controls_before
    assert env.store.read_inputs(env.subject) == inputs_before
    assert _project(env, scenario).report_available is False
    _assert_handoff_kept(env, 1, "waiting_human")


@pytest.mark.parametrize("concluded", [False, True], ids=["no-conclusion", "concluded"])
def test_step4_a_fact_added_under_human_ownership_is_recorded_only(concluded):
    tag = f"s4-fact-{'concluded' if concluded else 'open'}"
    env = _concluded(tag) if concluded else _submitted(tag)
    scenario = _scenario(env, "4", f"f12-{tag}")
    conclusion_before = _incident(env)["conclusion"]
    _ok(_act(env, "takeover", 0), 1)
    # PR-1 record: a concluded Run stays completed on takeover.
    run_state = "completed" if concluded else "waiting_human"
    assert _runs(env)[env.old_run]["state"] == run_state

    text = "Added fact: the payments pod restarted at 10:02."
    _ok(_act(env, "correct", 1, text), 2)
    row = _incident(env)
    assert row["control_generation"] == 2
    assert row["conclusion"] == conclusion_before
    assert [
        (i["kind"], i["content"].get("text"), i["control_generation"])
        for i in env.store.read_inputs(env.subject)
    ] == [("correct", text, 2)]
    assert [(r["action"], r["resulting_generation"]) for r in _controls(env)] == [
        ("takeover", 1),
        ("correct", 2),
    ]
    _assert_decision_shown(env, _page(env), "correct", 2)
    outcome = _project(env, scenario)
    assert outcome.report_available is concluded
    _assert_handoff_kept(env, 1, run_state)
