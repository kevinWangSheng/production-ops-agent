"""Implementer PG tests for imported runbooks (C2a, #566).

A migrated scratch database; the workbench runs as a ``opspilot_web`` login
(the grants of 0028 must be enough), the worker side is a
``opspilot_worker`` login. Contract and acceptance tests belong to the
independent test author; these pin the store and the database guards.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge.runbooks import RunbookDraft, RunbookStore
from opspilot.persistence import PersistenceError, close_pools
from opspilot.web.app import create_app
from opspilot.web.auth import AuthConfig, Authenticator, hash_password
from opspilot.web.runbooks import RunbookReview
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import (
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
JSON_UI = {**basic(), **same_origin(), "accept": "application/json"}


@pytest.fixture(scope="module")
def scratch() -> Iterator[str]:
    name = f"opspilot_c2a_{uuid4().hex[:12]}"
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
        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def web_dsn(scratch: str) -> Iterator[str]:
    with login(scratch, "opspilot_web") as dsn:
        yield dsn


@pytest.fixture(scope="module")
def worker_dsn(scratch: str) -> Iterator[str]:
    with login(scratch, "opspilot_worker") as dsn:
        yield dsn


@pytest.fixture
def store(web_dsn: str) -> RunbookStore:
    return RunbookStore(web_dsn)


def _target() -> str:
    return f"target-{uuid4().hex[:8]}"


def _draft(target: str, name: str = "db", **overrides: Any) -> RunbookDraft:
    values: dict[str, Any] = {
        "resource_uid": target,
        "name": name,
        "description": "When the database times out",
        "content": "1. check the pool\n2. check password=hunter2 handling",
        "source_label": "HolmesGPT 评测夹具",
        "source_ref": "holmesgpt@a0009195:x/SKILL.md",
    }
    values.update(overrides)
    return RunbookDraft(**values)


def _key() -> str:
    return uuid4().hex


def _approved(store: RunbookStore, target: str, name: str = "db"):
    sub = store.submit(_draft(target, name), idempotency_key=_key(), actor_id="alice")
    return store.approve(
        sub.object_id, expected_generation=1, idempotency_key=_key(), actor_id="bob"
    )


def test_submit_approve_revoke_round_trip(store: RunbookStore) -> None:
    target = _target()
    sub = store.submit(
        _draft(target, "Database Troubleshooting"),
        idempotency_key=_key(),
        actor_id="alice",
    )
    assert (sub.state, sub.generation) == ("pending", 1)
    page = store.submission(sub.object_id)
    assert page["name"] == "database-troubleshooting" and page["state"] == "pending"
    assert store.overview()["entries"] == [] or all(
        e["resource_uid"] != target for e in store.overview()["entries"]
    )
    approved = store.approve(
        sub.object_id, expected_generation=1, idempotency_key=_key(), actor_id="alice"
    )
    assert (approved.revision, approved.entry_generation) == (1, 1)
    entry = store.entry(approved.entry_id)
    assert entry["active"]["approved_by"] == "alice"
    assert entry["active"]["content"].endswith("handling")
    revoked = store.revoke(
        approved.entry_id,
        1,
        reason="eval question switch",
        expected_generation=1,
        idempotency_key=_key(),
        actor_id="bob",
    )
    assert revoked.state == "revoked"
    entry = store.entry(approved.entry_id)
    assert entry["active"] is None
    assert entry["revisions"][0]["revoked_reason"] == "eval question switch"
    assert [a["action"] for a in entry["audit"]] == ["approve", "revoke"]


def test_the_same_key_replays_and_a_different_request_conflicts(
    store: RunbookStore,
) -> None:
    target, key = _target(), _key()
    first = store.submit(_draft(target), idempotency_key=key, actor_id="alice")
    again = store.submit(_draft(target), idempotency_key=key, actor_id="alice")
    assert again.replayed and again.object_id == first.object_id
    with pytest.raises(PersistenceError, match="IDEMPOTENCY_CONFLICT"):
        store.submit(
            _draft(target, description="other"), idempotency_key=key, actor_id="alice"
        )


def test_a_decided_submission_cannot_be_decided_again(store: RunbookStore) -> None:
    target = _target()
    sub = store.submit(_draft(target), idempotency_key=_key(), actor_id="alice")
    store.reject(
        sub.object_id,
        reason="wrong target",
        expected_generation=1,
        idempotency_key=_key(),
        actor_id="bob",
    )
    with pytest.raises(PersistenceError, match="GENERATION_CONFLICT"):
        store.approve(
            sub.object_id,
            expected_generation=1,
            idempotency_key=_key(),
            actor_id="bob",
        )
    assert store.submission(sub.object_id)["state"] == "rejected"


def test_active_names_are_unique_per_target(store: RunbookStore) -> None:
    target = _target()
    _approved(store, target)
    with pytest.raises(PersistenceError, match="RUNBOOK_NAME_TAKEN"):
        store.submit(_draft(target), idempotency_key=_key(), actor_id="alice")
    # Another target may use the name.
    _approved(store, _target())


def test_two_pending_submissions_of_one_name_approve_only_once(
    store: RunbookStore,
) -> None:
    target = _target()
    first = store.submit(_draft(target), idempotency_key=_key(), actor_id="alice")
    second = store.submit(_draft(target), idempotency_key=_key(), actor_id="alice")
    store.approve(
        first.object_id, expected_generation=1, idempotency_key=_key(), actor_id="bob"
    )
    with pytest.raises(PersistenceError, match="RUNBOOK_NAME_TAKEN"):
        store.approve(
            second.object_id,
            expected_generation=1,
            idempotency_key=_key(),
            actor_id="bob",
        )


def test_supersede_moves_the_entry_to_its_next_revision(store: RunbookStore) -> None:
    target = _target()
    approved = _approved(store, target)
    with pytest.raises(PersistenceError, match="SUPERSEDE_MISMATCH"):
        store.submit(
            _draft(target, "other", supersedes_entry_id=approved.entry_id),
            idempotency_key=_key(),
            actor_id="alice",
        )
    sub = store.submit(
        _draft(target, content="v2", supersedes_entry_id=approved.entry_id),
        idempotency_key=_key(),
        actor_id="alice",
    )
    with pytest.raises(PersistenceError, match="GENERATION_CONFLICT"):
        store.approve(
            sub.object_id,
            expected_generation=1,
            entry_generation=0,
            idempotency_key=_key(),
            actor_id="bob",
        )
    result = store.approve(
        sub.object_id,
        expected_generation=1,
        entry_generation=1,
        idempotency_key=_key(),
        actor_id="bob",
    )
    assert (result.entry_id, result.revision) == (approved.entry_id, 2)
    states = {
        r["revision"]: r["state"] for r in store.entry(approved.entry_id)["revisions"]
    }
    assert states == {1: "superseded", 2: "active"}


def test_a_revoked_entry_cannot_be_superseded(store: RunbookStore) -> None:
    target = _target()
    approved = _approved(store, target)
    store.revoke(
        approved.entry_id,
        1,
        reason="gone",
        expected_generation=1,
        idempotency_key=_key(),
        actor_id="bob",
    )
    with pytest.raises(PersistenceError, match="SUPERSEDE_TARGET_INACTIVE"):
        store.submit(
            _draft(target, supersedes_entry_id=approved.entry_id),
            idempotency_key=_key(),
            actor_id="alice",
        )
    with pytest.raises(PersistenceError, match="REVISION_NOT_ACTIVE"):
        store.revoke(
            approved.entry_id,
            1,
            reason="again",
            expected_generation=2,
            idempotency_key=_key(),
            actor_id="bob",
        )
    # The name is free again after the revocation: a new entry.
    again = _approved(store, target)
    assert again.entry_id != approved.entry_id


def test_rows_cannot_be_changed_or_written_without_audit(
    scratch: str, store: RunbookStore
) -> None:
    approved = _approved(store, _target())
    with psycopg.connect(scratch, autocommit=True) as conn:
        for statement in (
            "UPDATE opspilot_runbook_revisions SET content='x'",
            "UPDATE opspilot_runbook_submissions SET content='x'",
            "DELETE FROM opspilot_runbook_revisions",
            "DELETE FROM opspilot_runbook_audit",
            "TRUNCATE opspilot_runbook_entries CASCADE",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE opspilot_runbook_entries SET generation=generation+1 "
                "WHERE entry_id=%s",
                (approved.entry_id,),
            )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO opspilot_runbook_revocations(entry_id, revision, reason, "
                "revoked_by, event_id) SELECT entry_id, revision, 'r', 'x', event_id "
                "FROM opspilot_runbook_revisions WHERE entry_id=%s",
                (approved.entry_id,),
            )


def test_the_worker_reads_revisions_and_writes_nothing(
    worker_dsn: str, store: RunbookStore
) -> None:
    approved = _approved(store, _target())
    with psycopg.connect(worker_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT s.state FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
            "WHERE r.entry_id=%s",
            (approved.entry_id,),
        ).fetchone()
        assert row == ("active",)
        for statement in (
            "SELECT 1 FROM opspilot_runbook_submissions",
            "SELECT 1 FROM opspilot_runbook_audit",
            "INSERT INTO opspilot_runbook_requests(idempotency_key, action, "
            "request_sha256, result) VALUES ('w', 'submit', repeat('0', 64), '{}')",
            "UPDATE opspilot_runbook_entries SET generation=generation+1",
            "DELETE FROM opspilot_runbook_revisions",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


def test_the_workbench_flow_over_http(web_dsn: str) -> None:
    _, workbench, clock = build_workbench()
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
        event_tokens={},
        auth_revision="c2a",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        clock,
        runbooks=RunbookReview(store=RunbookStore(web_dsn), target_keys=lambda: None),
    )
    target = _target()
    submitted = post_form(
        app,
        "/runbooks",
        {
            "idempotency_key": _key(),
            "resource_uid": target,
            "name": "db",
            "description": "db timeouts",
            "content": "步骤 " * 3000,
            "source_label": "manual",
        },
        headers=JSON_UI,
    )
    assert submitted.status == 200, submitted.text
    sid = json.loads(submitted.text)["submission_id"]
    page = call(app, "GET", f"/runbooks/submissions/{sid}", headers=basic())
    assert page.status == 200 and "approve" in page.text
    approved = post_form(
        app,
        f"/runbooks/submissions/{sid}/review",
        {"idempotency_key": _key(), "action": "approve", "expected_generation": "1"},
        headers=JSON_UI,
    )
    assert approved.status == 200, approved.text
    body = json.loads(approved.text)
    assert body["revision"] == 1
    listing = call(app, "GET", "/runbooks", headers=basic())
    assert listing.status == 200 and target in listing.text
    entry = call(app, "GET", f"/runbooks/{body['entry_id']}", headers=basic())
    assert entry.status == 200 and "form-revoke" in entry.text
    refused = post_form(
        app,
        f"/runbooks/{body['entry_id']}/revoke",
        {
            "idempotency_key": _key(),
            "revision": "1",
            "expected_generation": "0",
            "reason": "x",
        },
        headers=JSON_UI,
    )
    assert refused.status == 409
    revoked = post_form(
        app,
        f"/runbooks/{body['entry_id']}/revoke",
        {
            "idempotency_key": _key(),
            "revision": "1",
            "expected_generation": "1",
            "reason": "x",
        },
        headers=JSON_UI,
    )
    assert revoked.status == 200 and json.loads(revoked.text)["state"] == "revoked"
