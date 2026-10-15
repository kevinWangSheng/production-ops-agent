"""Independent contract tests for F13 step 4 (PR-B) on a real PostgreSQL.

Written from the task record's public interface
(``docs/tasks/2026-10-15-f13-step4.md``, items 1, 2, 6) and the contract's
deterministic layer (``docs/tasks/2026-10-15-f13-step4-contract.md``,
"验收运行计划"), without reading the implementation. Knowledge is seeded
through the real store primitives (``record_draft`` / ``approve`` /
``revoke``) the way the M1-03 store tests do; only the incident, Run and
observation ending rows are written by SQL.

``KnowledgeStore.knowledge_for_target``: only active revisions, revoked and
superseded handling, target isolation, limit, fresh/stale at the threshold,
ordering, per-entry truncation, ``evidence_refs`` replacement, the total
bound and the exact returned shape. Workbench: a web submit records the
retrieved knowledge in the Run input, and a successor Run after revocation
re-retrieves (U5-A).
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.investigation.context import InvestigationInput
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
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
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
POLICY = "knowledge-hint-v1"
DAY = 86400
MONTH = 2592000
V5 = "opspilot-investigation-input-v5"
FACT_KEYS = {
    "policy_version",
    "status",
    "retrieved_at",
    "max_age_seconds",
    "omitted_for_size",
    "entries",
}
ENTRY_KEYS = {
    "entry_id",
    "revision",
    "name",
    "tags",
    "content",
    "content_sha256",
    "source_incident_id",
    "source_postmortem_id",
    "source_version",
    "approved_by",
    "approved_at",
    "observation_ended_at",
    "age_seconds",
    "freshness",
    "truncated",
}


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = f"opspilot_f13s4_{uuid4().hex[:12]}"
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
def store(dsn: str) -> Iterator[KnowledgeStore]:
    knowledge = KnowledgeStore(dsn, pool=POOL)
    knowledge.install()
    yield knowledge
    knowledge.close()


# --- seeding through the store primitives -------------------------------------


def _key() -> str:
    return f"idem-{uuid4().hex}"


def _target(dsn: str) -> tuple[UUID, str]:
    target_id = uuid4()
    uid = f"uid-{target_id}"
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id, resource_uid) VALUES (%s, %s)",
            (target_id, uid),
        )
    return target_id, uid


def _seed_incident(
    dsn: str, target_id: UUID, *, ended_ago: float = 0.0
) -> tuple[UUID, Watermark]:
    """An incident on ``target_id`` with one completed Run and an ended
    observation session whose ending was recorded ``ended_ago`` seconds
    before now (the knowledge's ``observation_ended_at``)."""
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


def _proposal(
    name: str = "checkout errors after deploy",
    *,
    tags: tuple[str, ...] = ("checkout", "deploy"),
    content: dict[str, Any] | None = None,
    supersedes: UUID | None = None,
) -> ProposalDraft:
    if content is None:
        content = {
            "symptoms": ["5xx"],
            "checks": ["error rate"],
            "evidence_refs": ["ev-old-1", "ev-old-2"],
        }
    return ProposalDraft(
        key="kb-1",
        name=name,
        tags=list(tags),
        content=content,
        supersedes_entry_id=supersedes,
    )


def _publish(
    store: KnowledgeStore,
    dsn: str,
    target_id: UUID,
    proposal: ProposalDraft,
    *,
    ended_ago: float = 0.0,
    entry_generations: dict[UUID, int] | None = None,
) -> dict[str, Any]:
    """Approve one proposal from a fresh incident on ``target_id``."""
    incident_id, watermark = _seed_incident(dsn, target_id, ended_ago=ended_ago)
    draft = store.record_draft(
        incident_id,
        expected_generation=0,
        idempotency_key=_key(),
        actor=WORKER,
        content={"impact": "checkout degraded"},
        watermark=watermark,
        conclusions=_conclusions(),
        proposals=[proposal],
    )
    approved = store.approve(
        draft.object_id,
        draft.version,
        expected_generation=draft.generation,
        idempotency_key=_key(),
        actor=REVIEWER,
        entry_generations=entry_generations,
    )
    published = approved.published["kb-1"]
    return {
        **published,
        "incident_id": incident_id,
        "postmortem_id": draft.object_id,
    }


def _revoke(store: KnowledgeStore, published: dict[str, Any]) -> None:
    store.revoke(
        published["entry_id"],
        published["revision"],
        reason="contradicted by later evidence",
        expected_generation=published["generation"],
        idempotency_key=_key(),
        actor=REVIEWER,
    )


def _retrieve(store, uid, query="checkout errors", **kwargs) -> dict[str, Any]:
    kwargs.setdefault("max_age_seconds", MONTH)
    result = store.knowledge_for_target(uid, query, **kwargs)
    assert set(result) == FACT_KEYS
    assert result["status"] == "ok"
    for entry in result["entries"]:
        assert set(entry) == ENTRY_KEYS
    return result


def _pairs(result) -> list[tuple[str, int]]:
    return [(e["entry_id"], e["revision"]) for e in result["entries"]]


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --- shape and values -------------------------------------------------------------


def test_the_returned_fact_has_the_contract_shape(store, dsn) -> None:
    target_id, uid = _target(dsn)
    published = _publish(store, dsn, target_id, _proposal(), ended_ago=120.0)
    result = _retrieve(store, uid, max_age_seconds=MONTH)
    assert result["policy_version"] == POLICY
    assert result["max_age_seconds"] == MONTH
    assert result["omitted_for_size"] == 0
    assert isinstance(result["retrieved_at"], str)
    retrieved_at = _at(result["retrieved_at"])
    assert retrieved_at.utcoffset() is not None
    [entry] = result["entries"]
    active = store.active_revision(published["entry_id"])
    assert active is not None
    assert entry["entry_id"] == str(published["entry_id"])
    assert entry["revision"] == 1
    assert entry["name"] == "checkout errors after deploy"
    assert list(entry["tags"]) == ["checkout", "deploy"]
    # R2: old evidence ids never reach the prompt; only their count does.
    assert entry["content"] == canonical_json(
        {
            "symptoms": ["5xx"],
            "checks": ["error rate"],
            "evidence_refs": {"omitted": True, "count": 2},
        }
    )
    assert "ev-old-1" not in entry["content"]
    assert entry["content_sha256"] == active["content_sha256"]
    assert entry["truncated"] is False
    assert entry["source_incident_id"] == str(published["incident_id"])
    assert entry["source_postmortem_id"] == str(published["postmortem_id"])
    assert entry["source_postmortem_id"] == str(active["source_postmortem_id"])
    assert entry["source_version"] == 1
    assert entry["approved_by"] == "alice"
    assert isinstance(entry["approved_at"], str)
    assert isinstance(entry["observation_ended_at"], str)
    assert _at(entry["approved_at"]) == active["approved_at"]
    observed = _at(entry["observation_ended_at"])
    assert observed == active["freshness"]["observation_ended_at"]
    assert entry["age_seconds"] == math.floor((retrieved_at - observed).total_seconds())
    assert 119 <= entry["age_seconds"] < 3600
    assert entry["freshness"] == "fresh"


def test_content_without_evidence_refs_is_sent_as_stored(store, dsn) -> None:
    target_id, uid = _target(dsn)
    content = {"symptoms": ["timeouts"], "checks": ["latency p99"]}
    published = _publish(store, dsn, target_id, _proposal(content=content))
    [entry] = _retrieve(store, uid)["entries"]
    active = store.active_revision(published["entry_id"])
    # The record says the top-level ``evidence_refs`` is *replaced*; it does
    # not say whether an absent one is added as ``count: 0``. Both readings
    # are accepted; every other field must be sent as stored.
    sent = json.loads(entry["content"])
    refs = sent.pop("evidence_refs", None)
    assert refs in (None, {"omitted": True, "count": 0})
    assert sent == content
    assert entry["content"] == canonical_json(json.loads(entry["content"]))
    assert entry["content_sha256"] == active["content_sha256"]
    assert entry["truncated"] is False


def test_an_unknown_target_has_no_knowledge(store, dsn) -> None:
    result = _retrieve(store, f"uid-unknown-{uuid4()}")
    assert result["entries"] == []
    assert result["omitted_for_size"] == 0


# --- which revisions --------------------------------------------------------------


def test_only_active_revisions_are_returned(store, dsn) -> None:
    target_id, uid = _target(dsn)
    replaced = _publish(store, dsn, target_id, _proposal("cache stampede"))
    entry_id = replaced["entry_id"]
    newer = _publish(
        store,
        dsn,
        target_id,
        _proposal("cache stampede (revised)", supersedes=entry_id),
        entry_generations={entry_id: replaced["generation"]},
    )
    assert (newer["entry_id"], newer["revision"]) == (entry_id, 2)
    revoked = _publish(store, dsn, target_id, _proposal("bad deploy"))
    kept = _publish(store, dsn, target_id, _proposal("payment provider down"))
    assert _pairs(_retrieve(store, uid)) != []
    _revoke(store, revoked)

    pairs = _pairs(_retrieve(store, uid))
    # Superseded: the new revision only.
    assert (str(entry_id), 2) in pairs
    assert (str(entry_id), 1) not in pairs
    # Revoked: gone.
    assert all(e != str(revoked["entry_id"]) for e, _ in pairs)
    assert (str(kept["entry_id"]), 1) in pairs
    assert len(pairs) == 2

    # Revoking the superseding revision leaves nothing of that entry.
    _revoke(store, newer)
    assert _pairs(_retrieve(store, uid)) == [(str(kept["entry_id"]), 1)]


def test_another_targets_knowledge_is_not_returned(store, dsn) -> None:
    mine, my_uid = _target(dsn)
    other, other_uid = _target(dsn)
    own = _publish(store, dsn, mine, _proposal("checkout errors after deploy"))
    foreign = _publish(store, dsn, other, _proposal("checkout errors after deploy"))
    assert _pairs(_retrieve(store, my_uid)) == [(str(own["entry_id"]), 1)]
    assert _pairs(_retrieve(store, other_uid)) == [(str(foreign["entry_id"]), 1)]


def test_at_most_limit_entries(store, dsn) -> None:
    target_id, uid = _target(dsn)
    for index in range(4):
        _publish(store, dsn, target_id, _proposal(f"checkout pattern {index}"))
    assert len(_retrieve(store, uid)["entries"]) == 3
    assert len(_retrieve(store, uid, limit=1)["entries"]) == 1


# --- freshness and order (U1, U4, R3) ----------------------------------------------


def test_age_equal_to_the_threshold_is_fresh_and_one_more_is_stale(store, dsn) -> None:
    target_id, uid = _target(dsn)
    # Half a second past a whole age so consecutive reads see the same age.
    _publish(store, dsn, target_id, _proposal(), ended_ago=1000.5)
    seen_equal = seen_over = False
    for _ in range(10):
        [probe] = _retrieve(store, uid, max_age_seconds=MONTH)["entries"]
        age = probe["age_seconds"]
        for threshold in (age, age - 1):
            result = _retrieve(store, uid, max_age_seconds=threshold)
            [entry] = result["entries"]
            assert result["max_age_seconds"] == threshold
            got = entry["age_seconds"]
            expected = "stale" if got > threshold else "fresh"
            assert entry["freshness"] == expected, (got, threshold)
            seen_equal |= got == threshold
            seen_over |= got == threshold + 1
        if seen_equal and seen_over:
            break
        time.sleep(0.1)
    assert seen_equal and seen_over


def test_stale_knowledge_is_still_provided_after_fresh_and_rank_orders_within(
    store, dsn
) -> None:
    target_id, uid = _target(dsn)
    query = "payment failure"
    # Fresh, matches the query, approved first.
    matching = _publish(
        store,
        dsn,
        target_id,
        _proposal("payment failure in checkout", tags=("payment", "failure")),
        ended_ago=60.0,
    )
    # Fresh, does not match, approved later.
    plain = _publish(
        store,
        dsn,
        target_id,
        _proposal("cart latency", tags=("cart",), content={"symptoms": ["slow"]}),
        ended_ago=60.0,
    )
    # Stale, matches best and is the newest approval.
    stale = _publish(
        store,
        dsn,
        target_id,
        _proposal(
            "payment failure payment failure",
            tags=("payment", "failure"),
            content={"symptoms": ["payment failure"]},
        ),
        ended_ago=10 * DAY,
    )
    result = _retrieve(store, uid, query, max_age_seconds=DAY)
    assert _pairs(result) == [
        (str(matching["entry_id"]), 1),
        (str(plain["entry_id"]), 1),
        (str(stale["entry_id"]), 1),
    ]
    assert [e["freshness"] for e in result["entries"]] == ["fresh", "fresh", "stale"]
    assert result["entries"][2]["age_seconds"] > DAY


def test_ties_order_by_newest_approval(store, dsn) -> None:
    target_id, uid = _target(dsn)
    first = _publish(store, dsn, target_id, _proposal("alpha"))
    second = _publish(store, dsn, target_id, _proposal("beta"))
    result = _retrieve(store, uid, "zzzunmatched")
    assert _pairs(result) == [
        (str(second["entry_id"]), 1),
        (str(first["entry_id"]), 1),
    ]


# --- size bounds (R2) -----------------------------------------------------------------


def test_content_over_4096_bytes_is_truncated(store, dsn) -> None:
    target_id, uid = _target(dsn)
    # 2000 characters, 6000 UTF-8 bytes: the bound is in bytes.
    content = {"notes": "検" * 2000, "evidence_refs": ["ev-old-1"]}
    published = _publish(store, dsn, target_id, _proposal(content=content))
    [entry] = _retrieve(store, uid)["entries"]
    active = store.active_revision(published["entry_id"])
    assert entry["truncated"] is True
    assert len(entry["content"].encode("utf-8")) <= 4096
    assert entry["content_sha256"] == active["content_sha256"]
    assert "ev-old-1" not in entry["content"]


def test_the_total_bound_drops_the_lowest_ranked_entries(store, dsn) -> None:
    target_id, uid = _target(dsn)
    published = [
        _publish(
            store,
            dsn,
            target_id,
            _proposal(f"big {index}", content={"notes": "x" * 5000}),
        )
        for index in range(3)
    ]
    result = _retrieve(store, uid, "zzzunmatched")
    entries = result["entries"]
    assert all(e["truncated"] is True for e in entries)
    assert len(canonical_json(entries).encode("utf-8")) <= 12288
    assert result["omitted_for_size"] == 1
    # Ranked newest approval first (no text match); the oldest is dropped.
    assert _pairs(result) == [
        (str(published[2]["entry_id"]), 1),
        (str(published[1]["entry_id"]), 1),
    ]


# --- workbench (interface item 6, U5-A) ----------------------------------------------


@pytest.fixture(scope="module")
def env(dsn: str) -> Iterator[dict[str, Any]]:
    durable = DurableStore(dsn, pool=POOL)
    durable.install()
    events = DurableEventLog(durable)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    workbench = Workbench(
        incidents=DurableIncidentStore(durable),
        events=events,
        evidence=DurableEvidenceStore(durable),
        ledger=DurableWebLedger(durable),
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        knowledge=knowledge,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-f13s4",
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
        "knowledge": knowledge,
        "dsn": dsn,
        "workbench": workbench,
    }
    knowledge.close()
    durable.close()


def _submit(env, question: str = "Why is checkout erroring?") -> tuple[UUID, UUID]:
    response = post_form(
        env["app"],
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": question,
            "idempotency_key": f"f13s4-{uuid4()}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    body = response.json()
    return UUID(body["incident_id"]), UUID(body["run_id"])


def _end_observation(env, incident_id: UUID) -> Watermark:
    """End the accepted incident's observation by SQL (as the M1-03
    workbench tests do) and return the draft watermark."""
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
    return Watermark(
        incident_control_generation=current[0],
        observation_generation=1,
        observation_session_id=session_id,
        observation_ending_id=ending_id,
        run_count=current[2],
        last_run_id=current[1],
        input_watermark=current[3],
        evidence_snapshot_sha256=env["knowledge"].evidence_snapshot_sha256(incident_id),
    )


def _current_input(env, incident_id: UUID) -> tuple[UUID, InvestigationInput]:
    run = env["store"].rebuild(incident_id)["run"]
    assert run["input"] is not None
    return UUID(str(run["run_id"])), InvestigationInput.from_json(run["input"])


def _control(env, incident_id: UUID, fields: dict[str, str]):
    return post_form(
        env["app"], f"/incidents/{incident_id}/control", fields, headers=UI
    )


def test_a_later_incident_reads_the_knowledge_and_a_successor_drops_the_revoked(
    env,
) -> None:
    knowledge: KnowledgeStore = env["knowledge"]
    # I1 on checkout-prod: accepted through the web, observation ended,
    # postmortem approved -> K1 r1 sourced from I1.
    first, _ = _submit(env)
    watermark = _end_observation(env, first)
    draft = knowledge.record_draft(
        first,
        expected_generation=0,
        idempotency_key=_key(),
        actor=WORKER,
        content={"impact": "checkout degraded"},
        watermark=watermark,
        conclusions=_conclusions(),
        proposals=[_proposal("checkout payment failures")],
    )
    approved = knowledge.approve(
        draft.object_id,
        draft.version,
        expected_generation=draft.generation,
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    k1 = approved.published["kb-1"]

    # I2 on the same target: its first Run's input carries K1 r1.
    second, run_id = _submit(env, "Is checkout payment failing again?")
    current_run, recorded = _current_input(env, second)
    assert current_run == run_id
    assert recorded.version == V5
    fact = recorded.scope_facts["knowledge"]
    assert fact["status"] == "ok"
    assert fact["max_age_seconds"] == MONTH
    pairs = [(e["entry_id"], e["revision"]) for e in fact["entries"]]
    assert (str(k1["entry_id"]), 1) in pairs
    [entry] = [e for e in fact["entries"] if e["entry_id"] == str(k1["entry_id"])]
    assert entry["source_incident_id"] == str(first)
    assert entry["freshness"] == "fresh"

    # Revoke K1, then cancel + new_run on I2: the successor re-retrieves.
    knowledge.revoke(
        k1["entry_id"],
        1,
        reason="root cause was elsewhere",
        expected_generation=k1["generation"],
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    assert (
        _control(
            env,
            second,
            {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
        ).status
        == 200
    )
    assert (
        _control(
            env,
            second,
            {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
        ).status
        == 200
    )
    successor_run, successor = _current_input(env, second)
    assert successor_run != run_id
    assert successor.version == V5
    after = successor.scope_facts["knowledge"]
    assert after["status"] == "ok"
    assert all(e["entry_id"] != str(k1["entry_id"]) for e in after["entries"])
    # Re-retrieved, not copied: a new retrieval instant.
    assert after["retrieved_at"] != fact["retrieved_at"]
