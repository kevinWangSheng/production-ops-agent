"""Independent #169 contract r1 / public interface r2 (P1-P6).

P4 representative storage paths: intake + human pause + remediation,
Alertmanager delivery identity sequence, Run claim/renew/step/publish/handoff,
generation claim/reservation/finish, draft/stale, all five F13 review actions.
Not covered: HTTP rendering/auth, full investigation/model/tool execution,
all controls, expiry sweep and live Run (lead's separate acceptance).

Q1-Q4 are applied from interface-r3: worker review-class writes on shared F13
tables are trigger-denied with SQLSTATE 42501; web may update only the
return-for-revision generation request column. Observer legitimately has
UPDATE(lifecycle) on incidents; zero-write tests cover all other investigation
columns and every F13 column, preserving that existing recovery boundary.
Roles are cluster-global: do not run downgrade concurrently with other #169
scratch migrations or runtime members. Only this module's DB/logins are removed.
"""

import importlib
import os
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge import Actor, KnowledgeStore
from opspilot.knowledge.jobs import GenerationStore
from opspilot.observation import ObservationStore
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_02_register_remediation_postgres import _register
from tests.integration.test_m1_03_knowledge_store_postgres import (
    F13_TABLES,
    _draft,
    _proposal,
    _seed_incident,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
ROLES = ("opspilot_web", "opspilot_worker")
POOL = PoolConfig(min_size=0, max_size=2, timeout=5)
REVIEWER = Actor("contract-reviewer", "basic_auth")
WORKER = Actor("contract-worker", "worker")
REVIEW_TABLES = (
    "opspilot_postmortems",
    "opspilot_postmortem_versions",
    "opspilot_f13_requests",
    "opspilot_f13_audit",
    "opspilot_knowledge_entries",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
)
GENERATION_TABLES = (
    "opspilot_postmortem_generation_jobs",
    "opspilot_postmortem_generation_attempts",
)


def _config(dsn):
    config = Config(attributes={"dsn": dsn})
    config.set_main_option(
        "script_location", str(schema.__file__.replace("schema.py", "migrations"))
    )
    return config


@contextmanager
def _database():
    name = f"opspilot_roles_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="module")
def dsn():
    with _database() as value:
        yield value


@contextmanager
def _login(dsn, group):
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (group,)
        ).fetchone(), f"P1 missing group role {group}"
    login = f"roles_test_{uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION IN ROLE {}"
            ).format(sql.Identifier(login), sql.Identifier(group))
        )
    try:
        yield make_conninfo(dsn, user=login)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.fixture
def web_dsn(dsn):
    with _login(dsn, ROLES[0]) as value:
        yield value


@pytest.fixture
def worker_dsn(dsn):
    with _login(dsn, ROLES[1]) as value:
        yield value


@pytest.fixture
def observer_dsn(dsn):
    with _login(dsn, "opspilot_observer") as value:
        yield value


@contextmanager
def _store(kind, dsn):
    store = kind(dsn, pool=POOL)
    try:
        store.install()
        yield store
    finally:
        store.close()


def _objects(conn):
    return [
        row[0]
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f') AND (starts_with(c.relname,'opspilot_') OR c.relname='alembic_version') ORDER BY c.relname"
        )
    ]


def _actual_grants(conn, role):
    result = {name: set() for name in _objects(conn)}
    for name, privilege in conn.execute(
        "SELECT c.relname,a.privilege_type FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a JOIN pg_roles r ON r.oid=a.grantee WHERE n.nspname='public' AND r.rolname=%s",
        (role,),
    ):
        if name in result:
            result[name].add(privilege)
    columns = {}
    for name, col, privilege in conn.execute(
        "SELECT c.relname,t.attname,a.privilege_type FROM pg_attribute t JOIN pg_class c ON c.oid=t.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(t.attacl) a JOIN pg_roles r ON r.oid=a.grantee WHERE n.nspname='public' AND r.rolname=%s AND t.attnum>0 AND NOT t.attisdropped",
        (role,),
    ):
        if name in result and privilege not in result[name]:
            columns.setdefault((name, privilege), []).append(col)
    for (name, privilege), cols in columns.items():
        result[name].add(f"{privilege}({','.join(sorted(cols))})")
    return {name: frozenset(value) for name, value in result.items()}


