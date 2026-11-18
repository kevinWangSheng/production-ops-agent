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
            if (
                isinstance(evidence_id, str)
                and evidence_id
                and evidence_id not in found
            ):
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


# --------------------------------------------------------------------------
# 5. human_owned with a conclusion: notes are only recorded (U7-A)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 6. the successor Run sees the prior conclusion as context (U5-A)
# --------------------------------------------------------------------------


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


def test_relay_pass_reaches_an_eligible_incident_past_blocked_ones():
    """Review F1 (R2 / P2-2 fallback): incidents whose unread messages
    predate a human cancel of their continuation (human control first) are
    not relay candidates, so a pass bounded by ``limit`` still reaches an
    eligible incident ranked after them."""
    app, workbench, store, events, evidence = _build()

    def published(tag):
        submitted = _submit(app, f"f12-pr1-relay-{tag}-{uuid4()}")
        env = SimpleNamespace(
            app=app,
            workbench=workbench,
            store=store,
            events=events,
            evidence=evidence,
            subject=UUID(submitted["incident_id"]),
        )
        _publish(env, f"Checkout errors {tag}.", f"Payments latency {tag}.")
        return env

    limit = 3
    blocked = []
    for n in range(limit + 1):
        env = published(f"blocked-{n}")
        sent = _act(env, "message", 0, "Still valid?")
        assert sent.json().get("run_id"), sent.text
        _ok(_act(env, "cancel", 0), 1)
        blocked.append(env.subject)
    # Bot finding on #702: a relay continuation that ended before reading its
    # message (parked as the deadline sweep parks it) keeps the message
    # unread; relaying it again is a no-op and must not crowd the pass.
    parked = []
    for n in range(limit + 1):
        env = published(f"parked-{n}")
        sent = _act(env, "message", 0, "Anything new?")
        assert sent.json().get("run_id"), sent.text
        with store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_runs SET state='waiting_human',owner=NULL,"
                "lease_until=NULL WHERE run_id=%s",
                (UUID(sent.json()["run_id"]),),
            )
        parked.append(env.subject)
    eligible = published("eligible")
    # The in-request relay is skipped (as when it fails, P2-2): the message
    # is admitted at the store and only the background pass can relay it.
    store.append_message(
        eligible.subject,
        uuid4(),
        {"text": "Explain the finding.", "channel": "investigation"},
        actor="operator",
    )
    with store.transaction() as conn:
        # Rank every blocked incident ahead of the eligible one.
        conn.execute(
            "UPDATE opspilot_incidents SET priority=0 WHERE incident_id=ANY(%s)",
            (blocked + parked,),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET priority=2 WHERE incident_id=%s",
            (eligible.subject,),
        )
    before = store.rebuild(eligible.subject)["run"]["run_id"]

    opened = workbench.relay_pass(limit=limit)

    current = store.rebuild(eligible.subject)["run"]
    assert current["run_id"] != before and current["state"] == "queued"
    assert current["run_id"] in opened
    for subject in blocked:
        assert store.rebuild(subject)["run"]["state"] == "cancelled"
    for subject in parked:
        assert store.rebuild(subject)["run"]["state"] == "waiting_human"
