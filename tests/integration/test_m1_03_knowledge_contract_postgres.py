"""Independent PostgreSQL contract tests for the F13 persistence slice."""

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
    DisputeDraft,
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
def incident(scratch_dsn: str) -> tuple[UUID, UUID, UUID, UUID]:
    incident_id, run_id, target_id = uuid4(), uuid4(), uuid4()
    session_id, ending_id = uuid4(), uuid4()
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
        # A confirmed ending requires a sample_id; use the non-confirming terminal
        # form because this slice only needs the ended-session watermark.
        conn.execute(
            "UPDATE opspilot_observation_sessions SET ended_reason='authority_revoked' WHERE session_id=%s",
            (session_id,),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,transition,lifecycle_before,lifecycle_after) VALUES(%s,%s,%s,'authority_revoked',NULL,'observing_recovery','resolved')",
            (ending_id, session_id, incident_id),
        )
    return incident_id, session_id, ending_id, run_id


def _watermark(store: KnowledgeStore, ids: tuple[UUID, UUID, UUID, UUID]) -> Watermark:
    _, session_id, ending_id, run_id = ids
    return Watermark(
        0,
        0,
        session_id,
        ending_id,
        1,
        run_id,
        0,
        store.evidence_snapshot_sha256(ids[0]),
    )


def _conclusion(key: str = "c1", valid: bool = True) -> ConclusionDraft:
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
                "citable_as_fact": valid,
            }
        ],
    )


def _proposal(key: str = "p1", supersedes: UUID | None = None) -> ProposalDraft:
    return ProposalDraft(
        key, "checkout errors", ["checkout"], {"symptom": "5xx"}, supersedes
    )


def _draft(
    store: KnowledgeStore,
    ids: tuple[UUID, UUID, UUID, UUID],
    *,
    key: str,
    conclusions=(_conclusion(),),
    proposals=(),
    disputes=(),
    actor: Actor | None = None,
    expected: int = 0,
    content=None,
    revises: int | None = None,
):
    return store.record_draft(
        ids[0],
        expected_generation=expected,
        idempotency_key=key,
        actor=actor or Actor("worker-1", "worker"),
        content=content or {"impact": "limited", "timeline": []},
        watermark=_watermark(store, ids),
        conclusions=conclusions,
        proposals=proposals,
        disputes=disputes,
        revises_version=revises,
    )


def _entry(store: KnowledgeStore, postmortem_id: UUID) -> UUID:
    with psycopg.connect(store.dsn) as conn:
        return conn.execute(
            "SELECT entry_id FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s ORDER BY revision DESC LIMIT 1",
            (postmortem_id,),
        ).fetchone()[0]


def test_migration_and_owner_append_only_guards(scratch_dsn: str) -> None:
    with psycopg.connect(scratch_dsn) as conn:
        assert schema.current_revision(conn) == schema.head_revision()
        for table in (
            "opspilot_postmortems",
            "opspilot_postmortem_versions",
            "opspilot_f13_audit",
        ):
            with pytest.raises(psycopg.Error), conn.transaction():
                conn.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(table)))
        inc = uuid4()
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,control_generation) VALUES(%s,%s,'completed','resolved',0)",
            (inc, f"guard-{inc}"),
        )
        conn.execute(
            "INSERT INTO opspilot_postmortems(postmortem_id,incident_id) VALUES(%s,%s)",
            (inc, inc),
        )
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "DELETE FROM opspilot_postmortems WHERE postmortem_id=%s", (inc,)
            )


def test_worker_generation_auto_enters_review_with_watermark_hash_and_audit(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    result = _draft(store, incident, key="generate", proposals=(_proposal(),))
    assert result.state == "under_review" and result.generation == 2
    got = store.postmortem_for_incident(incident[0])
    assert got and got["versions"][0]["content_sha256"] == content_sha256(
        canonical_json({"impact": "limited", "timeline": []})
    )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT evidence_snapshot_sha256 FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=1",
            (result.object_id,),
        ).fetchone() == (store.evidence_snapshot_sha256(incident[0]),)
    assert len(store.audit_trail("postmortem", result.object_id)) == 2


