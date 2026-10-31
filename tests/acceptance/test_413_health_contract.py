"""Independent acceptance tests for the F8 health listener contract (#413)."""

from __future__ import annotations

import importlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict

import pytest


def _health():
    try:
        return importlib.import_module("opspilot.health")
    except ModuleNotFoundError as exc:
        pytest.fail(f"#413 public health module is missing: {exc}")


def _get(server, path: str, method: str = "GET") -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}", method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_health_listener_is_disabled_for_empty_bind_and_supports_ephemeral_port():
    health = _health()

    def readiness():
        return health.Readiness(True, None)

    disabled = health.start_health_server("", readiness=readiness, metrics=lambda: "")
    assert disabled is None
    server = health.start_health_server(
        "127.0.0.1:0", readiness=readiness, metrics=lambda: "# empty\n"
    )
    assert server is not None and server.port > 0
    server.close()


def test_health_listener_has_only_three_get_routes_and_fixed_small_bodies():
    health = _health()
    server = health.start_health_server(
        "127.0.0.1:0",
        readiness=lambda: health.Readiness(True, None),
        metrics=lambda: "opspilot_build_info{version=\"0.1.0\",revision=\"r\"} 1\n",
    )
    assert server is not None
    try:
        status, body, _ = _get(server, "/healthz")
        assert status == 200
        assert set(json.loads(body)) == {"status", "version"}
        assert json.loads(body)["status"] == "ok"
        assert len(body) <= 4096

        status, body, _ = _get(server, "/readyz")
        assert status == 200
        assert set(json.loads(body)) == {"status", "version"}
        assert json.loads(body)["status"] == "ready"
        assert len(body) <= 4096

        status, body, _ = _get(server, "/metrics")
        assert status == 200
        assert b"opspilot_build_info" in body
        assert len(body) <= 4096

        for path in ("/", "/health", "/healthz/", "/report"):
            assert _get(server, path)[0] == 404
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD"):
            for path in ("/healthz", "/readyz", "/metrics"):
                assert _get(server, path, method=method)[0] == 404
    finally:
        server.close()


def test_health_status_bodies_do_not_echo_operational_or_secret_text():
    health = _health()
    server = health.start_health_server(
        "127.0.0.1:0",
        readiness=lambda: health.Readiness(False, "POOL_UNAVAILABLE"),
        metrics=lambda: "opspilot_schema_at_head 0\n",
    )
    assert server is not None
    try:
        for path in ("/healthz", "/readyz"):
            status, body, _ = _get(server, path)
            assert status in (200, 503)
            assert not any(word.encode() in body for word in ("report", "evidence", "provider", "credential", "sk-"))
        status, body, _ = _get(server, "/metrics")
        assert status == 200
        assert len(body) <= 4096
    finally:
        server.close()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"last_success_monotonic": None, "poll_interval_seconds": 10, "now_monotonic": 0, "stopping": False}, "POLL_STALE"),
        ({"last_success_monotonic": 1, "poll_interval_seconds": 10, "now_monotonic": 31.001, "stopping": False}, "POLL_STALE"),
        ({"last_success_monotonic": None, "poll_interval_seconds": 10, "now_monotonic": 0, "stopping": True}, "STOPPING"),
        ({"last_success_monotonic": 1, "poll_interval_seconds": 10, "now_monotonic": 2, "stopping": True}, "STOPPING"),
    ],
)
def test_poll_readiness_reason_codes_and_stopping_precedence(kwargs, expected):
    result = _health().poll_readiness(**kwargs)
    assert not result.ready and result.reason == expected


def test_poll_readiness_is_ready_after_recent_success_and_web_reasons_are_503():
    health = _health()
    assert asdict(health.poll_readiness(last_success_monotonic=10, poll_interval_seconds=10, now_monotonic=40, stopping=False)) == {"ready": True, "reason": None}
    schema = health.web_readiness(schema_at_head=False, pool_ok=True)
    pool = health.web_readiness(schema_at_head=True, pool_ok=False)
    assert (schema.ready, schema.reason) == (False, "SCHEMA_NOT_AT_HEAD")
    assert (pool.ready, pool.reason) == (False, "POOL_UNAVAILABLE")
    assert health.web_readiness(schema_at_head=False, pool_ok=False).reason == "POOL_UNAVAILABLE"


@pytest.mark.parametrize("reason", ["SCHEMA_NOT_AT_HEAD", "POOL_UNAVAILABLE", "POLL_STALE", "STOPPING"])
def test_readyz_returns_503_with_fixed_reason(reason):
    health = _health()
    server = health.start_health_server(
        "127.0.0.1:0",
        readiness=lambda: health.Readiness(False, reason),
        metrics=lambda: "",
    )
    assert server is not None
    try:
        status, body, _ = _get(server, "/readyz")
        assert status == 503
        assert json.loads(body) == {"status": "not_ready", "reason": reason}
    finally:
        server.close()


_METRIC_NAMES = {
    "opspilot_build_info",
    "opspilot_schema_at_head",
    "opspilot_queue_depth",
    "opspilot_observation_due",
    "opspilot_last_poll_age_seconds",
    "opspilot_in_flight_attempts",
    "opspilot_model_calls_total",
    "opspilot_model_consecutive_failures",
    "opspilot_last_model_call_category",
}


def test_metrics_use_only_f8_names_and_bounded_labels():
    health = _health()
    text = """# HELP opspilot_build_info Build information.
# TYPE opspilot_build_info gauge
opspilot_build_info{version="0.1.0",revision="r"} 1
opspilot_schema_at_head 1
opspilot_queue_depth{state="queued"} 1
opspilot_queue_depth{state="running_lapsed"} 2
opspilot_observation_due 3
opspilot_last_poll_age_seconds 4
opspilot_in_flight_attempts 1
opspilot_model_calls_total{category="ok"} 2
opspilot_model_calls_total{category="MODEL_UNAVAILABLE"} 1
opspilot_model_consecutive_failures 1
opspilot_last_model_call_category{category="ok"} 1
"""
    server = health.start_health_server(
        "127.0.0.1:0",
        readiness=lambda: health.Readiness(True, None),
        metrics=lambda: text,
    )
    assert server is not None
    try:
        status, body, _ = _get(server, "/metrics")
        assert status == 200
        text = body.decode()
    finally:
        server.close()
    names = set(re.findall(r"^(opspilot_[a-z0-9_]+)(?:\{|\s)", text, re.MULTILINE))
    assert names == _METRIC_NAMES
    assert "run_id" not in text and "incident_id" not in text
    assert "state=\"queued\"" in text and "state=\"running_lapsed\"" in text
    assert "category=\"ok\"" in text
    labels_by_name = {}
    for line in text.splitlines():
        match = re.match(r"^(opspilot_[a-z0-9_]+)(?:\{([^}]*)\})?\s", line)
        if not match:
            continue
        labels_by_name.setdefault(match.group(1), set()).update(
            key for key in re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)=", match.group(2) or "")
        )
    assert labels_by_name["opspilot_build_info"] == {"version", "revision"}
    assert labels_by_name["opspilot_queue_depth"] == {"state"}
    assert labels_by_name["opspilot_model_calls_total"] == {"category"}
    assert labels_by_name["opspilot_last_model_call_category"] == {"category"}
    for name in _METRIC_NAMES - {
        "opspilot_build_info",
        "opspilot_queue_depth",
        "opspilot_model_calls_total",
        "opspilot_last_model_call_category",
    }:
        assert labels_by_name.get(name, set()) == set()

