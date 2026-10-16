"""Independent acceptance tests for F13 step 4 (PR-C) on a real PostgreSQL.

Written from ``docs/tasks/2026-10-15-f13-step4-acceptance.md`` (public
interface, acceptance verdicts), the contract's deterministic layer
(``docs/tasks/2026-10-15-f13-step4-contract.md``, "验收运行计划": page and
projection agree field by field; a stub model's tagged report is judged
correctly) and the PR-B interface (``docs/tasks/2026-10-15-f13-step4.md``),
without reading the implementation.

Two approved knowledge entries on the target the web submits against: one
fresh, one stale (its observation ended ten days ago, the workbench threshold
is one day). A real ``Workbench`` web submit creates the incident; the real
runner and worker loop drive it with a scripted model whose final report tags
the fresh entry as a ``fact`` and the stale one as a ``rejected_hypothesis``,
both citing this Run's evidence. ``knowledge_use_records`` then reads through
the workbench's own ``IncidentStore``. Knowledge is seeded through the store
primitives (``record_draft`` / ``approve`` / ``revoke``); only the source
incident, Run and observation-ending rows are written by SQL, as the PR-B
contract tests do.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance import (
    IncidentScenario,
    knowledge_use_outcome,
    knowledge_use_records,
)
from opspilot.investigation.runner import InvestigationRunner
from opspilot.knowledge import (
    Actor,
    ConclusionDraft,
    KnowledgeStore,
    ProposalDraft,
    Watermark,
    canonical_json,
    content_sha256,
)
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.tools.fixture import (
    FIXTURE_TARGET,
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
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
from opspilot.worker import Worker
from opspilot.worker_main import WorkerLoop
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import ScriptedModel, reply, tool_call
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
REVIEWER = Actor("alice", "basic_auth")
WORKER = Actor("worker-1", "worker")
EVIDENCE_HASH = content_sha256(canonical_json([]))
UI = {**basic(), **same_origin()}
DAY = 86400
QUESTION = "Why is checkout erroring?"
TIME_REF = "policy-window-1"


# --- database and stack ------------------------------------------------------------


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = f"opspilot_f13s4c_{uuid4().hex[:12]}"
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


class _Scoped:
    """Queue and sweeper limited to the incidents a test names."""

    def __init__(self, store: DurableStore, incidents) -> None:
        self._store = store
        self._ids = tuple(incidents)

    def claimable_incidents(self, *, limit: int = 20):
        listed = self._store.claimable_incidents(limit=10_000)
        return tuple(i for i in listed if i in self._ids)[:limit]

    def sweep_expired_runs(self, *, incident_id=None, limit: int = 100):
        parked: list = []
        for subject in self._ids:
            if incident_id is None or incident_id == subject:
                parked.extend(
                    self._store.sweep_expired_runs(incident_id=subject, limit=limit)
                )
        return tuple(parked)

    def sweep_expired_runs_with_generations(
        self, *, incident_id=None, limit: int = 100
    ):
        parked: list = []
        for subject in self._ids:
            if incident_id is None or incident_id == subject:
                parked.extend(
                    self._store.sweep_expired_runs_with_generations(
                        incident_id=subject, limit=limit
                    )
                )
        return tuple(parked)


@pytest.fixture(scope="module")
def env(dsn: str) -> Iterator[dict[str, Any]]:
    durable = DurableStore(dsn, pool=POOL)
    durable.install()
    events = DurableEventLog(durable)
    events.install()
    evidence = DurableEvidenceStore(durable)
    evidence.install()
    ledger = DurableWebLedger(durable)
    ledger.install()
    knowledge = KnowledgeStore(dsn, pool=POOL)
    knowledge.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(durable),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        knowledge=knowledge,
        knowledge_max_age_seconds=DAY,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-f13s4c",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(durable),
        sse_poll_seconds=0.02,
        sse_idle_seconds=0.2,
    )
    yield {
        "app": app,
        "store": durable,
        "events": events,
        "evidence": evidence,
        "knowledge": knowledge,
        "dsn": dsn,
        "workbench": workbench,
    }
    knowledge.close()
    durable.close()


# --- seeding knowledge on the submitted target ---------------------------------------


def _key() -> str:
    return f"idem-{uuid4().hex}"


def _seed_source_incident(
    dsn: str, target_id: UUID, *, ended_ago: float
) -> tuple[UUID, Watermark]:
    """A source incident on ``target_id`` whose observation ended
    ``ended_ago`` seconds before now (the knowledge's
    ``observation_ended_at``)."""
    incident_id, run_id = uuid4(), uuid4()
    session_id, ending_id = uuid4(), uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id, intake_key, state, lifecycle, "
            "target_id, observation_generation) "
            "VALUES (%s, %s, 'investigating', 'resolved', %s, 1)",
            (incident_id, f"key-{incident_id}", target_id),
        )
        conn.execute(
            "INSERT INTO opspilot_runs(run_id, incident_id, state, control_generation, "
            "budget_limit, deadline, versions) "
            "VALUES (%s, %s, 'completed', 0, 1, clock_timestamp(), '{}')",
            (run_id, incident_id),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (run_id, incident_id),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id, incident_id, purpose, "
            "target_id, target, subject_control_generation, observation_generation, "
            "authorized_by, state, ended_reason, authorized_global_generation, "
            "authorized_target_generation, deadline_at, max_samples, "
            "sample_interval_seconds, sustained_window_seconds) VALUES "
            "(%s, %s, 'incident_recovery', %s, '{}', 0, 1, 'op', 'completed', "
            "'recovery_confirmed', 0, 0, clock_timestamp(), 1, 60, 60)",
            (session_id, incident_id, target_id),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id, session_id, incident_id, "
            "ended_reason, lifecycle_before, lifecycle_after, recorded_at) VALUES "
            "(%s, %s, %s, 'deadline_expired', 'observing_recovery', "
            "'observing_recovery', clock_timestamp() - make_interval(secs => %s))",
            (ending_id, session_id, incident_id, ended_ago),
        )
    return incident_id, Watermark(
        incident_control_generation=0,
        observation_generation=1,
        observation_session_id=session_id,
        observation_ending_id=ending_id,
        run_count=1,
        last_run_id=run_id,
        input_watermark=0,
        evidence_snapshot_sha256=EVIDENCE_HASH,
    )


def _conclusions() -> list[ConclusionDraft]:
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
            certainty="supported",
            citations_valid=True,
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


def _publish(
    knowledge: KnowledgeStore,
    dsn: str,
    target_id: UUID,
    name: str,
    *,
    ended_ago: float,
) -> dict[str, Any]:
    incident_id, watermark = _seed_source_incident(dsn, target_id, ended_ago=ended_ago)
    draft = knowledge.record_draft(
        incident_id,
        expected_generation=0,
        idempotency_key=_key(),
        actor=WORKER,
        content={"impact": "checkout degraded"},
        watermark=watermark,
        conclusions=_conclusions(),
        proposals=[
            ProposalDraft(
                key="kb-1",
                name=name,
                tags=["checkout"],
                content={
                    "symptoms": ["5xx"],
                    "checks": ["error rate"],
                    "evidence_refs": ["ev-old-1"],
                },
            )
        ],
    )
    approved = knowledge.approve(
        draft.object_id,
        draft.version,
        expected_generation=draft.generation,
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    return {**approved.published["kb-1"], "source_incident_id": incident_id}


def _tag(published: dict[str, Any]) -> str:
    return f"[knowledge:{published['entry_id']}@r{published['revision']}]"


# --- web and worker -------------------------------------------------------------------


def _submit(env, question: str = QUESTION) -> tuple[UUID, UUID]:
    response = post_form(
        env["app"],
        "/intake/ui",
        {
            "target_id": FIXTURE_TARGET,
            "question": question,
            "idempotency_key": f"f13s4c-{uuid4()}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    body = response.json()
    return UUID(body["incident_id"]), UUID(body["run_id"])


def _control(env, incident_id: UUID, fields: dict[str, str]):
    return post_form(
        env["app"], f"/incidents/{incident_id}/control", fields, headers=UI
    )


def _tool_round():
    return reply(
        tool_calls=[
            tool_call(
                call_id="call-1",
                name=FIXTURE_TOOL,
                arguments=json.dumps({"expr": "rate(http_errors[5m])"}),
            )
        ],
        finish="tool_calls",
    )


def _tagged_report(consistent: dict[str, Any], contradicted: dict[str, Any]):
    """Final round: a fact tagged with the fresh entry and a rejected
    hypothesis tagged with the stale one, both citing the evidence id the
    tool round delivered in this Run."""

    def build(request):
        evidence_id = None
        for message in reversed(request.messages):
            if message.get("role") == "tool":
                evidence_id = json.loads(message["content"])["evidence_id"]
                break
        assert evidence_id is not None
        claim_scope = {"target_refs": [FIXTURE_TARGET], "time_scope_ref": TIME_REF}
        report = {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "Checkout errors are visible; payment failure is ruled out.",
            "claims": [
                {
                    "kind": "fact",
                    "text": f"{_tag(consistent)} The error-rate sample is "
                    "consistent with the reviewed checkout pattern.",
                    "evidence_ids": [evidence_id],
                    **claim_scope,
                },
                {
                    "kind": "rejected_hypothesis",
                    "text": f"{_tag(contradicted)} The current sample does not "
                    "support the older payment-failure pattern.",
                    "evidence_ids": [evidence_id],
                    **claim_scope,
                },
            ],
            "gaps": ["No HealthProfile was supplied."],
            "next_steps": ["Have a human compare the sample against the SLO."],
        }
        return reply(content=json.dumps(report, ensure_ascii=False))

    return build


def _investigate(env, incident_id: UUID, replies) -> ScriptedModel:
    store: DurableStore = env["store"]
    clock = DurableClock(store)
    model = ScriptedModel(list(replies))
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(fixture_versions())),
        model=model,
        executor_factory=fixture_executor_factory(
            store, evidence=env["evidence"], clock=clock
        ),
        clock=clock,
        lease_seconds=60,
        events=env["events"],
        evidence=env["evidence"],
    )
    scoped = _Scoped(store, (incident_id,))
    loop = WorkerLoop(
        queue=scoped,
        runner=runner,
        sweeper=scoped,
        events=env["events"],
        stop=threading.Event(),
        poll_seconds=0.05,
        batch=20,
    )
    results = loop.poll_once()
    assert [(i, o.status) for i, o in results] == [(incident_id, "published")]
    return model


def _scenario(incident_id: UUID) -> IncidentScenario:
    return IncidentScenario("f13-step4-pg", "F13", "4", "incident", str(incident_id))


def _outcome(env, incident_id: UUID):
    records = knowledge_use_records(env["workbench"].incidents, incident_id)
    return knowledge_use_outcome(_scenario(incident_id), records)


# --- the investigated incident (shared, read-only for the tests below) ----------------


@pytest.fixture(scope="module")
def investigated(env) -> dict[str, Any]:
    store: DurableStore = env["store"]
    target_id = store.register_target(FIXTURE_TARGET)
    fresh = _publish(
        env["knowledge"], env["dsn"], target_id, "checkout errors", ended_ago=60.0
    )
    stale = _publish(
        env["knowledge"],
        env["dsn"],
        target_id,
        "checkout payment failure",
        ended_ago=10 * DAY,
    )
    incident_id, run_id = _submit(env)
    model = _investigate(
        env, incident_id, [_tool_round(), _tagged_report(fresh, stale)]
    )
    assert len(model.calls) == 2
    return {
        "incident_id": incident_id,
        "run_id": run_id,
        "fresh": fresh,
        "stale": stale,
    }


def _hint(outcome, published: dict[str, Any]):
    found = [
        h
        for h in outcome.entries
        if h.entry_id == str(published["entry_id"])
        and h.revision == published["revision"]
    ]
    assert len(found) == 1, [(h.entry_id, h.revision) for h in outcome.entries]
    return found[0]


def test_the_stub_models_tagged_report_is_judged_by_the_projection(
    env, investigated
) -> None:
    outcome = _outcome(env, investigated["incident_id"])
    assert outcome.status == "ok"
    assert outcome.unknown_reasons == ()
    assert outcome.run_id == str(investigated["run_id"])
    assert outcome.run_state == "completed"
    assert outcome.report_available is True
    assert outcome.max_age_seconds == DAY
    assert outcome.policy_version == "knowledge-hint-v1"
    assert len(outcome.run_evidence_ids) >= 1
    assert outcome.unprovided_tags == ()
    assert outcome.model_requests == ()

    # Run 1 shape: the current fresh revision, consistent and handled.
    fresh = _hint(outcome, investigated["fresh"])
    assert fresh.freshness == "fresh"
    assert fresh.verdict == "consistent"
    assert fresh.claim_kinds == ("fact",)
    assert fresh.handled is True
    assert fresh.source_incident_id == str(investigated["fresh"]["source_incident_id"])
    [claim] = fresh.tagged_claims
    assert claim["kind"] == "fact"
    assert claim["evidence_ids"]
    assert set(claim["evidence_ids"]) <= set(outcome.run_evidence_ids)

    # Run 3 shape: stale, contradicted with this Run's evidence, handled,
    # and no fact rests on it.
    stale = _hint(outcome, investigated["stale"])
    assert stale.freshness == "stale"
    assert stale.age_seconds > DAY
    assert stale.verdict == "contradicted"
    assert stale.handled is True
    assert "fact" not in stale.claim_kinds
    assert stale.claim_kinds == ("rejected_hypothesis",)
    [rejected] = stale.tagged_claims
    assert set(rejected["evidence_ids"]) <= set(outcome.run_evidence_ids)

    # Fresh knowledge is ranked before stale knowledge (PR-B item 1).
    order = [h.entry_id for h in outcome.entries]
    assert order.index(fresh.entry_id) < order.index(stale.entry_id)


# --- page and projection agree ---------------------------------------------------------


class _Hints(HTMLParser):
    """The ``<li>`` items inside ``#knowledge-hints``: text and links."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.depth = 0
        self.items: list[dict[str, Any]] = []
        self._item: dict[str, Any] | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.depth:
            self.depth += 1 if tag not in {"br", "img", "input", "hr"} else 0
        elif tag == "section" and attrs.get("id") == "knowledge-hints":
            self.depth = 1
            return
        if not self.depth:
            return
        if tag == "li":
            self._item = {"text": "", "links": []}
        if tag == "a" and self._item is not None:
            self._item["links"].append(attrs.get("href"))

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "li" and self._item is not None:
            self._item["text"] = " ".join(self._item["text"].split())
            self.items.append(self._item)
            self._item = None
        self.depth -= 1

    def handle_data(self, data):
        if self.depth and self._item is not None:
            self._item["text"] += data


