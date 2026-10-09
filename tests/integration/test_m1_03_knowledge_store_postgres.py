"""M1-03 step 1: postmortem/knowledge store on a real PostgreSQL (F13, D1-D16).

Each module run creates its own database on the lab server (or the
``OPSPILOT_LAB_DSN`` throwaway) and migrates it to head. Covered: the
version chain and its states, generation/idempotency/audit in the same
transaction, approval blocked by failed citations or disputes, publish /
supersede / revoke with tombstones, database-layer immutability for the
owner role, the Observer role's zero access, and the 0007 <-> 0008 round
trip with existing data.
"""

import os
from collections.abc import Iterator
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
from opspilot.persistence import PersistenceError, PoolConfig
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=0, max_size=2, timeout=5.0)
F13_TABLES = (
    "opspilot_postmortems",
    "opspilot_postmortem_versions",
    "opspilot_postmortem_conclusions",
    "opspilot_postmortem_disputes",
    "opspilot_postmortem_proposals",
    "opspilot_knowledge_entries",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
    "opspilot_f13_requests",
    "opspilot_f13_audit",
)
REVIEWER = Actor("alice", "basic_auth")
WORKER = Actor("worker-1", "worker")
# the evidence snapshot of an incident with no committed evidence
EVIDENCE_HASH = content_sha256(canonical_json([]))


def _new_database() -> str:
    name = f"opspilot_f13_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    return name


def _drop_database(name: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = _new_database()
    scratch = make_conninfo(DSN, dbname=name)
    schema.migrate(scratch, pg_dump=PG_DUMP)
    try:
        yield scratch
    finally:
        _drop_database(name)


@pytest.fixture(scope="module")
def store(dsn: str) -> Iterator[KnowledgeStore]:
    knowledge = KnowledgeStore(dsn, pool=POOL)
    knowledge.install()
    yield knowledge
    knowledge.close()


def _seed_incident(dsn: str) -> tuple[UUID, Watermark]:
    """An incident with one completed Run and an ended observation session."""
    incident_id, target_id, run_id = uuid4(), uuid4(), uuid4()
    session_id, ending_id = uuid4(), uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id, resource_uid) VALUES (%s, %s)",
            (target_id, f"uid-{target_id}"),
        )
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
            "ended_reason, lifecycle_before, lifecycle_after) VALUES "
            "(%s, %s, %s, 'deadline_expired', 'observing_recovery', 'observing_recovery')",
            (ending_id, session_id, incident_id),
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
                }
            ],
        ),
    ]


def _proposal(key: str = "kb-1", supersedes: UUID | None = None) -> ProposalDraft:
    return ProposalDraft(
        key=key,
        name="checkout errors after deploy",
        tags=["checkout", "deploy"],
        content={"symptoms": ["5xx"], "checks": ["error rate"], "evidence": ["ev-1"]},
        supersedes_entry_id=supersedes,
    )


def _key() -> str:
    return f"idem-{uuid4().hex}"


def _draft(store, incident_id, watermark, *, generation=0, **kwargs):
    kwargs.setdefault("conclusions", _conclusions())
    return store.record_draft(
        incident_id,
        expected_generation=generation,
        idempotency_key=_key(),
        actor=WORKER,
        content={"impact": "checkout degraded"},
        watermark=watermark,
        **kwargs,
    )


def _approved(store, dsn, proposals=None, entry_generations=None):
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(
        store,
        incident_id,
        watermark,
        proposals=[_proposal()] if proposals is None else proposals,
    )
    return store.approve(
        draft.object_id,
        draft.version,
        expected_generation=draft.generation,
        idempotency_key=_key(),
        actor=REVIEWER,
        entry_generations=entry_generations,
    )


def _add_run(dsn: str, incident_id: UUID) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_runs(run_id, incident_id, state, control_generation, "
            "budget_limit, deadline, versions) "
            "VALUES (%s, %s, 'queued', 0, 1, clock_timestamp(), '{}')",
            (uuid4(), incident_id),
        )


# --- version chain, generations, audit


