"""Black-box PostgreSQL acceptance tests for M1-03 step 2 (F13/D1--D23)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.investigation.loop import ModelError, ModelReply
from opspilot.knowledge import Actor, DisputeDraft, KnowledgeStore
from opspilot.knowledge.contract import EVENT_PAYLOAD_KEYS
from opspilot.knowledge.jobs import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    GENERATION_LEASE_SECONDS,
    MAX_ATTEMPTS_PER_WATERMARK,
    GenerationStore,
)
from opspilot.knowledge.worker import PostmortemWorker
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.web.events import MemoryEventLog
from opspilot.web.evidence import DurableEvidenceStore
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
POOL = PoolConfig(min_size=0, max_size=3, timeout=5.0)


def _db() -> str:
    name = f"opspilot_f13_d2_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    return name


def _drop(name: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


@pytest.fixture(scope="module")
def dsn():
    name = _db()
    value = make_conninfo(DSN, dbname=name)
    schema.migrate(value, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
    try:
        yield value
    finally:
        _drop(name)


@pytest.fixture
def stores(dsn):
    knowledge = KnowledgeStore(dsn, pool=POOL)
    jobs = GenerationStore(dsn, pool=POOL)
    knowledge.install()
    jobs.install()
    yield knowledge, jobs
    jobs.close()
    knowledge.close()


def _ended_incident(dsn: str, *, ended_at: datetime | None = None) -> UUID:
    incident, target, run = uuid4(), uuid4(), uuid4()
    session, ending = uuid4(), uuid4()
    ended_at = ended_at or datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id, resource_uid) VALUES (%s,%s)",
            (target, f"uid-{target}"),
        )
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,target_id,observation_generation) VALUES (%s,%s,'investigating','resolved',%s,1)",
            (incident, f"intake-{incident}", target),
        )
        conn.execute(
            "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) VALUES (%s,%s,'completed',0,2,%s,'{}')",
            (run, incident, ended_at),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (run, incident),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,state,ended_reason,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds) VALUES (%s,%s,'incident_recovery',%s,'{}',0,1,'test','completed','recovery_confirmed',0,0,%s,1,60,60)",
            (session, incident, target, ended_at),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,lifecycle_before,lifecycle_after,recorded_at) VALUES (%s,%s,%s,'deadline_expired','observing_recovery','observing_recovery',%s)",
            (ending, session, incident, ended_at),
        )
    return incident


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, call):
        self.calls.append(call)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reply(payload):
    return ModelReply(json.dumps(payload), None, (), "stop", "deepseek-flash", {}, {})


def _valid_model_output():
    return {
        "narrative_sections": [
            {
                "key": "impact",
                "section": "impact_summary",
                "body": "impact",
                "evidence_ids": ["missing"],
            }
        ],
        "conclusions": [],
        "proposals": [],
        "disputes": [],
    }


def test_d17_candidates_are_oldest_first_and_claim_is_single_lease(stores, dsn):
    _, jobs = stores
    old = _ended_incident(dsn, ended_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = _ended_incident(dsn, ended_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert jobs.candidates(limit=2)[:2] == (old, new)
    versions = {
        "model": "deepseek-flash",
        "model_profile": "p",
        "prompt_version": "q",
        "output_schema_version": "o",
        "input_policy_version": "i",
    }
    assert (
        jobs.claim(old, owner=uuid4(), versions=versions, max_model_requests=2)
        is not None
    )
    assert (
        jobs.claim(old, owner=uuid4(), versions=versions, max_model_requests=2) is None
    )


def test_d23_budget_is_independent_and_bounded(stores, dsn):
    _, jobs = stores
    claim = jobs.claim(
        _ended_incident(dsn),
        owner=uuid4(),
        versions={
            "model": "m",
            "model_profile": "p",
            "prompt_version": "q",
            "output_schema_version": "o",
            "input_policy_version": "i",
        },
        max_model_requests=2,
    )
    assert claim is not None
    assert jobs.reserve_model_request(claim) == 1
    assert jobs.reserve_model_request(claim) == 2
    with pytest.raises(PersistenceError, match="BUDGET_EXHAUSTED"):
        jobs.reserve_model_request(claim)


def test_d26_snapshot_shape_distinguishes_not_generated(stores, dsn):
    knowledge, _ = stores
    view = knowledge.incident_postmortem(_ended_incident(dsn))
    assert view["status"] == "not_generated" and view["postmortem"] is None
    assert set(view["schedule"]) == {
        "pending_regeneration_version",
        "lease_active",
        "consecutive_failures",
        "next_attempt_at",
        "last_error_code",
    }
    assert view["attempts"] == []


def test_d22_provider_failure_is_generation_failed_and_writes_no_version(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    model = ScriptedModel([ModelError("MODEL_UNAVAILABLE")])
    outcome = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    ).generate(incident)
    assert outcome.status == "failed" and outcome.error_code == "MODEL_UNAVAILABLE"
    assert outcome.postmortem_id is None and len(model.calls) == 1
    assert knowledge.incident_postmortem(incident)["status"] == "generation_failed"


def test_d22_invalid_output_after_one_repair_is_bounded(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    model = ScriptedModel([_reply({"bad": []}), _reply({"still_bad": []})])
    outcome = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    ).generate(incident)
    assert (
        outcome.status == "failed"
        and outcome.error_code == "OUTPUT_INVALID"
        and len(model.calls) == 2
    )
    assert knowledge.incident_postmortem(incident)["attempts"][0]["model_requests"] == 2


def test_d22_citation_failure_is_distinct_draft_and_never_under_review(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    model = ScriptedModel(
        [_reply(_valid_model_output()), _reply(_valid_model_output())]
    )
    outcome = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    ).generate(incident)
    assert outcome.status == "succeeded" and outcome.state == "draft"
    view = knowledge.incident_postmortem(incident)
    assert (
        view["status"] == "citations_failed"
        and view["postmortem"]["versions"][0]["state"] == "draft"
    )
    assert view["postmortem"]["versions"][0]["conclusions"][0]["author"] == "code"
    document = json.loads(view["postmortem"]["versions"][0]["content"])
    assert document["generation"]["model"] == "deepseek-flash"
    assert document["generation"]["model_requests"] <= 2
    assert document["generation"]["prompt_version"]
    assert document["validation"]["citations_valid"] is False


def test_d21_credentials_never_reach_model_call(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    secret = "db-password-should-never-leak"
    durable = DurableStore(dsn, pool=POOL)
    try:
        durable.append_input(
            incident, uuid4(), {"text": f"operator pasted password={secret}"}
        )
        model = ScriptedModel(
            [_reply(_valid_model_output()), _reply(_valid_model_output())]
        )
        outcome = PostmortemWorker(
            knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
        ).generate(incident)
        assert outcome.status == "succeeded"
        assert model.calls
        wire = "\n".join(
            str(message) for call in model.calls for message in call.messages
        )
        assert secret not in wire
    finally:
        durable.close()


def test_d21_read_input_shape_and_limits_are_checked_at_call_time(
    stores, dsn, monkeypatch
):
    _knowledge, jobs = stores
    incident = _ended_incident(dsn)
    raw = jobs.read_input(incident)
    import opspilot.knowledge.generation as generation

    monkeypatch.setattr(generation, "MAX_INPUT_BYTES", 1)
    with pytest.raises(generation.InputTooLarge) as exc:
        generation.build_input(raw)
    assert exc.value.limit == "MAX_INPUT_BYTES"


def test_d1_authorized_observation_is_not_a_candidate(stores, dsn):
    _knowledge, jobs = stores
    incident = _ended_incident(dsn)
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s", (incident,)
        ).fetchone()
        session = uuid4()
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,state,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds) VALUES (%s,%s,'incident_recovery',%s,'{}',0,1,'test','authorized',0,0,clock_timestamp(),1,60,60)",
            (session, incident, row[0]),
        )
    assert incident not in jobs.candidates(limit=50)


def test_d18_compensation_scan_marks_a_row_changed_behind_product_back(stores, dsn):
    knowledge, jobs = stores
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _seed_incident,
    )

    incident, watermark = _seed_incident(dsn)
    draft = _draft(knowledge, incident, watermark)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_inputs(input_id,incident_id,sequence,kind,content,actor,control_generation) "
            "VALUES (%s,%s,1,'event','{\"text\":\"new fact\"}','test',0)",
            (uuid4(), incident),
        )
    # Direct SQL bypasses the synchronous product hook: this version is still
    # reviewable in storage until the compensation scanner repairs it.
    assert (
        knowledge.postmortem_for_incident(incident)["versions"][0]["state"]
        == "under_review"
    )
    stale = next(
        row for row in jobs.stale_versions(limit=50) if row["incident_id"] == incident
    )
    assert stale["postmortem_id"] == draft.object_id
    assert stale["reason"] == "input_added"
    assert (
        PostmortemWorker(
            knowledge=knowledge, jobs=jobs, model=ScriptedModel([]), stale_batch=50
        ).compensate_stale()
        >= 1
    )
    assert (
        knowledge.postmortem_for_incident(incident)["versions"][0]["state"] == "stale"
    )


def test_d3_d16_dispute_is_visible_and_blocks_approval(dsn):
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _key,
        _seed_incident,
    )

    knowledge = KnowledgeStore(dsn, pool=POOL)
    knowledge.install()
    try:
        incident, watermark = _seed_incident(dsn)
        draft = _draft(
            knowledge,
            incident,
            watermark,
            disputes=[DisputeDraft("finding-1", "contradictory evidence")],
        )
        with pytest.raises(PersistenceError, match="DISPUTED"):
            knowledge.approve(
                draft.object_id,
                1,
                expected_generation=draft.generation,
                idempotency_key=_key(),
                actor=Actor("alice", "basic_auth"),
            )
        row = next(
            c
            for c in knowledge.postmortem(draft.object_id)["versions"][0]["conclusions"]
            if c["conclusion_key"] == "finding-1"
        )
        assert (
            row["dispute_state"] == "disputed"
            and row["disputes"][0]["reason"] == "contradictory evidence"
        )
    finally:
        knowledge.close()


def test_d18_evidence_commit_marks_draft_stale(dsn):
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _seed_incident,
    )

    knowledge = KnowledgeStore(dsn, pool=POOL)
    durable = DurableStore(dsn, pool=POOL)
    knowledge.install()
    try:
        incident, watermark = _seed_incident(dsn)
        _draft(knowledge, incident, watermark)
        evidence_id = f"ev-{uuid4()}"
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,committed) VALUES(%s,%s,%s,'ok',true,%s,%s,%s,%s,%s,%s,false)",
                (
                    evidence_id,
                    str(watermark.last_run_id),
                    str(incident),
                    b"evidence",
                    "a" * 64,
                    "{}",
                    "b" * 64,
                    "p1",
                    datetime.now(timezone.utc),
                ),
            )
        DurableEvidenceStore(durable).commit(
            evidence_id, {"status": "ok", "adopted": True}
        )
        version = knowledge.incident_postmortem(incident)["postmortem"]["versions"][0]
        assert (
            version["state"] == "stale"
            and version["stale_reason"] == "evidence_changed"
        )
    finally:
        durable.close()
        knowledge.close()


def test_d18_control_takeover_marks_draft_stale(dsn):
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _seed_incident,
    )

    knowledge = KnowledgeStore(dsn, pool=POOL)
    durable = DurableStore(dsn, pool=POOL)
    knowledge.install()
    try:
        incident, watermark = _seed_incident(dsn)
        with psycopg.connect(dsn) as conn:
            target = conn.execute(
                "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO opspilot_target_suspensions(target_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (target,),
            )
        _draft(knowledge, incident, watermark)
        durable.control(incident, 0, "takeover", "operator")
        version = knowledge.incident_postmortem(incident)["postmortem"]["versions"][0]
        assert (
            version["state"] == "stale"
            and version["stale_reason"] == "control_generation_changed"
        )
    finally:
        durable.close()
        knowledge.close()


def test_d19_return_regenerates_with_reason_and_revises_version(stores, dsn):
    knowledge, jobs = stores
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _key,
        _seed_incident,
    )

    incident, watermark = _seed_incident(dsn)
    draft = _draft(knowledge, incident, watermark)
    returned = knowledge.return_for_revision(
        draft.object_id,
        1,
        reason="add rollback timeline",
        expected_generation=draft.generation,
        idempotency_key=_key(),
        actor=Actor("alice", "basic_auth"),
    )
    assert returned.state == "returned"
    before = knowledge.incident_postmortem(incident)["schedule"]
    assert before["pending_regeneration_version"] == 1
    model = ScriptedModel(
        [_reply(_valid_model_output()), _reply(_valid_model_output())]
    )
    outcome = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    ).generate(incident)
    assert outcome.status == "succeeded"
    snapshot = knowledge.incident_postmortem(incident)
    assert snapshot["schedule"]["pending_regeneration_version"] is None
    new_version = snapshot["postmortem"]["versions"][-1]
    assert new_version["version"] == 2
    assert new_version["revises_version"] == 1
    assert any(
        "add rollback timeline" in str(message)
        for call in model.calls
        for message in call.messages
    )


def test_d8_observer_has_no_privilege_on_step_two_tables(dsn):
    with psycopg.connect(dsn) as conn:
        for table in (
            "opspilot_postmortem_generation_jobs",
            "opspilot_postmortem_generation_attempts",
        ):
            assert (
                conn.execute(
                    "SELECT has_table_privilege('opspilot_observer', %s, 'SELECT')",
                    (table,),
                ).fetchone()[0]
                is False
            )
            assert (
                conn.execute(
                    "SELECT has_table_privilege('opspilot_observer', %s, 'INSERT')",
                    (table,),
                ).fetchone()[0]
                is False
            )
            assert (
                conn.execute(
                    "SELECT has_table_privilege('opspilot_observer', %s, 'UPDATE')",
                    (table,),
                ).fetchone()[0]
                is False
            )
            assert (
                conn.execute(
                    "SELECT has_table_privilege('opspilot_observer', %s, 'DELETE')",
                    (table,),
                ).fetchone()[0]
                is False
            )


def test_d23_expired_attempt_is_abandoned_by_next_claim(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    versions = {
        "model": "m",
        "model_profile": "p",
        "prompt_version": "q",
        "output_schema_version": "o",
        "input_policy_version": "i",
    }
    owner = uuid4()
    claim = jobs.claim(incident, owner=owner, versions=versions, max_model_requests=2)
    assert claim is not None
    assert jobs.reserve_model_request(claim) == 1
    attempt_id = knowledge.incident_postmortem(incident)["attempts"][0]["attempt_id"]
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_postmortem_generation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE incident_id=%s",
            (incident,),
        )
    with psycopg.connect(dsn) as conn:
        before_reap = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    assert (
        jobs.claim(incident, owner=uuid4(), versions=versions, max_model_requests=2)
        is None
    )
    view = knowledge.incident_postmortem(incident)
    abandoned = next(a for a in view["attempts"] if a["attempt_id"] == attempt_id)
    assert abandoned["status"] == "abandoned"
    assert abandoned["error_code"] == "LEASE_LOST"
    assert abandoned["finished_at"] is not None
    assert abandoned["model_requests"] == 1
    schedule = view["schedule"]
    assert schedule["consecutive_failures"] == 1
    assert schedule["last_error_code"] == "LEASE_LOST"
    assert schedule["lease_active"] is False
    assert schedule["next_attempt_at"] > before_reap
    assert incident not in jobs.candidates(limit=100)
    with pytest.raises(PersistenceError, match="^LEASE_LOST$"):
        jobs.reserve_model_request(claim)
    # Clock injection only; all attempt state remains observed via the public
    # snapshot. A new attempt has a new budget, the abandoned one stays frozen.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_postmortem_generation_jobs SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE incident_id=%s",
            (incident,),
        )
    next_claim = jobs.claim(
        incident, owner=uuid4(), versions=versions, max_model_requests=2
    )
    assert next_claim is not None
    assert jobs.reserve_model_request(next_claim) == 1
    assert jobs.reserve_model_request(next_claim) == 2
    with pytest.raises(PersistenceError, match="^BUDGET_EXHAUSTED$"):
        jobs.reserve_model_request(next_claim)
    attempts = knowledge.incident_postmortem(incident)["attempts"]
    assert attempts[0]["attempt_id"] != attempt_id
    assert attempts[0]["status"] == "running"
    assert attempts[0]["model_requests"] == 2
    abandoned = next(a for a in attempts if a["attempt_id"] == attempt_id)
    assert abandoned["status"] == "abandoned" and abandoned["model_requests"] == 1


def test_d18_append_input_marks_existing_draft_stale_in_same_business_flow(dsn):
    """The sync stale hook is observable through the frozen postmortem view."""
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _seed_incident,
    )

    incident, watermark = _seed_incident(dsn)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    durable = DurableStore(dsn, pool=POOL)
    knowledge.install()
    try:
        _draft(knowledge, incident, watermark)
        durable.append_input(incident, uuid4(), {"text": "new fact"})
        version = knowledge.incident_postmortem(incident)["postmortem"]["versions"][0]
        assert version["state"] == "stale" and version["stale_reason"] == "input_added"
    finally:
        durable.close()
        knowledge.close()


def test_d17_poll_once_is_batch_bounded_and_event_payload_allowlisted(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    events = MemoryEventLog()
    worker = PostmortemWorker(
        knowledge=knowledge,
        jobs=jobs,
        model=ScriptedModel([ModelError("MODEL_UNAVAILABLE")]),
        events=events,
        owner=uuid4(),
        batch=1,
    )
    assert len(worker.poll_once()) == 1
    for event in events.read_after(incident, 0, limit=20):
        assert set(event.payload) <= set(EVENT_PAYLOAD_KEYS[event.kind])


def test_d18_compensation_scan_exposes_fixed_contract_limits(stores):
    knowledge, jobs = stores
    assert (
        PostmortemWorker(
            knowledge=knowledge, jobs=jobs, model=ScriptedModel([]), stale_batch=7
        ).compensate_stale()
        >= 0
    )
    assert (
        GENERATION_LEASE_SECONDS,
        MAX_ATTEMPTS_PER_WATERMARK,
        BACKOFF_BASE_SECONDS,
        BACKOFF_MAX_SECONDS,
    ) == (900, 5, 60, 3600)


def test_pending_q1_sync_stale_audit_uses_system_worker_principal(stores, dsn):
    knowledge, _jobs = stores
    from tests.integration.test_m1_03_knowledge_store_postgres import (
        _draft,
        _seed_incident,
    )

    incident, watermark = _seed_incident(dsn)
    durable = DurableStore(dsn, pool=POOL)
    try:
        draft = _draft(knowledge, incident, watermark)
        durable.append_input(incident, uuid4(), {"text": "new"})
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT principal_kind,actor_id FROM opspilot_f13_audit "
                "WHERE object_kind='postmortem' AND object_id=%s AND action='mark_stale' "
                "ORDER BY recorded_at DESC LIMIT 1",
                (draft.object_id,),
            ).fetchone()
        assert row is not None
        assert tuple(row) == ("worker", "system:append_input")
    finally:
        durable.close()


def test_pending_q2_one_generation_per_watermark(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    model = ScriptedModel(
        [_reply(_valid_model_output()), _reply(_valid_model_output())]
    )
    worker = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    )
    first = worker.generate(incident)
    assert first.status == "succeeded"
    assert incident not in jobs.candidates(limit=20)


def test_pending_q3_invalid_and_citation_failures_are_distinct(stores, dsn):
    knowledge, jobs = stores
    incident = _ended_incident(dsn)
    model = ScriptedModel([_reply({"bad": []}), _reply({"still_bad": []})])
    failed = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    ).generate(incident)
    assert failed.status == "failed" and failed.error_code == "OUTPUT_INVALID"
    incident2 = _ended_incident(dsn)
    model.replies[:] = [_reply(_valid_model_output()), _reply(_valid_model_output())]
    cited = PostmortemWorker(
        knowledge=knowledge, jobs=jobs, model=model, owner=uuid4()
    ).generate(incident2)
    assert cited.status == "succeeded" and cited.state == "draft"