def test_p1_revision_and_nologin_roles(dsn):
    revision = ScriptDirectory.from_config(_config(dsn)).get_revision(
        "0011_runtime_roles"
    )
    assert revision is not None
    assert revision.down_revision == "0010_alert_intake"
    with psycopg.connect(dsn) as conn:
        for role in ROLES:
            assert (
                conn.execute(
                    "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s",
                    (role,),
                ).fetchone()
                == (False,) * 6
            )


def test_p2_declaration_covers_every_object_and_equals_actual_acl(dsn):
    assert importlib.util.find_spec("opspilot.persistence.grants") is not None, (
        "P2 grants module missing"
    )
    grants = importlib.import_module("opspilot.persistence.grants").RUNTIME_GRANTS
    assert isinstance(grants, Mapping)
    assert set(grants) == set(ROLES)
    with psycopg.connect(dsn) as conn:
        for role in ROLES:
            assert isinstance(grants[role], Mapping)
            assert all(isinstance(value, frozenset) for value in grants[role].values())
            assert dict(grants[role]) == _actual_grants(conn, role)
            assert "SELECT" in grants[role]["alembic_version"]


@pytest.mark.parametrize("role", ROLES)
def test_p1_p5_no_ownership_ddl_temp_or_dangerous_table_grants(dsn, role):
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
        ).fetchone(), f"P1 missing role {role}"
        assert not conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE starts_with(c.relname,'opspilot_') AND r.rolname=%s",
            (role,),
        ).fetchall()
        # Direct ACL only, as P1 explicitly excludes PUBLIC's TEMP from scope.
        assert not conn.execute(
            "SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid=a.grantee WHERE n.nspname='public' AND r.rolname=%s AND a.privilege_type='CREATE'",
            (role,),
        ).fetchall()
        assert not conn.execute(
            "SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a JOIN pg_roles r ON r.oid=a.grantee WHERE d.datname=current_database() AND r.rolname=%s AND a.privilege_type='TEMPORARY'",
            (role,),
        ).fetchall()
        assert conn.execute(
            "SELECT has_schema_privilege(%s,'public','USAGE')", (role,)
        ).fetchone()[0]
        for table in _objects(conn):
            for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not conn.execute(
                    "SELECT has_table_privilege(%s,%s,%s)", (role, table, privilege)
                ).fetchone()[0], (role, table, privilege)
                assert not conn.execute(
                    "SELECT has_any_column_privilege(%s,%s,'REFERENCES')", (role, table)
                ).fetchone()[0]


def test_p1_downgrade_clears_local_acl_then_upgrade_restores():
    # Separate DB, no test login members active. A failed member precondition
    # must fail rather than revoke another session's cluster-global role.
    with _database() as scratch:
        assert schema.head_revision() == "0018_alert_correlation", (
            "P1 migration 0011 missing"
        )
        with psycopg.connect(scratch) as conn:
            assert not conn.execute(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid WHERE r.rolname=ANY(%s)",
                (list(ROLES),),
            ).fetchall(), "P1 downgrade requires no active runtime members"
            role_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT oid FROM pg_roles WHERE rolname=ANY(%s)", (list(ROLES),)
                )
            ]
            assert len(role_ids) == 2
        try:
            command.downgrade(_config(scratch), "0010_alert_intake")
            with psycopg.connect(scratch) as conn:
                assert schema.current_revision(conn) == "0010_alert_intake"
                # P1/r5: roles may survive because another DB remains at head.
                # Assert every local ACL, including schema/database/functions.
                for query in (
                    "SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a WHERE a.grantee=ANY(%s::oid[])",
                    "SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a WHERE d.datname=current_database() AND a.grantee=ANY(%s::oid[])",
                    "SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE a.grantee=ANY(%s::oid[])",
                ):
                    assert not conn.execute(query, (role_ids,)).fetchall()
                assert not conn.execute(
                    "SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE a.grantee=ANY(%s::oid[])",
                    (role_ids,),
                ).fetchall()
                assert not conn.execute(
                    "SELECT 1 FROM pg_attribute t CROSS JOIN LATERAL aclexplode(t.attacl) a WHERE a.grantee=ANY(%s::oid[])",
                    (role_ids,),
                ).fetchall()
        finally:
            command.upgrade(_config(scratch), "head")
        with psycopg.connect(scratch) as conn:
            for role in ROLES:
                assert conn.execute(
                    "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone() == (False,)


def _accept(store, *, target_id=None):
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        str(uuid4()),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=10),
        budget_limit=100,
        versions={"contract": "r1"},
        target_id=target_id,
    )
    return incident, run