def test_the_page_and_the_snapshot_agree_with_the_projection(env, investigated) -> None:
    incident_id = investigated["incident_id"]
    outcome = _outcome(env, incident_id)

    snapshot = env["workbench"].snapshot(incident_id)
    view = snapshot["knowledge"]
    assert view is not None
    assert view["status"] == outcome.status
    assert view["policy_version"] == outcome.policy_version
    assert view["retrieved_at"] == outcome.retrieved_at
    assert view["max_age_seconds"] == outcome.max_age_seconds
    assert view["omitted_for_size"] == outcome.omitted_for_size
    assert len(view["entries"]) == len(outcome.entries)
    assert len(view["checks"]) == len(outcome.entries)
    for entry, check, hint in zip(
        view["entries"], view["checks"], outcome.entries, strict=True
    ):
        assert entry["entry_id"] == hint.entry_id
        assert entry["revision"] == hint.revision
        assert entry["freshness"] == hint.freshness
        assert entry["age_seconds"] == hint.age_seconds
        assert check["entry_id"] == hint.entry_id
        assert check["revision"] == hint.revision
        assert check["verdict"] == hint.verdict
        assert tuple(check["claim_kinds"]) == hint.claim_kinds

    page = call(env["app"], "GET", f"/incidents/{incident_id}", headers=basic())
    assert page.status == 200, page.text
    items = _Hints(page.text).items
    assert len(items) == len(outcome.entries)
    for item, hint in zip(items, outcome.entries, strict=True):
        assert f"/knowledge/{hint.entry_id}" in item["links"]
        assert f"/incidents/{hint.source_incident_id}" in item["links"]
        text = item["text"]
        revisions = re.findall(r"\br(\d+)\b", text)
        assert revisions and int(revisions[0]) == hint.revision, text
        assert re.search(rf"\b{hint.freshness}\b", text), text
        other = "fresh" if hint.freshness == "stale" else "stale"
        assert not re.search(rf"\b{other}\b", text), text
        ages = re.findall(r"\bage (\d+)", text)
        assert ages and int(ages[0]) == hint.age_seconds, text
        verdicts = re.findall(r"\bcheck: (\w+)", text)
        assert verdicts == [hint.verdict], text


