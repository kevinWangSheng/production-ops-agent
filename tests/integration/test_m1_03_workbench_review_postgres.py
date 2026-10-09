"""M1-03 step 3: the workbench review pages and actions on a real PostgreSQL.

Implementer tests (the independent contract tests live in their own file).
The incident is accepted through the workbench intake, its observation is
ended by SQL, and drafts are written with the worker primitive the way step
2's generator will; everything after that goes through the HTTP routes:
the incident page's postmortem section, the version page, the knowledge
entry page and the five review actions (D5, D6, D9, D25, R2-R6).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge import (
    Actor,
    ConclusionDraft,
    DisputeDraft,
    KnowledgeStore,
    ProposalDraft,
    Watermark,
)
from opspilot.knowledge.contract import EVENT_PAYLOAD_KEYS, PublishedRevisionView
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    PostmortemReview,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=0, max_size=4, timeout=5.0)
WORKER = Actor("worker-1", "worker")
UI = {**basic(), **same_origin()}
HTML = {**UI, "accept": "text/html"}


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = f"opspilot_f13w_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    schema.migrate(scratch, pg_dump=PG_DUMP)
    try:
        yield scratch
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def env(dsn: str) -> Iterator[dict[str, Any]]:
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD), "bob": hash_password("pw-bob")},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-f13",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(store),
        sse_poll_seconds=0.02,
        sse_idle_seconds=0.2,
        review=PostmortemReview(knowledge=knowledge, events=events),
    )
    yield {"app": app, "knowledge": knowledge, "events": events, "dsn": dsn}
    knowledge.close()
    store.close()


def _incident(env: dict[str, Any]) -> tuple[UUID, Watermark]:
    """An incident accepted through intake whose observation has ended."""
    response = post_form(
        env["app"],
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why is checkout erroring?",
            "idempotency_key": f"f13w-{uuid4()}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    incident_id = UUID(response.json()["incident_id"])
    session_id, ending_id = uuid4(), uuid4()
    with psycopg.connect(env["dsn"]) as conn:
        row = conn.execute(
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE opspilot_incidents SET observation_generation=1 WHERE incident_id=%s",
            (incident_id,),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id, incident_id, purpose, "
            "target_id, target, subject_control_generation, observation_generation, "
            "authorized_by, state, ended_reason, authorized_global_generation, "
            "authorized_target_generation, deadline_at, max_samples, "
            "sample_interval_seconds, sustained_window_seconds) VALUES "
            "(%s, %s, 'incident_recovery', %s, '{}', 0, 1, 'op', 'completed', "
            "'recovery_confirmed', 0, 0, clock_timestamp(), 1, 60, 60)",
            (session_id, incident_id, row[0]),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id, session_id, incident_id, "
            "ended_reason, lifecycle_before, lifecycle_after) VALUES "
            "(%s, %s, %s, 'deadline_expired', 'observing_recovery', 'observing_recovery')",
            (ending_id, session_id, incident_id),
        )
        current = conn.execute(
            "SELECT i.control_generation, i.current_run_id, "
            "(SELECT count(*) FROM opspilot_runs r WHERE r.incident_id=i.incident_id), "
            "(SELECT COALESCE(max(n.sequence), 0) FROM opspilot_inputs n "
            "WHERE n.incident_id=i.incident_id) "
            "FROM opspilot_incidents i WHERE i.incident_id=%s",
            (incident_id,),
        ).fetchone()
        assert current is not None
    return incident_id, Watermark(
        incident_control_generation=current[0],
        observation_generation=1,
        observation_session_id=session_id,
        observation_ending_id=ending_id,
        run_count=current[2],
        last_run_id=current[1],
        input_watermark=current[3],
        evidence_snapshot_sha256=env["knowledge"].evidence_snapshot_sha256(incident_id),
    )


def _conclusions(*, citations_valid: bool = True) -> list[ConclusionDraft]:
    return [
        ConclusionDraft(
            key="timeline",
            section="timeline",
            body="12:00 alert; 12:05 Run started",
            author="code",
            certainty="deterministic",
            citations_valid=True,
        ),
        ConclusionDraft(
            key="finding-1",
            section="findings",
            body="checkout error rate rose after the deploy",
            author="model",
            certainty="supported" if citations_valid else "uncertain",
            citations_valid=citations_valid,
            evidence_refs=[
                {
                    "evidence_id": "ev-1",
                    "scope": "checkout",
                    "window_start": "2026-10-09T12:00:00Z",
                    "window_end": "2026-10-09T12:05:00Z",
                    "citable_as_fact": True,
                }
            ],
        ),
    ]


def _draft(env, incident_id, watermark, *, generation=0, **kwargs):
    kwargs.setdefault("conclusions", _conclusions())
    kwargs.setdefault("proposals", [_proposal()])
    return env["knowledge"].record_draft(
        incident_id,
        expected_generation=generation,
        idempotency_key=f"worker-{uuid4()}",
        actor=WORKER,
        content={"impact": "checkout degraded"},
        watermark=watermark,
        **kwargs,
    )


def _proposal(key: str = "kb-1", supersedes: UUID | None = None) -> ProposalDraft:
    return ProposalDraft(
        key=key,
        name=f"checkout errors after deploy ({key})",
        tags=["checkout", "deploy"],
        content={"symptoms": ["5xx"], "checks": ["error rate"], "evidence": ["ev-1"]},
        supersedes_entry_id=supersedes,
    )


def _review(env, draft, action, *, headers=None, **fields):
    form = {
        "action": action,
        "expected_generation": str(fields.pop("expected_generation", draft.generation)),
        "idempotency_key": fields.pop("idempotency_key", f"k-{uuid4().hex}"),
        **fields,
    }
    return post_form(
        env["app"],
        f"/postmortems/{draft.object_id}/versions/{draft.version}/review",
        form,
        headers=headers or UI,
    )


def _schedule(env, incident_id, *, failures: int, next_attempt: str) -> None:
    with psycopg.connect(env["dsn"]) as conn:
        conn.execute(
            "INSERT INTO opspilot_postmortem_generation_jobs(incident_id, "
            "consecutive_failures, next_attempt_at) VALUES (%s, %s, "
            + next_attempt
            + ") ON CONFLICT (incident_id) DO UPDATE SET "
            "consecutive_failures=EXCLUDED.consecutive_failures, "
            "next_attempt_at=EXCLUDED.next_attempt_at",
            (incident_id, failures),
        )


def _events(env, incident_id, kind):
    return [e for e in env["events"].read_after(incident_id, 0) if e.kind == kind]


# --- pages


def test_incident_page_shows_status_and_links_the_version(env) -> None:
    incident_id, watermark = _incident(env)
    page = call(env["app"], "GET", f"/incidents/{incident_id}", headers=basic())
    assert page.status == 200
    assert 'id="postmortem-status">not_generated<' in page.text

    draft = _draft(env, incident_id, watermark)
    page = call(env["app"], "GET", f"/incidents/{incident_id}", headers=basic())
    assert 'id="postmortem-status">under_review<' in page.text
    assert f"/postmortems/{draft.object_id}/versions/1" in page.text


def test_version_page_offers_approve_only_without_disputes_or_failed_citations(
    env,
) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    page = call(
        env["app"], "GET", f"/postmortems/{draft.object_id}/versions/1", headers=basic()
    )
    assert page.status == 200
    assert 'id="form-approve"' in page.text and 'id="form-supersede"' not in page.text
    assert 'id="form-return"' in page.text and 'id="form-reject"' in page.text

    disputed_incident, disputed_watermark = _incident(env)
    disputed = _draft(
        env,
        disputed_incident,
        disputed_watermark,
        disputes=[DisputeDraft("finding-1", "deploy happened after the errors began")],
    )
    page = call(
        env["app"],
        "GET",
        f"/postmortems/{disputed.object_id}/versions/1",
        headers=basic(),
    )
    assert 'id="form-approve"' not in page.text
    assert 'id="form-return"' in page.text
    assert "deploy happened after the errors began" in page.text
    assert 'id="no-approval"' in page.text

    failed_incident, failed_watermark = _incident(env)
    failed = _draft(
        env,
        failed_incident,
        failed_watermark,
        conclusions=_conclusions(citations_valid=False),
    )
    assert failed.state == "draft"
    page = call(
        env["app"],
        "GET",
        f"/postmortems/{failed.object_id}/versions/1",
        headers=basic(),
    )
    assert 'id="no-review"' in page.text and 'id="citation-errors"' in page.text
    assert "<form" not in page.text
    incident_page = call(
        env["app"], "GET", f"/incidents/{failed_incident}", headers=basic()
    )
    assert 'id="postmortem-status">citations_failed<' in incident_page.text


def test_unknown_or_malformed_paths_are_404_and_pages_need_auth(env) -> None:
    for path in (
        f"/postmortems/{uuid4()}/versions/1",
        "/postmortems/not-a-uuid/versions/1",
        f"/postmortems/{uuid4()}/versions/0",
        f"/knowledge/{uuid4()}",
    ):
        assert call(env["app"], "GET", path, headers=basic()).status == 404, path
    assert call(env["app"], "GET", f"/knowledge/{uuid4()}").status == 401


# --- actions


def test_approve_records_the_basic_auth_actor_and_announces_ids_only(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    key = f"k-{uuid4().hex}"
    # a forged actor field is ignored: the actor is the signed-in principal
    response = _review(env, draft, "approve", idempotency_key=key, actor_id="mallory")
    assert response.status == 200, response.text
    body = response.json()
    assert (body["state"], body["generation"], body["replayed"]) == (
        "approved",
        draft.generation + 1,
        False,
    )
    (published,) = body["published"].values()
    audit = env["knowledge"].audit_trail("postmortem", draft.object_id)
    assert [(a["action"], a["actor_id"], a["principal_kind"]) for a in audit][-1] == (
        "approve",
        UI_USER,
        "basic_auth",
    )

    (reviewed,) = _events(env, incident_id, "postmortem_reviewed")
    assert set(reviewed.payload) == set(EVENT_PAYLOAD_KEYS["postmortem_reviewed"])
    assert reviewed.payload["state"] == "approved"
    (changed,) = _events(env, incident_id, "knowledge_changed")
    assert set(changed.payload) == set(EVENT_PAYLOAD_KEYS["knowledge_changed"])
    assert changed.payload["entry_id"] == published["entry_id"]

    replay = _review(env, draft, "approve", idempotency_key=key)
    assert replay.status == 200 and replay.json()["replayed"] is True
    assert len(_events(env, incident_id, "postmortem_reviewed")) == 1

    entry = call(
        env["app"], "GET", f"/knowledge/{published['entry_id']}", headers=basic()
    )
    assert entry.status == 200 and 'id="active-revision">1<' in entry.text
    incident_page = call(
        env["app"], "GET", f"/incidents/{incident_id}", headers=basic()
    )
    assert f"/knowledge/{published['entry_id']}" in incident_page.text


def test_stale_form_is_a_conflict_with_the_current_generation(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    response = _review(env, draft, "approve", expected_generation=draft.generation - 1)
    assert response.status == 409
    assert response.json() == {
        "code": "GENERATION_CONFLICT",
        "error_class": "conflict",
        "current_generation": draft.generation,
    }
    html = _review(
        env, draft, "approve", expected_generation=draft.generation - 1, headers=HTML
    )
    assert html.status == 409
    assert 'id="review-error"' in html.text and "GENERATION_CONFLICT" in html.text


def test_forged_approval_of_a_disputed_version_is_refused(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(
        env,
        incident_id,
        watermark,
        disputes=[DisputeDraft("finding-1", "contradicting evidence")],
    )
    response = _review(env, draft, "approve")
    assert response.status == 422
    assert response.json()["code"] == "DISPUTED"
    assert response.json()["error_class"] == "refused"


def test_reject_and_return_need_a_reason_and_are_terminal(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    missing = _review(env, draft, "return")
    assert missing.status == 400
    assert missing.json()["fields"] == ["reason"]
    returned = _review(env, draft, "return", reason="cite the deploy event")
    assert returned.status == 200 and returned.json()["state"] == "returned"
    (event,) = _events(env, incident_id, "postmortem_reviewed")
    assert event.payload["reason"] == "cite the deploy event"
    again = _review(
        env, draft, "reject", reason="no", expected_generation=draft.generation + 1
    )
    assert again.status == 422 and again.json()["code"] == "ILLEGAL_TRANSITION"


def test_supersede_is_its_own_action_with_complete_entry_generations(env) -> None:
    first_incident, first_watermark = _incident(env)
    first = _draft(env, first_incident, first_watermark)
    approved = _review(env, first, "approve").json()
    entry_id = UUID(next(iter(approved["published"].values()))["entry_id"])
    entry_generation = next(iter(approved["published"].values()))["generation"]

    incident_id, watermark = _incident(env)
    draft = _draft(
        env, incident_id, watermark, proposals=[_proposal("kb-2", supersedes=entry_id)]
    )
    page = call(
        env["app"], "GET", f"/postmortems/{draft.object_id}/versions/1", headers=basic()
    )
    assert 'id="form-supersede"' in page.text and 'id="form-approve"' not in page.text
    assert f'name="entry_generation:{entry_id}" value="{entry_generation}"' in page.text

    as_approve = _review(env, draft, "approve", **{f"entry_generation:{entry_id}": "1"})
    assert as_approve.status == 400 and as_approve.json()["fields"] == ["action"]
    without = _review(env, draft, "supersede")
    assert without.status == 400 and without.json()["fields"] == ["entry_generations"]
    old = _review(
        env,
        draft,
        "supersede",
        **{f"entry_generation:{entry_id}": str(entry_generation - 1)},
    )
    assert old.status == 409
    assert old.json()["code"] == "ENTRY_GENERATION_CONFLICT"
    assert old.json()["entry_generations"] == {str(entry_id): entry_generation}

    done = _review(
        env,
        draft,
        "supersede",
        **{f"entry_generation:{entry_id}": str(entry_generation)},
    )
    assert done.status == 200, done.text
    assert done.json()["published"]["kb-2"]["action"] == "supersede"
    history = call(env["app"], "GET", f"/knowledge/{entry_id}", headers=basic())
    assert 'class="revision-superseded"' in history.text
    assert 'id="active-revision">2<' in history.text


def test_revoke_submits_entry_revision_generation_and_reason(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    published = next(iter(_review(env, draft, "approve").json()["published"].values()))
    entry_id, generation = published["entry_id"], published["generation"]
    path = f"/knowledge/{entry_id}/revoke"
    base = {"revision": "1", "idempotency_key": f"k-{uuid4().hex}", "reason": "wrong"}

    stale = post_form(
        env["app"],
        path,
        {**base, "expected_generation": str(generation - 1)},
        headers=UI,
    )
    assert stale.status == 409
    assert stale.json()["current_generation"] == generation
    no_reason = post_form(
        env["app"],
        path,
        {**base, "reason": "", "expected_generation": str(generation)},
        headers=UI,
    )
    assert no_reason.status == 400

    done = post_form(
        env["app"],
        path,
        {**base, "expected_generation": str(generation), "idempotency_key": "k-rev"},
        headers={**HTML},
    )
    assert done.status == 303 and done.headers["location"] == f"/knowledge/{entry_id}"
    page = call(env["app"], "GET", f"/knowledge/{entry_id}", headers=basic())
    assert 'id="no-active"' in page.text and "wrong" in page.text
    assert 'id="form-revoke"' not in page.text
    changes = _events(env, incident_id, "knowledge_changed")
    assert [c.payload["state"] for c in changes] == ["active", "revoked"]
    assert env["knowledge"].active_revision(UUID(entry_id)) is None


def test_mutations_need_basic_auth_and_a_same_origin_request(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    no_origin = _review(env, draft, "approve", headers=basic())
    assert no_origin.status == 403 and no_origin.json()["code"] == "ORIGIN_REJECTED"
    foreign = _review(
        env, draft, "approve", headers={**basic(), "origin": "https://evil.test"}
    )
    assert foreign.status == 403
    anonymous = _review(env, draft, "approve", headers=same_origin())
    assert anonymous.status == 401
    other_user = _review(
        env, draft, "approve", headers={**basic("bob", "pw-bob"), **same_origin()}
    )
    assert other_user.status == 200
    audit = env["knowledge"].audit_trail("postmortem", draft.object_id)
    assert audit[-1]["actor_id"] == "bob"


def test_an_unknown_action_is_refused_before_the_store(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    for action in ("generate", "mark_stale", "revoke", ""):
        response = _review(env, draft, action)
        assert response.status == 400, action
    assert (
        env["knowledge"].postmortem(draft.object_id)["generation"] == draft.generation
    )


def test_a_stale_version_shows_its_reason_and_offers_no_review(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    env["knowledge"].mark_stale(
        draft.object_id,
        draft.version,
        reason="run_added",
        expected_generation=draft.generation,
        idempotency_key=f"worker-{uuid4()}",
        actor=WORKER,
    )
    page = call(
        env["app"], "GET", f"/postmortems/{draft.object_id}/versions/1", headers=basic()
    )
    assert 'id="stale-reason">run_added<' in page.text
    assert 'id="no-review"' in page.text and "<form" not in page.text
    incident_page = call(
        env["app"], "GET", f"/incidents/{incident_id}", headers=basic()
    )
    assert 'id="postmortem-status">stale<' in incident_page.text
    assert "run_added" in incident_page.text
    late = _review(env, draft, "approve", expected_generation=draft.generation + 1)
    assert late.status == 422 and late.json()["code"] == "ILLEGAL_TRANSITION"


def test_a_moved_watermark_refuses_approval_with_an_explanation(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    # a Run added outside the business transaction that marks stale (D18):
    # a missed marking, which only the store's watermark check catches
    run_id = uuid4()
    with psycopg.connect(env["dsn"]) as conn:
        conn.execute(
            "INSERT INTO opspilot_runs(run_id, incident_id, state, control_generation, "
            "budget_limit, deadline, versions) "
            "VALUES (%s, %s, 'queued', 0, 1, clock_timestamp(), '{}')",
            (run_id, incident_id),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (run_id, incident_id),
        )
    response = _review(env, draft, "approve", headers=HTML)
    assert response.status == 409
    assert "WATERMARK_MOVED" in response.text and "watermark" in response.text
    assert env["knowledge"].postmortem(draft.object_id)["versions"][0]["state"] == (
        "under_review"
    )


def test_superseded_knowledge_is_shown_as_not_retrievable(env) -> None:
    first_incident, first_watermark = _incident(env)
    first = _draft(env, first_incident, first_watermark)
    published = next(iter(_review(env, first, "approve").json()["published"].values()))
    incident_id, watermark = _incident(env)
    draft = _draft(
        env,
        incident_id,
        watermark,
        proposals=[_proposal("kb-2", supersedes=UUID(published["entry_id"]))],
    )
    done = _review(
        env,
        draft,
        "supersede",
        **{f"entry_generation:{published['entry_id']}": str(published["generation"])},
    )
    assert done.status == 200
    page = call(env["app"], "GET", f"/incidents/{first_incident}", headers=basic())
    assert "superseded</span> not retrievable" in page.text
    (row,) = env["knowledge"].knowledge_from_postmortem(first.object_id)
    assert set(row) == set(PublishedRevisionView.__annotations__)
    assert row["state"] == "superseded"


def test_several_citation_failed_drafts_then_a_reviewable_version(env) -> None:
    """D31: citation-failed drafts at one watermark are failed attempts; the
    worker retries within R7's limit, then stops with a visible state."""
    incident_id, watermark = _incident(env)
    failed = _conclusions(citations_valid=False)
    first = _draft(env, incident_id, watermark, conclusions=failed)
    second = _draft(
        env, incident_id, watermark, generation=first.generation, conclusions=failed
    )
    assert (first.state, second.state, second.version) == ("draft", "draft", 2)
    # the worker's schedule as step 2 writes it (R7): failures and the next
    # attempt in one row, ``next_attempt_at`` cleared when exhausted
    _schedule(
        env,
        incident_id,
        failures=2,
        next_attempt="clock_timestamp() + interval '2 minutes'",
    )
    page = call(env["app"], "GET", f"/incidents/{incident_id}", headers=basic())
    assert 'id="postmortem-status">citations_failed<' in page.text
    assert 'id="postmortem-retry"' in page.text and "2 failed attempts" in page.text
    assert page.text.count("citations failed</strong>") == 2

    _schedule(env, incident_id, failures=3, next_attempt="NULL")
    page = call(env["app"], "GET", f"/incidents/{incident_id}", headers=basic())
    assert 'id="postmortem-exhausted"' in page.text and "3 failed attempts" in page.text

    _schedule(env, incident_id, failures=0, next_attempt="NULL")
    third = _draft(env, incident_id, watermark, generation=second.generation)
    assert (third.version, third.state) == (3, "under_review")
    page = call(env["app"], "GET", f"/incidents/{incident_id}", headers=basic())
    assert 'id="postmortem-status">under_review<' in page.text
    assert page.text.count("citations failed</strong>") == 2
    for draft in (first, second):
        version = call(
            env["app"],
            "GET",
            f"/postmortems/{draft.object_id}/versions/{draft.version}",
            headers=basic(),
        )
        assert 'id="no-review"' in version.text and "<form" not in version.text
    assert _review(env, third, "approve").status == 200


def test_a_reason_of_invisible_characters_is_blank(env) -> None:
    incident_id, watermark = _incident(env)
    draft = _draft(env, incident_id, watermark)
    for reason in ("​", "⁠ ​", "﻿"):
        response = _review(env, draft, "reject", reason=reason)
        assert response.status == 400, reason
        assert response.json()["fields"] == ["reason"]
    assert (
        env["knowledge"].postmortem(draft.object_id)["generation"] == draft.generation
    )
