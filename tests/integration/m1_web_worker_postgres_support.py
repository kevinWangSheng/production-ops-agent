"""Workbench -> worker -> real driver on PostgreSQL (ROADMAP M1-01 item 3b).

An incident submitted through the authenticated intake is claimed by the
polling worker loop, driven through ``InvestigationRunner`` with the
durable event log (the page's stream shows every step) and the durable
tool ledger (tool counts survive a restart), and ends published or parked;
a follow_up after a handoff is picked up; a worker killed mid-run is
resumed by the next one; two workers never double-run a Run; ``resume``
on an overdue Run is refused. The model is scripted; the tool profile is
the product fixture one, exactly as ``python -m opspilot.worker_main``
composes it.
"""

from __future__ import annotations

import os
import threading

import pytest

from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
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
from opspilot.worker import Worker
from opspilot.worker_main import WorkerLoop
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    tool_call,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    post_form,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

LEASE = 2
QUESTION = "Why is checkout erroring?"


def _build(*, run_seconds=600):
    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=run_seconds,
        tool_face=fixture_face(),
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
    return app, workbench, store, events, evidence


class _Only:
    """The store's listing narrowed to one incident: the lab database keeps
    every earlier test's rows, and a stale claimable Run of another test
    would otherwise consume this test's scripted replies. Test 1 checks the
    unnarrowed listing itself."""

    def __init__(self, store, subject):
        self.store, self.subject = store, subject

    def claimable_incidents(self, *, limit=20):
        return tuple(
            i for i in self.store.claimable_incidents(limit=1000) if i == self.subject
        )


def _worker(
    store, events, evidence, replies, *, subject, executor_hook=None, stop=None
):
    """One worker instance: its own owner, the product composition otherwise."""
    clock = DurableClock(store)
    factory = fixture_executor_factory(store, evidence=evidence, clock=clock)
    if executor_hook is not None:
        inner = factory

        def factory(lease, input):  # type: ignore[misc]
            executor = inner(lease, input)

            class Hooked:
                scope = executor.scope
                tool_seconds_used = 0.0

                def execute(self, request):
                    outcome = executor.execute(request)
                    executor_hook(request)
                    return outcome

            return Hooked()

    model = ScriptedModel(replies)
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, fixture_versions()),
        model=model,
        executor_factory=factory,
        clock=clock,
        lease_seconds=LEASE,
        events=events,
        evidence=evidence,
    )
    loop = WorkerLoop(
        queue=_Only(store, subject),
        runner=runner,
        sweeper=store,
        events=events,
        stop=stop if stop is not None else threading.Event(),
        poll_seconds=0.05,
    )
    return loop, model


def _tool_round(n=1):
    return reply(
        tool_calls=[tool_call(call_id=f"call-{n}", name=FIXTURE_TOOL)],
        finish="tool_calls",
    )


def _submit(app, key):
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": QUESTION, "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201
    return response.json()


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def _kinds(events, subject):
    return [e.kind for e in events.read_after(subject, 0, limit=1000)]
