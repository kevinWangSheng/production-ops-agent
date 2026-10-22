"""#169: web and worker as non-owner runtime roles on a real PostgreSQL.

The lab database is at head (``conftest``). Covered: the catalog equals
``RUNTIME_GRANTS`` for every relation (U4), the group roles' own attributes,
the 0011 round trip, the legitimate paths through throwaway login roles
(workbench intake and controls as ``opspilot_web``, claim/investigate/
publish and postmortem generation as ``opspilot_worker``, the five review
actions as ``opspilot_web``), and the refusals: the worker cannot write
knowledge or review, the web cannot write a draft, claim a Run lease or a
generation lease.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.investigation.loop import ModelError
from opspilot.knowledge import (
    Actor,
    ConclusionDraft,
    KnowledgeStore,
)
from opspilot.knowledge.jobs import GenerationStore, current_watermark
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.persistence.grants import (
    RUNTIME_GRANTS,
    TRIGGER_GUARDED,
    WEB_ROLE,
    WORKER_ROLE,
)
from opspilot.tools.fixture import fixture_face, fixture_versions
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
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import CountingModel, Harness, output
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    REGISTRY,
    _alert,
    _webhook,
)
from tests.integration.test_m1_web_worker_postgres import (
    QUESTION,
    _tool_round,
    _worker,
)
from tests.m1_investigation_support import report_from_transcript
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    post_form,
    post_json,
    same_origin,
)
from tests.runtime_roles_support import catalog_grants, login, relations

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
REVIEW_TABLES = (
    "opspilot_postmortems",
    "opspilot_postmortem_versions",
    "opspilot_knowledge_entries",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
    "opspilot_f13_audit",
    "opspilot_f13_requests",
)


@pytest.fixture(scope="module")
def web_dsn():
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def worker_dsn():
    with login(DSN, WORKER_ROLE) as dsn:
        yield dsn


def _count(table: str, where: str = "true", *args) -> int:
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE " + where).format(
                sql.Identifier(table)
            ),
            args,
        ).fetchone()[0]


def _snapshot(incident: UUID) -> dict[str, int]:
    """Rows of the review tables that belong to one incident."""
    with psycopg.connect(DSN) as conn:
        pm = conn.execute(
            "SELECT postmortem_id FROM opspilot_postmortems WHERE incident_id=%s",
            (incident,),
        ).fetchone()
        if pm is None:
            return {}
        rows = {}
        for table, column in (
            ("opspilot_postmortem_versions", "postmortem_id"),
            ("opspilot_knowledge_revisions", "source_postmortem_id"),
        ):
            rows[table] = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
                    sql.Identifier(table), sql.Identifier(column)
                ),
                (pm[0],),
            ).fetchone()[0]
        rows["generation"] = conn.execute(
            "SELECT generation FROM opspilot_postmortems WHERE postmortem_id=%s",
            (pm[0],),
        ).fetchone()[0]
        rows["states"] = conn.execute(
            "SELECT string_agg(state, ',' ORDER BY version) "
            "FROM opspilot_postmortem_versions WHERE postmortem_id=%s",
            (pm[0],),
        ).fetchone()[0]
        rows["audit"] = conn.execute(
            "SELECT count(*) FROM opspilot_f13_audit WHERE object_id=%s", (pm[0],)
        ).fetchone()[0]
        return rows


# --- U4: the declaration is the catalog


def test_every_relation_has_a_decision_and_the_catalog_equals_it() -> None:
    with psycopg.connect(DSN) as conn:
        present = relations(conn)
        for role, declared in RUNTIME_GRANTS.items():
            assert set(declared) == present, role
            actual = catalog_grants(conn, role)
            diff = {
                name: (sorted(actual[name]), sorted(declared[name]))
                for name in present
                if actual[name] != declared[name]
            }
            assert not diff, (role, diff)


def test_the_group_roles_are_bare_nologin_roles_owning_nothing() -> None:
    with psycopg.connect(DSN) as conn:
        for role in (WEB_ROLE, WORKER_ROLE):
            row = conn.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                "rolbypassrls, rolinherit FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            assert row == (False, False, False, False, False, True), role
            # schema: USAGE granted to the role itself, never CREATE
            schema_privileges = {
                r[0]
                for r in conn.execute(
                    "SELECT a.privilege_type FROM pg_namespace n, "
                    "LATERAL aclexplode(n.nspacl) a "
                    "WHERE n.nspname='public' AND a.grantee = %s::regrole",
                    (role,),
                )
            }
            assert schema_privileges == {"USAGE"}, role
            database_privileges = {
                r[0]
                for r in conn.execute(
                    "SELECT a.privilege_type FROM pg_database d, "
                    "LATERAL aclexplode(d.datacl) a "
                    "WHERE d.datname = current_database() AND a.grantee = %s::regrole",
                    (role,),
                )
            }
            assert database_privileges == set(), role
            assert conn.execute(
                "SELECT count(*) FROM pg_class WHERE relowner = %s::regrole",
                (role,),
            ).fetchone() == (0,), role
            for granted in catalog_grants(conn, role).values():
                for privilege in granted:
                    assert not privilege.startswith(
                        ("TRUNCATE", "REFERENCES", "TRIGGER")
                    ), (role, privilege)


def test_0011_downgrade_revokes_here_and_keeps_roles_another_database_uses() -> None:
    name = f"opspilot_roles_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(scratch, pg_dump=PG_DUMP)
        before = schema.schema_dump(scratch, pg_dump=PG_DUMP)
        schema.command.downgrade(schema._config(scratch), "0010_alert_intake")
        with psycopg.connect(scratch) as conn:
            for role in (WEB_ROLE, WORKER_ROLE):
                assert all(not held for held in catalog_grants(conn, role).values())
                assert not conn.execute(
                    "SELECT has_schema_privilege(%s, 'public', 'USAGE') "
                    "FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a "
                    "WHERE n.nspname = 'public' AND a.grantee = %s::regrole",
                    (role, role),
                ).fetchall()
        # the lab database still grants them, so the roles stay
        with psycopg.connect(DSN) as conn:
            assert conn.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
                ([WEB_ROLE, WORKER_ROLE],),
            ).fetchone() == (2,)
        schema.command.upgrade(schema._config(scratch), "head")
        assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == before
        with psycopg.connect(scratch) as conn:
            for role, declared in RUNTIME_GRANTS.items():
                assert catalog_grants(conn, role) == declared
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


# --- P4: the legitimate paths through login roles


def _workbench(dsn: str):
    store = DurableStore(dsn)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    ledger = DurableWebLedger(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
        targets=MappingTargetRegistry(REGISTRY),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="auth-rev-roles",
        allowed_origins=frozenset({ORIGIN}),
    )
    knowledge = KnowledgeStore(dsn)
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(store),
        review=PostmortemReview(knowledge=knowledge, events=events),
    )
    return app, workbench, store, events


def _form(app, path, fields):
    return post_form(
        app,
        path,
        fields,
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )


def _submit(app) -> UUID:
    submitted = _form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": QUESTION,
            "idempotency_key": f"roles-{uuid4()}",
        },
    )
    assert submitted.status == 201, submitted.text
    return UUID(submitted.json()["incident_id"])


def _control(app, subject: UUID, action: str, generation: int, **extra) -> int:
    response = _form(
        app,
        f"/incidents/{subject}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": uuid4().hex,
            **extra,
        },
    )
    assert response.status == 200, (action, response.text)
    return int(response.json()["generation"])


def test_intake_controls_and_investigation_run_under_the_two_roles(
    web_dsn, worker_dsn
) -> None:
    app, workbench, web_store, _ = _workbench(web_dsn)
    worker_store = DurableStore(worker_dsn)
    worker_store.install()
    worker_events = DurableEventLog(worker_store)
    worker_evidence = DurableEvidenceStore(worker_store)

    # handoff by the worker, follow-up by the web, investigation and
    # publication by the worker
    subject = _submit(app)
    first, _ = _worker(
        worker_store,
        worker_events,
        worker_evidence,
        [ModelError("MODEL_REJECTED")],
        subject=subject,
    )
    ((_, outcome),) = first.poll_once()
    assert outcome.status == "handed_off"
    _control(app, subject, "follow_up", 0, text="Query the metric and cite it.")
    second, _ = _worker(
        worker_store,
        worker_events,
        worker_evidence,
        [_tool_round(), report_from_transcript],
        subject=subject,
    )
    ((_, outcome),) = second.poll_once()
    assert outcome.status == "published", outcome
    rebuilt = web_store.rebuild(subject)
    assert rebuilt["run"]["state"] == "completed" and rebuilt["conclusion"]
    assert workbench.snapshot(subject)["report"]["parsed"] is True
    assert worker_store.run_usage(rebuilt["run"]["run_id"])["tool_operations_used"] == 1

    # pause, resume and takeover by the web on a queued Run
    other = _submit(app)
    generation = _control(app, other, "pause", 0)
    generation = _control(app, other, "resume", generation)
    _control(app, other, "takeover", generation)
    assert web_store.rebuild(other)["run"]["state"] == "waiting_human"


def test_alertmanager_intake_writes_identity_and_delivery_rows_as_the_web(
    web_dsn,
) -> None:
    app, _, _, _ = _workbench(web_dsn)
    alert = _alert()
    response = post_json(app, "/intake/alertmanager", _webhook(alert), headers=bearer())
    assert response.status == 200, response.text
    [result] = response.json()["results"]
    assert result["outcome"] == "created"
    # delivery_id is an identity column: inserted without a sequence grant
    assert (
        _count("opspilot_alert_deliveries", "fingerprint = %s", alert["fingerprint"])
        == 1
    )


@pytest.fixture
def harness(web_dsn, worker_dsn):
    """F13 harness: pages and reviews through the web login, generation
    through the worker login; only the upstream seed is written as owner."""
    app, _, web_store, _ = _workbench(web_dsn)
    knowledge = KnowledgeStore(worker_dsn)
    jobs = GenerationStore(worker_dsn)
    worker_store = DurableStore(worker_dsn)
    events = DurableEventLog(worker_store)
    try:
        yield Harness(app, knowledge, jobs, web_store, DSN, events)
    finally:
        jobs.close()
        knowledge.close()


def _entry_of(h: Harness, incident: UUID) -> tuple[UUID, int, int]:
    """The incident's published entry, read as the owner (the worker the
    harness generates with has no privilege on knowledge entries)."""
    pm = h.knowledge.incident_postmortem(incident)["postmortem"]
    [revision] = h.knowledge.knowledge_from_postmortem(pm["postmortem_id"])
    return (
        revision["entry_id"],
        revision["revision"],
        _owner_history(revision["entry_id"])["generation"],
    )


def _owner_history(entry: UUID) -> dict:
    owner = KnowledgeStore(DSN)
    try:
        return owner.knowledge_history(entry)
    finally:
        owner.close()


def test_generation_by_the_worker_and_every_review_action_by_the_web(
    harness,
) -> None:
    h = harness
    # approve, then revoke the published entry
    incident, ev, _, _ = h.seed()
    h.generate(incident, CountingModel(output(ev)))
    approved = h.review(incident, "approve")
    assert approved.status == 200, approved.text
    entry, revision, generation = _entry_of(h, incident)
    revoked = h.post(
        f"/knowledge/{entry}/revoke",
        {
            "revision": str(revision),
            "expected_generation": str(generation),
            "idempotency_key": uuid4().hex,
            "reason": "superseded by a runbook",
        },
    )
    assert revoked.status == 200, revoked.text

    # return, regenerate the returned version, reject the regeneration
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev))
    h.generate(incident, model)
    assert h.review(incident, "return", reason="add a control window").status == 200
    h.generate(incident, model)
    assert h.review(incident, "reject", reason="not this incident").status == 200
    assert _snapshot(incident)["states"] == "returned,rejected"

    # supersede: after a follow-up moves the watermark, a regeneration whose
    # proposal names the active revision replaces it
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev))
    h.generate(incident, model)
    assert h.review(incident, "approve").status == 200
    entry, revision, generation = _entry_of(h, incident)
    follow_up = h.reopen_follow_up(incident, "Re-check the checkout error window.")
    assert follow_up.status == 200, follow_up.text
    h.finish_run(incident)
    model.payload = output(ev, supersedes_revision=revision)
    view = h.generate(incident, model)["postmortem"]
    assert view["versions"][-1]["proposals"][0]["supersedes_entry_id"] == entry
    response = h.review(
        incident, "supersede", **{f"entry_generation:{entry}": str(generation)}
    )
    assert response.status == 200, response.text
    states = [r["state"] for r in _owner_history(entry)["revisions"]]
    assert states == ["superseded", "active"]


# --- P5: refusals


def test_the_worker_cannot_run_a_review_action_and_leaves_no_row(harness, worker_dsn):
    h = harness
    incident, ev, _, _ = h.seed()
    h.generate(incident, CountingModel(output(ev)))
    pm = h.knowledge.incident_postmortem(incident)["postmortem"]
    worker = KnowledgeStore(worker_dsn)
    reviewer = Actor("forged-reviewer", "basic_auth")
    before = _snapshot(incident)
    try:
        attempts = (
            lambda: worker.approve(
                pm["postmortem_id"],
                1,
                expected_generation=pm["generation"],
                idempotency_key=uuid4().hex,
                actor=reviewer,
            ),
            lambda: worker.reject(
                pm["postmortem_id"],
                1,
                reason="forged",
                expected_generation=pm["generation"],
                idempotency_key=uuid4().hex,
                actor=reviewer,
            ),
            lambda: worker.return_for_revision(
                pm["postmortem_id"],
                1,
                reason="forged",
                expected_generation=pm["generation"],
                idempotency_key=uuid4().hex,
                actor=reviewer,
            ),
        )
        for attempt in attempts:
            with pytest.raises(PersistenceError) as refused:
                attempt()
            assert isinstance(
                refused.value.__cause__, psycopg.errors.InsufficientPrivilege
            )
            assert _snapshot(incident) == before
    finally:
        worker.close()
    # a published entry: revoke as the worker is refused too
    assert h.review(incident, "approve").status == 200
    entry, revision, generation = _entry_of(h, incident)
    worker = KnowledgeStore(worker_dsn)
    try:
        before = _snapshot(incident)
        with pytest.raises(PersistenceError):
            worker.revoke(
                entry,
                revision,
                reason="forged",
                expected_generation=generation,
                idempotency_key=uuid4().hex,
                actor=reviewer,
            )
        assert _snapshot(incident) == before
        assert _count("opspilot_knowledge_revocations", "entry_id=%s", entry) == 0
    finally:
        worker.close()


def test_the_web_cannot_write_a_draft_or_take_a_lease(harness, web_dsn) -> None:
    h = harness
    incident, ev, session, ending = h.seed()
    web = KnowledgeStore(web_dsn)
    try:
        owner = KnowledgeStore(DSN)
        try:
            with owner.transaction() as conn:
                watermark = current_watermark(conn, incident)
        finally:
            owner.close()
        with pytest.raises(PersistenceError):
            web.record_draft(
                incident,
                expected_generation=0,
                idempotency_key=uuid4().hex,
                actor=Actor("system:web", "worker"),
                content={"sections": []},
                watermark=watermark,
                conclusions=[
                    ConclusionDraft(
                        key="impact",
                        section="impact_summary",
                        body="forged",
                        author="model",
                        certainty="uncertain",
                        citations_valid=False,
                        evidence_refs=(),
                    )
                ],
            )
    finally:
        web.close()
    assert _snapshot(incident) == {}

    jobs = GenerationStore(web_dsn)
    try:
        with pytest.raises(PersistenceError):
            jobs.claim(incident, owner=uuid4(), versions={}, max_model_requests=2)
    finally:
        jobs.close()
    assert (
        _count("opspilot_postmortem_generation_attempts", "incident_id=%s", incident)
        == 0
    )

    run = DurableStore(web_dsn)
    rebuilt = run.rebuild(incident)
    with pytest.raises(PersistenceError):
        run.claim(incident, rebuilt["run"]["run_id"], uuid4(), fixture_versions())
    with psycopg.connect(DSN) as conn:
        owner, epoch = conn.execute(
            "SELECT owner, epoch FROM opspilot_runs WHERE run_id=%s",
            (rebuilt["run"]["run_id"],),
        ).fetchone()
    assert owner is None and epoch == 0
    with psycopg.connect(web_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE opspilot_runs SET epoch = epoch + 1 WHERE run_id=%s",
                (rebuilt["run"]["run_id"],),
            )


def test_runtime_suspension_write_boundaries(web_dsn, worker_dsn) -> None:
    for dsn in (web_dsn, worker_dsn):
        statements = [
            "TRUNCATE opspilot_web_ledger",
            "UPDATE opspilot_scope_controls SET global_suspended = true",
            "UPDATE opspilot_target_suspensions SET suspended = true",
            "INSERT INTO opspilot_suspension_audit(target_id,suspended,generation,actor) "
            "VALUES (NULL, true, 1, 'forged')",
            "CREATE TABLE opspilot_forged (x int)",
        ]
        if dsn == web_dsn:
            statements = [
                "TRUNCATE opspilot_web_ledger",
                "CREATE TABLE opspilot_forged (x int)",
                "SELECT * FROM opspilot_suspension_audit",
                "UPDATE opspilot_suspension_audit SET actor='forged'",
                "INSERT INTO opspilot_suspension_audit(audit_id,target_id,suspended,generation,actor) "
                "OVERRIDING SYSTEM VALUE VALUES (999999, NULL, true, 1, 'forged')",
                "INSERT INTO opspilot_target_suspensions(target_id,updated_at) "
                "VALUES (gen_random_uuid(),clock_timestamp())",
            ]
            # Exercise exactly the newly permitted column writes; roll back
            # so this role-boundary test does not change investigation gates.
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    "UPDATE opspilot_scope_controls SET global_suspended=false,global_generation=global_generation"
                )
                conn.execute(
                    "UPDATE opspilot_target_suspensions SET suspended=suspended,generation=generation"
                )
                conn.execute(
                    "INSERT INTO opspilot_suspension_audit(target_id,suspended,generation,actor) VALUES(NULL,false,0,'role-test')"
                )
                conn.rollback()
        for statement in statements:
            with psycopg.connect(dsn) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)


def test_the_guard_triggers_named_next_to_the_grants_exist() -> None:
    with psycopg.connect(DSN) as conn:
        for table, (trigger, _) in TRIGGER_GUARDED.items():
            assert conn.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgrelid = %s::regclass AND tgname = %s AND tgenabled = 'O'",
                (table, trigger),
            ).fetchone() == (1,), trigger
        # no function of the schema runs with its owner's privileges
        assert conn.execute(
            "SELECT count(*) FROM pg_proc "
            "WHERE pronamespace = 'public'::regnamespace AND prosecdef"
        ).fetchone() == (0,)


def test_direct_review_writes_by_the_worker_are_refused_with_42501(
    harness, worker_dsn
) -> None:
    h = harness
    incident, ev, _, _ = h.seed()
    h.generate(incident, CountingModel(output(ev)))
    pm = h.knowledge.incident_postmortem(incident)["postmortem"]
    before = _snapshot(incident)
    requests = _count("opspilot_f13_requests")
    statements = (
        (
            "INSERT INTO opspilot_f13_audit(event_id, idempotency_key, object_kind, "
            "object_id, action, actor_id, principal_kind, expected_generation, "
            "resulting_generation, version, reason) VALUES "
            "(gen_random_uuid(), %s, 'postmortem', %s, 'reject', 'forged', "
            "'basic_auth', %s, %s, 1, 'forged')",
            (uuid4().hex, pm["postmortem_id"], pm["generation"], pm["generation"] + 1),
        ),
        (
            "INSERT INTO opspilot_f13_requests(idempotency_key, action, "
            "request_sha256, result) VALUES (%s, 'approve', %s, '{}')",
            (uuid4().hex, "0" * 64),
        ),
        (
            "UPDATE opspilot_postmortem_versions SET state='approved' "
            "WHERE postmortem_id=%s AND version=1",
            (pm["postmortem_id"],),
        ),
        (
            "UPDATE opspilot_postmortem_versions SET state='rejected' "
            "WHERE postmortem_id=%s AND version=1",
            (pm["postmortem_id"],),
        ),
        (
            "INSERT INTO opspilot_knowledge_entries(entry_id) VALUES (gen_random_uuid())",
            (),
        ),
    )
    for statement, args in statements:
        with psycopg.connect(worker_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, args)
    assert _snapshot(incident) == before
    assert _count("opspilot_f13_requests") == requests


def test_the_web_may_clear_a_run_lease_but_not_set_one(web_dsn) -> None:
    app, _, web_store, _ = _workbench(web_dsn)
    subject = _submit(app)
    run_id = web_store.rebuild(subject)["run"]["run_id"]
    for statement in (
        "UPDATE opspilot_runs SET owner = gen_random_uuid() WHERE run_id=%s",
        "UPDATE opspilot_runs SET lease_until = clock_timestamp() + interval '1 hour' "
        "WHERE run_id=%s",
        "UPDATE opspilot_runs SET state='running', owner=gen_random_uuid(), "
        "lease_until=clock_timestamp() + interval '1 hour' WHERE run_id=%s",
    ):
        with psycopg.connect(web_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, (run_id,))
    with psycopg.connect(web_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET owner=NULL, lease_until=NULL WHERE run_id=%s",
            (run_id,),
        )
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT state, owner, lease_until, epoch FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone() == ("queued", None, None, 0)


def test_0011_downgrade_revokes_only_its_own_grants_from_a_preexisting_role() -> None:
    """A role that existed before 0011 keeps what it owned and was granted
    elsewhere; only the 0011 grants and guard triggers go."""
    name = f"opspilot_roles_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    try:
        schema.command.upgrade(schema._config(scratch), "0010_alert_intake")
        with psycopg.connect(scratch) as conn:
            conn.execute("CREATE TABLE web_owned (x int)")
            conn.execute("ALTER TABLE web_owned OWNER TO opspilot_web")
            conn.execute("CREATE TABLE unrelated (x int)")
            conn.execute("GRANT SELECT, INSERT ON unrelated TO opspilot_web")
            conn.execute("GRANT SELECT ON opspilot_incidents TO opspilot_worker")
        schema.command.upgrade(schema._config(scratch), "head")
        schema.command.downgrade(schema._config(scratch), "0010_alert_intake")
        with psycopg.connect(scratch) as conn:
            assert conn.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE relname = 'web_owned' AND relnamespace = 'public'::regnamespace"
            ).fetchone() == ("opspilot_web",)
            assert conn.execute(
                "SELECT has_table_privilege('opspilot_web', 'unrelated', 'SELECT'), "
                "has_table_privilege('opspilot_web', 'unrelated', 'INSERT')"
            ).fetchone() == (True, True)
            for role in (WEB_ROLE, WORKER_ROLE):
                held = catalog_grants(conn, role)
                assert all(not privileges for privileges in held.values()), role
                assert not conn.execute(
                    "SELECT 1 FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a "
                    "WHERE n.nspname = 'public' AND a.grantee = %s::regrole",
                    (role,),
                ).fetchall(), role
            assert conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = ANY(%s)",
                ([trigger for trigger, _ in TRIGGER_GUARDED.values()],),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname IN "
                "('opspilot_worker_review_guard', 'opspilot_web_lease_guard')"
            ).fetchone() == (0,)
        with psycopg.connect(DSN) as conn:
            assert conn.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
                ([WEB_ROLE, WORKER_ROLE],),
            ).fetchone() == (2,)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _scratch_at_0010() -> tuple[str, str]:
    name = f"opspilot_roles_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    schema.command.upgrade(schema._config(scratch), "0010_alert_intake")
    return name, scratch


def _drop_database(name: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


_ATTRIBUTES = (
    "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
    "rolbypassrls FROM pg_roles WHERE rolname = %s"
)


@pytest.mark.parametrize(
    ("role", "grant", "revert", "reported"),
    [
        (WEB_ROLE, "LOGIN", "NOLOGIN", "LOGIN"),
        (WORKER_ROLE, "CREATEDB", "NOCREATEDB", "CREATEDB"),
        (WEB_ROLE, "BYPASSRLS", "NOBYPASSRLS", "BYPASSRLS"),
    ],
)
def test_0011_refuses_an_existing_role_that_is_not_a_plain_group(
    role, grant, revert, reported
) -> None:
    """The roles are cluster-wide and already exist (the lab database is at
    head); the attribute is set for the duration of the test only."""
    name, scratch = _scratch_at_0010()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} " + grant).format(sql.Identifier(role)))
    try:
        with psycopg.connect(DSN) as conn:
            altered = conn.execute(_ATTRIBUTES, (role,)).fetchone()
        with pytest.raises(
            Exception, match=f"{role} is not a plain NOLOGIN group .*{reported}"
        ):
            schema.command.upgrade(schema._config(scratch), "head")
        with psycopg.connect(scratch) as conn:
            assert schema.current_revision(conn) == "0010_alert_intake"
            for runtime in (WEB_ROLE, WORKER_ROLE):
                held = catalog_grants(conn, runtime)
                assert all(not privileges for privileges in held.values()), runtime
            assert conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = ANY(%s)",
                ([trigger for trigger, _ in TRIGGER_GUARDED.values()],),
            ).fetchone() == (0,)
        with psycopg.connect(DSN) as conn:
            # never altered by the migration
            assert conn.execute(_ATTRIBUTES, (role,)).fetchone() == altered
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER ROLE {} " + revert).format(sql.Identifier(role))
            )
        _drop_database(name)


def test_0011_reuses_an_existing_plain_group_untouched() -> None:
    name, scratch = _scratch_at_0010()
    try:
        with psycopg.connect(DSN) as conn:
            before = {
                r: conn.execute(_ATTRIBUTES, (r,)).fetchone() for r in RUNTIME_GRANTS
            }
        assert all(row is not None and not any(row) for row in before.values())
        schema.command.upgrade(schema._config(scratch), "head")
        with psycopg.connect(scratch) as conn:
            for role, declared in RUNTIME_GRANTS.items():
                assert catalog_grants(conn, role) == declared
        with psycopg.connect(DSN) as conn:
            assert {
                r: conn.execute(_ATTRIBUTES, (r,)).fetchone() for r in RUNTIME_GRANTS
            } == before
    finally:
        _drop_database(name)
