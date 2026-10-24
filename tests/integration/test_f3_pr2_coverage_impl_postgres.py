"""F3 PR-2 implementer test on PostgreSQL: the workbench -> worker path.

Under the ``otel-demo`` profile with Jaeger refusing connections, the page's
``#coverage-gaps`` and the acceptance projection agree that jaeger failed;
a Run whose last request is forced by its two-request bound is marked
``#collection-truncated``; and neither reading reaches the model (R-14).
"""

from __future__ import annotations

import json
import os
import urllib.error
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.acceptance_investigation import investigation_evidence
from opspilot.tools.otel_demo import METRICS_TOOL, TARGET_ID, TRACES_TOOL
from opspilot.tools.profiles import PROFILE_ENV, select_profile
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
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_otel_demo_contract_postgres import (
    QUESTION,
    _install_opener,
    _store,
    _worker,
)
from tests.m1_investigation_support import reply, report_json, tool_call
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
)
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,
    GOOD_EXPR,
    JAEGER_URL,
    PROMETHEUS_URL,
    WINDOW_END,
    FakeOpener,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

MODEL_VISIBLE_MARKERS = ("coverage-gaps", "Coverage gaps", "truncated at the model")


def _build(profile, budget_limit):
    store = _store()
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
        run_versions=profile.versions(),
        run_seconds=600,
        budget_limit=budget_limit,
        tool_face=profile.face(FakeClock(start=WINDOW_END)),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={},
        auth_revision="auth-rev-f3-pr2",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, store, events, evidence


def _run(monkeypatch, opener, replies, *, budget_limit):
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    monkeypatch.setenv("OPSPILOT_OTEL_PROMETHEUS_URL", PROMETHEUS_URL)
    monkeypatch.setenv("OPSPILOT_OTEL_JAEGER_URL", JAEGER_URL)
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    _install_opener(monkeypatch, opener)
    app, store, events, evidence = _build(profile, budget_limit)
    submitted = post_form(
        app,
        "/intake/ui",
        {
            "target_id": TARGET_ID,
            "question": QUESTION,
            "idempotency_key": f"f3-pr2-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert submitted.status == 201, submitted.text
    subject = UUID(submitted.json()["incident_id"])
    loop, model = _worker(profile, store, events, evidence, replies, subject=subject)
    loop.poll_once()
    page = call(app, "GET", f"/incidents/{subject}", headers=basic())
    assert page.status == 200, page.text
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        projected = investigation_evidence(conn, subject)
        conn.rollback()
    for request in model.calls:
        sent = json.dumps(list(request.messages))
        assert not any(marker in sent for marker in MODEL_VISIBLE_MARKERS)
    return page.text, projected, model


def _metrics_evidence_report(call_):
    evidence_id = None
    for message in reversed(call_.messages):
        if message.get("role") == "tool":
            view = json.loads(message["content"])
            if view.get("status") == "ok":
                evidence_id = view["evidence_id"]
                break
    return reply(
        content=report_json(
            evidence_id=evidence_id or "missing",
            target_ref=TARGET_ID,
            conclusion="partial",
        )
    )


def test_refused_jaeger_is_a_failed_source_on_page_and_projection(monkeypatch):
    refused = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
    opener = FakeOpener(
        by_query={GOOD_EXPR: CHECKOUT_BODY}, routes={"/api/traces": refused}
    )
    tools = reply(
        tool_calls=[
            tool_call(
                call_id="t1", name=TRACES_TOOL, arguments='{"service":"checkout"}'
            ),
            tool_call(
                call_id="t2", name=TRACES_TOOL, arguments='{"service":"payment"}'
            ),
            tool_call(
                call_id="m1",
                name=METRICS_TOOL,
                arguments=json.dumps({"expr": GOOD_EXPR}),
            ),
        ],
        finish="tool_calls",
    )
    replies = [tools, _metrics_evidence_report]
    html, projected, _ = _run(monkeypatch, opener, replies, budget_limit=6)

    (coverage,) = projected.coverage
    rows = {row.name: row for row in coverage.sources}
    assert (rows["jaeger"].non_ok, rows["jaeger"].state, rows["jaeger"].gap) == (
        2,
        "failed",
        True,
    )
    assert dict(rows["jaeger"].reasons) == {"SOURCE_UNAVAILABLE": 2}
    assert (rows["prometheus"].ok, rows["prometheus"].state) == (1, "all_ok")
    assert 'id="coverage-gaps"' in html
    assert 'data-source="jaeger" data-state="failed"' in html
    assert 'data-source="prometheus" data-state="all_ok"' in html
    assert 'id="not-integrated"' in html
    assert "SOURCE_UNAVAILABLE</code> ×2" in html
    (report,) = projected.reports
    assert report.collection_truncated is False
    assert 'id="collection-truncated"' not in html


def test_a_report_forced_by_the_bound_is_marked_truncated(monkeypatch):
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    tools = reply(
        tool_calls=[
            tool_call(
                call_id="m1",
                name=METRICS_TOOL,
                arguments=json.dumps({"expr": GOOD_EXPR}),
            )
        ],
        finish="tool_calls",
    )
    html, projected, model = _run(
        monkeypatch, opener, [tools, _metrics_evidence_report], budget_limit=2
    )
    assert len(model.calls) == 2
    assert model.calls[1].tools is None  # the forced final round
    (report,) = projected.reports
    assert report.collection_truncated is True
    assert 'id="report-status"' in html
    assert 'id="collection-truncated"' in html
    (coverage,) = projected.coverage
    rows = {row.name: row.state for row in coverage.sources}
    assert rows["jaeger"] == "not_queried"
    # #341 F1: all_ok says nothing about data having come back.
    assert "returned data" not in html
    assert "without an error or refusal (no-data counts shown separately)" in html
