"""Offline fidelity checks; no provider or telemetry calls."""

import copy
import json

from scripts.m0_environment.holmes_baseline import trace_projection


def raw_trace():
    spans = []
    for i in range(30):
        spans.append(
            {
                "spanID": str(i),
                "processID": "target" if i < 8 else "other",
                "operationName": "op",
                "duration": i + 1,
                "tags": [
                    {"key": "error", "value": True},
                    {"key": "otel.status_description", "value": "diagnostic " * 200},
                ],
                "logs": [
                    {
                        "timestamp": 1,
                        "fields": [
                            {"key": "exception.message", "value": "visible error"},
                            {"key": "exception.stacktrace", "value": "stack " * 200},
                        ],
                    }
                ],
                "references": [
                    {"refType": "CHILD_OF", "traceID": "t", "spanID": "missing-parent"}
                ],
            }
        )
    return {
        "evidence_id": "e",
        "tool": "otel_traces",
        "query": {"service": "payment"},
        "data": {
            "data": {
                "data": [
                    {
                        "traceID": "t",
                        "processes": {
                            "target": {"serviceName": "payment"},
                            "other": {"serviceName": "checkout"},
                        },
                        "spans": spans,
                    }
                ]
            }
        },
    }


def test_actual_counts_and_details_do_not_confuse_caps_with_visible_rows():
    raw = raw_trace()
    before = copy.deepcopy(raw)
    view = trace_projection(raw)
    data = view["data"]
    assert data["backend_returned_span_count"] == 30
    assert data["actual_visible_span_count"] == len(data["sampled_spans"])
    assert data["actual_visible_span_count"] + data["omitted_span_count"] == 30
    assert data["limits"]["display_max_spans"] == 20
    assert data["sampled_spans"][0]["service"] == "payment"
    assert data["error_detail_coverage"]["raw_detail_field_count"] == 90
    assert data["error_detail_coverage"]["omitted_detail_field_count"] > 0
    assert data["error_detail_coverage"]["truncated_visible_detail_field_count"] > 0
    assert all(
        not ref["parent_is_visible"]
        for span in data["sampled_spans"]
        for ref in span["parent_references"]
    )
    assert len(json.dumps(view, ensure_ascii=False).encode()) <= 14000
    assert raw == before
