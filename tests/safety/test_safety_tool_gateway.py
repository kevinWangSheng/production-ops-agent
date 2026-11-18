"""Table-driven read-only boundaries of the tool gateway, over every tool.

PRODUCT-CONSTRAINTS "Explicit exclusions" (no target mutation, no arbitrary
command), "Runtime and human control requirements" (exact target resolution,
query scope outside the model's authority) and the data-flow contract
(credentials never reach model inputs or stored evidence); C3 section 8.

Each check enumerates the shipped ``otel-demo-ext`` registry, the widest
profile, so a newly registered tool fails ``test_every_registered_tool_is_
classified`` until it is added to the tables below and therefore to every
boundary. Every refusal is asserted as "no request left the gateway".
"""

from __future__ import annotations

import json

import pytest

from opspilot.tools import (
    FORBIDDEN_VERBS,
    MAX_REQUEST_TIMEOUT_SECONDS,
    MAX_RESULT_BYTES,
    READ_ONLY_VERBS,
    ToolContractError,
    elasticsearch_logs,
    fixture,
    otel_demo,
)
from opspilot.tools.otel_demo_ext import _registrations as ext_registrations
from tests.m1_tool_support import build, registration
from tests.safety.gateway_support import (
    ES_TOKEN,
    K8S_TOKEN,
    OTEL_TOKEN,
    OUTSIDE,
    everything_seen,
    gateway,
)

#: A valid call for every tool that reads an external source.
SOURCE_CALLS: dict[str, dict[str, object]] = {
    "metrics_range_query": {"expr": "rate(http_server_duration_count[5m])"},
    "execute_prometheus_instant_query": {"query": "up"},
    "get_metric_names": {"match": "up"},
    "get_label_values": {"label": "job"},
    "get_series": {"match": "up"},
    "get_all_labels": {},
    "get_metric_metadata": {},
    "list_prometheus_rules": {},
    "get_prometheus_target": {"target_name": "checkout"},
    "traces_search": {"service": "checkout"},
    "traces_search_by_tags": {"service": "checkout"},
    "traces_query_trace_by_id": {"trace_id": "0123456789abcdef0123456789abcdef"},
    "traces_list_services": {},
    "traces_list_operations": {"service": "checkout"},
    "kubernetes_jq_query": {"kind": "pods", "jq_expr": ".items[] | .metadata.name"},
    "kubernetes_count": {"kind": "pods", "jq_expr": ".items[]"},
    "kubernetes_tabular_query": {"kind": "pods", "columns": "NAME:.metadata.name"},
    "fetch_pod_logs": {"pod_name": "checkout-0", "namespace": "otel-demo"},
    "elasticsearch_search": {"index": "logs-checkout"},
    "elasticsearch_mappings": {"index": "logs-checkout"},
    "elasticsearch_list_indices": {},
}
#: Reads only OpsPilot's own stored evidence for the same Run.
INTERNAL_TOOLS = {"read_evidence"}
#: Tools that take a Kubernetes resource kind.
KIND_TOOLS = ("kubernetes_jq_query", "kubernetes_count", "kubernetes_tabular_query")
#: Tools whose PromQL parameter can move evaluation outside the window.
PROMQL_PARAMS = {
    "metrics_range_query": "expr",
    "execute_prometheus_instant_query": "query",
}

#: Credential shapes a source may return. Each canary must never reach the
#: model view or the stored evidence; the redaction rules recognise them by
#: their name context (PRODUCT-CONSTRAINTS: a bare secret is out of scope).
CANARIES = {
    "userinfo": "postgres://admin:canary-userinfo-31@db.internal:5432/app",
    "assignment": "password=canary-assign-32",
    # assembled so the source line itself is not a credential-shaped literal
    "quoted": '"'.join(["", "api_key", ": ", "canary-quoted-33", ""]),
    "bearer": "Authorization: Bearer canary-bearer-34",
    "scheme": "retried upstream with Bearer canary-scheme-36 after 401",
    # a log line that is itself JSON encoded into a string, once and twice
    # (#710): the key's and value's quotes are backslash-escaped
    "escaped": json.dumps(json.dumps({"password": "canary-escaped-37"})),
    "escaped_twice": json.dumps(
        json.dumps(json.dumps({"msg": "x", "client_secret": "canary-escaped-38"}))
    ),
}
SECRET_VALUES = (
    "canary-userinfo-31",
    "canary-assign-32",
    "canary-quoted-33",
    "canary-bearer-34",
    "canary-scheme-36",
    "canary-escaped-37",
    "canary-escaped-38",
    # The gateway's own configured credentials (PRODUCT-CONSTRAINTS data flow:
    # credentials remain outside model inputs and traces).
    K8S_TOKEN,
    OTEL_TOKEN,
    ES_TOKEN,
)


