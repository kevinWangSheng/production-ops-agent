"""Bring the opt-in lab database to the Alembic head before any PG test.

``install()`` no longer creates tables; it only verifies the version
(ADR-0007). The tests keep calling it unchanged, so the schema must be at
head first. This is the "owner connection runs the migration" step that a
deployment does with ``make migrate``; the lab user owns the database.
"""

import os

import pytest

from opspilot import schema
from scripts.m0.postgres_lab import DSN


@pytest.fixture(scope="session", autouse=True)
def _lab_schema_at_head() -> None:
    if (
        os.environ.get("M1_DURABLE_POSTGRES") != "1"
        and os.environ.get("M0_B_POSTGRES") != "1"
    ):
        return
    pg_dump = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
    schema.migrate(DSN, pg_dump=pg_dump)
