"""#859: the ``otel-demo-ext`` trace tools against a Jaeger that only serves
the v3 query API (Jaeger >= 2.21 removed ``/api/services``,
``/api/operations`` and the ``/api/traces`` search).

Independent contract tests, written from the #859 pre-review decision table
(D1-D11, all at their recommended defaults) and the public tool interface,
without the implementation. They drive the shipped profile through its
public factory with a faked Jaeger (``tests/jaeger_v3_support.py``) and
assert what the tool call returns -- outcome status, model view, the
projected record kept as evidence -- and what was asked of the source, not
how the adapter is built.

1. v1 Jaeger: every trace tool's outcome is byte-identical to ``main``
   318dc016 (pinned below) and no v3 request is sent (D1, D2, D9).
2. v3-only Jaeger: every trace tool maps the OTLP answer onto the existing
   projection (D3, D5, D7, D9), the probe is not repeated per call and a
   failed probe is not cached (D1), credentials per tool stay as they were
   (D4).
3. v3 search 404 "No traces found" is no_data, not an error; a trace ID 404
   is the existing not-found branch (D8).

The credential case for the new input channel is in
``tests/safety/test_safety_jaeger_v3_redaction.py``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from typing import Any

import pytest

from tests.jaeger_v3_support import (
    EXCEPTION_MESSAGE,
    EXCEPTION_TYPE,
    SPAN_A1,
    SPAN_A2,
    SPAN_B1,
    STATUS_MESSAGE,
    T0_US,
    TRACE_A,
    TRACE_B,
    UNKNOWN_TRACE,
    WINDOW,
    JaegerDouble,
    ext_gateway,
    parse_rfc3339,
    record_of,
)
from tests.safety.gateway_support import OTEL_TOKEN

#: One call per trace tool and branch; the same list in v1 and v3 mode.
CALLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("search", "traces_search", {"service": "checkout"}),
    (
        "by_tags",
        "traces_search_by_tags",
        {
            "service": "payment",
            "tags": 'rpc.system="grpc"',
            "operation": "Charge",
            "min_duration": "5ms",
            "max_duration": "2s",
            "limit": 5,
        },
    ),
    ("by_id", "traces_query_trace_by_id", {"trace_id": TRACE_A}),
    ("by_id_missing", "traces_query_trace_by_id", {"trace_id": UNKNOWN_TRACE}),
    ("services", "traces_list_services", {}),
    ("operations", "traces_list_operations", {"service": "checkout"}),
    (
        "operations_kind",
        "traces_list_operations",
        {"service": "checkout", "span_kind": "server"},
    ),
)

#: Fields of the view that legitimately differ: a fresh evidence id per call,
#: and the revisions D10 bumps (``PROJECTION_REVISION`` v19 -> v20) or that
#: track unrelated registry changes.
_VOLATILE = {
    "evidence_id",
    "projection_revision",
    "registry_revision",
    "tool_registry_revision",
}


def _fingerprint(outcome) -> str:
    view = {k: v for k, v in outcome.model_view.items() if k not in _VOLATILE}
    raw = outcome.evidence.raw.decode() if outcome.evidence is not None else None
    blob = json.dumps(
        {
            "status": outcome.status,
            "reason": outcome.reason,
            "view": view,
            "record": raw,
        },
        sort_keys=True,
        default=str,
    )
    return sha256(blob.encode()).hexdigest()


def _run(gw) -> dict[str, Any]:
    return {
        label: gw.call(tool, params, index=i)
        for i, (label, tool, params) in enumerate(CALLS)
    }


# -- 1. v1 Jaeger: unchanged ------------------------------------------------------

#: Pinned on main 318dc016 (before #859) with ``JaegerDouble("v1")``.
V1_PINS = {
    "search": "ccd3f38e5832d255fed3f18bdbff081b5817f3549e0980ad328ff2e288c301cf",
    "by_tags": "1e2ee3ebd7b9b604d60e47ca47c50e08fa097b293522cbb98905194a5d5a5647",
    "by_id": "c59399bc754635cec811c1cd2eabfcf6d569d3a4a9f5ec08c97b062892048194",
    "by_id_missing": "229362696a8d296af6e87f21701055a8965d4e48da66c63a224890cfd6eb5173",
    "services": "0eaa7dce8a772f0c7e4ddc56cc4ffbbc4d91b69c6dd857bbca8af3971669f20f",
    "operations": "dcc69f16f8368fd2d1a5147e905383e4eb6aa6de5e8c1c086d7aba3c638a11a3",
    "operations_kind": "b778941f1e90ea6ec677b6afa6a200fe8bd2f9a8f9e5730f9897930961f0eaf5",
}
#: Every distinct path+query main sends for CALLS (base URL stripped).
V1_REQUESTS: list[str] = [
    "/api/operations?service=checkout",
    "/api/operations?service=checkout&spanKind=server",
    "/api/services",
    "/api/traces/5a5a0000000000000000000000000a01",
    "/api/traces/5a5a00000000000000000000000ffff1",
    "/api/traces?service=checkout&start=1794999600000000&end=1795003140000000&limit=20",
    (
        "/api/traces?service=payment&operation=Charge&tags=%7B%22rpc.system%22%3A%22grpc%22%7D"
        "&minDuration=5ms&maxDuration=2s&start=1794999600000000&end=1795003140000000&limit=5"
    ),
]


def _path_and_query(sent) -> str:
    query = sent.url.split("?", 1)[1] if "?" in sent.url else ""
    return sent.path + ("?" + query if query else "")


def test_v1_jaeger_outputs_are_unchanged(monkeypatch, tmp_path):
    """D1/D2/D9/D11-1: a Jaeger that answers ``/api/services`` is used as
    before -- same outcome, view and record bytes, same source queries, and
    no ``/api/v3`` request at all."""
    double = JaegerDouble("v1")
    outcomes = _run(ext_gateway(monkeypatch, tmp_path, double))

    got = {label: _fingerprint(outcome) for label, outcome in outcomes.items()}
    assert got == V1_PINS
    assert not [s.url for s in double.requests if s.path.startswith("/api/v3")]
    assert sorted({_path_and_query(s) for s in double.requests}) == V1_REQUESTS


# -- 2. v3-only Jaeger: mapped results --------------------------------------------


def _by_span(view) -> dict[str, dict[str, Any]]:
    return {row["span_id"]: row for row in view["content"]}


def _detail(row, key) -> dict[str, Any]:
    matches = [d for d in row["error_details"] if d["key"] == key]
    assert len(matches) == 1, (key, row["error_details"])
    return matches[0]


def _assert_jaeger_api_v3(outcome) -> None:
    """D9: the record says which API answered (``query.jaeger_api``)."""
    record = record_of(outcome)
    assert record.get("query", {}).get("jaeger_api") == "v3", record.get("query")


def _assert_no_v1_storage_claims(outcome) -> None:
    """D9: in v3 mode the notes do not repeat the v1 lab Jaeger's in-memory,
    5000-trace, since-last-start claims. The replacement wording is the
    implementer's; only the old claims are checked for absence."""
    notes = " ".join(outcome.model_view.get("notes") or [])
    for claim in ("5000", "in memory", "since it last started", "restart empties"):
        assert claim not in notes, (claim, notes)


