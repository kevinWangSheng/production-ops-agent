"""#311 implementer test on PostgreSQL: review F1.

A Prometheus 400 whose envelope decodes to a lone surrogate used to leave the
view unhashable, so the tool result never committed and the Run went to
waiting_human after one model request. Through the workbench -> worker path
of the ``otel-demo`` profile, the refusal now commits with
``source_error.error`` null and reaches the model's second request.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from uuid import UUID, uuid4

import pytest

from opspilot.tools.otel_demo import METRICS_TOOL, TARGET_ID
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.integration.test_m1_otel_demo_contract_postgres import (
    JAEGER_URL,
    QUESTION,
    _build,
    _install_opener,
    _worker,
)
from tests.m1_investigation_support import reply, report_json, tool_call
from tests.m1_web_support import basic, post_form, same_origin
from tests.test_m1_otel_demo_contract import PROMETHEUS_URL, FakeOpener

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

SURROGATE_400 = (
    b'{"status":"error","errorType":"bad_data","error":"parse \\ud800 error"}'
)
EXPR = 'sum(rate(traces_span_metrics_calls_total{case="surrogate"}[1m])'


def test_a_lone_surrogate_400_commits_as_invalid_params(monkeypatch):
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    monkeypatch.setenv("OPSPILOT_OTEL_PROMETHEUS_URL", PROMETHEUS_URL)
    monkeypatch.setenv("OPSPILOT_OTEL_JAEGER_URL", JAEGER_URL)
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    error = urllib.error.HTTPError(
        PROMETHEUS_URL, 400, "error", {}, io.BytesIO(SURROGATE_400)
    )
    opener = FakeOpener(by_query={EXPR: error})
    _install_opener(monkeypatch, opener)

    app, _workbench, store, events, evidence = _build(profile)
    submitted = post_form(
        app,
        "/intake/ui",
        {
            "target_id": TARGET_ID,
            "question": QUESTION,
            "idempotency_key": f"otel-311-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert submitted.status == 201, submitted.text
    subject = UUID(submitted.json()["incident_id"])

    tool_round = reply(
        tool_calls=[
            tool_call(
                call_id="call-1",
                name=METRICS_TOOL,
                arguments=json.dumps({"expr": EXPR}),
            )
        ],
        finish="tool_calls",
    )
    final = reply(content=report_json(evidence_id="none", target_ref=TARGET_ID))
    loop, model = _worker(
        profile, store, events, evidence, [tool_round, final], subject=subject
    )
    loop.poll_once()

    assert len(opener.requests) == 1
    # The refusal reached the model: the second request carries it (the
    # scripted report may be retried afterwards; that is not under test).
    assert len(model.calls) >= 2
    (tool_message,) = [m for m in model.calls[1].messages if m.get("role") == "tool"]
    view = json.loads(tool_message["content"])
    assert view["reason"] == "INVALID_PARAMS"
    assert view["source_error"]["error"] is None
    assert view["source_error"]["http_status"] == 400
    rebuilt = store.rebuild(subject)
    assert not rebuilt["pending_tools"]
    committed = [
        item["result"]
        for step in rebuilt["steps"]
        for item in step.get("tool_results") or []
    ]
    assert [r["reason"] for r in committed] == ["INVALID_PARAMS"]
    # How the Run ends depends on the scripted report (it cites no evidence),
    # not on this tool result, so its final state is not asserted.
