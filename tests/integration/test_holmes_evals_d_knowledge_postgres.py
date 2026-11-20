"""Implementer PG test for #666 PR-D: the driver's owner-connection reads of
the runbook catalog (``scripts.holmes_evals.knowledge``) against the real
schema."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge.runbooks import RunbookDraft, RunbookStore
from opspilot.persistence import close_pools
from scripts.holmes_evals import knowledge
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")


@pytest.fixture(scope="module")
def scratch() -> Iterator[str]:
    name = f"opspilot_666d_{uuid4().hex[:12]}"
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


def test_active_runbooks_follow_approval_and_revocation(scratch: str) -> None:
    target = f"t-{uuid4().hex[:6]}"
    assert knowledge.active_runbooks(scratch, target) == []
    store = RunbookStore(scratch)
    sub = store.submit(
        RunbookDraft(target, "db-steps", "d", "1. check", "HolmesGPT 评测夹具"),
        idempotency_key=uuid4().hex,
        actor_id="holmes-eval-import-x",
    )
    assert knowledge.active_runbooks(scratch, target) == []
    approved = store.approve(
        sub.object_id,
        expected_generation=1,
        idempotency_key=uuid4().hex,
        actor_id="holmes-eval-import-x",
    )
    assert knowledge.active_runbooks(scratch, target) == ["db-steps"]
    assert knowledge.active_runbooks(scratch, "other-target") == []
    store.revoke(
        approved.entry_id,
        1,
        reason="评测题切换",
        expected_generation=1,
        idempotency_key=uuid4().hex,
        actor_id="holmes-eval-import-x",
    )
    assert knowledge.active_runbooks(scratch, target) == []


def test_run_catalog_of_an_unknown_run_is_none(scratch: str) -> None:
    assert knowledge.run_catalog(scratch, str(uuid4())) is None
