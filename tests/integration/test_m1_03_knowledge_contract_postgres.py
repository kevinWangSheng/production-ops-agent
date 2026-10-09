"""Independent F13 storage contract tests.

These tests deliberately exercise only the public KnowledgeStore API and the
observable PostgreSQL contract.  The incident/Run/observation rows are small
fixtures inserted by the owner connection because generation itself belongs to
the next F13 step.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge import (
    Actor,
    ConclusionDraft,
    KnowledgeStore,
    ProposalDraft,
    Watermark,
    canonical_json,
    content_sha256,
)
from opspilot.persistence import PersistenceError
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
_OBS_SESSION: UUID | None = None
_OBS_ENDING: UUID | None = None
_RUN: UUID | None = None


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_f13c_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=PG_DUMP)
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def store(scratch_dsn: str) -> Iterator[KnowledgeStore]:
    value = KnowledgeStore(scratch_dsn)
    value.install()
    yield value
    value.close()


@pytest.fixture
def incident(scratch_dsn: str) -> UUID:
    global _OBS_SESSION, _OBS_ENDING, _RUN
    incident_id, run_id, target_id = uuid4(), uuid4(), uuid4()
    session_id, ending_id = uuid4(), uuid4()
    _OBS_SESSION, _OBS_ENDING, _RUN = session_id, ending_id, run_id
    now = datetime.now(timezone.utc)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
            (target_id, f"f13/{target_id}"),
        )
        conn.execute(
            "INSERT INTO opspilot_target_suspensions(target_id) VALUES(%s)",
            (target_id,),
        )
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,control_generation,current_run_id,target_id,observation_generation) VALUES(%s,%s,'completed','resolved',0,%s,%s,0)",
            (incident_id, f"f13-{incident_id}", run_id, target_id),
        )
        conn.execute(
            "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions,input) VALUES(%s,%s,'completed',0,10,%s,%s,%s)",
            (
                run_id,
                incident_id,
                now + timedelta(hours=1),
                '{"v":"1"}',
                '{"question":"f13"}',
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds,state,ended_reason) VALUES(%s,%s,'incident_recovery',%s,%s,0,0,'test',0,0,%s,1,1,1,'completed','recovery_confirmed')",
            (
                session_id,
                incident_id,
                target_id,
                '{"resource_uid":"f13"}',
                now + timedelta(hours=1),
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,transition,lifecycle_before,lifecycle_after) VALUES(%s,%s,%s,'authority_revoked',NULL,'observing_recovery','resolved')",
            (ending_id, session_id, incident_id),
        )
    return incident_id


def _watermark() -> Watermark:
    assert _OBS_SESSION is not None and _OBS_ENDING is not None and _RUN is not None
    return Watermark(0, 0, _OBS_SESSION, _OBS_ENDING, 1, _RUN, 0, "e" * 64)


def _draft(
    store: KnowledgeStore,
    incident: UUID,
    *,
    key: str = "req-1",
    actor: Actor | None = None,
    conclusions=(),
    proposals=(),
    disputes=(),
):
    return store.record_draft(
        incident,
        expected_generation=0,
        idempotency_key=key,
        actor=actor or Actor("worker-1", "worker"),
        content={"impact": "limited", "timeline": []},
        watermark=_watermark(),
        conclusions=conclusions or (_conclusion(),),
        proposals=proposals,
        disputes=disputes,
    )


def _conclusion(key: str = "c1", *, valid: bool = True) -> ConclusionDraft:
    return ConclusionDraft(
        key,
        "findings",
        "observed",
        "model",
        "supported" if valid else "uncertain",
        valid,
        [
            {
                "evidence_id": "e1",
                "scope": "run",
                "window_start": "2026-10-09T00:00:00Z",
                "window_end": "2026-10-09T01:00:00Z",
            }
        ],
    )


def _proposal(key: str = "p1", supersedes: UUID | None = None) -> ProposalDraft:
    return ProposalDraft(
        key, "checkout errors", ["checkout"], {"symptom": "5xx"}, supersedes
    )


def test_migration_has_f13_head_and_owner_cannot_delete_or_truncate(
    scratch_dsn: str,
) -> None:
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == "0008_postmortem_knowledge"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'opspilot_f13_%' OR tablename LIKE 'opspilot_postmortem%' OR tablename LIKE 'opspilot_knowledge_%'"
            )
        }
        assert {
            "opspilot_postmortems",
            "opspilot_postmortem_versions",
            "opspilot_knowledge_revisions",
        } <= tables
        delete_incident = uuid4()
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,control_generation) VALUES(%s,%s,'completed','resolved',0)",
            (delete_incident, f"delete-{delete_incident}"),
        )
        conn.execute(
            "INSERT INTO opspilot_postmortems(postmortem_id,incident_id) VALUES(%s,%s)",
            (delete_incident, delete_incident),
        )
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "DELETE FROM opspilot_postmortems WHERE postmortem_id=%s",
                (delete_incident,),
            )
        for table in (
            "opspilot_postmortems",
            "opspilot_postmortem_versions",
            "opspilot_f13_audit",
        ):
            with pytest.raises(psycopg.Error), conn.transaction():
                conn.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(table)))


def test_draft_persists_watermark_content_hash_and_audit(
    store: KnowledgeStore, incident: UUID
) -> None:
    result = _draft(
        store, incident, conclusions=(_conclusion(),), proposals=(_proposal(),)
    )
    assert result.state == "draft" and result.version == 1 and result.published == {}
    got = store.postmortem_for_incident(incident)
    assert got is not None
    version = got["versions"][0]
    assert version["content_sha256"] == content_sha256(
        canonical_json({"impact": "limited", "timeline": []})
    )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT evidence_snapshot_sha256 FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=1",
            (result.object_id,),
        ).fetchone() == ("e" * 64,)
    assert len(store.audit_trail("postmortem", result.object_id)) == 1


def test_idempotency_replays_identical_result_and_generation_conflict_is_rejected(
    store: KnowledgeStore, incident: UUID
) -> None:
    result = _draft(store, incident, key="idem-1", conclusions=(_conclusion(),))
    with pytest.raises(PersistenceError):
        _draft(store, incident, key="idem-2")
    replay = store.record_draft(
        incident,
        expected_generation=0,
        idempotency_key="idem-1",
        actor=Actor("worker-1", "worker"),
        content={"impact": "limited", "timeline": []},
        watermark=_watermark(),
        conclusions=(_conclusion(),),
    )
    assert replay.replayed is True
    assert replay.object_id == result.object_id and replay.version == result.version


def test_invalid_citations_block_submit_and_dispute_blocks_approval(
    store: KnowledgeStore, incident: UUID
) -> None:
    bad = _draft(
        store,
        incident,
        key="bad-citations",
        conclusions=(_conclusion(valid=False),),
        proposals=(_proposal("bad"),),
    )
    with pytest.raises(PersistenceError):
        store.submit_for_review(
            bad.object_id,
            1,
            expected_generation=1,
            idempotency_key="submit-bad",
            actor=Actor("web", "basic_auth"),
        )
    store.mark_stale(
        bad.object_id,
        1,
        reason="evidence_changed",
        expected_generation=1,
        idempotency_key="stale-bad",
        actor=Actor("worker-1", "worker"),
    )
    good = store.record_draft(
        incident,
        expected_generation=2,
        idempotency_key="good-dispute",
        actor=Actor("worker-1", "worker"),
        content={"impact": "limited"},
        watermark=_watermark(),
        conclusions=(_conclusion(),),
        proposals=(_proposal("disputed"),),
    )
    store.submit_for_review(
        good.object_id,
        2,
        expected_generation=3,
        idempotency_key="submit-good",
        actor=Actor("web", "basic_auth"),
    )
    store.raise_dispute(
        good.object_id,
        2,
        "c1",
        reason="contradictory",
        expected_generation=4,
        idempotency_key="dispute-1",
        actor=Actor("worker-1", "worker"),
    )
    with pytest.raises(PersistenceError):
        store.approve(
            good.object_id,
            2,
            expected_generation=5,
            idempotency_key="approve-disputed",
            actor=Actor("web", "basic_auth"),
        )


def test_review_requires_basic_auth_and_approved_revision_is_immutable(
    store: KnowledgeStore, incident: UUID
) -> None:
    draft = _draft(
        store,
        incident,
        key="review",
        conclusions=(_conclusion(),),
        proposals=(_proposal("review"),),
    )
    store.submit_for_review(
        draft.object_id,
        1,
        expected_generation=1,
        idempotency_key="submit-review",
        actor=Actor("worker-1", "worker"),
    )
    with pytest.raises(PersistenceError):
        store.approve(
            draft.object_id,
            1,
            expected_generation=2,
            idempotency_key="approve-worker",
            actor=Actor("worker-1", "worker"),
        )
    approved = store.approve(
        draft.object_id,
        1,
        expected_generation=2,
        idempotency_key="approve-web",
        actor=Actor("alice", "basic_auth"),
    )
    assert approved.state == "approved" and approved.published
    with psycopg.connect(store.dsn) as conn:
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "UPDATE opspilot_postmortem_versions SET content='{}' WHERE postmortem_id=%s AND version=1",
                (draft.object_id,),
            )
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute("DELETE FROM opspilot_knowledge_revisions")


def test_revoke_leaves_tombstone_and_knowledge_read_returns_provenance(
    store: KnowledgeStore, incident: UUID
) -> None:
    draft = _draft(
        store,
        incident,
        key="revoke",
        conclusions=(_conclusion(),),
        proposals=(_proposal("revoke"),),
    )
    store.submit_for_review(
        draft.object_id,
        1,
        expected_generation=1,
        idempotency_key="submit-revoke",
        actor=Actor("alice", "basic_auth"),
    )
    store.approve(
        draft.object_id,
        1,
        expected_generation=2,
        idempotency_key="approve-revoke",
        actor=Actor("alice", "basic_auth"),
    )
    with psycopg.connect(store.dsn) as conn:
        entry, digest, content = conn.execute(
            "SELECT entry_id,content_sha256,content FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s",
            (draft.object_id,),
        ).fetchone()
    assert store.knowledge_entry(entry)
    store.revoke(
        entry,
        1,
        reason="incorrect",
        expected_generation=1,
        idempotency_key="revoke-1",
        actor=Actor("alice", "basic_auth"),
    )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT state FROM opspilot_knowledge_revision_states WHERE entry_id=%s AND revision=1",
            (entry,),
        ).fetchone() == ("revoked",)
        assert conn.execute(
            "SELECT reason,revoked_by,revoked_by_kind FROM opspilot_knowledge_revocations WHERE entry_id=%s",
            (entry,),
        ).fetchone() == ("incorrect", "alice", "basic_auth")
        assert conn.execute(
            "SELECT content_sha256,content FROM opspilot_knowledge_revisions WHERE entry_id=%s",
            (entry,),
        ).fetchone() == (digest, content)


def test_content_sha256_constraint_rejects_wrong_digest(store: KnowledgeStore) -> None:
    with psycopg.connect(store.dsn) as conn:
        content = '{"x":1}'
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "INSERT INTO opspilot_postmortem_versions(postmortem_id,version,state,content,content_sha256,incident_control_generation,observation_generation,observation_session_id,observation_ending_id,run_count,input_watermark,evidence_snapshot_sha256,generated_by,generated_by_kind) VALUES(%s,1,'draft',%s,%s,0,0,%s,%s,0,0,%s,'w','worker')",
                (uuid4(), content, "0" * 64, uuid4(), uuid4(), "e" * 64),
            )


def test_observer_role_cannot_read_or_write_any_f13_table(scratch_dsn: str) -> None:
    login = f"opspilot_f13_observer_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
    try:
        dsn = make_conninfo(scratch_dsn, user=login)
        with psycopg.connect(dsn) as conn:
            for table in (
                "opspilot_postmortems",
                "opspilot_postmortem_versions",
                "opspilot_f13_audit",
                "opspilot_knowledge_revisions",
            ):
                with pytest.raises(psycopg.Error), conn.transaction():
                    conn.execute(
                        sql.SQL("SELECT * FROM {} LIMIT 1").format(
                            sql.Identifier(table)
                        )
                    )
                with pytest.raises(psycopg.Error), conn.transaction():
                    conn.execute(
                        sql.SQL("INSERT INTO {} DEFAULT VALUES").format(
                            sql.Identifier(table)
                        )
                    )
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.mark.parametrize(
    "action", ["approve", "reject", "return_for_revision", "revoke"]
)
def test_worker_cannot_perform_any_review_action(
    store: KnowledgeStore, incident: UUID, action: str
) -> None:
    """D6: every human review action rejects the worker principal."""
    draft = _draft(store, incident, key=f"worker-{action}", proposals=(_proposal(),))
    review = store.submit_for_review(
        draft.object_id,
        1,
        expected_generation=1,
        idempotency_key=f"worker-submit-{action}",
        actor=Actor("generator", "worker"),
    )
    object_id, generation = draft.object_id, review.generation
    if action == "revoke":
        store.approve(
            object_id,
            1,
            expected_generation=generation,
            idempotency_key="web-before-worker-revoke",
            actor=Actor("alice", "basic_auth"),
        )
        with psycopg.connect(store.dsn) as conn:
            object_id = conn.execute(
                "SELECT entry_id FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s",
                (object_id,),
            ).fetchone()[0]
        generation = 1
    kwargs = dict(
        expected_generation=generation,
        idempotency_key=f"worker-denied-{action}",
        actor=Actor("generator", "worker"),
    )
    if action != "approve":
        kwargs["reason"] = "review decision"
    with pytest.raises(PersistenceError):
        getattr(store, action)(object_id, 1, **kwargs)
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_f13_requests WHERE idempotency_key=%s",
            (kwargs["idempotency_key"],),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "terminal", ["approve", "reject", "return_for_revision", "mark_stale"]
)
def test_terminal_versions_cannot_reenter_review(
    store: KnowledgeStore, incident: UUID, terminal: str
) -> None:
    """D1/D5: every terminal version is read-only; old state cannot reopen."""
    draft = _draft(
        store, incident, key=f"terminal-{terminal}", proposals=(_proposal(),)
    )
    review = store.submit_for_review(
        draft.object_id,
        1,
        expected_generation=1,
        idempotency_key=f"terminal-submit-{terminal}",
        actor=Actor("alice", "basic_auth"),
    )
    kwargs = dict(
        expected_generation=review.generation,
        idempotency_key=f"terminal-action-{terminal}",
        actor=Actor("alice", "basic_auth"),
    )
    if terminal != "approve":
        kwargs["reason"] = (
            "evidence_changed" if terminal == "mark_stale" else "needs correction"
        )
    result = getattr(store, terminal)(draft.object_id, 1, **kwargs)
    with pytest.raises(PersistenceError):
        store.submit_for_review(
            draft.object_id,
            1,
            expected_generation=result.generation,
            idempotency_key=f"terminal-reopen-{terminal}",
            actor=Actor("alice", "basic_auth"),
        )
    with psycopg.connect(store.dsn) as conn:
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "UPDATE opspilot_postmortem_versions SET state='draft',stale_reason=NULL WHERE postmortem_id=%s AND version=1",
                (draft.object_id,),
            )
        assert conn.execute(
            "SELECT state FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=1",
            (draft.object_id,),
        ).fetchone() == (result.state,)


def test_return_and_supersede_use_new_versions_with_immutable_provenance(
    store: KnowledgeStore, incident: UUID
) -> None:
    """D4/D5/D7/D9: return writes a new draft; reviewed replacement appends revision 2."""
    actor = Actor("alice", "basic_auth")
    draft = _draft(store, incident, key="return-generate", proposals=(_proposal(),))
    store.submit_for_review(
        draft.object_id,
        1,
        expected_generation=1,
        idempotency_key="return-submit",
        actor=actor,
    )
    returned = store.return_for_revision(
        draft.object_id,
        1,
        reason="correct timeline",
        expected_generation=2,
        idempotency_key="return-action",
        actor=actor,
    )
    next_draft = store.record_draft(
        incident,
        expected_generation=returned.generation,
        idempotency_key="return-new",
        actor=actor,
        content={"impact": "corrected"},
        watermark=_watermark(),
        conclusions=(_conclusion(),),
        proposals=(_proposal(),),
        revises_version=1,
    )
    assert next_draft.version == 2
    review = store.submit_for_review(
        draft.object_id,
        2,
        expected_generation=next_draft.generation,
        idempotency_key="return-new-submit",
        actor=actor,
    )
    approved = store.approve(
        draft.object_id,
        2,
        expected_generation=review.generation,
        idempotency_key="return-new-approve",
        actor=actor,
    )
    with psycopg.connect(store.dsn) as conn:
        entry = conn.execute(
            "SELECT entry_id FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s",
            (draft.object_id,),
        ).fetchone()[0]
        old = conn.execute(
            "SELECT content,content_sha256,approved_by,approved_by_kind,approved_at FROM opspilot_knowledge_revisions WHERE entry_id=%s AND revision=1",
            (entry,),
        ).fetchone()
        assert old[2:4] == ("alice", "basic_auth") and old[4] is not None
        assert conn.execute(
            "SELECT state,revises_version FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=2",
            (draft.object_id,),
        ).fetchone() == ("approved", 1)
    replacement = store.record_draft(
        incident,
        expected_generation=approved.generation,
        idempotency_key="replace-generate",
        actor=actor,
        content={"impact": "new facts"},
        watermark=_watermark(),
        conclusions=(_conclusion(),),
        proposals=(_proposal("replacement", entry),),
    )
    review = store.submit_for_review(
        draft.object_id,
        3,
        expected_generation=replacement.generation,
        idempotency_key="replace-submit",
        actor=actor,
    )
    with pytest.raises(PersistenceError):
        store.approve(
            draft.object_id,
            3,
            expected_generation=review.generation,
            idempotency_key="replace-missing-generation",
            actor=actor,
        )
    store.approve(
        draft.object_id,
        3,
        expected_generation=review.generation,
        idempotency_key="replace-approve",
        actor=actor,
        entry_generations={entry: 1},
    )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT revision,state FROM opspilot_knowledge_revision_states WHERE entry_id=%s ORDER BY revision",
            (entry,),
        ).fetchall() == [(1, "superseded"), (2, "active")]
        assert (
            conn.execute(
                "SELECT content,content_sha256,approved_by,approved_by_kind,approved_at FROM opspilot_knowledge_revisions WHERE entry_id=%s AND revision=1",
                (entry,),
            ).fetchone()
            == old
        )
        assert conn.execute(
            "SELECT supersedes_revision,source_version,source_proposal_key FROM opspilot_knowledge_revisions WHERE entry_id=%s AND revision=2",
            (entry,),
        ).fetchone() == (1, 3, "replacement")


@pytest.mark.parametrize("changed", ["content", "actor", "watermark"])
def test_idempotency_key_cannot_be_rebound_to_another_request(
    store: KnowledgeStore, incident: UUID, changed: str
) -> None:
    """D9/C3 §9: identical key with different payload or principal is rejected."""
    key = f"rebind-{changed}"
    _draft(store, incident, key=key)
    content, actor, watermark = (
        {"impact": "limited", "timeline": []},
        Actor("worker-1", "worker"),
        _watermark(),
    )
    if changed == "content":
        content = {"impact": "different"}
    elif changed == "actor":
        actor = Actor("another-worker", "worker")
    else:
        watermark = Watermark(0, 0, _OBS_SESSION, _OBS_ENDING, 1, _RUN, 1, "e" * 64)
    with pytest.raises(PersistenceError):
        store.record_draft(
            incident,
            expected_generation=0,
            idempotency_key=key,
            actor=actor,
            content=content,
            watermark=watermark,
            conclusions=(_conclusion(),),
        )


def test_sql_cannot_change_object_generation_without_new_audit(
    store: KnowledgeStore, incident: UUID
) -> None:
    """D7/D9: ordinary owner SQL cannot advance generation without matching audit."""
    draft = _draft(store, incident, key="audit-generation")
    with pytest.raises(psycopg.Error):
        with psycopg.connect(store.dsn) as conn:
            conn.execute(
                "UPDATE opspilot_postmortems SET generation=generation+1 WHERE postmortem_id=%s",
                (draft.object_id,),
            )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT generation FROM opspilot_postmortems WHERE postmortem_id=%s",
            (draft.object_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "column", ["observation_session_id", "observation_ending_id", "last_run_id"]
)
def test_watermark_cannot_bind_foreign_identity(
    store: KnowledgeStore, incident: UUID, column: str
) -> None:
    """D1/public storage contract: unowned session, ending and Run are refused."""
    values = dict(
        incident_control_generation=0,
        observation_generation=0,
        observation_session_id=_OBS_SESSION,
        observation_ending_id=_OBS_ENDING,
        run_count=1,
        last_run_id=_RUN,
        input_watermark=0,
        evidence_snapshot_sha256="e" * 64,
    )
    values[column] = uuid4()
    with pytest.raises(PersistenceError):
        store.record_draft(
            incident,
            expected_generation=0,
            idempotency_key=f"foreign-{column}",
            actor=Actor("worker", "worker"),
            content={"impact": "limited"},
            watermark=Watermark(**values),
            conclusions=(_conclusion(),),
        )
    assert store.postmortem_for_incident(incident) is None