def test_a_valid_draft_enters_review_and_approval_publishes_an_audited_revision(
    store, dsn
) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark, proposals=[_proposal()])
    # D15: citations validated, so the draft entered review in its own transaction
    assert (draft.version, draft.generation, draft.state) == (1, 2, "under_review")

    approved = store.approve(
        draft.object_id,
        1,
        expected_generation=2,
        idempotency_key=_key(),
        actor=REVIEWER,
    )

    assert (approved.state, approved.generation) == ("approved", 3)
    published = approved.published["kb-1"]
    assert (published["revision"], published["action"]) == (1, "publish")
    active = store.active_revision(published["entry_id"])
    assert active["revision"] == 1 and active["state"] == "active"
    assert (active["approved_by"], active["approved_by_kind"]) == (
        "alice",
        "basic_auth",
    )
    assert active["content_sha256"] == content_sha256(active["content"])
    assert (active["source_postmortem_id"], active["source_version"]) == (
        draft.object_id,
        1,
    )
    trail = store.audit_trail("postmortem", draft.object_id)
    assert [
        (a["action"], a["actor_id"], a["principal_kind"], a["resulting_generation"])
        for a in trail
    ] == [
        ("generate", "worker-1", "worker", 1),
        ("submit", "worker-1", "worker", 2),
        ("approve", "alice", "basic_auth", 3),
    ]
    snapshot = store.postmortem_for_incident(incident_id)
    [version] = snapshot["versions"]
    assert version["state"] == "approved"
    assert version["evidence_snapshot_sha256"] == EVIDENCE_HASH
    assert version["observation_ending_id"] == watermark.observation_ending_id
    assert version["content"] == canonical_json({"impact": "checkout degraded"})
    assert [c["dispute_state"] for c in version["conclusions"]] == [
        "undisputed",
        "undisputed",
    ]


def test_replay_returns_the_recorded_result_and_a_reused_key_conflicts(
    store, dsn
) -> None:
    incident_id, watermark = _seed_incident(dsn)
    key = _key()
    args = dict(
        expected_generation=0,
        idempotency_key=key,
        actor=WORKER,
        content={"impact": "x"},
        watermark=watermark,
        conclusions=_conclusions(),
    )
    first = store.record_draft(incident_id, **args)
    again = store.record_draft(incident_id, **args)

    assert again.replayed and not first.replayed
    assert (again.object_id, again.version, again.generation, again.state) == (
        first.object_id,
        first.version,
        first.generation,
        first.state,
    )
    assert len(store.audit_trail("postmortem", first.object_id)) == 2
    with pytest.raises(PersistenceError, match="^IDEMPOTENCY_CONFLICT$"):
        store.record_draft(incident_id, **{**args, "content": {"impact": "y"}})
    # a used key cannot be spent on a different action either
    with pytest.raises(PersistenceError, match="^IDEMPOTENCY_CONFLICT$"):
        store.reject(
            first.object_id,
            1,
            reason="no",
            expected_generation=2,
            idempotency_key=key,
            actor=REVIEWER,
        )


def test_a_stale_expected_generation_conflicts_and_writes_nothing(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark)
    with pytest.raises(PersistenceError, match="^GENERATION_CONFLICT$"):
        store.approve(
            draft.object_id,
            1,
            expected_generation=1,
            idempotency_key=_key(),
            actor=REVIEWER,
        )
    assert store.postmortem(draft.object_id)["generation"] == 2
    assert len(store.audit_trail("postmortem", draft.object_id)) == 2


def test_each_side_is_refused_the_other_sides_actions(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark)
    for call in (
        lambda: store.approve(
            draft.object_id,
            1,
            expected_generation=2,
            idempotency_key=_key(),
            actor=WORKER,
        ),
        lambda: store.reject(
            draft.object_id,
            1,
            reason="no",
            expected_generation=2,
            idempotency_key=_key(),
            actor=WORKER,
        ),
        lambda: store.return_for_revision(
            draft.object_id,
            1,
            reason="redo",
            expected_generation=2,
            idempotency_key=_key(),
            actor=WORKER,
        ),
        lambda: store.revoke(
            uuid4(),
            1,
            reason="wrong",
            expected_generation=0,
            idempotency_key=_key(),
            actor=WORKER,
        ),
        # D14/D15: a person neither writes nor stales a draft
        lambda: store.record_draft(
            incident_id,
            expected_generation=2,
            idempotency_key=_key(),
            actor=REVIEWER,
            content={"impact": "edited by hand"},
            watermark=watermark,
            conclusions=_conclusions(),
        ),
        lambda: store.mark_stale(
            draft.object_id,
            1,
            reason="run_added",
            expected_generation=2,
            idempotency_key=_key(),
            actor=REVIEWER,
        ),
    ):
        with pytest.raises(PersistenceError, match="^PRINCIPAL_NOT_ALLOWED$"):
            call()
    assert store.postmortem(draft.object_id)["versions"][0]["state"] == "under_review"