def test_p4_web_intake_control_and_remediation(dsn, web_dsn):
    with _store(DurableStore, dsn) as owner:
        target = uuid4()
        owner.register_target(f"uid-{target}", target_id=target)
    with _store(DurableStore, web_dsn) as web:
        incident, _ = _accept(web, target_id=target)
        assert web.control(incident, 0, "pause", "operator") == 1
        assert web.rebuild(incident)["state"] == "paused"
        # A paused incident cannot authorize observations. Exercise remediation
        # on a separate open incident, preserving the pause decision.
        incident, _ = _accept(web, target_id=target)
    with _store(ObservationStore, web_dsn) as observations:
        assert _register(observations, incident, expected=0) == 1
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s", (incident,)
        ).fetchone() == ("observing_recovery",)


def test_p4_web_alert_delivery_identity_sequence(web_dsn):
    now = datetime.now(timezone.utc)
    with _store(DurableStore, web_dsn) as web:
        result = web.record_alert(
            dict(
                delivery_key=str(uuid4()),
                fingerprint="test",
                starts_at=now,
                starts_at_raw=now.isoformat(),
                status="resolved",
                actor="contract",
                raw_sha256="0" * 64,
                annotations_sha256="0" * 64,
                alert_json="{}",
                truncated=False,
            )
        )
        assert result["outcome"] == "resolved_recorded"


@pytest.mark.parametrize("terminal", ["publish", "handoff"])
def test_p4_worker_claim_renew_commit_and_terminal(dsn, worker_dsn, terminal):
    with _store(DurableStore, dsn) as owner:
        incident, run = _accept(owner)
    with _store(DurableStore, worker_dsn) as worker:
        lease = worker.claim(
            incident, run, uuid4(), {"contract": "r1"}, lease_seconds=60
        )
        assert worker.renew_lease(lease, 60) > datetime.now(timezone.utc)
        conclusion = {"summary": "contract storage probe"}
        step = worker.commit_step(lease, "final", conclusion)
        if terminal == "publish":
            assert worker.publish(lease, conclusion, step_id=step)
        else:
            worker.hand_off(lease)
    with psycopg.connect(dsn) as conn:
        state, owner_id, lease_until = conn.execute(
            "SELECT state,owner,lease_until FROM opspilot_runs WHERE run_id=%s", (run,)
        ).fetchone()
        assert (state, owner_id, lease_until) == (
            ("completed", None, None)
            if terminal == "publish"
            else ("waiting_human", None, None)
        )
        assert conn.execute(
            "SELECT count(*) FROM opspilot_steps WHERE step_id=%s", (step,)
        ).fetchone() == (1,)


def test_p4_worker_generation_draft_and_stale(dsn, worker_dsn):
    incident, watermark = _seed_incident(dsn)
    with (
        _store(GenerationStore, worker_dsn) as jobs,
        _store(KnowledgeStore, worker_dsn) as knowledge,
    ):
        versions = {
            key: "contract"
            for key in (
                "model",
                "model_profile",
                "prompt_version",
                "output_schema_version",
                "input_policy_version",
            )
        }
        assert incident in jobs.candidates(limit=100)
        claim = jobs.claim(
            incident, owner=uuid4(), versions=versions, max_model_requests=1
        )
        assert claim is not None
        assert jobs.reserve_model_request(claim) == 1
        draft = _draft(knowledge, incident, watermark, proposals=[_proposal()])
        assert draft.state == "under_review"
        jobs.finish(
            claim, status="succeeded", watermark=watermark, version=draft.version
        )
        stale = knowledge.mark_stale(
            draft.object_id,
            draft.version,
            reason="run_added",
            expected_generation=draft.generation,
            idempotency_key=str(uuid4()),
            actor=WORKER,
        )
        assert stale.state == "stale"
    with psycopg.connect(dsn) as conn:
        assert conn.execute(
            "SELECT status,model_requests FROM opspilot_postmortem_generation_attempts WHERE attempt_id=%s",
            (claim.attempt_id,),
        ).fetchone() == ("succeeded", 1)


