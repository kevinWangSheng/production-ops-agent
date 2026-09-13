"""Frozen pure v2 projections from verified wrapper 7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676."""

import hashlib
import json


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
