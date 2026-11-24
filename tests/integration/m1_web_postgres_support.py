"""Workbench end to end on the real DurableStore: submit, run, control, read back.

Same flows as ``tests/test_m1_web_workbench.py`` but through the durable
adapters, so generation fences, late results and evidence hashes are proven
against committed PostgreSQL rows rather than the in-memory double.
"""

from __future__ import annotations

import os

import pytest

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
    token_digest,
)
from scripts.m0.postgres_lab import DSN
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _build():
    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    incidents = DurableIncidentStore(store)
    workbench = Workbench(
        incidents=incidents,
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(store),
        sse_poll_seconds=0.02,
        sse_idle_seconds=0.3,
    )
    return app, workbench, store


class _DbClock(FakeClock):
    """Tool/loop clock that tracks the database clock instead of a fixed NOW."""

    def __init__(self, store: DurableStore) -> None:
        super().__init__()
        self._clock = DurableClock(store)

    def now(self):
        return self._clock.now()

    def monotonic(self):
        return self._clock.monotonic()
