"""Implementer PG tests for standing instructions (#780, migration 0029).

The store side as a ``opspilot_web`` login (0029's grants must be enough)
and the worker's ``fetch_skill`` read as a ``opspilot_worker`` login.
Acceptance, contract and safety tests belong to the independent author.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors

from opspilot.knowledge.runbooks import (
    STANDING_INSTRUCTIONS_DESCRIPTION,
    STANDING_INSTRUCTIONS_NAME,
    RunbookDraft,
    RunbookStore,
)
from opspilot.knowledge.standing import standing_instructions_for_target
from opspilot.persistence import PersistenceError
from opspilot.tools.knowledge_fetch import KnowledgeReader
from tests.integration import test_runbooks_impl_postgres as base

# The C2a module's scratch database, logins and helpers.
pytestmark = base.pytestmark
scratch = base.scratch
web_dsn = base.web_dsn
worker_dsn = base.worker_dsn
store = base.store
_approved, _draft, _key, _target = base._approved, base._draft, base._key, base._target

STANDING = "standing_instructions"


def _standing(target: str, content: str = "* check the queue depth", **kw: Any):
    return RunbookDraft(
        resource_uid=target,
        name=kw.pop("name", ""),
        description=kw.pop("description", ""),
        content=content,
        source_label="operator",
        kind=STANDING,
        **kw,
    )


def _approve_standing(store: RunbookStore, target: str, content: str = "* one"):
    sub = store.submit(_standing(target, content), idempotency_key=_key(), actor_id="a")
    return store.approve(
        sub.object_id, expected_generation=1, idempotency_key=_key(), actor_id="b"
    )


def test_standing_instructions_take_the_fixed_name_and_description(
    store: RunbookStore,
) -> None:
    target = _target()
    sub = store.submit(
        _standing(target, name="whatever"), idempotency_key=_key(), actor_id="a"
    )
    page = store.submission(sub.object_id)
    assert page["kind"] == STANDING
    assert page["name"] == STANDING_INSTRUCTIONS_NAME
    assert page["description"] == STANDING_INSTRUCTIONS_DESCRIPTION
    approved = store.approve(
        sub.object_id, expected_generation=1, idempotency_key=_key(), actor_id="b"
    )
    assert store.entry(approved.entry_id)["active"]["kind"] == STANDING


def test_a_runbook_cannot_take_the_reserved_name(store: RunbookStore) -> None:
    with pytest.raises(PersistenceError, match="RUNBOOK_NAME_INVALID"):
        store.submit(
            _draft(_target(), "Standing Instructions"),
            idempotency_key=_key(),
            actor_id="a",
        )


def test_content_over_4096_bytes_is_refused(store: RunbookStore) -> None:
    with pytest.raises(PersistenceError, match="RUNBOOK_CONTENT_INVALID"):
        store.submit(
            _standing(_target(), "é" * 2049), idempotency_key=_key(), actor_id="a"
        )


def test_a_second_entry_per_target_is_refused_and_supersede_replaces(
    store: RunbookStore,
) -> None:
    target = _target()
    first = _approve_standing(store, target, "* first")
    with pytest.raises(PersistenceError, match="STANDING_INSTRUCTIONS_EXISTS"):
        store.submit(_standing(target), idempotency_key=_key(), actor_id="a")
    replacement = store.submit(
        _standing(target, "* second", supersedes_entry_id=first.entry_id),
        idempotency_key=_key(),
        actor_id="a",
    )
    second = store.approve(
        replacement.object_id,
        expected_generation=1,
        idempotency_key=_key(),
        actor_id="b",
        entry_generation=first.entry_generation,
    )
    assert (second.entry_id, second.revision) == (first.entry_id, 2)
    fact = standing_instructions_for_target(store, target)
    assert fact is not None
    assert (fact["revision"], fact["content"]) == (2, "* second")


def test_two_pending_new_entries_approve_only_once(store: RunbookStore) -> None:
    target = _target()
    one = store.submit(_standing(target), idempotency_key=_key(), actor_id="a")
    two = store.submit(_standing(target), idempotency_key=_key(), actor_id="a")
    store.approve(
        one.object_id, expected_generation=1, idempotency_key=_key(), actor_id="b"
    )
    with pytest.raises(PersistenceError, match="STANDING_INSTRUCTIONS_EXISTS"):
        store.approve(
            two.object_id, expected_generation=1, idempotency_key=_key(), actor_id="b"
        )


def test_kinds_do_not_supersede_each_other(store: RunbookStore) -> None:
    target = _target()
    runbook = _approved(store, target, "db")
    with pytest.raises(PersistenceError, match="SUPERSEDE_MISMATCH"):
        store.submit(
            _standing(target, supersedes_entry_id=runbook.entry_id),
            idempotency_key=_key(),
            actor_id="a",
        )


def test_the_list_and_fetch_skill_see_only_runbooks(
    store: RunbookStore, worker_dsn: str
) -> None:
    target = _target()
    _approved(store, target, "db")
    standing = _approve_standing(store, target)
    assert [r["name"] for r in store.active_catalog(target)["rows"]] == ["db"]
    reader = KnowledgeReader(worker_dsn)
    assert reader.read(target, STANDING_INSTRUCTIONS_NAME)["status"] == "not_found"
    assert reader.read(target, str(standing.entry_id))["status"] == "not_found"
    assert reader.read(target, "db")["status"] == "ok"


def test_revoked_instructions_are_not_read(store: RunbookStore) -> None:
    target = _target()
    approved = _approve_standing(store, target)
    store.revoke(
        approved.entry_id,
        1,
        reason="retired",
        expected_generation=approved.entry_generation,
        idempotency_key=_key(),
        actor_id="b",
    )
    assert store.active_standing_instructions(target) is None


def test_the_database_binds_kind_name_and_size(scratch: str) -> None:
    with psycopg.connect(scratch) as conn:
        # The audit guards run first; with triggers off only the CHECK binds.
        conn.execute("SET session_replication_role = replica")
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO opspilot_runbook_submissions(submission_id, resource_uid, "
                "kind, name, description, content, content_sha256, source_label, "
                "source_ref, submitted_by, event_id) VALUES (%s,'t','standing_instructions',"
                "'other','d','x',encode(sha256('x'),'hex'),'l','','a',%s)",
                (uuid4(), uuid4()),
            )