def test_failed_citations_keep_a_draft_out_of_review(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(
        store, incident_id, watermark, conclusions=_conclusions(citations_valid=False)
    )
    assert (draft.state, draft.generation) == ("draft", 1)
    with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
        store.approve(
            draft.object_id,
            1,
            expected_generation=1,
            idempotency_key=_key(),
            actor=REVIEWER,
        )
    # the database refuses a later submission too (D15: only the generating
    # transaction may submit)
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO opspilot_f13_requests VALUES ('raw-submit', 'submit', %s, '{}')",
                ("0" * 64,),
            )
            conn.execute(
                "INSERT INTO opspilot_f13_audit(event_id, idempotency_key, object_kind, "
                "object_id, action, version, actor_id, principal_kind, expected_generation, "
                "resulting_generation) VALUES (gen_random_uuid(), 'raw-submit', 'postmortem', "
                "%s, 'submit', 1, 'worker-1', 'worker', 1, 2)",
                (draft.object_id,),
            )
            conn.execute(
                "UPDATE opspilot_postmortems SET generation=2 WHERE postmortem_id=%s",
                (draft.object_id,),
            )
            conn.execute(
                "UPDATE opspilot_postmortem_versions SET state='under_review' "
                "WHERE postmortem_id=%s",
                (draft.object_id,),
            )
    # the worker regenerates: a dead draft does not block a new version
    second = _draft(store, incident_id, watermark, generation=1)
    assert (second.version, second.state) == (2, "under_review")


def test_a_disputed_version_can_be_returned_but_never_approved(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    disputed = _draft(
        store,
        incident_id,
        watermark,
        disputes=[DisputeDraft("finding-1", "error rate also rose before the deploy")],
    )
    assert disputed.state == "under_review"
    with pytest.raises(PersistenceError, match="^DISPUTED$"):
        store.approve(
            disputed.object_id,
            1,
            expected_generation=2,
            idempotency_key=_key(),
            actor=REVIEWER,
        )
    version = store.postmortem(disputed.object_id)["versions"][0]
    finding = next(
        c for c in version["conclusions"] if c["conclusion_key"] == "finding-1"
    )
    assert finding["dispute_state"] == "disputed"
    assert finding["disputes"][0]["raised_by_kind"] == "worker"
    # D16: nothing clears it -- not even the owner
    _owner_refuses(dsn, "DELETE FROM opspilot_postmortem_disputes")
    _owner_refuses(
        dsn,
        "UPDATE opspilot_postmortem_disputes SET reason='resolved' WHERE postmortem_id=%s",
        (disputed.object_id,),
    )
    returned = store.return_for_revision(
        disputed.object_id,
        1,
        reason="check the earlier error rate",
        expected_generation=2,
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    assert returned.state == "returned"


def test_stale_and_returned_versions_are_terminal_and_a_new_version_follows(
    store, dsn
) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark)
    with pytest.raises(PersistenceError, match="^OPEN_VERSION_EXISTS$"):
        _draft(store, incident_id, watermark, generation=2)
    stale = store.mark_stale(
        draft.object_id,
        1,
        reason="run_added",
        expected_generation=2,
        idempotency_key=_key(),
        actor=WORKER,
    )
    assert stale.state == "stale"
    with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
        store.approve(
            draft.object_id,
            1,
            expected_generation=3,
            idempotency_key=_key(),
            actor=REVIEWER,
        )

    second = _draft(store, incident_id, watermark, generation=3)
    assert (second.version, second.generation) == (2, 5)
    returned = store.return_for_revision(
        draft.object_id,
        2,
        reason="timeline misses the rollback",
        expected_generation=5,
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    assert returned.state == "returned"
    third = _draft(store, incident_id, watermark, generation=6, revises_version=2)
    versions = store.postmortem(draft.object_id)["versions"]
    assert [(v["version"], v["state"], v["revises_version"]) for v in versions] == [
        (1, "stale", None),
        (2, "returned", None),
        (3, "under_review", 2),
    ]
    assert versions[0]["stale_reason"] == "run_added"
    assert third.generation == 8
    store.mark_stale(
        draft.object_id,
        3,
        reason="input_added",
        expected_generation=8,
        idempotency_key=_key(),
        actor=WORKER,
    )
    # only a returned version can be revised
    with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
        _draft(store, incident_id, watermark, generation=9, revises_version=1)


def test_a_watermark_from_another_incident_is_refused(store, dsn) -> None:
    incident_id, _ = _seed_incident(dsn)
    _, foreign = _seed_incident(dsn)
    with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
        _draft(store, incident_id, foreign)
    assert store.postmortem_for_incident(incident_id) is None


def test_a_moved_incident_refuses_drafts_and_approvals_of_the_old_watermark(
    store, dsn
) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark)
    _add_run(dsn, incident_id)
    # D1: the version no longer describes the incident; it cannot be approved
    with pytest.raises(PersistenceError, match="^WATERMARK_MOVED$"):
        store.approve(
            draft.object_id,
            1,
            expected_generation=2,
            idempotency_key=_key(),
            actor=REVIEWER,
        )
    store.mark_stale(
        draft.object_id,
        1,
        reason="run_added",
        expected_generation=2,
        idempotency_key=_key(),
        actor=WORKER,
    )
    with pytest.raises(PersistenceError, match="^WATERMARK_MOVED$"):
        _draft(store, incident_id, watermark, generation=3)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=control_generation+1 "
            "WHERE incident_id=%s",
            (incident_id,),
        )
    moved = Watermark(**{**watermark.__dict__, "run_count": 2})
    with pytest.raises(PersistenceError, match="^WATERMARK_MOVED$"):
        _draft(store, incident_id, moved, generation=3)
    current = Watermark(
        **{**watermark.__dict__, "run_count": 2, "incident_control_generation": 1}
    )
    assert _draft(store, incident_id, current, generation=3).version == 2