def _assert_mapped_trace_a(rows: dict[str, dict[str, Any]]) -> None:
    """D5: OTLP -> the v1-shaped projection."""
    root, child = rows[SPAN_A1], rows[SPAN_A2]
    # ids as lowercase hex; service and version from the resource
    assert (root["trace_id"], root["span_id"]) == (TRACE_A, SPAN_A1)
    assert (child["trace_id"], child["span_id"]) == (TRACE_A, SPAN_A2)
    assert (root["service"], root["service_version"]) == ("checkout", "1.4.0")
    assert (child["service"], child["service_version"]) == ("payment", "2.0.1")
    assert (root["operation"], child["operation"]) == ("PlaceOrder", "Charge")
    # startTime = floor(startNs / 1000), duration = (end - start) / 1000
    assert (root["start_us"], root["duration_us"]) == (T0_US, 50_000)
    assert (child["start_us"], child["duration_us"]) == (T0_US + 10_000, 20_000)
    # status ERROR -> otel.status_code=ERROR and error=true
    assert child["error_by_visible_tags"] is True
    assert child["status_tags"].get("otel.status_code") == "ERROR"
    assert child["status_tags"].get("error") is True
    assert child["status_state"] == "recorded"
    # unset status stays unset (not OK, not an error)
    assert root["error_by_visible_tags"] is False
    assert root["status_state"] == "not_recorded"
    # parentSpanId -> a CHILD_OF reference; a root has none
    assert root["parent_references"] == []
    assert [
        {k: ref[k] for k in ("refType", "traceID", "spanID")}
        for ref in child["parent_references"]
    ] == [{"refType": "CHILD_OF", "traceID": TRACE_A, "spanID": SPAN_A1}]
    # status.message -> otel.status_description (a span tag); exception
    # event attributes -> a span log at the event's time
    status = _detail(child, "otel.status_description")
    assert (status["location"], status["value"]) == ("span_tag", STATUS_MESSAGE)
    message = _detail(child, "exception.message")
    assert (message["location"], message["value"]) == ("span_log", EXCEPTION_MESSAGE)
    assert message["timestamp_us"] == T0_US + 20_000
    assert _detail(child, "exception.type")["value"] == EXCEPTION_TYPE
    assert root["error_details"] == []


