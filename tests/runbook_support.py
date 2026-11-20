"""Shared harness for the imported-runbook contract tests (#566 C2a).

Written by an independent test author from the C2 public interface
(``docs/tasks/2026-11-19-tool-expansion-c2.md``) without the implementation.
Assertions read persisted rows as the migration owner and drive the
workbench over in-process ASGI; nothing in the product is mocked.

``build_app`` is the one place that guesses how the workbench is handed a
``RunbookStore`` (the interface names the class, not its constructor or the
``create_app`` keyword). Adapting that wiring is not an assertion change.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot import schema
from opspilot.persistence import DurableStore
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
)
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.m1_web_support import ORIGIN, UI_PASSWORD, UI_USER, basic, call, post_form

#: A second signed-in user, so "who reviewed" is not trivially the submitter.
REVIEWER = "runbook-reviewer"
REVIEWER_PASSWORD = "runbook-reviewer-test-password"

#: Targets the workbench knows (``UNKNOWN_TARGET`` for anything else).
TARGET_A = "checkout-prod"
TARGET_B = "payments-prod"
KNOWN_TARGETS = (TARGET_A, TARGET_B)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")

#: Every relation C2a adds (interface section 1).
RUNBOOK_TABLES = (
    "opspilot_runbook_requests",
    "opspilot_runbook_audit",
    "opspilot_runbook_submissions",
    "opspilot_runbook_decisions",
    "opspilot_runbook_entries",
    "opspilot_runbook_revisions",
    "opspilot_runbook_revocations",
)
RUNBOOK_VIEW = "opspilot_runbook_revision_states"
RUNBOOK_RELATIONS = (*RUNBOOK_TABLES, RUNBOOK_VIEW)


def runbook_revision() -> tuple[str, str]:
    """The C2a migration's id and its down revision, found by name so a
    renumbering at merge time (``0028`` is "as of main") changes nothing."""
    from alembic.script import ScriptDirectory

    script_dir = ScriptDirectory.from_config(schema._config(""))
    matches = [
        s
        for s in script_dir.walk_revisions()
        if s.revision.endswith("_runbook_imports")
    ]
    assert len(matches) == 1, "exactly one *_runbook_imports migration"
    down = matches[0].down_revision
    assert isinstance(down, str)
    return matches[0].revision, down


@contextmanager
def scratch_database(prefix: str, *, migrate: bool = True) -> Iterator[str]:
    """A throwaway database on the lab server, at head unless ``migrate=False``."""
    name = f"{prefix}_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        if migrate:
            schema.migrate(dsn, pg_dump=PG_DUMP)
        yield dsn
    finally:
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def registry() -> MappingTargetRegistry:
    return MappingTargetRegistry(
        {
            uid: {
                "integration_id": "test-integration",
                "cluster_uid": "test-cluster",
                "namespace": "otel-demo",
            }
            for uid in KNOWN_TARGETS
        }
    )


class Harness:
    """Real persistence, the real ASGI workbench, owner reads of every row."""

    def __init__(self, dsn: str) -> None:
        from opspilot.knowledge.runbooks import RunbookStore

        self.dsn = dsn
        self.durable = DurableStore(dsn)
        self.durable.install()
        events = DurableEventLog(self.durable)
        events.install()
        evidence = DurableEvidenceStore(self.durable)
        evidence.install()
        ledger = DurableWebLedger(self.durable)
        ledger.install()
        self.runbooks = RunbookStore(dsn)
        install = getattr(self.runbooks, "install", None)
        if install is not None:
            install()
        bench = Workbench(
            incidents=DurableIncidentStore(self.durable),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions={"state": "v1"},
            run_seconds=600,
            targets=registry(),
        )
        auth = Authenticator(
            AuthConfig(
                ui_users={
                    UI_USER: hash_password(UI_PASSWORD),
                    REVIEWER: hash_password(REVIEWER_PASSWORD),
                },
                event_tokens={},
                auth_revision="runbook-contract",
                allowed_origins=frozenset({ORIGIN}),
            )
        )
        self.app = create_app(
            bench, auth, DurableClock(self.durable), runbooks=self.runbooks
        )

    def close(self) -> None:
        close = getattr(self.runbooks, "close", None)
        if close is not None:
            close()
        self.durable.close()

    # -- HTTP ----------------------------------------------------------

    def post(
        self,
        path: str,
        fields: dict[str, Any],
        *,
        user: str | None = UI_USER,
        origin: str | None = ORIGIN,
        accept: str = "application/json",
    ):
        headers: dict[str, str] = {"accept": accept}
        if user == UI_USER:
            headers.update(basic())
        elif user == REVIEWER:
            headers.update(basic(REVIEWER, REVIEWER_PASSWORD))
        if origin is not None:
            headers["origin"] = origin
        clean = {k: str(v) for k, v in fields.items() if v is not None}
        return post_form(self.app, path, clean, headers=headers)

    def get(self, path: str, *, signed_in: bool = True, accept: str = "text/html"):
        headers = {"accept": accept, **(basic() if signed_in else {})}
        return call(self.app, "GET", path, headers=headers)

    def submit(self, *, user: str = UI_USER, **overrides: Any):
        fields: dict[str, Any] = {
            "resource_uid": TARGET_A,
            "name": unique_name(),
            "description": "Steps for checkout 5xx after a deploy.",
            "content": "# Checkout errors\n\n1. Read the error rate.\n",
            "source_label": "contract test",
            "source_ref": "tests/runbook_support.py",
            "idempotency_key": uuid4().hex,
        }
        fields.update(overrides)
        return self.post("/runbooks", fields, user=user)

    def submitted(self, **overrides: Any) -> str:
        response = self.submit(**overrides)
        assert response.status == 200, response.text
        return response.json()["submission_id"]

    def review(
        self,
        submission_id: str,
        action: str = "approve",
        *,
        user: str = REVIEWER,
        **overrides: Any,
    ):
        fields: dict[str, Any] = {
            "action": action,
            "expected_generation": self.submission_generation(submission_id),
            "idempotency_key": uuid4().hex,
        }
        superseded = self.submission(submission_id)["supersedes_entry_id"]
        if superseded is not None:
            fields["entry_generation"] = self.entry(str(superseded))["generation"]
        fields.update(overrides)
        return self.post(
            f"/runbooks/submissions/{submission_id}/review", fields, user=user
        )

    def approved(self, submission_id: str, **overrides: Any) -> dict[str, Any]:
        response = self.review(submission_id, "approve", **overrides)
        assert response.status == 200, response.text
        return response.json()

    def revoke(
        self, entry_id: str, revision: int, *, user: str = REVIEWER, **overrides
    ):
        fields: dict[str, Any] = {
            "revision": revision,
            "reason": "contract test revoke",
            "expected_generation": self.entry(entry_id)["generation"],
            "idempotency_key": uuid4().hex,
        }
        fields.update(overrides)
        return self.post(f"/runbooks/{entry_id}/revoke", fields, user=user)

    def publish(self, **overrides: Any) -> tuple[str, dict[str, Any]]:
        """Submit and approve; returns (submission_id, approve result)."""
        submission_id = self.submitted(**overrides)
        return submission_id, self.approved(submission_id)

    # -- owner reads ---------------------------------------------------

    def rows(self, query: Any, params: tuple = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return list(conn.execute(query, params).fetchall())

    def one(self, query: Any, params: tuple = ()) -> dict[str, Any]:
        [row] = self.rows(query, params)
        return row

    def submission(self, submission_id: str) -> dict[str, Any]:
        return self.one(
            "SELECT * FROM opspilot_runbook_submissions WHERE submission_id::text = %s",
            (submission_id,),
        )

    def decisions(self, submission_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM opspilot_runbook_decisions WHERE submission_id::text = %s",
            (submission_id,),
        )

    def entry(self, entry_id: str) -> dict[str, Any]:
        return self.one(
            "SELECT * FROM opspilot_runbook_entries WHERE entry_id::text = %s",
            (entry_id,),
        )

    def revisions(self, entry_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT r.*, s.state FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s "
            "ON s.entry_id = r.entry_id AND s.revision = r.revision "
            "WHERE r.entry_id::text = %s ORDER BY r.revision",
            (entry_id,),
        )

    def revocations(self, entry_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM opspilot_runbook_revocations WHERE entry_id::text = %s "
            "ORDER BY revision",
            (entry_id,),
        )

    def audit(self, object_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM opspilot_runbook_audit WHERE object_id::text = %s "
            "ORDER BY resulting_generation",
            (object_id,),
        )

    def submission_generation(self, submission_id: str) -> int:
        """The submission's current generation: the last audit row's
        ``resulting_generation`` (interface section 1, ``+1`` per change)."""
        rows = [
            r
            for r in self.audit(submission_id)
            if r["object_kind"] == "runbook_submission"
        ]
        assert rows, "a submission has its submit audit row"
        return max(r["resulting_generation"] for r in rows)

    def counts(self) -> dict[str, int]:
        with psycopg.connect(self.dsn) as conn:
            return {
                table: conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                for table in RUNBOOK_TABLES
            }

    def active_names(self, resource_uid: str) -> list[str]:
        return [
            r["name"]
            for r in self.rows(
                "SELECT r.name FROM opspilot_runbook_revisions r "
                "JOIN opspilot_runbook_revision_states s "
                "ON s.entry_id = r.entry_id AND s.revision = r.revision "
                "WHERE s.state = 'active' AND r.resource_uid = %s ORDER BY r.name",
                (resource_uid,),
            )
        ]


def unique_name(prefix: str = "runbook") -> str:
    return f"{prefix}-{uuid4().hex[:10]}"
