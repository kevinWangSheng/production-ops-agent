"""Workbench end to end on the real DurableStore: submit, run, control, read back.

Same flows as ``tests/test_m1_web_workbench.py`` but through the durable
adapters, so generation fences, late results and evidence hashes are proven
against committed PostgreSQL rows rather than the in-memory double.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
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
from tests.m1_investigation_support import reply, report_from_transcript, tool_call
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    ScriptedInvestigator,
    basic,
    post_form,
    same_origin,
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


@pytest.mark.skipif(
    not hasattr(DurableStore, "renew_lease"), reason="needs PR #35 renew_lease"
)
def test_run_once_renews_a_short_lease_before_each_commit_on_postgres():
    """Two model rounds each longer than half the lease; renewals keep the
    attempt alive, and the lease is released at the end."""
    import time

    app, workbench, store = _build()
    key = f"pg-lease-{uuid4()}"
    accepted = post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": "lease", "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert accepted.status == 201
    subject = workbench.list_incidents()[0].incident_id
    run_id = workbench.list_incidents()[0].current_run_id
    seen: list = []

    def slow_round(call_):
        time.sleep(2.5)
        with store.transaction(snapshot=True) as conn:
            seen.append(
                conn.execute(
                    "SELECT lease_until FROM opspilot_runs WHERE run_id=%s", (run_id,)
                ).fetchone()["lease_until"]
            )
        return reply(tool_calls=[tool_call()], finish="tool_calls")

    def slow_final(call_):
        time.sleep(2.5)
        with store.transaction(snapshot=True) as conn:
            seen.append(
                conn.execute(
                    "SELECT lease_until FROM opspilot_runs WHERE run_id=%s", (run_id,)
                ).fetchone()["lease_until"]
            )
        return report_from_transcript(call_)

    investigator = ScriptedInvestigator(
        _DbClock(store), replies=[slow_round, slow_final]
    )
    outcome = workbench.run_once(subject, investigator, lease_seconds=4)
    assert outcome is not None and outcome.execution == "completed"
    # The lease observed during round 2 is later than the one granted at
    # claim time by more than the elapsed sleep would allow without renewal.
    assert seen[1] > seen[0]
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT state,owner,lease_until FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
    assert row["state"] == "completed" and row["owner"] is None


@pytest.mark.skipif(
    not hasattr(DurableStore, "renew_lease"), reason="needs PR #35 renew_lease"
)
def test_a_refused_renewal_on_postgres_hands_off_with_history():
    app, workbench, store = _build()
    key = f"pg-lease-refused-{uuid4()}"
    post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": "lease", "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    subject = workbench.list_incidents()[0].incident_id

    def pause_then_answer(call_):
        workbench.control(
            subject,
            actor_id=UI_USER,
            action="pause",
            expected_generation=0,
            idempotency_key="mid",
        )
        return report_from_transcript(call_)

    investigator = ScriptedInvestigator(
        _DbClock(store),
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            pause_then_answer,
        ],
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    snapshot = workbench.snapshot(subject)
    assert [s["status"] for s in snapshot["steps"]] == [
        "tool_result_committed",
        "late_result",
    ]
    assert workbench.events.read_after(subject, 0)[-1].kind == "run_handoff"


def test_durable_evidence_reregistration_reuses_the_first_observation():
    """Review thread (PR #33): replay after a restart must not conflict."""
    import hashlib
    from dataclasses import replace
    from datetime import timedelta

    from opspilot.tools import TransportResponse
    from opspilot.tools.registry import canonical_hash
    from opspilot.web import DurableEvidenceStore
    from tests.m1_tool_support import WINDOW_START, body, build, request

    executor, transport, _, _ = build(scope_overrides={"run_id": f"run-{uuid4()}"})
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
    )
    record = executor.execute(request(step_id=f"step-{uuid4()}")).evidence
    assert record is not None
    store = DurableStore(DSN)
    evidence = DurableEvidenceStore(store)
    evidence.install()
    assert evidence.register(record) == record.evidence_id
    later_view = dict(record.view, observed_at="2026-09-14T01:06:00+00:00")
    again = replace(
        record,
        observed_at=record.observed_at + timedelta(seconds=60),
        _view=later_view,
        view_sha256=canonical_hash(later_view),
    )
    assert evidence.register(again) == record.evidence_id
    kept = evidence.get(record.evidence_id)
    assert kept is not None and kept.view_sha256 == again.view_sha256
    assert kept.observed_at == again.observed_at
    other = b'{"data":{"result":[]}}'
    with pytest.raises(PersistenceError, match="IDENTITY_CONFLICT"):
        evidence.register(
            replace(record, raw=other, raw_sha256=hashlib.sha256(other).hexdigest())
        )


