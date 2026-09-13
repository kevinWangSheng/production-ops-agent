"""Frozen pure v2 projections from verified wrapper 7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676."""

import hashlib
import json

from scripts.m0_environment.round02 import canonical_hash


def _bind_identity_v2(service, attributes, registry):
    candidates = []
    for row in (registry or {}).get("containers", []):
        cid = attributes.get("container.id")
        host = attributes.get("host.name")
        if cid:
            matches = cid == row.get("container_id")
            basis = "exact telemetry container.id equals registry container_id"
        elif host:
            matches = host == row.get("hostname")
            basis = "exact telemetry host.name equals independently observed daemon hostname"
        else:
            matches = False
            basis = None
        if matches:
            if (
                attributes.get("host.name")
                and row.get("hostname")
                and attributes["host.name"] != row["hostname"]
            ):
                continue
            if row.get("labels", {}).get("com.docker.compose.service") not in (
                None,
                service,
            ):
                continue
            candidates.append((row, basis))
    result = {
        "service": service,
        "attributes": attributes,
        "container_mapping": "unknown",
    }
    if len(candidates) == 1:
        row, basis = candidates[0]
        result.update(
            container_mapping="matched",
            mapping_basis=basis,
            container_id=row["container_id"],
            image_id=row.get("image_id"),
            compose_service=row.get("labels", {}).get("com.docker.compose.service"),
            config_hash=row.get("labels", {}).get("com.docker.compose.config-hash"),
        )
    return result


def log_projection_v2(record, registry=None):
    """Preserve complete displayed log bodies, omit redundant backend metadata."""
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
    backend = record.get("data", {}).get("data", {})
    hits = backend.get("hits", {})
    rows = []
    identities = []
    for hit in hits.get("hits", []):
        source = hit.get("_source", {})
        attrs = source.get("attributes", {})
        identity = {
            "resource": source.get("resource", {}),
            "container_binding": _bind_identity_v2(
                source.get("resource", {}).get("service.name", "unknown"),
                {
                    k: source.get("resource", {})[k]
                    for k in (
                        "container.id",
                        "host.name",
                        "service.version",
                        "opspilot.integration.id",
                    )
                    if k in source.get("resource", {})
                },
                registry,
            ),
            "instrumentation_scope": source.get("instrumentationScope", {}),
        }
        if identity not in identities:
            identities.append(identity)
        rows.append(
            {
                "document_id": hit.get("_id"),
                "resource_identity_ref": identities.index(identity),
                "timestamp": source.get("@timestamp"),
                "service": source.get("resource", {}).get("service.name"),
                "severity": source.get("severity"),
                "body": source.get("body"),
                "trace_id": source.get("traceId"),
                "span_id": source.get("spanId"),
                "diagnostic_attributes": {
                    k: attrs[k]
                    for k in (
                        "event.name",
                        "exception.type",
                        "exception.message",
                        "http.response.status_code",
                        "rpc.grpc.status_code",
                    )
                    if k in attrs
                },
            }
        )
    view = {k: v for k, v in record.items() if k != "data"}
    view["data"] = {
        "source": "logs",
        "raw_observation_sha256": hashlib.sha256(raw).hexdigest(),
        "source_identity_table": identities,
        "backend_total_hits": hits.get("total"),
        "backend_timed_out": backend.get("timed_out"),
        "returned_hit_count": len(rows),
        "selection_policy": "Backend newest-first query, limit20; displayed log bodies kept complete. Full original resource/scope identity lifted to source_identity_table; rows reference its index. Index, shard and data-stream/network attributes omitted. Remove oldest displayed entries only if view exceeds14000bytes; not an unbiased sample or exhaustive error inventory.",
        "displayed_logs": rows,
        "omitted_returned_hit_count": 0,
    }
    while len(json.dumps(view, ensure_ascii=False).encode()) > 14000 and rows:
        rows.pop()
        view["data"]["omitted_returned_hit_count"] += 1
    return view


