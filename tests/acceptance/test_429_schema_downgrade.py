"""Independent acceptance tests for issue #429's schema downgrade CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from scripts.m0.postgres_lab import DSN

ROOT = Path(__file__).resolve().parents[2]
CURRENT = "0024_trace_outbox"
PREVIOUS = "0023_retention_raw_cleanup"
OUTBOX = "opspilot_trace_outbox"

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_429_{uuid4().hex[:12]}"
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


def _run_cli(
    dsn: str, *args: str, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["OPSPILOT_DSN"] = dsn
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "opspilot.schema", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _set_revision_for_test(dsn: str, revision: str) -> None:
    config = Config(attributes={"dsn": dsn})
    config.set_main_option("script_location", str(ROOT / "opspilot" / "migrations"))
    command.downgrade(config, revision)


def _business_counts(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        names = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name LIKE 'opspilot_%' "
            "AND table_name <> %s ORDER BY table_name",
            (OUTBOX,),
        ).fetchall()
        return {
            name: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
            ).fetchone()[0]
            for (name,) in names
        }


def test_unknown_revision_is_rejected_without_changing_schema(scratch_dsn: str) -> None:
    result = _run_cli(scratch_dsn, "downgrade", "--to", "0024_unknown")

    assert result.returncode == 1
    assert (
        "unknown revision 0024_unknown: not a migration in this build" in result.stderr
    )
    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            CURRENT,
        )


def test_equal_or_newer_revision_is_rejected(scratch_dsn: str) -> None:
    equal = _run_cli(scratch_dsn, "downgrade", "--to", CURRENT)
    assert equal.returncode == 1
    assert f"schema current: {CURRENT}" in equal.stdout
    assert f"revision {CURRENT} is not lower than the current {CURRENT}" in equal.stderr

    _set_revision_for_test(scratch_dsn, PREVIOUS)
    newer = _run_cli(scratch_dsn, "downgrade", "--to", CURRENT)
    assert newer.returncode == 1
    assert f"schema current: {PREVIOUS}" in newer.stdout
    assert (
        f"revision {CURRENT} is not lower than the current {PREVIOUS}" in newer.stderr
    )


def test_success_uses_owner_dsn_reports_target_and_drops_only_outbox(
    scratch_dsn: str,
) -> None:
    before_counts = _business_counts(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        for state in ("pending", "delivered", "dropped"):
            conn.execute(
                f"INSERT INTO {OUTBOX}(idempotency_key,incident_id,kind,payload,state,"
                "finalized_at) "
                "VALUES (%s,%s,'run_state','{}',%s,"
                "CASE WHEN %s='pending' THEN NULL ELSE clock_timestamp() END)",
                (f"429-{state}-{uuid4()}", uuid4(), state, state),
            )

    # A downgrade that reads any service DSN instead of the owner DSN fails
    # against this deliberately unusable value.
    result = _run_cli(
        scratch_dsn,
        "downgrade",
        "--to",
        PREVIOUS,
        OPSPILOT_WEB_DSN="host=127.0.0.1 port=1 dbname=wrong user=wrong",
        OPSPILOT_WORKER_DSN="host=127.0.0.1 port=1 dbname=wrong user=wrong",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:3] == [
        f"schema current: {CURRENT}",
        f"schema target: {PREVIOUS}",
        f"schema downgraded: {PREVIOUS}",
    ]
    assert (
        "opspilot_trace_outbox dropped by downgrade: pending=1, delivered=1, dropped=1"
    ) in result.stderr

    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PREVIOUS,
        )
        assert conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s",
            (OUTBOX,),
        ).fetchone() == (0,)
    assert _business_counts(scratch_dsn) == before_counts