def test_durable_evidence_commit_fences_stale_replacement():
    """Round 3 thread: a stale same-bytes replay cannot overwrite a committed view."""
    from dataclasses import replace
    from datetime import timedelta

    from opspilot.tools import TransportResponse
    from opspilot.tools.registry import canonical_hash
    from opspilot.web import DurableEvidenceStore
    from tests.m1_tool_support import WINDOW_START, body, build, request

    executor, transport, _, _ = build(scope_overrides={"run_id": f"run-{uuid4()}"})
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
    )
    record = executor.execute(request(step_id=f"step-{uuid4()}")).evidence
    assert record is not None
    view2 = dict(record.view, observed_at="2026-09-14T01:07:00+00:00")
    stale = replace(
        record,
        observed_at=record.observed_at + timedelta(minutes=2),
        _view=view2,
        view_sha256=canonical_hash(view2),
    )
    evidence = DurableEvidenceStore(DurableStore(DSN))
    evidence.install()
    evidence.register(record)
    evidence.commit(record.evidence_id, record.view)
    assert evidence.register(stale) == record.evidence_id
    kept = evidence.get(record.evidence_id)
    assert kept is not None and kept.committed is True
    assert kept.view_sha256 == record.view_sha256


def test_durable_append_once_admits_a_single_intake_event_under_concurrency():
    """Round 5 thread (P2): concurrent announcers must not duplicate the event."""
    from concurrent.futures import ThreadPoolExecutor

    _, workbench, _ = _build()
    events = workbench.events
    subject = uuid4()
    payload = {"question": "once"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(
            pool.map(
                lambda _: events.append_once(subject, "intake_accepted", payload),
                range(16),
            )
        )
    assert sequences == [1] * 16
    retained = events.read_after(subject, 0)
    assert [e.kind for e in retained] == ["intake_accepted"]
    # Other kinds still append normally; a later append_once returns the old row.
    assert events.append(subject, "run_claimed", {}) == 2
    assert events.append_once(subject, "intake_accepted", payload) == 1
    assert events.latest(subject) == 2


def test_durable_append_once_keys_repeating_kinds_by_payload_identity():
    """Round 6 threads (P2): one ``control_applied`` per generation, one
    ``run_completed`` per run, even under concurrent announcers."""
    from concurrent.futures import ThreadPoolExecutor

    _, workbench, _ = _build()
    events = workbench.events
    subject = uuid4()
    run_a, run_b = str(uuid4()), str(uuid4())

    def announce(generation):
        return events.append_once(
            subject,
            "control_applied",
            {"action": "follow_up", "generation": generation, "text": "x"},
            key={"generation": generation},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        first = list(pool.map(lambda _: announce(1), range(16)))
    assert first == [1] * 16
    # A different generation is a different fact and appends.
    assert announce(2) == 2
    assert announce(1) == 1
    # Same for runs: the reconciled stub reuses the worker's own event.
    assert (
        events.append_once(
            subject,
            "run_completed",
            {"run_id": run_a, "published": True},
            key={"run_id": run_a},
        )
        == 3
    )
    assert (
        events.append_once(
            subject,
            "run_completed",
            {"run_id": run_a, "published": True, "reconciled": True},
            key={"run_id": run_a},
        )
        == 3
    )
    assert (
        events.append_once(
            subject,
            "run_completed",
            {"run_id": run_b, "published": True},
            key={"run_id": run_b},
        )
        == 4
    )
    assert [(e.sequence, e.kind) for e in events.read_after(subject, 0)] == [
        (1, "control_applied"),
        (2, "control_applied"),
        (3, "run_completed"),
        (4, "run_completed"),
    ]