def _review_fixture(dsn, action):
    with _store(KnowledgeStore, dsn) as owner:
        incident, watermark = _seed_incident(dsn)
        draft = _draft(owner, incident, watermark, proposals=[_proposal()])
        if action not in ("replace", "revoke"):
            return draft, None
        approved = owner.approve(
            draft.object_id,
            1,
            expected_generation=draft.generation,
            idempotency_key=str(uuid4()),
            actor=REVIEWER,
        )
        entry = approved.published["kb-1"]["entry_id"]
        if action == "replace":
            incident, watermark = _seed_incident(dsn)
            draft = _draft(
                owner, incident, watermark, proposals=[_proposal(supersedes=entry)]
            )
        return draft, entry


def _review(store, action, draft, entry):
    args = dict(
        expected_generation=draft.generation,
        idempotency_key=str(uuid4()),
        actor=REVIEWER,
    )
    if action == "revoke":
        return store.revoke(
            entry,
            1,
            expected_generation=1,
            idempotency_key=args["idempotency_key"],
            actor=REVIEWER,
            reason="contract",
        )
    if action in ("approve", "replace"):
        return store.approve(
            draft.object_id,
            draft.version,
            **args,
            entry_generations={entry: 1} if entry else None,
        )
    method = store.reject if action == "reject" else store.return_for_revision
    return method(draft.object_id, draft.version, **args, reason="contract")


@pytest.mark.parametrize("action", ["approve", "reject", "return", "replace", "revoke"])
def test_p4_web_review_actions(dsn, web_dsn, action):
    draft, entry = _review_fixture(dsn, action)
    with _store(KnowledgeStore, web_dsn) as web:
        result = _review(web, action, draft, entry)
        assert (
            result.state
            == {
                "approve": "approved",
                "replace": "approved",
                "reject": "rejected",
                "return": "returned",
                "revoke": "revoked",
            }[action]
        )
        assert (
            web.audit_trail(result.object_kind, result.object_id)[-1]["actor_id"]
            == REVIEWER.actor_id
        )
        if action == "replace":
            assert web.active_revision(entry)["revision"] == 2
        if action == "revoke":
            assert web.active_revision(entry) is None


def _counts(dsn):
    with psycopg.connect(dsn) as conn:
        return {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in (*F13_TABLES, *GENERATION_TABLES)
        }


@pytest.mark.parametrize("action", ["approve", "reject", "return", "replace", "revoke"])
def test_p5_worker_review_refused_and_transaction_leaves_no_rows(
    dsn, worker_dsn, action
):
    draft, entry = _review_fixture(dsn, action)
    before = _counts(dsn)
    with _store(KnowledgeStore, worker_dsn) as worker:
        # Deliberately use a valid human Actor: app principal checks must not
        # masquerade as PG role separation. Owner prepared a legal action.
        with pytest.raises(PersistenceError) as caught:
            _review(worker, action, draft, entry)
        assert isinstance(
            caught.value.__cause__, psycopg.errors.InsufficientPrivilege
        ), caught.value
    assert _counts(dsn) == before


def _denied(dsn, statement):
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()


