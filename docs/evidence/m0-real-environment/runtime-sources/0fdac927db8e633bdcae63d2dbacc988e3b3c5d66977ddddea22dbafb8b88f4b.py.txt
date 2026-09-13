"""Offline-calibrated trace view v3; a bounded projection, never a complete graph."""

from __future__ import annotations

import copy
import json

from scripts.m0_environment.legacy_projections import _bind_identity_v2
from scripts.m0_environment.round02 import canonical_hash

VERSION = "m0-02-traces-v3"
DETAIL_KEYS = {
    "error_description",
    "error.description",
    "otel.status_description",
    "otel.status_message",
    "exception.type",
    "exception.message",
    "exception.stacktrace",
    "grpc.error_message",
    "grpc.error_name",
}


def details(span):
    result = []
    for tag in span.get("tags", []):
        if tag.get("key") in DETAIL_KEYS:
            result.append(
                {"location": "span_tag", "key": tag["key"], "value": tag.get("value")}
            )
    for log in span.get("logs", []):
        for field in log.get("fields", []):
            if field.get("key") in DETAIL_KEYS:
                result.append(
                    {
                        "location": "span_log",
                        "key": field["key"],
                        "value": field.get("value"),
                        "timestamp_us": log.get("timestamp"),
                    }
                )
    return result


def clipped_detail(field):
    value = field["value"]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    raw = text.encode()
    return {
        **field,
        "value": raw[:600].decode("utf-8", errors="ignore"),
        "original_utf8_bytes": len(raw),
        "value_truncated": len(raw) > 600,
    }


def trace_projection_v3(record, registry=None):
    traces = record.get("data", {}).get("data", {}).get("data", [])
    spans = []
    service_counts = {}
    raw_fields = 0
    raw_detail_spans = 0
    query_service = record.get("query", {}).get("service")
    for trace in traces:
        for span in trace.get("spans", []):
            process = trace.get("processes", {}).get(span.get("processID"), {})
            service = process.get("serviceName", "unknown")
            tags = {t.get("key"): t.get("value") for t in span.get("tags", [])}
            identity_tags = {
                t.get("key"): t.get("value") for t in process.get("tags", [])
            }
            identity = _bind_identity_v2(
                service,
                {
                    k: identity_tags[k]
                    for k in (
                        "container.id",
                        "host.name",
                        "opspilot.integration.id",
                        "service.version",
                    )
                    if k in identity_tags
                },
                registry,
            )
            error = (
                tags.get("error") is True
                or tags.get("otel.status_code") == "ERROR"
                or str(tags.get("rpc.grpc.status_code", "0")) not in {"0", "None"}
                or str(tags.get("http.status_code", "0")).startswith("5")
            )
            fields = details(span)
            raw_fields += len(fields)
            raw_detail_spans += bool(fields)
            count = service_counts.setdefault(
                service,
                {
                    "span_count": 0,
                    "error_spans_by_listed_status_tags": 0,
                    "max_duration_us": 0,
                },
            )
            count["span_count"] += 1
            count["error_spans_by_listed_status_tags"] += int(error)
            count["max_duration_us"] = max(
                count["max_duration_us"], span.get("duration", 0)
            )
            operation = str(span.get("operationName", ""))
            spans.append(
                {
                    "trace_id": trace.get("traceID"),
                    "span_id": span.get("spanID"),
                    "service": service,
                    "operation": operation[:150],
                    "operation_truncated": len(operation) > 150,
                    "start_us": span.get("startTime"),
                    "duration_us": span.get("duration"),
                    "error_by_visible_tags": error,
                    "status_tags": {
                        k: tags[k]
                        for k in (
                            "error",
                            "otel.status_code",
                            "rpc.grpc.status_code",
                            "http.status_code",
                        )
                        if k in tags
                    },
                    "parent_references": [
                        {k: ref.get(k) for k in ("refType", "traceID", "spanID")}
                        for ref in span.get("references", [])
                    ],
                    "error_details": [clipped_detail(field) for field in fields[:4]],
                    "raw_error_detail_field_count": len(fields),
                    "omitted_error_detail_field_count": max(0, len(fields) - 4),
                    "_identity": identity,
                }
            )
    spans.sort(
        key=lambda span: (
            span["service"] != query_service,
            not span["error_by_visible_tags"],
            -float(span.get("duration_us") or 0),
            str(span["trace_id"]),
            str(span["span_id"]),
        )
    )
    selected = spans[:20]
    byte_limited = False
    while True:
        identities = []
        visible = copy.deepcopy(selected)
        visible_keys = {(s["trace_id"], s["span_id"]) for s in visible}
        for span in visible:
            identity = span.pop("_identity")
            if identity not in identities:
                identities.append(identity)
            span["source_identity_ref"] = identities.index(identity)
            for ref in span["parent_references"]:
                ref["parent_is_visible"] = (
                    ref.get("traceID"),
                    ref.get("spanID"),
                ) in visible_keys
        visible_ids = sorted({str(s["trace_id"]) for s in visible})
        displayed_fields = sum(len(s["error_details"]) for s in visible)
        view = {k: copy.deepcopy(v) for k, v in record.items() if k != "data"}
        view["projection_version"] = VERSION
        view["data"] = {
            "source": "traces",
            "projection_version": VERSION,
            "raw_observation_sha256": canonical_hash(record),
            "backend_returned_trace_count": len(traces),
            "backend_returned_span_count": len(spans),
            "backend_trace_ids": [t.get("traceID") for t in traces],
            "backend_counts_by_service": service_counts,
            "actual_visible_trace_count": len(visible_ids),
            "actual_visible_trace_ids": visible_ids,
            "actual_visible_span_count": len(visible),
            "omitted_span_count": len(spans) - len(visible),
            "limits": {
                "source_query_limit": 20,
                "display_max_spans": 20,
                "view_max_utf8_bytes": 14000,
                "detail_max_fields_per_span": 4,
                "detail_max_utf8_bytes_per_field": 600,
            },
            "truncation": {
                "span_count_limit_applied": len(spans) > 20,
                "byte_limit_applied": byte_limited,
            },
            "error_detail_coverage": {
                "covered_keys": sorted(DETAIL_KEYS),
                "raw_has_listed_error_details": raw_fields > 0,
                "raw_detail_span_count": raw_detail_spans,
                "raw_detail_field_count": raw_fields,
                "visible_detail_span_count": sum(
                    bool(s["error_details"]) for s in visible
                ),
                "visible_detail_field_count": displayed_fields,
                "omitted_detail_field_count": raw_fields - displayed_fields,
                "truncated_visible_detail_field_count": sum(
                    f["value_truncated"] for s in visible for f in s["error_details"]
                ),
                "scope": "Counts cover only the listed diagnostic keys in this returned raw artifact. Omitted details are not proof of missing telemetry; exact text/links apply only to their displayed span and trace.",
            },
            "selection_policy": "Requested query.service first, then listed error status, then longest duration with stable trace/span tie-break. Biased sample, not a complete graph or population failure rate. Limits are caps, not visible counts. Parent links only connect shown endpoints when parent_is_visible=true; do not generalize a displayed error detail to unshown spans/traces.",
            "source_identity_table": identities,
            "sampled_spans": visible,
        }
        if len(json.dumps(view, ensure_ascii=False).encode()) <= 14000:
            return view
        if not selected:
            raise ValueError("trace view metadata exceeds byte limit")
        selected.pop()
        byte_limited = True
