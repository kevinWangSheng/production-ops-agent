"""Imported runbook source, database layer (#566 C2a; C2 interface sections 1-3).

Independent contract tests (the author did not see the implementation).
Migration up/down, the append-only and same-transaction-audit rules, the
runtime role grants, and that the F13 postmortem-knowledge objects of 0008
are byte-for-byte what they were before C2a. Rows are created through the
workbench JSON API (``tests/runbook_support.py``), then attacked as the
migration owner and as a real ``opspilot_worker`` login.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from opspilot import schema
from opspilot.persistence.grants import (
    RETENTION_GRANTS,
    RETENTION_ROLE,
    RUNTIME_GRANTS,
    WEB_ROLE,
    WORKER_ROLE,
)
from tests.runbook_support import (
    RUNBOOK_RELATIONS,
    RUNBOOK_TABLES,
    RUNBOOK_VIEW,
    TARGET_A,
    Harness,
    runbook_revision,
    scratch_database,
)
from tests.runtime_roles_support import catalog_grants, login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

#: The 0008 postmortem-knowledge relations C2a must leave untouched.
F13_RELATIONS = (
    "opspilot_postmortems",
    "opspilot_postmortem_versions",
    "opspilot_postmortem_conclusions",
    "opspilot_knowledge_entries",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
    "opspilot_knowledge_revision_states",
    "opspilot_f13_requests",
    "opspilot_f13_audit",
)


@pytest.fixture(scope="module")
def dsn():
    with scratch_database("opspilot_rb_db") as scratch:
        yield scratch


@pytest.fixture(scope="module")
def h(dsn):
    harness = Harness(dsn)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture(scope="module")
def world(h):
    """One approved runbook (rev 1 superseded by rev 2), one revoked, one
    pending, one rejected; ids for the attacks below."""
    _, first = h.publish(name="disk-full")
    entry = first["entry_id"]
    replacement = h.submitted(
        name="disk-full",
        supersedes_entry_id=entry,
        content="# Disk full v2\n\n1. Check the volume.\n",
    )
    h.approved(replacement)
    _, gone = h.publish(name="old-cache")
    assert h.revoke(gone["entry_id"], 1).status == 200
    pending = h.submitted(name="pending-one")
    rejected = h.submitted(name="rejected-one")
    response = h.review(rejected, "reject", reason="wrong service")
    assert response.status == 200, response.text
    return {
        "entry": entry,
        "revoked_entry": gone["entry_id"],
        "pending": pending,
        "rejected": rejected,
    }


def _refused(dsn, statement, params=()):
    """Run one statement as the owner; return the error (or fail)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error) as caught:
            conn.execute(statement, params)
    return caught.value


# -- migration ---------------------------------------------------------


def test_head_has_every_runbook_relation_and_downgrade_removes_only_them():
    revision, down = runbook_revision()
    with scratch_database("opspilot_rb_mig") as dsn:
        with psycopg.connect(dsn) as conn:
            assert schema.current_revision(conn) == schema.head_revision()
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT relname FROM pg_class WHERE relnamespace = "
                    "'public'::regnamespace AND relname LIKE 'opspilot\\_runbook\\_%'"
                )
            }
            kinds = dict(
                conn.execute(
                    "SELECT relname, relkind FROM pg_class WHERE relname = ANY(%s)",
                    (list(RUNBOOK_RELATIONS),),
                ).fetchall()
            )
        assert set(RUNBOOK_RELATIONS) <= present
        assert kinds[RUNBOOK_VIEW] == "v"
        assert all(kinds[t] == "r" for t in RUNBOOK_TABLES)

        # Head may already be past C2a; step down to just below it.
        schema.command.downgrade(schema._config(dsn), down)
        with psycopg.connect(dsn) as conn:
            assert schema.current_revision(conn) == down
            left = conn.execute(
                "SELECT relname FROM pg_class WHERE relname LIKE 'opspilot\\_runbook\\_%'"
            ).fetchall()
            assert left == []
            for relation in F13_RELATIONS:
                assert conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL", (relation,)
                ).fetchone()[0], relation
        schema.upgrade_to(dsn, revision)
        with psycopg.connect(dsn) as conn:
            assert schema.current_revision(conn) == revision
            for relation in RUNBOOK_RELATIONS:
                assert conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL", (relation,)
                ).fetchone()[0], relation


