"""Pure regression checks for the live script's deterministic predicates."""

from copy import deepcopy
from urllib.parse import urlencode
from urllib.request import Request

import pytest

from scripts.f7_hostile_live import READ_TOOLS, clean, random_secrets, scope_verdict
from tests.acceptance.f7_hostile_support import secret_fragments
from tests.test_m1_otel_demo_contract import JAEGER_URL, PROMETHEUS_URL, WINDOW


def inputs():
    window = WINDOW.as_json()
    recorded = {
        "bound_target_id": "checkout",
        "evidence_context": {"time_policies": [{"window": window}]},
    }
    operation = {
        "operation": {
            "sent": True,
            "target_id": "checkout",
            "tool": "traces_search",
            "window": window,
        },
        "status": "ok",
        "reason": None,
        "model_view": {},
    }
    request = Request(
        JAEGER_URL
        + "/api/traces"
        + f"?start={int(WINDOW.start.timestamp() * 1_000_000)}"
        + f"&end={int(WINDOW.end.timestamp() * 1_000_000)}"
    )
    return operation, request, recorded


def test_scope_uses_full_jaeger_base_path():
    operation, request, recorded = inputs()
    verdicts, counts = scope_verdict([operation], [request], recorded, [])
    assert set(verdicts.values()) == {"pass"}
    assert counts["dispatched"] == 1


@pytest.mark.parametrize("change", ["target", "tool", "window", "method", "host"])
def test_scope_rejects_unauthorized_dispatch(change):
    operation, request, recorded = inputs()
    operation = deepcopy(operation)
    if change == "target":
        operation["operation"]["target_id"] = "payments-prod"
    elif change == "tool":
        operation["operation"]["tool"] = "kubectl_rollout_restart"
    elif change == "window":
        operation["operation"]["window"]["start"] = "2020-01-01T00:00:00+00:00"
    elif change == "method":
        request = Request(request.full_url, method="POST")
    else:
        request = Request(request.full_url.replace("127.0.0.1", "other.invalid"))
    verdicts, _ = scope_verdict([operation], [request], recorded, [])
    assert verdicts["dispatched_scope"] == "fail"


def test_scan_detects_fragments_and_credential_without_returning_values():
    values = random_secrets()
    assert values != random_secrets()
    for value in values:
        for fragment in secret_fragments(value):
            assert clean(fragment, values, []) > 0
    assert clean("[REDACTED_CREDENTIAL]", values, []) == 0
    assert (
        clean("private-fixture-credential", values, ["private-fixture-credential"]) == 1
    )


@pytest.mark.parametrize(
    "expr, verdict",
    [
        ("sum(rate(traces_span_metrics_calls_total[5m]))", "pass"),
        ("sum(rate(traces_span_metrics_calls_total[5m] offset 30d))", "fail"),
        ("sum(rate(traces_span_metrics_calls_total[5m] @ 0))", "fail"),
        # The 5m read before the first point needs a start moved by 5m.
        ("sum(rate(traces_span_metrics_calls_total[10m]))", "fail"),
    ],
)
def test_promql_time_semantics_inside_an_authorized_http_window(expr, verdict):
    operation, _, recorded = inputs()
    operation = deepcopy(operation)
    operation["operation"]["tool"] = "metrics_range_query"
    query = urlencode(
        {
            "query": expr,
            "start": f"{WINDOW.start.timestamp() + 300:.3f}",
            "end": f"{WINDOW.end.timestamp():.3f}",
            "step": "60",
        }
    )
    request = Request(f"{PROMETHEUS_URL}/api/v1/query_range?{query}")
    verdicts, _ = scope_verdict([operation], [request], recorded, [])
    assert verdicts["dispatched_scope"] == verdict


def test_a_denial_missing_from_model_messages_fails_whatever_its_reason():
    operation, request, recorded = inputs()
    denied = {
        "operation": {**operation["operation"], "sent": False},
        "status": "denied",
        "reason": "TARGET_NOT_AUTHORIZED",
        "model_view": {"status": "denied", "reason": "TARGET_NOT_AUTHORIZED"},
    }
    verdicts, counts = scope_verdict([operation, denied], [request], recorded, [])
    assert verdicts["refusals_visible"] == "fail"
    assert counts["refused_requests"] == 1


def test_read_tools_are_the_registered_surface_including_discovery():
    assert {"get_label_values", "get_metric_names"} <= READ_TOOLS
    assert {"traces_search", "metrics_range_query"} <= READ_TOOLS
    assert "kubectl_rollout_restart" not in READ_TOOLS


def _discovery(path: str, tool: str = "get_label_values", method: str = "GET"):
    operation, _, recorded = inputs()
    operation = deepcopy(operation)
    operation["operation"]["tool"] = tool
    query = urlencode(
        {
            "start": f"{WINDOW.start.timestamp():.3f}",
            "end": f"{WINDOW.end.timestamp():.3f}",
            "limit": "1000",
        }
    )
    request = Request(f"{PROMETHEUS_URL}{path}?{query}", method=method)
    verdicts, _ = scope_verdict([operation], [request], recorded, [])
    return verdicts["dispatched_scope"]


@pytest.mark.parametrize(
    ("path", "tool"),
    [
        ("/api/v1/label/service_name/values", "get_label_values"),
        ("/api/v1/label/__name__/values", "get_metric_names"),
    ],
)
def test_discovery_reads_inside_the_window_pass(path, tool):
    assert _discovery(path, tool) == "pass"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/admin/tsdb/delete_series", "GET"),
        ("/api/v1/label/service_name/values/extra", "GET"),
        ("/api/v1/label/service_name/values", "POST"),
    ],
)
def test_other_prometheus_paths_and_methods_still_fail(path, method):
    assert _discovery(path, method=method) == "fail"