@pytest.mark.parametrize(
    "table",
    (
        "opspilot_knowledge_entries",
        "opspilot_knowledge_revisions",
        "opspilot_knowledge_revocations",
    ),
)
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
def test_p5_worker_review_only_table_writes_denied(worker_dsn, table, operation):
    columns = {
        "opspilot_knowledge_entries": "generation",
        "opspilot_knowledge_revisions": "revision",
        "opspilot_knowledge_revocations": "reason",
    }
    statement = {
        "INSERT": f"INSERT INTO {table} DEFAULT VALUES",
        "UPDATE": f"UPDATE {table} SET {columns[table]}=DEFAULT WHERE false",
        "DELETE": f"DELETE FROM {table} WHERE false",
    }[operation]
    _denied(worker_dsn, "EXPLAIN " + statement)


def test_c1_worker_nonworker_audit_principal_is_rejected_without_rows(dsn, worker_dsn):
    event_id, object_id, key = uuid4(), uuid4(), f"c1-{uuid4().hex}"
    with psycopg.connect(worker_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
            conn.execute(
                "INSERT INTO opspilot_f13_requests(idempotency_key, action, request_sha256, result) VALUES (%s, 'approve', %s, '{}')",
                (key, "0" * 64),
            )
            conn.execute(
                "INSERT INTO opspilot_f13_audit(event_id,idempotency_key,object_kind,object_id,action,version,actor_id,principal_kind,expected_generation,resulting_generation) VALUES (%s,%s,'postmortem',%s,'approve',1,'worker-1','basic_auth',0,1)",
                (event_id, key, object_id),
            )
        assert caught.value.sqlstate == "42501"
        conn.rollback()
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM opspilot_f13_requests WHERE idempotency_key=%s",
                (key,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM opspilot_f13_audit WHERE event_id=%s", (event_id,)
            ).fetchone()[0]
            == 0
        )


def test_p5_worker_shared_f13_review_content_trigger_denies_and_leaves_rows(
    dsn, worker_dsn
):
    draft, _ = _review_fixture(dsn, "reject")
    with psycopg.connect(dsn) as conn:
        before = conn.execute(
            "SELECT state FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=%s",
            (draft.object_id, draft.version),
        ).fetchone()
        audit_id = conn.execute(
            "SELECT event_id FROM opspilot_f13_audit WHERE object_id=%s ORDER BY recorded_at DESC, resulting_generation DESC LIMIT 1",
            (draft.object_id,),
        ).fetchone()[0]
    with psycopg.connect(worker_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
            conn.execute(
                "UPDATE opspilot_postmortem_versions SET state='approved' WHERE postmortem_id=%s AND version=%s",
                (draft.object_id, draft.version),
            )
        assert caught.value.sqlstate == "42501"
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
            conn.execute(
                "UPDATE opspilot_f13_audit SET action='approve' WHERE event_id=%s",
                (audit_id,),
            )
        assert caught.value.sqlstate == "42501"
        conn.rollback()
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT state FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=%s",
                (draft.object_id, draft.version),
            ).fetchone()
            == before
        )
        assert (
            conn.execute(
                "SELECT action FROM opspilot_f13_audit WHERE event_id=%s", (audit_id,)
            ).fetchone()[0]
            != "approve"
        )


def test_p5_web_generation_attempts_and_lease_result_columns_denied(web_dsn):
    for operation, table, column in (
        ("INSERT", "opspilot_postmortem_generation_attempts", None),
        ("UPDATE", "opspilot_postmortem_generation_attempts", "status"),
        ("DELETE", "opspilot_postmortem_generation_attempts", None),
        ("UPDATE", "opspilot_postmortem_generation_jobs", "lease_owner"),
        ("UPDATE", "opspilot_postmortem_generation_jobs", "lease_epoch"),
        ("UPDATE", "opspilot_postmortem_generation_jobs", "lease_until"),
    ):
        statement = (
            f"INSERT INTO {table} DEFAULT VALUES"
            if operation == "INSERT"
            else f"DELETE FROM {table} WHERE false"
            if operation == "DELETE"
            else f"UPDATE {table} SET {column}=DEFAULT WHERE false"
        )
        _denied(web_dsn, "EXPLAIN " + statement)


