"""#859: the OTLP normalization over bodies recorded from a real Jaeger 2.23
(synthetic spans, ``tests/fixtures/jaeger_v3/``; how they were made is in
``docs/evidence/859-jaeger-v3-local/``)."""

from __future__ import annotations

import json
from pathlib import Path

from opspilot.tools.jaeger_v3 import traces_data, v3_error_text

FIXTURES = Path(__file__).parent / "fixtures" / "jaeger_v3"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_a_recorded_trace_maps_to_the_v1_shape():
    payload = traces_data(_load("trace_by_id.json"))
    assert payload is not None
    (trace,) = payload["data"]
    assert trace["traceID"] == TRACE
    spans = {span["spanID"]: span for span in trace["spans"]}
    root, call = spans["00f067aa0ba902b7"], spans["b7ad6b7169203331"]
    services = {pid: p["serviceName"] for pid, p in trace["processes"].items()}
    assert services[root["processID"]] == "frontend"
    assert services[call["processID"]] == "cart"
    assert trace["processes"][root["processID"]]["tags"] == [
        {"key": "service.version", "value": "1.2.3"}
    ]
    assert root["references"] == []
    assert call["references"] == [
        {"refType": "CHILD_OF", "traceID": TRACE, "spanID": "00f067aa0ba902b7"}
    ]
    assert (root["duration"], call["duration"]) == (120_000, 100_000)
    root_tags = {t["key"]: t["value"] for t in root["tags"]}
    assert root_tags["http.status_code"] == 500  # intValue arrives as a string
    tags = {t["key"]: t["value"] for t in call["tags"]}
    assert tags["otel.status_code"] == "ERROR" and tags["error"] is True
    assert tags["otel.status_description"] == "redis timeout"
    assert tags["rpc.grpc.status_code"] == 14
    (log,) = call["logs"]
    fields = {f["key"]: f["value"] for f in log["fields"]}
    assert fields["event"] == "exception"
    assert fields["exception.type"] == "TimeoutError"
    assert log["timestamp"] == root["startTime"] + 50_000


def test_a_recorded_search_is_regrouped_by_trace_id():
    payload = traces_data(_load("search_frontend.json"))
    assert payload is not None
    counts = {t["traceID"]: len(t["spans"]) for t in payload["data"]}
    assert counts == {"00000000000000000000000000abc123": 1, TRACE: 3}


def test_recorded_error_bodies():
    raw = (FIXTURES / "bad_request.json").read_bytes()
    assert (v3_error_text(raw) or "").startswith(
        "malformed parameter query.startTimeMin"
    )
    assert (
        v3_error_text((FIXTURES / "not_found.json").read_bytes()) == "No traces found"
    )
    assert traces_data({"error": {"httpCode": 404}}) is None