def _es_registrations():
    return elasticsearch_logs.elasticsearch_registrations(
        max_window_seconds=otel_demo.OBSERVATION_SECONDS
    )


def _registered():
    return {item.name: item for item in (*ext_registrations(), *_es_registrations())}


def _shipped_registrations():
    return [
        fixture._registration(),
        *otel_demo._registrations(),
        *ext_registrations(),
        *_es_registrations(),
    ]


# -- registry: every tool is a bounded read ------------------------------------


def test_every_registered_tool_is_classified(monkeypatch, tmp_path):
    assert set(_registered()) == set(SOURCE_CALLS) | INTERNAL_TOOLS
    # The worker's real scope over the widest configuration lists the same.
    scope = gateway(monkeypatch, tmp_path).executor.scope
    assert set(scope.tool_names) == set(SOURCE_CALLS) | INTERNAL_TOOLS
    for tool in KIND_TOOLS:
        assert "kind" in _registered()[tool].parameters
    kind_taking = {n for n, r in _registered().items() if "kind" in r.parameters}
    assert kind_taking == set(KIND_TOOLS)


@pytest.mark.parametrize(
    "item", _shipped_registrations(), ids=lambda item: f"{item.source}:{item.name}"
)
def test_every_shipped_tool_is_a_bounded_read(item):
    assert item.read_only is True
    assert item.verb in READ_ONLY_VERBS
    assert 0 < item.request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS
    assert 0 < item.max_result_bytes <= MAX_RESULT_BYTES


@pytest.mark.parametrize("verb", sorted(FORBIDDEN_VERBS | {"approve", "deploy", "put"}))
def test_the_registry_refuses_any_write_verb(verb):
    with pytest.raises(ToolContractError):
        registration(verb=verb)


def test_the_registry_refuses_a_registration_that_is_not_read_only():
    with pytest.raises(ToolContractError, match="WRITE_CAPABILITY_FORBIDDEN"):
        registration(read_only=False)


def test_the_executor_offers_only_a_read_entry_point():
    executor, _, _, _ = build()
    public = {name for name in dir(executor) if not name.startswith("_")}
    assert public == {"execute", "operations_used", "scope", "tool_seconds_used"}


# -- scope: target, window, resource kind, PromQL --------------------------------


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_a_valid_call_reaches_its_source(monkeypatch, tmp_path, tool):
    """The control for every refusal below: the same call, in scope, is sent."""
    gw = gateway(monkeypatch, tmp_path)
    gw.call(tool, SOURCE_CALLS[tool])
    assert gw.source.requests, f"{tool} never contacted its source"


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_an_unregistered_target_is_refused_before_any_request(
    monkeypatch, tmp_path, tool
):
    gw = gateway(monkeypatch, tmp_path)
    outcome = gw.call(tool, SOURCE_CALLS[tool], target="payments-prod")
    assert outcome.status in {"denied", "error"}
    assert gw.source.requests == []


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_a_window_outside_the_authorization_is_refused_before_any_request(
    monkeypatch, tmp_path, tool
):
    gw = gateway(monkeypatch, tmp_path)
    outcome = gw.call(tool, SOURCE_CALLS[tool], window=OUTSIDE)
    assert outcome.status in {"denied", "error"}
    assert gw.source.requests == []


@pytest.mark.parametrize("kind", ["secrets", "Secret", "SECRETS", "secrets.v1"])
@pytest.mark.parametrize("tool", KIND_TOOLS)
def test_kubernetes_secrets_are_never_read(monkeypatch, tmp_path, tool, kind):
    gw = gateway(monkeypatch, tmp_path)
    outcome = gw.call(tool, {**SOURCE_CALLS[tool], "kind": kind})
    assert (outcome.status, outcome.reason) == ("error", "RESOURCE_NOT_ALLOWED")
    assert gw.source.requests == []


ESCAPES = [
    "up offset 1h",
    "up OFFSET 30m",
    "up @ 1700000000",
    "rate(up[48h])",
    "max_over_time(rate(up[5m])[30h:1m])",
    # each range fits the 59-minute window, their nested sum does not
    "max_over_time(rate(up[40m])[40m:1m])",
]