def _q(sent, canonical: str) -> str | None:
    values = sent.query.get(canonical)
    return values[0] if values else None


def test_v3_only_jaeger_every_trace_tool_maps_the_otlp_answer(monkeypatch, tmp_path):
    """D3/D5/D7/D9/D11-2 over the four A4 tools and ``traces_search``."""
    double = JaegerDouble("v3")
    out = _run(ext_gateway(monkeypatch, tmp_path, double))

    # traces_search: both traces regrouped by traceId out of the merged
    # resourceSpans (trace counts dedup by traceId)
    search = out["search"]
    assert search.status == "ok", search.model_view
    assert search.model_view["backend_traces_returned"] == 2
    assert search.model_view["backend_spans_returned"] == 3
    rows = _by_span(search.model_view)
    assert set(rows) == {SPAN_A1, SPAN_A2, SPAN_B1}
    _assert_mapped_trace_a(rows)
    assert rows[SPAN_B1]["trace_id"] == TRACE_B
    assert rows[SPAN_B1]["error_by_visible_tags"] is False  # status OK
    assert (rows[SPAN_B1]["start_us"], rows[SPAN_B1]["duration_us"]) == (
        T0_US + 5_000_000,
        20_000,
    )
    _assert_jaeger_api_v3(search)

    # traces_search_by_tags: same mapping, plus the D7 query mapping
    by_tags = out["by_tags"]
    assert by_tags.status == "ok", by_tags.model_view
    assert by_tags.model_view["backend_traces_returned"] == 2
    _assert_mapped_trace_a(_by_span(by_tags.model_view))
    _assert_jaeger_api_v3(by_tags)
    searches = double.on("/api/v3/traces")
    tagged = [s for s in searches if _q(s, "query.serviceName") == "payment"]
    assert len(tagged) == 1, [s.url for s in searches]
    sent = tagged[0]
    assert _q(sent, "query.operationName") == "Charge"
    assert json.loads(_q(sent, "query.attributes") or "null") == {"rpc.system": "grpc"}
    assert (_q(sent, "query.durationMin"), _q(sent, "query.durationMax")) == (
        "5ms",
        "2s",
    )
    assert _q(sent, "query.searchDepth") == "5"
    low = parse_rfc3339(_q(sent, "query.startTimeMin") or "")
    high = parse_rfc3339(_q(sent, "query.startTimeMax") or "")
    assert low == WINDOW.start
    # startTimeMax excludes its end point (D7); a nudge past the window end
    # to keep it inclusive is allowed, anything wider is not
    assert high is not None and WINDOW.end <= high <= WINDOW.end + timedelta(seconds=1)
    # traces_search's own query: its service, its default window, limit 20
    plain = [s for s in searches if _q(s, "query.serviceName") == "checkout"]
    assert len(plain) == 1, [s.url for s in searches]
    assert parse_rfc3339(_q(plain[0], "query.startTimeMin") or "") == WINDOW.start
    assert _q(plain[0], "query.searchDepth") == "20"

    # traces_query_trace_by_id: the one trace, by start time, nothing else
    by_id = out["by_id"]
    assert by_id.status == "ok", by_id.model_view
    assert by_id.model_view["trace_found"] is True
    assert [row["span_id"] for row in by_id.model_view["content"]] == [SPAN_A1, SPAN_A2]
    _assert_mapped_trace_a(_by_span(by_id.model_view))
    _assert_jaeger_api_v3(by_id)
    _assert_no_v1_storage_claims(by_id)

    # traces_list_services / traces_list_operations from /api/v3/*
    services = out["services"]
    assert services.status == "ok", services.model_view
    assert services.model_view["content"] == ["checkout", "payment"]
    _assert_jaeger_api_v3(services)
    _assert_no_v1_storage_claims(services)
    operations = out["operations"]
    assert operations.status == "ok", operations.model_view
    assert operations.model_view["content"] == [
        {"name": "GetCart", "name_truncated": False, "span_kind": "client"},
        {"name": "PlaceOrder", "name_truncated": False, "span_kind": "server"},
    ]
    _assert_jaeger_api_v3(operations)
    _assert_no_v1_storage_claims(operations)
    asked = double.on("/api/v3/operations")
    assert [(_q(s, "service"), _q(s, "spanKind")) for s in asked] == [
        ("checkout", None),
        ("checkout", "server"),
    ]
    assert out["operations_kind"].status == "ok", out["operations_kind"].model_view