def test_p5_web_return_request_column_is_not_acl_denied(web_dsn):
    # C2: return_for_revision updates exactly its six regeneration-request
    # columns; lease/result columns and attempts remain protected.
    allowed = (
        "pending_regeneration_version",
        "consecutive_failures",
        "next_attempt_at",
        "last_error_code",
        "failure_watermark",
        "updated_at",
    )
    with psycopg.connect(web_dsn) as conn:
        for column in allowed:
            assert conn.execute(
                "SELECT has_column_privilege(current_user, 'opspilot_postmortem_generation_jobs', %s, 'UPDATE')",
                (column,),
            ).fetchone()[0], column
        for column in ("incident_id", "pending_regeneration_version"):
            assert conn.execute(
                "SELECT has_column_privilege(current_user, 'opspilot_postmortem_generation_jobs', %s, 'INSERT')",
                (column,),
            ).fetchone()[0], column


def test_p5_web_claim_and_nonnull_lease_writes_denied_but_epoch_is_readonly(
    dsn, web_dsn
):
    with _store(DurableStore, dsn) as owner:
        incident, run = _accept(owner)
    with _store(DurableStore, web_dsn) as web:
        with pytest.raises((PersistenceError, psycopg.errors.InsufficientPrivilege)):
            web.claim(incident, run, uuid4(), {"contract": "r1"}, lease_seconds=60)
    owner_id = uuid4()
    for column, value in (
        ("owner", owner_id),
        ("lease_until", datetime.now(timezone.utc) + timedelta(minutes=1)),
    ):
        with psycopg.connect(web_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
                conn.execute(
                    sql.SQL("UPDATE opspilot_runs SET {}=%s WHERE run_id=%s").format(
                        sql.Identifier(column)
                    ),
                    (value, run),
                )
            assert caught.value.sqlstate == "42501"
            conn.rollback()
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    sql.SQL("SELECT {} FROM opspilot_runs WHERE run_id=%s").format(
                        sql.Identifier(column)
                    ),
                    (run,),
                ).fetchone()[0]
                is None
            )
    _denied(web_dsn, "EXPLAIN UPDATE opspilot_runs SET epoch=epoch+1 WHERE false")


@pytest.mark.parametrize(
    "table,column",
    [
        ("opspilot_postmortem_versions", "content"),
        ("opspilot_postmortem_conclusions", "body"),
        ("opspilot_postmortem_proposals", "content"),
    ],
)
def test_p5_web_draft_content_update_denied(web_dsn, table, column):
    _denied(web_dsn, f"EXPLAIN UPDATE {table} SET {column}=DEFAULT WHERE false")
    _denied(web_dsn, f"EXPLAIN INSERT INTO {table} DEFAULT VALUES")


def test_p5_observer_investigation_and_f13_zero_write(dsn, observer_dsn):
    investigation = (
        "opspilot_runs",
        "opspilot_steps",
        "opspilot_controls",
        "opspilot_inputs",
        "opspilot_input_rounds",
        "opspilot_budget_reservations",
        "opspilot_tool_charges",
        "opspilot_evidence",
        "opspilot_subject_events",
    )
    with psycopg.connect(dsn) as owner:
        tables = (*investigation, *F13_TABLES, *GENERATION_TABLES)
        for table in tables:
            assert owner.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0], (
                table
            )
            columns = [
                r[0]
                for r in owner.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped",
                    (table,),
                )
            ]
            for col in columns:
                for privilege in ("INSERT", "UPDATE", "REFERENCES"):
                    assert not owner.execute(
                        "SELECT has_column_privilege('opspilot_observer',%s,%s,%s)",
                        (table, col, privilege),
                    ).fetchone()[0], (table, col, privilege)
            _denied(observer_dsn, f"EXPLAIN INSERT INTO {table} DEFAULT VALUES")
            _denied(
                observer_dsn,
                f"EXPLAIN UPDATE {table} SET {columns[0]}=DEFAULT WHERE false",
            )
            _denied(observer_dsn, f"EXPLAIN DELETE FROM {table} WHERE false")
        for col in (
            "state",
            "mode",
            "control_generation",
            "current_run_id",
            "conclusion",
        ):
            _denied(
                observer_dsn,
                f"EXPLAIN UPDATE opspilot_incidents SET {col}=DEFAULT WHERE false",
            )
