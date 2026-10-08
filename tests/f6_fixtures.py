"""Opt-in F6 PostgreSQL boundary fixtures, shared by acceptance/contracts."""

import os
import socket
from copy import deepcopy
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from scripts.m0.postgres_lab import DSN
from tests.f6_boundary_support import GuardedRecoveryDriver, RecoveryBoundaries
from tests.f6_product_driver import ProductRecoveryRuntime


@pytest.fixture(scope="module")
def f6_database():
    if os.environ.get("M1_DURABLE_POSTGRES") != "1":
        pytest.skip("explicit PG opt-in required: M1_DURABLE_POSTGRES=1")
    name, login = f"f6_acceptance_{uuid4().hex[:12]}", f"f6_observer_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                    sql.Identifier(login)
                )
            )
        yield dsn, make_conninfo(dsn, user=login)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login))
            )


@pytest.fixture
def recovery_boundaries():
    return RecoveryBoundaries()


@pytest.fixture
def recovery_runtime(f6_database, recovery_boundaries, monkeypatch):
    # Observer has only InstantSource as an outgoing dependency, no model or
    # environment writer. Also reject Python socket fallback globally while
    # this runtime is alive (libpq connections do not use Python sockets).
    def reject_network(*args, **kwargs):
        recovery_boundaries.environment_write("UNCONFIGURED_NETWORK", repr(args))

    import openai

    monkeypatch.setattr(openai.OpenAI, "request", recovery_boundaries.model_request)

    async def reject_async_model(*args, **kwargs):
        recovery_boundaries.model_request(*args, **kwargs)

    monkeypatch.setattr(openai.AsyncOpenAI, "request", reject_async_model)

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    runtime = ProductRecoveryRuntime(*f6_database, recovery_boundaries, monkeypatch)
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture
def recovery_driver(recovery_runtime, recovery_boundaries):
    from tests.acceptance.test_f6_recovery import TARGET

    driver = GuardedRecoveryDriver(recovery_runtime, recovery_boundaries)
    driver.seed_incident("incident-f6", target=deepcopy(TARGET), lifecycle="open")
    return driver


@pytest.fixture
def f6_profile():
    from tests.acceptance.test_f6_recovery import profile

    return profile.__wrapped__()