def test_basic_auth_cannot_generate_or_mark_stale(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    with pytest.raises(PersistenceError):
        _draft(store, incident, key="web-generate", actor=Actor("alice", "basic_auth"))
    draft = _draft(
        store, incident, key="stale-worker", conclusions=(_conclusion(valid=False),)
    )
    with pytest.raises(PersistenceError):
        store.mark_stale(
            draft.object_id,
            1,
            reason="evidence_changed",
            expected_generation=1,
            idempotency_key="web-stale",
            actor=Actor("alice", "basic_auth"),
        )


def test_invalid_citations_stay_draft_and_cannot_be_approved(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft = _draft(
        store,
        incident,
        key="invalid",
        conclusions=(_conclusion(valid=False),),
        proposals=(_proposal(),),
    )
    assert draft.state == "draft" and draft.generation == 1
    with pytest.raises(PersistenceError):
        store.approve(
            draft.object_id,
            1,
            expected_generation=1,
            idempotency_key="approve-invalid",
            actor=Actor("alice", "basic_auth"),
        )


def test_evidence_refs_require_d2_shape_and_fact_citability(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    """D2: model evidence bindings must be complete and fact-capable."""
    missing_field = [
        {
            "evidence_id": "e1",
            "scope": "run",
            "window_start": "2026-10-09T00:00:00Z",
            "window_end": "2026-10-09T01:00:00Z",
        }
    ]
    non_fact = [
        {
            "evidence_id": "e1",
            "scope": "run",
            "window_start": "2026-10-09T00:00:00Z",
            "window_end": "2026-10-09T01:00:00Z",
            "citable_as_fact": False,
        }
    ]
    for key, refs in (
        ("missing-ref-field", missing_field),
        ("supported-non-fact", non_fact),
        ("model-without-binding", []),
    ):
        conclusion = ConclusionDraft(
            "c1", "findings", "observed", "model", "supported", True, refs
        )
        with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
            _draft(store, incident, key=key, conclusions=(conclusion,))


def test_dispute_is_written_with_draft_cannot_be_cleared_and_only_return_is_allowed(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft = _draft(
        store,
        incident,
        key="disputed",
        proposals=(_proposal(),),
        disputes=(DisputeDraft("c1", "contradictory evidence"),),
    )
    assert draft.state == "under_review"
    with psycopg.connect(store.dsn) as conn:
        row = conn.execute(
            "SELECT reason,raised_by,raised_by_kind FROM opspilot_postmortem_disputes WHERE postmortem_id=%s",
            (draft.object_id,),
        ).fetchone()
        assert row == ("contradictory evidence", "worker-1", "worker")
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "DELETE FROM opspilot_postmortem_disputes WHERE postmortem_id=%s",
                (draft.object_id,),
            )
    with pytest.raises(PersistenceError):
        store.approve(
            draft.object_id,
            1,
            expected_generation=2,
            idempotency_key="approve-disputed",
            actor=Actor("alice", "basic_auth"),
        )
    returned = store.return_for_revision(
        draft.object_id,
        1,
        reason="resolve dispute",
        expected_generation=2,
        idempotency_key="return-disputed",
        actor=Actor("alice", "basic_auth"),
    )
    assert returned.state == "returned"


@pytest.mark.parametrize(
    "action", ["approve", "reject", "return_for_revision", "revoke"]
)
def test_worker_cannot_perform_review_actions(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID], action: str
) -> None:
    draft = _draft(store, incident, key=f"worker-{action}", proposals=(_proposal(),))
    object_id, version, generation = draft.object_id, 1, draft.generation
    if action == "revoke":
        store.approve(
            object_id,
            version,
            expected_generation=generation,
            idempotency_key="before-revoke",
            actor=Actor("alice", "basic_auth"),
        )
        object_id, version, generation = _entry(store, object_id), 1, 1
    kwargs = {
        "expected_generation": generation,
        "idempotency_key": f"denied-{action}",
        "actor": Actor("worker", "worker"),
    }
    if action != "approve":
        kwargs["reason"] = "review decision"
    with pytest.raises(PersistenceError):
        getattr(store, action)(object_id, version, **kwargs)


def test_basic_auth_review_records_actor_and_approved_revision_is_immutable(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft = _draft(store, incident, key="approve", proposals=(_proposal(),))
    approved = store.approve(
        draft.object_id,
        1,
        expected_generation=draft.generation,
        idempotency_key="approve-web",
        actor=Actor("alice", "basic_auth"),
    )
    assert approved.state == "approved" and approved.published
    entry = _entry(store, draft.object_id)
    active = store.active_revision(entry)
    assert (
        active
        and active["approved_by"] == "alice"
        and active["approved_by_kind"] == "basic_auth"
    )
    assert active["state"] == "active"
    assert active["source_postmortem_id"] == draft.object_id
    assert active["source_version"] == 1
    assert active["source_proposal_key"] == "p1"
    assert set(active["freshness"]) == {
        "approved_at",
        "generated_at",
        "observation_ended_at",
        "evidence_snapshot_sha256",
    }
    assert active["freshness"]["approved_at"] is not None
    assert active["freshness"]["generated_at"] is not None
    assert active["freshness"]["observation_ended_at"] is not None
    assert active["freshness"][
        "evidence_snapshot_sha256"
    ] == store.evidence_snapshot_sha256(incident[0])
    with psycopg.connect(store.dsn) as conn:
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "UPDATE opspilot_knowledge_revisions SET content='{}' WHERE entry_id=%s AND revision=1",
                (entry,),
            )


def test_active_revision_hides_revoked_and_history_keeps_tombstone(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft = _draft(store, incident, key="revoke", proposals=(_proposal(),))
    store.approve(
        draft.object_id,
        1,
        expected_generation=draft.generation,
        idempotency_key="approve-revoke",
        actor=Actor("alice", "basic_auth"),
    )
    entry = _entry(store, draft.object_id)
    assert store.active_revision(entry) is not None
    store.revoke(
        entry,
        1,
        reason="incorrect",
        expected_generation=1,
        idempotency_key="revoke-active-read",
        actor=Actor("alice", "basic_auth"),
    )
    assert store.active_revision(entry) is None
    history = store.knowledge_history(entry)
    assert history["revisions"][0]["state"] == "revoked"
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT reason,revoked_by,revoked_by_kind FROM opspilot_knowledge_revocations WHERE entry_id=%s AND revision=1",
            (entry,),
        ).fetchone() == ("incorrect", "alice", "basic_auth")


def test_returned_version_is_regenerated_only_by_worker_and_keeps_reason_link(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    actor = Actor("alice", "basic_auth")
    draft = _draft(store, incident, key="return", proposals=(_proposal(),))
    returned = store.return_for_revision(
        draft.object_id,
        1,
        reason="correct timeline",
        expected_generation=draft.generation,
        idempotency_key="return-regenerate",
        actor=actor,
    )
    with pytest.raises(PersistenceError):
        _draft(
            store,
            incident,
            key="human-edit",
            actor=actor,
            expected=returned.generation,
            revises=1,
        )
    regenerated = _draft(
        store,
        incident,
        key="regenerate",
        expected=returned.generation,
        revises=1,
        content={"impact": "corrected", "return_reason": "correct timeline"},
        proposals=(_proposal(),),
    )
    assert regenerated.version == 2 and regenerated.state == "under_review"


def test_watermark_move_rejects_approval_and_old_draft_cannot_be_trusted(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft = _draft(store, incident, key="moving-watermark", proposals=(_proposal(),))
    with psycopg.connect(store.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=1 WHERE incident_id=%s",
            (incident[0],),
        )
    with pytest.raises(PersistenceError):
        store.approve(
            draft.object_id,
            1,
            expected_generation=draft.generation,
            idempotency_key="approve-moved",
            actor=Actor("alice", "basic_auth"),
        )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s",
            (draft.object_id,),
        ).fetchone() == (0,)


def test_new_committed_evidence_makes_existing_draft_unapprovable(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID]
) -> None:
    """D1: evidence added after generation moves the evidence watermark."""
    draft = _draft(store, incident, key="evidence-watermark", proposals=(_proposal(),))
    before = store.evidence_snapshot_sha256(incident[0])
    with psycopg.connect(store.dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,committed) VALUES(%s,%s,%s,'ok',true,%s,%s,%s,%s,%s,%s,true)",
            (
                f"ev-{uuid4()}",
                str(incident[3]),
                str(incident[0]),
                b"evidence",
                "a" * 64,
                "{}",
                "b" * 64,
                "projection-1",
                datetime.now(timezone.utc),
            ),
        )
    assert store.evidence_snapshot_sha256(incident[0]) != before
    with pytest.raises(PersistenceError, match="^WATERMARK_MOVED$"):
        store.approve(
            draft.object_id,
            1,
            expected_generation=draft.generation,
            idempotency_key="approve-after-evidence",
            actor=Actor("alice", "basic_auth"),
        )
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s",
            (draft.object_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("changed", ["content", "actor", "watermark"])
def test_idempotency_key_cannot_be_rebound(
    store: KnowledgeStore, incident: tuple[UUID, UUID, UUID, UUID], changed: str
) -> None:
    key = f"rebind-{changed}"
    _draft(store, incident, key=key)
    content, actor, watermark = (
        {"impact": "limited"},
        Actor("worker-1", "worker"),
        _watermark(store, incident),
    )
    if changed == "content":
        content = {"impact": "different"}
    if changed == "actor":
        actor = Actor("other", "worker")
    if changed == "watermark":
        watermark = Watermark(
            0,
            0,
            incident[1],
            incident[2],
            1,
            incident[3],
            1,
            store.evidence_snapshot_sha256(incident[0]),
        )
    with pytest.raises(PersistenceError):
        store.record_draft(
            incident[0],
            expected_generation=0,
            idempotency_key=key,
            actor=actor,
            content=content,
            watermark=watermark,
            conclusions=(_conclusion(),),
        )


def test_wrong_digest_is_rejected_by_database(store: KnowledgeStore) -> None:
    with psycopg.connect(store.dsn) as conn:
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(
                "INSERT INTO opspilot_postmortem_versions(postmortem_id,version,state,content,content_sha256,incident_control_generation,observation_generation,observation_session_id,observation_ending_id,run_count,input_watermark,evidence_snapshot_sha256,generated_by,generated_by_kind) VALUES(%s,1,'draft',%s,%s,0,0,%s,%s,0,0,%s,'w','worker')",
                (uuid4(), '{"x":1}', "0" * 64, uuid4(), uuid4(), "e" * 64),
            )


def test_observer_has_no_f13_read_or_write_access(scratch_dsn: str) -> None:
    login = f"opspilot_f13_observer_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
    try:
        with psycopg.connect(make_conninfo(scratch_dsn, user=login)) as conn:
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