def _f13_shape(dsn):
    """Columns, constraints, triggers (with function bodies) and grants of the
    0008 relations, in a form comparable across databases."""
    with psycopg.connect(dsn) as conn:
        columns = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = ANY(%s) "
            "ORDER BY table_name, column_name",
            (list(F13_RELATIONS),),
        ).fetchall()
        constraints = conn.execute(
            "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid) "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "WHERE c.relname = ANY(%s) ORDER BY 1, 2",
            (list(F13_RELATIONS),),
        ).fetchall()
        triggers = conn.execute(
            "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid), "
            "md5(pg_get_functiondef(t.tgfoid)) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relname = ANY(%s) ORDER BY 1, 2",
            (list(F13_RELATIONS),),
        ).fetchall()
        view = conn.execute(
            "SELECT pg_get_viewdef('opspilot_knowledge_revision_states'::regclass)"
        ).fetchone()
        grants = {
            role: {
                name: held
                for name, held in catalog_grants(conn, role).items()
                if name in F13_RELATIONS
            }
            for role in (WEB_ROLE, WORKER_ROLE, RETENTION_ROLE)
        }
    return columns, constraints, triggers, view, grants


def test_f13_postmortem_knowledge_objects_are_unchanged_by_c2a():
    """Interface goal: the 0008 tables, triggers and grants do not change."""
    _, down = runbook_revision()
    with scratch_database("opspilot_rb_before", migrate=False) as before:
        schema.upgrade_to(before, down)
        with scratch_database("opspilot_rb_after") as after:
            assert _f13_shape(after) == _f13_shape(before)


# -- append-only, decisions once, same-transaction audit ---------------