def test_an_open_observation_session_cannot_back_a_draft(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET state='authorized', ended_reason=NULL "
            "WHERE session_id=%s",
            (watermark.observation_session_id,),
        )
    with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
        _draft(store, incident_id, watermark)


def test_invalid_input_is_reported_as_such_not_as_an_outage(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    for conclusions in (
        # code-written but not deterministic
        [ConclusionDraft("k", "s", "b", "code", "supported", True)],
        # failed citations but claimed supported
        [ConclusionDraft("k", "s", "b", "model", "supported", False)],
        [ConclusionDraft("", "s", "b", "model", "supported", True)],
    ):
        with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
            _draft(store, incident_id, watermark, conclusions=conclusions)
    with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
        _draft(
            store,
            incident_id,
            Watermark(**{**watermark.__dict__, "evidence_snapshot_sha256": "x"}),
        )
    # malformed containers are refused as input, never escape as TypeError
    for kwargs in (
        {"proposals": [ProposalDraft("p", "n", None, {})]},  # type: ignore[arg-type]
        {"proposals": None},
        {"disputes": None},
        {"conclusions": None},
        {
            "conclusions": [
                ConclusionDraft("k", "s", "b", "model", "supported", True, None)  # type: ignore[arg-type]
            ]
        },
    ):
        with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
            _draft(store, incident_id, watermark, **kwargs)
    draft = _draft(store, incident_id, watermark)
    with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
        store.mark_stale(
            draft.object_id,
            1,
            reason="because",  # type: ignore[arg-type]
            expected_generation=2,
            idempotency_key=_key(),
            actor=WORKER,
        )


def test_supersede_and_revoke_keep_every_revision_with_a_tombstone(store, dsn) -> None:
    first = _approved(store, dsn)
    entry_id = first.published["kb-1"]["entry_id"]

    second = _approved(
        store,
        dsn,
        proposals=[_proposal(supersedes=entry_id)],
        entry_generations={entry_id: 1},
    )
    assert second.published["kb-1"] == {
        "entry_id": entry_id,
        "revision": 2,
        "generation": 2,
        "action": "supersede",
    }
    assert store.active_revision(entry_id)["revision"] == 2
    revoked = store.revoke(
        entry_id,
        2,
        reason="root cause was the cache, not the deploy",
        expected_generation=2,
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    assert (revoked.state, revoked.generation) == ("revoked", 3)
    # D3: neither the superseded nor the revoked revision is a knowledge read
    assert store.active_revision(entry_id) is None
    with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
        store.revoke(
            entry_id,
            2,
            reason="again",
            expected_generation=3,
            idempotency_key=_key(),
            actor=REVIEWER,
        )
    # a stale entry generation refuses the whole approval
    with pytest.raises(PersistenceError, match="^ENTRY_GENERATION_CONFLICT$"):
        _approved(
            store,
            dsn,
            proposals=[_proposal(supersedes=entry_id)],
            entry_generations={entry_id: 1},
        )
    third = _approved(
        store,
        dsn,
        proposals=[_proposal(supersedes=entry_id)],
        entry_generations={entry_id: 3},
    )
    assert third.published["kb-1"]["action"] == "publish"
    revisions = store.knowledge_history(entry_id)["revisions"]
    assert [
        (r["revision"], r["state"], r["supersedes_revision"]) for r in revisions
    ] == [
        (1, "superseded", None),
        (2, "revoked", 1),
        (3, "active", None),
    ]
    assert revisions[1]["revoked_reason"] == "root cause was the cache, not the deploy"
    assert revisions[1]["revoked_by"] == "alice"
    assert store.active_revision(entry_id)["revision"] == 3


# --- database layer (owner role): append-only, approved content frozen


def _owner_refuses(dsn: str, statement: str, params: tuple = ()) -> str:
    with psycopg.connect(dsn) as conn:
        with pytest.raises(
            (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)
        ) as refused:
            conn.execute(statement, params)
    return str(refused.value)


def test_the_owner_cannot_rewrite_or_delete_approved_records(store, dsn) -> None:
    approved = _approved(store, dsn)
    pm, entry_id = approved.object_id, approved.published["kb-1"]["entry_id"]

    for statement, params in (
        (
            "UPDATE opspilot_postmortem_versions SET content='{}' WHERE postmortem_id=%s",
            (pm,),
        ),
        (
            "UPDATE opspilot_postmortem_versions SET state='under_review' WHERE postmortem_id=%s",
            (pm,),
        ),
        (
            "UPDATE opspilot_postmortem_conclusions SET body='edited' WHERE postmortem_id=%s",
            (pm,),
        ),
        (
            "UPDATE opspilot_knowledge_revisions SET name='x' WHERE entry_id=%s",
            (entry_id,),
        ),
        ("UPDATE opspilot_f13_audit SET actor_id='mallory' WHERE object_id=%s", (pm,)),
        ("UPDATE opspilot_f13_requests SET result='{}'", ()),
        # identity of an object row, and a generation bump without audit
        (
            "UPDATE opspilot_postmortems SET incident_id=gen_random_uuid() WHERE postmortem_id=%s",
            (pm,),
        ),
        (
            "UPDATE opspilot_postmortems SET generation=generation+1 WHERE postmortem_id=%s",
            (pm,),
        ),
        (
            "UPDATE opspilot_knowledge_entries SET generation=generation+1 WHERE entry_id=%s",
            (entry_id,),
        ),
    ):
        _owner_refuses(dsn, statement, params)
    for table in F13_TABLES:
        _owner_refuses(dsn, f"DELETE FROM {table}")
        _owner_refuses(dsn, f"TRUNCATE {table} CASCADE")
    assert store.active_revision(entry_id)["revision"] == 1


def test_raw_writes_without_their_audit_row_are_refused(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark)
    pm = draft.object_id
    # a state change with no audit row in this transaction
    message = _owner_refuses(
        dsn,
        "UPDATE opspilot_postmortem_versions SET state='approved' WHERE postmortem_id=%s",
        (pm,),
    )
    assert "without a approve audit row" in message
    # a worker-kind audit row cannot carry a review action, nor a Basic Auth
    # one a worker action
    for action, kind in (("approve", "worker"), ("generate", "basic_auth")):
        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO opspilot_f13_audit(event_id, idempotency_key, object_kind, "
                    "object_id, action, version, actor_id, principal_kind, expected_generation, "
                    "resulting_generation) VALUES (gen_random_uuid(), 'k', 'postmortem', %s, "
                    "%s, 1, 'someone', %s, 2, 3)",
                    (pm, action, kind),
                )
    # a dispute or a conclusion added after the draft's own transaction
    _owner_refuses(
        dsn,
        "INSERT INTO opspilot_postmortem_disputes(dispute_id, postmortem_id, version, "
        "conclusion_key, reason, raised_by, raised_by_kind, event_id) "
        "SELECT gen_random_uuid(), %s, 1, 'finding-1', 'r', 'worker-1', 'worker', event_id "
        "FROM opspilot_f13_audit WHERE object_id=%s LIMIT 1",
        (pm, pm),
    )
    _owner_refuses(
        dsn,
        "INSERT INTO opspilot_postmortem_conclusions(postmortem_id, version, conclusion_key, "
        "ordinal, section, body, author, certainty, citations_valid, evidence_refs) "
        "VALUES (%s, 1, 'late', 9, 'findings', 'b', 'code', 'deterministic', true, '[]')",
        (pm,),
    )
    assert store.postmortem(pm)["versions"][0]["state"] == "under_review"


def test_a_stored_hash_must_match_its_content(store, dsn) -> None:
    """The CHECK holds on its own: with the insert guard disabled for this
    transaction only, a mismatching hash is still refused."""
    approved = _approved(store, dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "ALTER TABLE opspilot_postmortem_proposals "
            "DISABLE TRIGGER opspilot_postmortem_proposals_guard"
        )
        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            conn.execute(
                "INSERT INTO opspilot_postmortem_proposals(postmortem_id, version, proposal_key, "
                "name, tags, content, content_sha256) VALUES (%s, 1, 'p', 'n', '{}', '{}', %s)",
                (approved.object_id, "0" * 64),
            )
        assert "content_sha256_check" in str(refused.value)
        conn.rollback()


# --- roles (D8 as revised): Observer has no access to F13 tables


def test_the_observer_role_has_no_privilege_on_f13_tables(store, dsn) -> None:
    login = f"opspilot_obs_f13_{uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
    try:
        observer = make_conninfo(dsn, user=login)
        for table in (*F13_TABLES, "opspilot_knowledge_revision_states"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                with psycopg.connect(dsn) as conn:
                    allowed = conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        (login, table, privilege),
                    ).fetchone()[0]
                assert not allowed, (table, privilege)
        with psycopg.connect(observer) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM opspilot_postmortems")
        with psycopg.connect(observer) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO opspilot_f13_requests VALUES ('k', 'approve', %s, '{}')",
                    ("0" * 64,),
                )
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


# --- migration round trip with existing data


def test_0007_to_0008_round_trip_keeps_business_rows() -> None:
    name = _new_database()
    scratch = make_conninfo(DSN, dbname=name)
    try:
        schema.command.upgrade(schema._config(scratch), "0007_ending_job_identity")
        incident_id, _ = _seed_incident(scratch)
        before = schema.schema_dump(scratch, pg_dump=PG_DUMP)

        assert (
            schema.migrate(scratch, pg_dump=PG_DUMP).revision
            == "0008_postmortem_knowledge"
        )
        head_dump = schema.schema_dump(scratch, pg_dump=PG_DUMP)
        assert head_dump == schema.fresh_head_dump(scratch, pg_dump=PG_DUMP)
        # every F13 table is still empty here: the refusals are per statement,
        # so they hold even with no row for a row trigger to fire on
        for table in F13_TABLES:
            _owner_refuses(scratch, f"DELETE FROM {table}")
        for statement in (
            "UPDATE opspilot_postmortem_disputes SET reason='x'",
            "UPDATE opspilot_knowledge_revisions SET name='x'",
            "UPDATE opspilot_f13_audit SET reason='x'",
        ):
            _owner_refuses(scratch, statement)

        schema.command.downgrade(schema._config(scratch), "0007_ending_job_identity")
        assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == before
        with psycopg.connect(scratch) as conn:
            leftovers = conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname LIKE 'opspilot\\_f13\\_%%' "
                "OR proname LIKE 'opspilot\\_postmortem\\_%%' "
                "OR proname LIKE 'opspilot\\_knowledge\\_%%'"
            ).fetchone()[0]
            kept = conn.execute(
                "SELECT count(*) FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()[0]
        assert (leftovers, kept) == (0, 1)

        schema.migrate(scratch, pg_dump=PG_DUMP)
        assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == head_dump
    finally:
        _drop_database(name)


def test_new_committed_evidence_moves_the_watermark(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    assert store.evidence_snapshot_sha256(incident_id) == EVIDENCE_HASH
    draft = _draft(store, incident_id, watermark)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_evidence(evidence_id, run_id, subject_id, status, adopted, "
            "raw, raw_sha256, view, view_sha256, projection_revision, observed_at, committed) "
            "VALUES (%s, %s, %s, 'ok', true, '\\x00', %s, '{}', %s, 'p1', clock_timestamp(), true)",
            (
                f"ev-{uuid4().hex}",
                str(watermark.last_run_id),
                str(incident_id),
                "b" * 64,
                "c" * 64,
            ),
        )
    moved = store.evidence_snapshot_sha256(incident_id)
    assert moved != EVIDENCE_HASH
    with pytest.raises(PersistenceError, match="^WATERMARK_MOVED$"):
        store.approve(
            draft.object_id,
            1,
            expected_generation=2,
            idempotency_key=_key(),
            actor=REVIEWER,
        )