@pytest.mark.parametrize("expr", ESCAPES)
@pytest.mark.parametrize("tool", sorted(PROMQL_PARAMS))
def test_promql_cannot_move_evaluation_outside_the_window(
    monkeypatch, tmp_path, tool, expr
):
    gw = gateway(monkeypatch, tmp_path)
    params = {**SOURCE_CALLS[tool], PROMQL_PARAMS[tool]: expr}
    outcome = gw.call(tool, params)
    assert outcome.status in {"denied", "error"}
    assert gw.source.requests == []


# -- human control outranks an in-flight read --------------------------------------


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_a_suspension_refuses_the_read_before_any_request(monkeypatch, tmp_path, tool):
    gw = gateway(monkeypatch, tmp_path)
    gw.control.suspended = True
    outcome = gw.call(tool, SOURCE_CALLS[tool])
    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert gw.source.requests == []


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_a_human_decision_during_the_read_makes_its_result_history_only(
    monkeypatch, tmp_path, tool
):
    """C3 section 4: a pause invalidates in-flight results; they stay history."""
    holder: dict[str, object] = {}

    def decide_while_reading(parsed):
        holder["gw"].control.generation_bump = 1  # a human decision lands
        return b"{}"

    gw = gateway(monkeypatch, tmp_path, body=decide_while_reading)
    holder["gw"] = gw
    outcome = gw.call(tool, SOURCE_CALLS[tool])
    assert gw.source.requests
    assert (outcome.status, outcome.reason) == ("denied", "CONTROL_GENERATION_CHANGED")
    assert outcome.adopted is False
    assert all(not getattr(r, "adopted", False) for r in gw.evidence.records)


# -- credentials in source content -------------------------------------------------


def _canary_body(parsed) -> bytes:
    text = "\n".join(CANARIES.values())
    if parsed.path.endswith("/log"):
        return text.encode()
    return json.dumps(
        {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
            "items": [{"metadata": {"name": "checkout-0", "annotations": CANARIES}}],
            "message": text,
            "errors": [{"msg": text}],
        }
    ).encode()


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_credentials_in_source_content_never_reach_the_model_or_evidence(
    monkeypatch, tmp_path, tool
):
    gw = gateway(monkeypatch, tmp_path, body=_canary_body)
    outcome = gw.call(tool, SOURCE_CALLS[tool])
    assert gw.source.requests, "the call must reach the source to test the reply"
    seen = everything_seen(outcome, gw.evidence)
    for value in SECRET_VALUES:
        assert value not in seen, (tool, value)


@pytest.mark.parametrize("tool", sorted(SOURCE_CALLS))
def test_a_source_error_echoing_credentials_is_redacted(monkeypatch, tmp_path, tool):
    """A failing source's error text is shown to the model (#311): redacted."""
    text = "\n".join(CANARIES.values())
    # Prometheus' error envelope, which the gateway echoes to the model (#311)
    envelope = {"status": "error", "errorType": "bad_data", "error": text}
    gw = gateway(
        monkeypatch, tmp_path, body=lambda parsed: (400, json.dumps(envelope).encode())
    )
    outcome = gw.call(tool, SOURCE_CALLS[tool])
    assert gw.source.requests
    assert outcome.status == "error"
    seen = everything_seen(outcome, gw.evidence)
    for value in SECRET_VALUES:
        assert value not in seen, (tool, value)
    echoed = (outcome.model_view or {}).get("source_error") or {}
    if tool in PROMQL_PARAMS:  # the echo exists, so the check is not vacuous
        assert echoed.get("error"), outcome.model_view


def test_a_credential_across_the_span_detail_cut_is_redacted_first(
    monkeypatch, tmp_path
):
    """Span detail is cut to 600 bytes per field; redaction runs before the
    cut, so a URL credential straddling it never leaks a prefix (#311 rule)."""
    from opspilot.tools.otel_demo import MAX_DETAIL_BYTES_PER_FIELD
    from tests.acceptance.f7_hostile_support import jaeger_payload
    from tests.safety.gateway_support import WINDOW

    planted = "sk-straddle-" + "a" * 12  # low entropy, assembled for the scanner
    value = "x" * (MAX_DETAIL_BYTES_PER_FIELD - 30) + (
        f" dsn postgres://admin:{planted}@db.internal:5432/app"
    )
    base_us = int(WINDOW.start.timestamp() * 1_000_000) + 10_000_000
    body = jaeger_payload({"key": "exception.message", "value": value}, base_us=base_us)
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call("traces_search", SOURCE_CALLS["traces_search"])
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, gw.evidence)
    assert "x" * 100 in seen, "the detail field was not projected"
    assert planted[:8] not in seen
