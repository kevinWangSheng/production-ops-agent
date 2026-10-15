"""F12 PR-1 acceptance on real PostgreSQL (contract r1, section 5 PR-1).

Fresh-context acceptance tests written from ``docs/tasks/2026-10-15-f12-contract.md``
(U1-A, U3 ``REOPEN_REQUIRED`` part, U4-A, U5-A, U7-A, G1, G2) and the task
record ``docs/tasks/2026-10-15-f12-followup-history.md``, against public
interfaces only: ``POST /incidents/{id}/control``, ``GET /incidents/{id}``,
``GET /incidents/{id}/evidence/{evidence_id}``, ``Workbench.snapshot``,
``DurableStore.rebuild`` / ``claimable_incidents`` / ``claim`` and
``continuation_context``. Conclusions are produced by the product worker
(``WorkerLoop`` + ``InvestigationRunner``) with a scripted model and the
product fixture tool; no model HTTP, no network, no kind cluster.

The only rows touched by hand: ``opspilot_incidents.lifecycle`` set to
``resolved``/``closed`` (close/reopen do not exist until PR-2), and read-only
SQL to inspect ``opspilot_runs``, ``opspilot_controls`` and ``opspilot_inputs``.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import continuation_context
from opspilot.persistence import PersistenceError
from tests.integration.test_m1_web_worker_postgres import (
    _build,
    _control,
    _submit,
    _tool_round,
    _worker,
)
from tests.m1_investigation_support import reply, report_from_transcript
from tests.m1_web_support import ORIGIN, basic, call, post_form, same_origin

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

TARGET = "checkout-prod"
_NOT_INSTRUCTIONS = re.compile(r"not\s+(an?\s+)?instructions?", re.IGNORECASE)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _reporter(summary: str, hypothesis: str):
    """Final-round reply: the standard cited report with a distinct summary
    and one hypothesis claim citing the same delivered view."""

    def build(model_call):
        payload = json.loads(report_from_transcript(model_call).content)
        payload["summary"] = summary
        fact = payload["claims"][0]
        payload["claims"].append({**fact, "kind": "hypothesis", "text": hypothesis})
        return reply(content=json.dumps(payload, ensure_ascii=False))

    return build


def _publish(env, summary: str, hypothesis: str) -> None:
    loop, _ = _worker(
        env.store,
        env.events,
        env.evidence,
        [_tool_round(), _reporter(summary, hypothesis)],
        subject=env.subject,
    )
    results = loop.poll_once()
    assert [(i, o.status) for i, o in results] == [(env.subject, "published")], results


def _concluded(tag: str):
    """A workbench incident driven by the product worker to a published conclusion."""
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"f12-pr1-{tag}-{uuid4()}")
    marker = uuid4().hex[:10]
    env = SimpleNamespace(
        app=app,
        workbench=workbench,
        store=store,
        events=events,
        evidence=evidence,
        subject=UUID(submitted["incident_id"]),
        old_run=UUID(submitted["run_id"]),
        old_summary=f"Checkout errors rose in the window, prior finding {marker}.",
        old_hypothesis=f"Payments dependency latency is the likely cause {marker}.",
    )
    _publish(env, env.old_summary, env.old_hypothesis)
    rebuilt = store.rebuild(env.subject)
    assert rebuilt["conclusion"] is not None
    assert rebuilt["run"]["run_id"] == env.old_run
    assert rebuilt["run"]["state"] == "completed"
    return env


def _act(env, action, generation, text=None, *, headers=None):
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": f"{action}-{uuid4()}",
    }
    if text is not None:
        fields["text"] = text
    if headers is None:
        return _control(env.app, env.subject, fields)
    return post_form(
        env.app, f"/incidents/{env.subject}/control", fields, headers=headers
    )


def _ok(response, generation):
    assert response.status == 200, response.text
    assert response.json()["generation"] == generation
    return response


def _page(env) -> str:
    response = call(env.app, "GET", f"/incidents/{env.subject}", headers=basic())
    assert response.status == 200
    return response.text


def _section(page: str, heading: str) -> str:
    """The page text from ``heading`` (case-insensitive) up to the next <h2>."""
    found = re.search(re.escape(heading), page, re.IGNORECASE)
    assert found is not None, f"no {heading!r} section on the page"
    end = page.find("<h2", found.end())
    return page[found.start() : end if end != -1 else len(page)]


def _runs(env):
    with env.store.transaction(snapshot=True) as conn:
        return {
            row["run_id"]: row
            for row in conn.execute(
                "SELECT run_id,state,control_generation FROM opspilot_runs WHERE incident_id=%s",
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


def _inputs(env):
    return env.store.read_inputs(env.subject)


def _incident(env):
    with env.store.transaction(snapshot=True) as conn:
        return conn.execute(
            "SELECT state,lifecycle,mode,control_generation,conclusion,current_run_id "
            "FROM opspilot_incidents WHERE incident_id=%s",
            (env.subject,),
        ).fetchone()


def _claimable(env) -> bool:
    return env.subject in env.store.claimable_incidents(limit=1000)


def _evidence_ids(snapshot) -> list[str]:
    found: list[str] = []
    for step in snapshot["steps"]:
        if step["status"] == "late_result":
            continue
        for entry in step["tool_results"] or ():
            evidence_id = entry["result"].get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id and evidence_id not in found:
                found.append(evidence_id)
    return found


def _assert_new_run_opened(env, action, generation, text):
    """U1-A: a new Run is current and claimable, the old Run keeps its
    terminal state, the conclusion is cleared, audit + input recorded."""
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["control_generation"] == generation
    new_run = rebuilt["run"]["run_id"]
    assert new_run != env.old_run
    assert rebuilt["run"]["state"] == "queued"
    assert rebuilt["conclusion"] is None
    assert _claimable(env)
    runs = _runs(env)
    assert set(runs) == {env.old_run, new_run}
    assert _incident(env)["current_run_id"] == new_run
    audit = [r for r in _controls(env) if r["resulting_generation"] == generation]
    assert len(audit) == 1 and audit[0]["action"] == action
    assert audit[0]["actor"] == "alice"
    assert (audit[0]["payload"] or {}).get("text") == text
    notes = [i for i in _inputs(env) if i["control_generation"] == generation]
    assert [(i["kind"], i["content"].get("text")) for i in notes] == [(action, text)]
    return new_run


# --------------------------------------------------------------------------
# 1. follow_up / correct after a published conclusion opens a new Run (U1-A,
#    U4-A investigation history)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,text",
    [
        ("follow_up", "Check the payments dependency as well."),
        ("correct", "Added fact: payments was redeployed at 10:02; the latency hypothesis is wrong."),
    ],
)
def test_note_after_conclusion_opens_new_run_and_page_keeps_history(action, text):
    env = _concluded(action)
    _ok(_act(env, action, 0, text), 1)
    new_run = _assert_new_run_opened(env, action, 1, text)
    assert _runs(env)[env.old_run]["state"] == "completed"
    assert _incident(env)["lifecycle"] == "open"

    new_summary = f"Updated assessment after the human note {uuid4().hex[:10]}."
    _publish(env, new_summary, "Payments redeploy is the more likely cause.")
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["run"]["run_id"] == new_run
    assert rebuilt["run"]["state"] == "completed"
    assert rebuilt["conclusion"] is not None
    assert _runs(env)[env.old_run]["state"] == "completed"

    snapshot = env.workbench.snapshot(env.subject)
    assert snapshot["report"] is not None
    assert snapshot["report"]["summary"] == new_summary
    page = _page(env)
    assert new_summary in page
    history = _section(page, "Investigation history")
    assert str(env.old_run) in history
    assert env.old_summary in history


def test_note_after_conclusion_needs_authentication_and_same_origin():
    env = _concluded("auth")
    fields = ("follow_up", 0, "Check the payments dependency as well.")
    unauthenticated = _act(env, *fields, headers=same_origin())
    assert unauthenticated.status == 401
    cross_origin = _act(
        env, *fields, headers={**basic(), "origin": "https://evil.example.test"}
    )
    assert cross_origin.status == 403
    assert ORIGIN != "https://evil.example.test"
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["control_generation"] == 0
    assert rebuilt["run"]["run_id"] == env.old_run
    assert rebuilt["conclusion"] is not None
    assert _controls(env) == []


# --------------------------------------------------------------------------
# 2. follow_up after cancel opens a new Run (U1-A "已取消的事故")
# --------------------------------------------------------------------------


def test_follow_up_after_cancel_opens_new_run():
    # Cancel is refused once a conclusion exists (item 3), so this incident is
    # cancelled before its first Run publishes.
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"f12-pr1-cancel-{uuid4()}")
    env = SimpleNamespace(
        app=app,
        workbench=workbench,
        store=store,
        events=events,
        evidence=evidence,
        subject=UUID(submitted["incident_id"]),
        old_run=UUID(submitted["run_id"]),
    )
    _ok(_act(env, "cancel", 0), 1)
    assert _incident(env)["state"] == "cancelled"
    assert _runs(env)[env.old_run]["state"] == "cancelled"
    assert not _claimable(env)

    text = "Please investigate again, the errors are back."
    _ok(_act(env, "follow_up", 1, text), 2)
    new_run = _assert_new_run_opened(env, "follow_up", 2, text)
    assert _runs(env)[env.old_run]["state"] == "cancelled"

    summary = f"Investigation after the cancel {uuid4().hex[:10]}."
    _publish(env, summary, "A dependency regression is plausible.")
    rebuilt = store.rebuild(env.subject)
    assert rebuilt["run"]["run_id"] == new_run
    assert rebuilt["run"]["state"] == "completed"
    assert summary in _page(env)


# --------------------------------------------------------------------------
# 3. pause / resume / cancel stay refused after the conclusion
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_pause_resume_cancel_after_conclusion_stay_refused(action):
    env = _concluded(f"refuse-{action}")
    refused = _act(env, action, 0)
    assert refused.status == 409, refused.text
    assert refused.json()["code"] == "ILLEGAL_TRANSITION"
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["control_generation"] == 0
    assert rebuilt["conclusion"] is not None
    assert rebuilt["run"]["run_id"] == env.old_run
    assert _runs(env)[env.old_run]["state"] == "completed"
    assert _controls(env) == []


# --------------------------------------------------------------------------
# 4. resolved / closed: REOPEN_REQUIRED in both modes (U3 part, task record)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["follow_up", "correct"])
@pytest.mark.parametrize("mode", ["automatic", "human_owned"])
@pytest.mark.parametrize("lifecycle", ["resolved", "closed"])
def test_note_on_resolved_or_closed_incident_requires_reopen(lifecycle, mode, action):
    env = _concluded(f"reopen-{lifecycle}-{mode}-{action}")
    generation = 0
    if mode == "human_owned":
        _ok(_act(env, "takeover", 0), 1)
        generation = 1
    with env.store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
            (lifecycle, env.subject),
        )
    runs_before = _runs(env)
    controls_before = _controls(env)
    inputs_before = _inputs(env)

    refused = _act(env, action, generation, "Is it still broken?")
    assert refused.status == 409, refused.text
    assert refused.json()["code"] == "REOPEN_REQUIRED"

    row = _incident(env)
    assert row["control_generation"] == generation
    assert row["lifecycle"] == lifecycle
    assert row["mode"] == mode
    assert row["current_run_id"] == env.old_run
    assert _runs(env) == runs_before
    assert _controls(env) == controls_before
    assert _inputs(env) == inputs_before
    assert not _claimable(env)


# --------------------------------------------------------------------------
# 5. human_owned with a conclusion: notes are only recorded (U7-A)
# --------------------------------------------------------------------------


def test_notes_under_human_ownership_after_conclusion_are_recorded_only():
    env = _concluded("human-owned")
    _ok(_act(env, "takeover", 0), 1)
    after_takeover = _runs(env)
    assert set(after_takeover) == {env.old_run}

    notes = [
        ("correct", "Added fact: the payments pod restarted at 10:02."),
        ("follow_up", "Did the restart line up with the error spike?"),
    ]
    for offset, (action, text) in enumerate(notes):
        _ok(_act(env, action, 1 + offset, text), 2 + offset)
        row = _incident(env)
        assert row["mode"] == "human_owned"
        assert row["control_generation"] == 2 + offset
        assert row["current_run_id"] == env.old_run
        assert set(_runs(env)) == {env.old_run}
        assert _runs(env)[env.old_run]["state"] == after_takeover[env.old_run]["state"]
        assert not _claimable(env)
        with pytest.raises(PersistenceError):
            env.store.claim(
                env.subject, env.old_run, uuid4(), {"state": "v1"}, lease_seconds=30
            )

    audit = _controls(env)
    assert [(r["action"], r["resulting_generation"]) for r in audit] == [
        ("takeover", 1),
        ("correct", 2),
        ("follow_up", 3),
    ]
    assert [(r["payload"] or {}).get("text") for r in audit[1:]] == [
        t for _, t in notes
    ]
    recorded = _inputs(env)
    assert [(i["kind"], i["content"].get("text"), i["control_generation"]) for i in recorded] == [
        ("correct", notes[0][1], 2),
        ("follow_up", notes[1][1], 3),
    ]


# --------------------------------------------------------------------------
# 6. the successor Run sees the prior conclusion as context (U5-A)
# --------------------------------------------------------------------------


def test_successor_input_carries_prior_conclusion_labelled_not_instructions():
    env = _concluded("successor-input")
    text = "Correction: the latency hypothesis is wrong, payments was healthy."
    _ok(_act(env, "correct", 0, text), 1)
    rebuilt = env.store.rebuild(env.subject)
    assert rebuilt["run"]["run_id"] != env.old_run
    stored = rebuilt["run"]["input"]
    assert stored is not None
    question = stored["question"]
    assert env.old_summary in question
    assert env.old_hypothesis in question
    assert _NOT_INSTRUCTIONS.search(question), question


def test_continuation_context_of_a_concluded_run_includes_the_prior_report():
    env = _concluded("continuation-unit")
    snapshot = env.store.rebuild(env.subject)
    assert snapshot["conclusion"] is not None
    cont = continuation_context(
        snapshot, new_run_id=str(uuid4()), authorized_targets=frozenset({TARGET})
    )
    assert cont.previous_run_id == str(env.old_run)
    note = cont.handoff_note
    assert env.old_summary in note
    assert env.old_hypothesis in note
    assert _NOT_INSTRUCTIONS.search(note), note
    # The evidence the prior Run committed is still carried for citation.
    assert set(_evidence_ids(snapshot)) <= set(cont.evidence_ids)


# --------------------------------------------------------------------------
# 7. human decisions show their database time (G1)
# --------------------------------------------------------------------------

_TIME_KEYS = ("decided_at", "created_at", "at", "time", "timestamp", "recorded_at")


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
            forms.add(dt.strftime(f"%Y-%m-%d{sep}%H:%M"))
    return forms


def test_human_decisions_carry_their_database_timestamp():
    env = _concluded("decision-time")
    _ok(_act(env, "takeover", 0), 1)
    _ok(_act(env, "follow_up", 1, "Was a deploy in the window?"), 2)
    db_times = {r["resulting_generation"]: r["created_at"] for r in _controls(env)}
    assert set(db_times) == {1, 2}

    rows = env.workbench.snapshot(env.subject)["controls"]
    assert [r["generation"] for r in rows] == [1, 2]
    for row in rows:
        values = [_as_datetime(row.get(k)) for k in _TIME_KEYS if row.get(k) is not None]
        values = [v for v in values if v is not None]
        assert values, f"no timestamp field on decision row {sorted(row)}"
        assert values[0] == db_times[row["generation"]]

    decisions = _section(_page(env), "Human decisions")
    for moment in db_times.values():
        assert any(f in decisions for f in _rendered_forms(moment)), (
            moment,
            decisions,
        )


# --------------------------------------------------------------------------
# 8. evidence read-back renders HTML for a browser, JSON unchanged (G2)
# --------------------------------------------------------------------------

_BASE_EVIDENCE_KEYS = {
    "evidence_id",
    "run_id",
    "status",
    "adopted",
    "raw_sha256",
    "view_sha256",
    "hashes_verified",
    "projection_revision",
    "observed_at",
    "data_as_of",
    "view",
}


def test_evidence_page_is_html_for_browsers_and_json_otherwise():
    env = _concluded("evidence-view")
    evidence_ids = _evidence_ids(env.store.rebuild(env.subject))
    assert evidence_ids
    evidence_id = evidence_ids[0]
    path = f"/incidents/{env.subject}/evidence/{evidence_id}"

    plain = call(env.app, "GET", path, headers=basic())
    as_json = call(
        env.app, "GET", path, headers={**basic(), "accept": "application/json"}
    )
    for response in (plain, as_json):
        assert response.status == 200
        assert response.headers.get("content-type", "").startswith("application/json")
        body = response.json()
        assert _BASE_EVIDENCE_KEYS <= set(body)
        assert set(body) - _BASE_EVIDENCE_KEYS in ({"raw_utf8"}, {"raw_base64"})
        assert body["evidence_id"] == evidence_id
    raw = as_json.json()["raw_utf8"]

    page = call(
        env.app,
        "GET",
        path,
        headers={**basic(), "accept": "text/html,application/xhtml+xml;q=0.9"},
    )
    assert page.status == 200
    assert page.headers.get("content-type", "").startswith("text/html")
    text = html_lib.unescape(page.text)
    assert evidence_id in text
    assert raw in text
    assert as_json.json()["raw_sha256"] in text
