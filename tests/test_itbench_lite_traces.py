"""#581: trace TSV (ClickHouse OTel schema) -> OTLP/HTTP JSON without losing
spans. Synthetic rows only."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.itbench_lite.snapshot import load_snapshot
from scripts.itbench_lite.traces import (
    TraceStats,
    iter_otlp_requests,
    require_loopback,
)

COLUMNS = [
    "Timestamp", "TraceId", "SpanId", "ParentSpanId", "TraceState", "SpanName",
    "SpanKind", "ServiceName", "ResourceAttributes", "ScopeName", "ScopeVersion",
    "SpanAttributes", "Duration", "StatusCode", "StatusMessage",
    "Events.Timestamp", "Events.Name", "Events.Attributes",
    "Links.TraceId", "Links.SpanId", "Links.TraceState", "Links.Attributes",
]  # fmt: skip
TRACE = "0af7651916cd43dd8448eb211c80319c"


def _span(i: int, **over: str) -> dict[str, str]:
    row = {
        "Timestamp": f"2025-12-15 17:30:0{i}.123456789",
        "TraceId": TRACE,
        "SpanId": f"b7ad6b716920333{i}",
        "ParentSpanId": "" if i == 0 else "b7ad6b7169203330",
        "TraceState": "",
        "SpanName": "oteldemo.CheckoutService/PlaceOrder",
        "SpanKind": "Server" if i % 2 == 0 else "Client",
        "ServiceName": "checkout" if i < 3 else "payment",
        "ResourceAttributes": "{'k8s.pod.name': 'checkout-1'}" if i < 3 else "{}",
        "ScopeName": "go.opentelemetry.io/otel",
        "ScopeVersion": "1.0",
        "SpanAttributes": "{'rpc.method': 'PlaceOrder'}",
        "Duration": "2500000",
        "StatusCode": "Error" if i == 1 else "Unset",
        "StatusMessage": 'failed:\n"quoted"\twith tab' if i == 1 else "",
        "Events.Timestamp": "['2025-12-15 17:30:00.200000000']" if i == 0 else "[]",
        "Events.Name": "['exception']" if i == 0 else "[]",
        "Events.Attributes": "[{'exception.message': 'boom'}]" if i == 0 else "[]",
        "Links.TraceId": f"['{TRACE}']" if i == 2 else "[]",
        "Links.SpanId": "['b7ad6b7169203330']" if i == 2 else "[]",
        "Links.TraceState": "['']" if i == 2 else "[]",
        "Links.Attributes": "[{}]" if i == 2 else "[]",
    }
    row.update(over)
    return row


def _snapshot(tmp_path: Path, rows: list[dict[str, str]]):
    root = tmp_path / "s"
    root.mkdir()
    with (root / "otel_traces_raw.tsv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return load_snapshot(root)


def _spans(bodies):
    return [
        (rs, span)
        for body in bodies
        for rs in body["resourceSpans"]
        for scope in rs["scopeSpans"]
        for span in scope["spans"]
    ]


def test_every_row_becomes_one_span_across_batches(tmp_path):
    snap = _snapshot(tmp_path, [_span(i) for i in range(5)])
    stats = TraceStats()
    bodies = list(iter_otlp_requests(snap, offset_seconds=0, batch_size=2, stats=stats))
    spans = _spans(bodies)
    assert len(bodies) == 3 and stats.requests == 3
    assert stats.rows == stats.spans == len(spans) == 5
    assert sorted(s["spanId"] for _, s in spans) == [
        f"b7ad6b716920333{i}" for i in range(5)
    ]


def test_span_fields_and_offset(tmp_path):
    snap = _snapshot(tmp_path, [_span(i) for i in range(3)])
    spans = {
        s["spanId"]: (rs, s)
        for rs, s in _spans(iter_otlp_requests(snap, offset_seconds=3600))
    }
    rs0, root = spans["b7ad6b7169203330"]
    start = (1765819800 + 3600) * 10**9 + 123456789
    assert root["startTimeUnixNano"] == str(start)
    assert root["endTimeUnixNano"] == str(start + 2500000)
    assert root["kind"] == 2 and "parentSpanId" not in root
    assert root["events"] == [
        {
            "timeUnixNano": str((1765819800 + 3600) * 10**9 + 200000000),
            "name": "exception",
            "attributes": [
                {"key": "exception.message", "value": {"stringValue": "boom"}}
            ],
        }
    ]
    assert {"key": "service.name", "value": {"stringValue": "checkout"}} in rs0[
        "resource"
    ]["attributes"]
    _, err = spans["b7ad6b7169203331"]
    assert err["status"] == {"code": 2, "message": 'failed:\n"quoted"\twith tab'}
    assert err["kind"] == 3 and err["parentSpanId"] == "b7ad6b7169203330"
    _, linked = spans["b7ad6b7169203332"]
    assert linked["links"] == [
        {
            "traceId": TRACE,
            "spanId": "b7ad6b7169203330",
            "traceState": "",
            "attributes": [],
        }
    ]


def test_proto_enum_names_are_accepted(tmp_path):
    snap = _snapshot(
        tmp_path, [_span(0, SpanKind="SPAN_KIND_CONSUMER", StatusCode="STATUS_CODE_OK")]
    )
    ((_, span),) = _spans(iter_otlp_requests(snap, offset_seconds=0))
    assert (span["kind"], span["status"]["code"]) == (5, 1)


@pytest.mark.parametrize(
    "over",
    [
        {"SpanKind": "Sideways"},
        {"Duration": "x"},
        {"SpanAttributes": "{broken"},
        {"Events.Name": "['a', 'b']"},
        {"SpanId": ""},
    ],
)
def test_a_bad_row_fails_instead_of_dropping_a_span(tmp_path, over):
    snap = _snapshot(tmp_path, [_span(0), _span(1, **over)])
    with pytest.raises(ValueError, match="traces line 3"):
        list(iter_otlp_requests(snap, offset_seconds=0))


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:4318/v1/traces", "http://localhost:4318", "http://[::1]:4318"],
)
def test_loopback_endpoints_pass(url):
    require_loopback(url)


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.5:4318", "https://collector.example.com/v1/traces", "http:///x"],
)
def test_other_endpoints_are_refused(url):
    with pytest.raises(ValueError):
        require_loopback(url)
