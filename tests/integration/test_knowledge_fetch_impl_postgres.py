"""Implementer PG tests for C2b (#566): the worker's knowledge read and the
web's catalog read, each under its own runtime-role login.

Contract and acceptance tests belong to the independent test author.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge.catalog import catalog_for_target, valid_catalog
from opspilot.knowledge.runbooks import RunbookDraft, RunbookStore
from opspilot.persistence import close_pools
from opspilot.tools.knowledge_fetch import KnowledgeReader
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")


@pytest.fixture(scope="module")
def scratch() -> Iterator[str]:
    name = f"opspilot_c2b_{uuid4().hex[:12]}"
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
def world(scratch: str) -> dict[str, Any]:
    store = RunbookStore(scratch)
    target, other = f"t-{uuid4().hex[:6]}", f"o-{uuid4().hex[:6]}"

    def publish(uid: str, name: str, content: str = "steps") -> Any:
        sub = store.submit(
            RunbookDraft(uid, name, f"{name} help", content, "HolmesGPT 评测夹具"),
            idempotency_key=uuid4().hex,
            actor_id="alice",
        )
        return store.approve(
            sub.object_id,
            expected_generation=1,
            idempotency_key=uuid4().hex,
            actor_id="bob",
        )

    active = publish(target, "database-troubleshooting")
    revoked = publish(target, "old-runbook")
    store.revoke(
        revoked.entry_id,
        1,
        reason="gone",
        expected_generation=1,
        idempotency_key=uuid4().hex,
        actor_id="bob",
    )
    pending = store.submit(
        RunbookDraft(target, "pending-runbook", "p", "x", "l"),
        idempotency_key=uuid4().hex,
        actor_id="alice",
    )
    foreign = publish(other, "foreign-runbook")
    return {
        "target": target,
        "other": other,
        "active": active,
        "revoked": revoked,
        "pending": pending,
        "foreign": foreign,
    }


@pytest.fixture(scope="module")
def reader(scratch: str) -> Iterator[KnowledgeReader]:
    with login(scratch, "opspilot_worker") as dsn:
        yield KnowledgeReader(dsn)


def test_an_active_runbook_by_name_and_by_entry_id(reader, world) -> None:
    by_name = reader.read(world["target"], "Database Troubleshooting")
    assert by_name["status"] == "ok" and by_name["kind"] == "runbook_import"
    assert by_name["row"]["content"] == "steps"
    by_id = reader.read(world["target"], str(world["active"].entry_id))
    assert by_id["row"]["entry_id"] == world["active"].entry_id


def test_a_revoked_runbook_is_not_available(reader, world) -> None:
    assert reader.read(world["target"], "old-runbook") == {
        "status": "not_available",
        "state": "revoked",
    }
    assert reader.read(world["target"], str(world["revoked"].entry_id))["status"] == (
        "not_available"
    )


@pytest.mark.parametrize("which", ["pending-runbook", "foreign-runbook", "custom-crd"])
def test_pending_foreign_and_unknown_are_not_found(reader, world, which) -> None:
    result = reader.read(world["target"], which)
    assert result == {"status": "not_found", "available": ["database-troubleshooting"]}


def test_another_targets_entry_id_is_not_found(reader, world) -> None:
    result = reader.read(world["target"], str(world["foreign"].entry_id))
    assert result["status"] == "not_found"
    # The postmortem query ran too (no such entry): its SQL is valid.
    assert reader.read(world["target"], str(uuid4()))["status"] == "not_found"


def test_the_web_reads_the_catalog_with_its_own_role(scratch, world) -> None:
    with login(scratch, "opspilot_web") as dsn:
        fact = catalog_for_target(
            RunbookStore(dsn), world["target"], max_age_seconds=3600
        )
    assert valid_catalog(fact)
    assert [e["skill_id"] for e in fact["entries"]] == ["database-troubleshooting"]
    assert fact["entries"][0]["freshness"] == "fresh"