def test_v3_probe_is_cached_and_a_failed_probe_is_not(monkeypatch, tmp_path):
    """D1: the v1-or-v3 decision is made once per worker, not per call; a
    probe that no Jaeger answers returns the source error as before and is
    not remembered, so the same worker works once Jaeger is back."""
    double = JaegerDouble("down")
    gw = ext_gateway(monkeypatch, tmp_path, double)

    down = gw.call("traces_list_services", {})
    assert down.status == "error", down.model_view
    assert down.reason == "SOURCE_UNAVAILABLE", down.model_view

    double.mode = "v3"
    first = _run(gw)
    assert {label: o.status for label, o in first.items()} == {
        "search": "ok",
        "by_tags": "ok",
        "by_id": "ok",
        "by_id_missing": "no_data",
        "services": "ok",
        "operations": "ok",
        "operations_kind": "ok",
    }
    probes = len(double.on("/api/services"))
    second = _run(gw)
    assert {label: o.status for label, o in second.items()} == {
        label: o.status for label, o in first.items()
    }
    # no new v1 probe for the second round of the very same calls
    assert len(double.on("/api/services")) == probes
    # and the removed v1 search/listing routes are not retried per call
    assert double.on("/api/traces") == [] and double.on("/api/operations") == []


def test_v3_mode_keeps_each_tool_credential_as_before(monkeypatch, tmp_path):
    """D4: ``traces_search`` keeps sending the configured bearer token, the
    A4 tools stay anonymous; v3 mode changes neither."""
    double = JaegerDouble("v3")
    gw = ext_gateway(monkeypatch, tmp_path, double)

    for i, (label, tool, params) in enumerate(CALLS):
        before = len(double.requests)
        outcome = gw.call(tool, params, index=i)
        assert outcome.status in {"ok", "no_data"}, (label, outcome.model_view)
        sent = double.requests[before:]
        if tool == "traces_search":
            data = [s for s in sent if s.path == "/api/v3/traces"]
            assert data and all(s.authorization == f"Bearer {OTEL_TOKEN}" for s in data)
        else:
            assert sent and all(s.authorization is None for s in sent), (label, sent)


# -- 3. v3 not found ------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, params",
    [
        ("traces_search", {"service": "checkout"}),
        ("traces_search_by_tags", {"service": "payment", "tags": "rpc.system=grpc"}),
    ],
)
def test_v3_search_404_no_traces_found_is_no_data(monkeypatch, tmp_path, tool, params):
    """D8/D11-3: v3 answers an empty search with HTTP 404 "No traces found";
    that is an empty result, not a source error (the S20 false gap)."""
    double = JaegerDouble("v3", search_hits=False)
    outcome = ext_gateway(monkeypatch, tmp_path, double).call(tool, params)

    assert double.on("/api/v3/traces"), [s.url for s in double.requests]
    assert outcome.status == "no_data", outcome.model_view
    assert outcome.reason == "NO_DATA"
    assert outcome.model_view["content"] == []
    assert "source_error" not in outcome.model_view
    if tool == "traces_search_by_tags":
        # D8: the existing empty-search hint is attached
        notes = " ".join(outcome.model_view.get("notes") or [])
        assert "No trace matched" in notes, notes


def test_v3_trace_id_404_is_the_existing_not_found_branch(monkeypatch, tmp_path):
    """D8: a trace ID v3 does not know is an empty, successful lookup with
    trace_found false -- never INVALID_PARAMS -- and its note does not claim
    an in-memory 5000-trace store (D9)."""
    double = JaegerDouble("v3")
    outcome = ext_gateway(monkeypatch, tmp_path, double).call(
        "traces_query_trace_by_id", {"trace_id": UNKNOWN_TRACE}
    )

    assert outcome.status == "no_data", outcome.model_view
    assert outcome.model_view["trace_found"] is False
    assert outcome.model_view["content"] == []
    assert outcome.model_view.get("notes"), "the not-found branch says why"
    _assert_no_v1_storage_claims(outcome)
    _assert_jaeger_api_v3(outcome)