# --- revocation: a later Run no longer carries the revoked revision --------------------


def test_a_successor_run_and_a_new_incident_drop_the_revoked_revision(
    env, investigated
) -> None:
    knowledge: KnowledgeStore = env["knowledge"]
    fresh = investigated["fresh"]
    revoked = (str(fresh["entry_id"]), fresh["revision"])

    # A later incident before the revocation still carries it.
    later, later_run = _submit(env, "Is checkout erroring again?")
    before = _outcome(env, later)
    assert before.status == "ok"
    assert before.run_id == str(later_run)
    assert revoked in [(h.entry_id, h.revision) for h in before.entries]
    # Not investigated yet: no report, nothing judged.
    assert before.report_available is False
    assert before.run_evidence_ids == ()
    assert all(h.verdict == "untagged" and not h.handled for h in before.entries)

    knowledge.revoke(
        fresh["entry_id"],
        fresh["revision"],
        reason="root cause was elsewhere",
        expected_generation=fresh["generation"],
        idempotency_key=_key(),
        actor=REVIEWER,
    )

    # Successor Run of the same incident (cancel + new_run) re-retrieves.
    assert (
        _control(
            env,
            later,
            {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
        ).status
        == 200
    )
    renewed = _control(
        env,
        later,
        {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
    )
    assert renewed.status == 200, renewed.text
    successor = _outcome(env, later)
    assert successor.status == "ok"
    assert successor.run_id is not None and successor.run_id != str(later_run)
    pairs = [(h.entry_id, h.revision) for h in successor.entries]
    assert revoked not in pairs
    assert all(entry_id != revoked[0] for entry_id, _ in pairs)
    # The stale entry is still active and still provided.
    stale = investigated["stale"]
    assert (str(stale["entry_id"]), stale["revision"]) in pairs

    # A new incident after the revocation does not carry it either.
    newest, _ = _submit(env, "Checkout errors once more?")
    after = _outcome(env, newest)
    assert after.status == "ok"
    assert revoked not in [(h.entry_id, h.revision) for h in after.entries]

    # The investigated incident's own record is unchanged by the revocation:
    # its Run input is a frozen snapshot (C3:152).
    original = _outcome(env, investigated["incident_id"])
    assert revoked in [(h.entry_id, h.revision) for h in original.entries]