def trace_projection_v2(record, registry=None):
    """Deterministic bounded view; full raw observation remains separately stored."""
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
    backend = record.get("data", {}).get("data", {})
    traces = backend.get("data", []) if isinstance(backend, dict) else []
    spans = []
    counts = {}
    trace_ids = []
    identities = []
    for trace in traces:
        trace_ids.append(trace.get("traceID"))
        processes = trace.get("processes", {})
        for span in trace.get("spans", []):
            tags = {tag.get("key"): tag.get("value") for tag in span.get("tags", [])}
            process = processes.get(span.get("processID"), {})
            identity_tags = {
                tag.get("key"): tag.get("value") for tag in process.get("tags", [])
            }
            identity = _bind_identity_v2(
                process.get("serviceName", "unknown"),
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
            if identity not in identities:
                identities.append(identity)
            service = processes.get(span.get("processID"), {}).get(
                "serviceName", "unknown"
            )
            is_error = (
                tags.get("error") is True
                or tags.get("otel.status_code") == "ERROR"
                or str(tags.get("rpc.grpc.status_code", "0")) not in {"0", "None"}
                or str(tags.get("http.status_code", "0")).startswith("5")
            )
            count = counts.setdefault(
                service,
                {
                    "span_count": 0,
                    "error_spans_by_visible_tags": 0,
                    "max_duration_us": 0,
                },
            )
            count["span_count"] += 1
            count["error_spans_by_visible_tags"] += int(is_error)
            count["max_duration_us"] = max(
                count["max_duration_us"], span.get("duration", 0)
            )
            spans.append(
                {
                    "trace_id": trace.get("traceID"),
                    "span_id": span.get("spanID"),
                    "parent_references": [
                        {k: ref.get(k) for k in ("refType", "traceID", "spanID")}
                        for ref in span.get("references", [])
                    ],
                    "source_identity_ref": identities.index(identity),
                    "service": service,
                    "operation": str(span.get("operationName", ""))[:150],
                    "start_us": span.get("startTime"),
                    "duration_us": span.get("duration"),
                    "error_by_visible_tags": is_error,
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
                }
            )
    spans.sort(
        key=lambda span: (
            not span["error_by_visible_tags"],
            -float(span.get("duration_us") or 0),
        )
    )
    view = {k: v for k, v in record.items() if k != "data"}
    view["data"] = {
        "source": "traces",
        "raw_observation_sha256": hashlib.sha256(raw).hexdigest(),
        "trace_ids": trace_ids,
        "source_identity_table": identities,
        "projection_version": "m0-02-v2",
        "trace_count": len(traces),
        "total_span_count": len(spans),
        "per_service_counts_over_returned_traces": counts,
        "selection_policy": "visible-error-tag spans first, then longest duration; bounded to 20 and 14000 UTF-8 bytes. Sampling is biased, not a population failure-rate estimate. Error detection covers only listed status tags; missing tags are unknown.",
        "sampled_spans": spans[:20],
        "omitted_span_count": max(0, len(spans) - 20),
        "source_query_limit": 20,
    }
    while (
        len(json.dumps(view, ensure_ascii=False).encode()) > 14000
        and view["data"]["sampled_spans"]
    ):
        view["data"]["sampled_spans"].pop()
        view["data"]["omitted_span_count"] += 1
    return view


def metric_projection_v1(record):
    """Keep every original query/result; annotate semantics without executing a rewrite."""
    view = dict(record)
    query = record.get("query", {})
    view["projection_version"] = "m0-02-metrics-v1"
    view["raw_observation_sha256"] = canonical_hash(record)
    view["metric_semantics"] = {
        "query_mode": "instant_evaluation",
        "evaluation_time": query.get("end"),
        "authorization_window": {"start": query.get("start"), "end": query.get("end")},
        "authorization_window_is_value_range": False,
        "value_time_semantics": "Defined by the original PromQL expression, not by the authorization start/end fields.",
        "counter_semantics": "Untransformed counters and classic histogram _bucket/_count values are cumulative since reset; a nonzero raw value does not establish events in this authorization window. _total is a naming convention, not proof of type.",
        "window_semantics": "Window increases/rates require explicit matching range expressions such as increase(counter[duration]) or rate(counter[duration]); inspect all terms, offsets and selectors. The runner does not insert or validate a delta interpretation. increase is extrapolated and may be fractional, not unique requests.",
        "gauge_semantics": "Gauge values are observations at evaluation time, not window event counts. Metric type unknown stays unknown.",
    }
    return view