@pytest.mark.parametrize("table", RUNBOOK_TABLES)
def test_no_row_of_any_runbook_table_can_be_deleted_or_truncated(h, world, table):
    before = h.counts()
    _refused(h.dsn, sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
    _refused(h.dsn, sql.SQL("TRUNCATE {} CASCADE").format(sql.Identifier(table)))
    assert h.counts() == before


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("opspilot_runbook_submissions", "content", "edited"),
        ("opspilot_runbook_submissions", "name", "edited-name"),
        ("opspilot_runbook_decisions", "reason", "edited"),
        ("opspilot_runbook_decisions", "decision", "rejected"),
        ("opspilot_runbook_revisions", "content", "edited"),
        ("opspilot_runbook_revisions", "name", "edited-name"),
        ("opspilot_runbook_revisions", "approved_by", "someone-else"),
        ("opspilot_runbook_revocations", "reason", "edited"),
        ("opspilot_runbook_audit", "actor_id", "someone-else"),
        ("opspilot_runbook_entries", "resource_uid", "payments-prod"),
        ("opspilot_runbook_requests", "request_sha256", "0" * 64),
    ],
)
def test_written_rows_are_immutable(h, world, table, column, value):
    snapshot = h.rows(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
    assert snapshot, f"{table} has rows to attack"
    _refused(
        h.dsn,
        sql.SQL("UPDATE {} SET {} = %s").format(
            sql.Identifier(table), sql.Identifier(column)
        ),
        (value,),
    )
    after = h.rows(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
    assert sorted(map(repr, after)) == sorted(map(repr, snapshot))


def test_a_submission_is_decided_at_most_once(h, world):
    for sid, decision in (
        (world["rejected"], "approved"),
        (world["rejected"], "rejected"),
    ):
        before = h.decisions(sid)
        _refused(
            h.dsn,
            "INSERT INTO opspilot_runbook_decisions "
            "SELECT (jsonb_populate_record(NULL::opspilot_runbook_decisions, "
            "to_jsonb(d) || %s)).* FROM opspilot_runbook_decisions d "
            "WHERE d.submission_id::text = %s",
            (Jsonb({"decision": decision, "reason": "again"}), sid),
        )
        assert h.decisions(sid) == before
    assert len(h.decisions(world["rejected"])) == 1


def test_a_write_without_its_audit_row_in_the_same_transaction_is_refused(h, world):
    """Copy a real submission under a fresh id: only the audit rule (and
    nothing about column values) can refuse it."""
    before = h.counts()
    _refused(
        h.dsn,
        "INSERT INTO opspilot_runbook_submissions "
        "SELECT (jsonb_populate_record(NULL::opspilot_runbook_submissions, "
        "to_jsonb(s) || %s)).* FROM opspilot_runbook_submissions s "
        "WHERE s.submission_id::text = %s",
        (
            Jsonb({"submission_id": str(uuid4()), "name": "copied-no-audit"}),
            world["pending"],
        ),
    )
    assert h.counts() == before


def test_no_revision_exists_for_a_pending_or_rejected_submission(h, world):
    """F13 step 2 shape: only an approved submission becomes a revision; a
    copied revision pointing at a pending submission is refused."""
    for sid in (world["pending"], world["rejected"]):
        assert (
            h.rows(
                "SELECT 1 FROM opspilot_runbook_revisions WHERE submission_id::text = %s",
                (sid,),
            )
            == []
        )
    before = h.counts()
    _refused(
        h.dsn,
        "INSERT INTO opspilot_runbook_revisions "
        "SELECT (jsonb_populate_record(NULL::opspilot_runbook_revisions, "
        "to_jsonb(r) || %s)).* FROM opspilot_runbook_revisions r "
        "WHERE r.entry_id::text = %s AND r.revision = 2",
        (
            Jsonb(
                {
                    "submission_id": world["pending"],
                    "revision": 3,
                    "supersedes_revision": 2,
                }
            ),
            world["entry"],
        ),
    )
    assert h.counts() == before


def test_revision_states_derive_active_superseded_revoked(h, world):
    assert [r["state"] for r in h.revisions(world["entry"])] == ["superseded", "active"]
    assert [r["state"] for r in h.revisions(world["revoked_entry"])] == ["revoked"]
    states = {
        r["state"]
        for r in h.rows(f"SELECT state FROM {RUNBOOK_VIEW}")  # noqa: S608
    }
    assert states <= {"active", "superseded", "revoked"}


# -- grants ------------------------------------------------------------


@pytest.fixture(scope="module")
def catalog(dsn):
    with psycopg.connect(dsn) as conn:
        yield {
            role: catalog_grants(conn, role)
            for role in (WEB_ROLE, WORKER_ROLE, RETENTION_ROLE)
        }


@pytest.mark.parametrize("role", [WEB_ROLE, WORKER_ROLE])
def test_runbook_relations_are_declared_and_granted_as_declared(catalog, role):
    for relation in RUNBOOK_RELATIONS:
        assert relation in RUNTIME_GRANTS[role], relation
        assert catalog[role][relation] == RUNTIME_GRANTS[role][relation], relation


def test_the_worker_reads_only_approved_revisions_and_their_states(catalog):
    held = {r: catalog[WORKER_ROLE][r] for r in RUNBOOK_RELATIONS}
    assert held == {
        r: (
            frozenset({"SELECT"})
            if r in ("opspilot_runbook_revisions", RUNBOOK_VIEW)
            else frozenset()
        )
        for r in RUNBOOK_RELATIONS
    }


def test_the_retention_role_holds_nothing_on_runbooks(catalog):
    for relation in RUNBOOK_RELATIONS:
        assert RETENTION_GRANTS[relation] == frozenset(), relation
        assert catalog[RETENTION_ROLE][relation] == frozenset(), relation


def test_the_web_selects_inserts_by_column_and_updates_only_entry_counters(catalog):
    for relation in RUNBOOK_RELATIONS:
        held = catalog[WEB_ROLE][relation]
        assert "SELECT" in held, relation
        assert not held & {
            "DELETE",
            "TRUNCATE",
            "UPDATE",
            "INSERT",
            "TRIGGER",
            "REFERENCES",
        }, (
            relation,
            held,
        )
        updates = {p for p in held if p.startswith("UPDATE(")}
        inserts = {p for p in held if p.startswith("INSERT(")}
        if relation == "opspilot_runbook_entries":
            assert updates == {"UPDATE(generation,latest_revision,updated_at)"}
        else:
            assert updates == set(), relation
        if relation == RUNBOOK_VIEW:
            assert inserts == set()
        else:
            assert len(inserts) == 1, relation


@pytest.mark.parametrize("table", RUNBOOK_TABLES)
def test_a_worker_login_cannot_write_any_runbook_table(h, world, table):
    """F13 step 2 on the new source: no worker can approve, so every write
    by a real worker login is refused by PostgreSQL (42501), rows unchanged."""
    before = h.counts()
    with login(h.dsn, WORKER_ROLE) as worker_dsn:
        with psycopg.connect(worker_dsn, autocommit=True) as conn:
            statements = [
                sql.SQL("INSERT INTO {} DEFAULT VALUES").format(sql.Identifier(table)),
                sql.SQL("UPDATE {} SET {} = {}").format(
                    sql.Identifier(table),
                    sql.Identifier(_first_column(h, table)),
                    sql.Identifier(_first_column(h, table)),
                ),
                sql.SQL("DELETE FROM {}").format(sql.Identifier(table)),
            ]
            for statement in statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
    assert h.counts() == before


def test_a_worker_login_reads_active_revisions_but_not_submissions(h, world):
    with login(h.dsn, WORKER_ROLE) as worker_dsn:
        with psycopg.connect(worker_dsn, autocommit=True) as conn:
            active = conn.execute(
                f"SELECT count(*) FROM {RUNBOOK_VIEW} WHERE state = 'active'"  # noqa: S608
            ).fetchone()[0]
            assert active >= 1
            conn.execute("SELECT content FROM opspilot_runbook_revisions LIMIT 1")
            for table in set(RUNBOOK_TABLES) - {"opspilot_runbook_revisions"}:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        sql.SQL("SELECT 1 FROM {} LIMIT 1").format(
                            sql.Identifier(table)
                        )
                    )


def _first_column(h, table):
    return h.one(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position LIMIT 1",
        (table,),
    )["column_name"]


def test_runbook_targets_are_stored_as_given(h, world):
    """Implementation decision 3: ``resource_uid`` text, no FK to targets."""
    assert h.entry(world["entry"])["resource_uid"] == TARGET_A
