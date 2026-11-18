"""PostgreSQL acceptance checks for #428 claim and terminal-state semantics."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture
def scratch_dsn():
    name = f"opspilot_428_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn)
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _insert_pending(dsn: str, key: str, *, next_attempt_at=None):
    run_id = uuid4()
    if next_attempt_at is None:
        next_attempt_at = datetime.now(UTC)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            INSERT INTO opspilot_trace_outbox
                (idempotency_key, incident_id, run_id, kind, payload, state, next_attempt_at)
            VALUES (%s, %s, %s, 'model_step', %s::jsonb, 'pending', %s)
            """,
            (key, uuid4(), run_id, '{"span_kind":"model_step"}', next_attempt_at),
        )
    return run_id


def _state(dsn: str, key: str):
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT state, attempts, drop_reason FROM opspilot_trace_outbox "
            "WHERE idempotency_key=%s",
            (key,),
        ).fetchone()
