"""ITBench-Lite ``otel_traces_raw.tsv`` -> OTLP/HTTP JSON requests (#581).

The TSV is the ClickHouse OTel exporter schema. Each row becomes one span; a
row that cannot be converted raises instead of being dropped, so the span
count is lossless by construction. Times move by the scenario's offset
(whole seconds, see ``shift``).

Posting goes only to a loopback endpoint (the replay Jaeger binds 127.0.0.1).
"""

from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import re
import sys
import urllib.request
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from scripts.itbench_lite.shift import NS, parse_clickhouse_ns
from scripts.itbench_lite.snapshot import Snapshot, load_snapshot

DEFAULT_BATCH = 3000
_TRACE_ID = re.compile(r"[0-9a-fA-F]{32}")
_SPAN_ID = re.compile(r"[0-9a-fA-F]{16}")
TIMEOUT_SECONDS = 60

#: ClickHouse writes ``ptrace`` enum names; the proto names are accepted too.
SPAN_KIND = {
    "Unspecified": 0,
    "Internal": 1,
    "Server": 2,
    "Client": 3,
    "Producer": 4,
    "Consumer": 5,
    "SPAN_KIND_UNSPECIFIED": 0,
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}
STATUS_CODE = {
    "Unset": 0,
    "Ok": 1,
    "Error": 2,
    "STATUS_CODE_UNSET": 0,
    "STATUS_CODE_OK": 1,
    "STATUS_CODE_ERROR": 2,
}


@dataclass
class TraceStats:
    rows: int = 0
    spans: int = 0
    requests: int = 0
    start_ns: int | None = None
    end_ns: int | None = None


def _literal(text: str | None, kind: type) -> Any:
    if text is None or not text.strip():
        return kind()
    value = ast.literal_eval(text)
    if not isinstance(value, kind if kind is dict else (list, tuple)):
        raise ValueError(f"expected {kind.__name__}")
    return value


def _attributes(value: Mapping[Any, Any]) -> list[dict[str, Any]]:
    return [{"key": str(k), "value": {"stringValue": str(v)}} for k, v in value.items()]


def _id(value: object, pattern: re.Pattern[str], what: str) -> str:
    """A hex trace or span id as OTLP/JSON wants it; all-zero ids are invalid."""
    text = str(value)
    if pattern.fullmatch(text) is None or not text.strip("0"):
        raise ValueError(f"invalid {what}")
    return text


def _ns(text: str, offset_seconds: int) -> int:
    return parse_clickhouse_ns(text) + offset_seconds * NS


def span_from_row(row: Mapping[str, str], offset_seconds: int) -> dict[str, Any]:
    """The OTLP JSON span of one TSV row; raises ``ValueError`` on any defect."""
    try:
        kind = SPAN_KIND[row["SpanKind"]]
        status = STATUS_CODE[row["StatusCode"]]
    except KeyError as exc:
        raise ValueError(f"unknown enum value {exc}") from None
    start = _ns(row["Timestamp"], offset_seconds)
    duration = int(row["Duration"])
    if duration < 0:
        raise ValueError("negative duration")
    stamps = _literal(row.get("Events.Timestamp"), list)
    names = _literal(row.get("Events.Name"), list)
    attrs = _literal(row.get("Events.Attributes"), list)
    if not len(stamps) == len(names) == len(attrs):
        raise ValueError("event arrays differ in length")
    events = [
        {
            "timeUnixNano": str(_ns(str(stamp), offset_seconds)),
            "name": str(name),
            "attributes": _attributes(dict(attr)),
        }
        for stamp, name, attr in zip(stamps, names, attrs, strict=True)
    ]
    link_traces = _literal(row.get("Links.TraceId"), list)
    link_spans = _literal(row.get("Links.SpanId"), list)
    link_states = _literal(row.get("Links.TraceState"), list) or [""] * len(link_traces)
    link_attrs = _literal(row.get("Links.Attributes"), list) or [{}] * len(link_traces)
    if not len(link_traces) == len(link_spans) == len(link_states) == len(link_attrs):
        raise ValueError("link arrays differ in length")
    links = [
        {
            "traceId": _id(trace, _TRACE_ID, "link trace id"),
            "spanId": _id(span, _SPAN_ID, "link span id"),
            "traceState": str(state),
            "attributes": _attributes(dict(attr)),
        }
        for trace, span, state, attr in zip(
            link_traces, link_spans, link_states, link_attrs, strict=True
        )
    ]
    span: dict[str, Any] = {
        "traceId": _id(row["TraceId"], _TRACE_ID, "trace id"),
        "spanId": _id(row["SpanId"], _SPAN_ID, "span id"),
        "name": row["SpanName"],
        "kind": kind,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + duration),
        "attributes": _attributes(_literal(row.get("SpanAttributes"), dict)),
        "events": events,
        "links": links,
        "status": {"code": status, "message": row.get("StatusMessage") or ""},
    }
    if row.get("ParentSpanId"):
        span["parentSpanId"] = _id(row["ParentSpanId"], _SPAN_ID, "parent span id")
    if row.get("TraceState"):
        span["traceState"] = row["TraceState"]
    return span


GroupKey = tuple[str, str, str, str]


def _request(groups: Mapping[GroupKey, list[dict[str, Any]]]) -> dict[str, Any]:
    resource_spans = []
    for (service, resource, scope_name, scope_version), spans in groups.items():
        attributes = _attributes(_literal(resource, dict))
        if not any(a["key"] == "service.name" for a in attributes):
            attributes.append(
                {"key": "service.name", "value": {"stringValue": service}}
            )
        resource_spans.append(
            {
                "resource": {"attributes": attributes},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name, "version": scope_version},
                        "spans": spans,
                    }
                ],
            }
        )
    return {"resourceSpans": resource_spans}


def iter_otlp_requests(
    snapshot: Snapshot,
    *,
    offset_seconds: int,
    batch_size: int = DEFAULT_BATCH,
    stats: TraceStats | None = None,
) -> Iterator[dict[str, Any]]:
    """OTLP ``ExportTraceServiceRequest`` JSON bodies of at most ``batch_size`` spans."""
    path = snapshot.tsv_path("traces")
    if path is None:
        return
    stats = stats if stats is not None else TraceStats()
    groups: dict[GroupKey, list[dict[str, Any]]] = defaultdict(list)
    pending = 0
    for number, row in enumerate(snapshot.iter_tsv(path), start=2):
        stats.rows += 1
        try:
            span = span_from_row(row, offset_seconds)
        except (KeyError, ValueError, SyntaxError, TypeError) as exc:
            raise ValueError(f"traces line {number}: {exc}") from exc
        key = (
            row.get("ServiceName") or "",
            row.get("ResourceAttributes") or "",
            row.get("ScopeName") or "",
            row.get("ScopeVersion") or "",
        )
        groups[key].append(span)
        stats.spans += 1
        start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
        stats.start_ns = start if stats.start_ns is None else min(stats.start_ns, start)
        stats.end_ns = end if stats.end_ns is None else max(stats.end_ns, end)
        pending += 1
        if pending >= batch_size:
            stats.requests += 1
            yield _request(groups)
            groups = defaultdict(list)
            pending = 0
    if pending:
        stats.requests += 1
        yield _request(groups)


def require_loopback(url: str) -> None:
    """Refuse any URL whose host is not a loopback address."""
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("replay endpoints must be loopback addresses")


def post_json_loopback(
    url: str, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> int:
    """POST ``body`` as JSON to a loopback ``url``; returns the HTTP status."""
    require_loopback(url)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        response.read()
        return int(response.status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot")
    parser.add_argument("endpoint", help="loopback OTLP/HTTP URL, e.g. .../v1/traces")
    parser.add_argument("--offset-seconds", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    args = parser.parse_args(argv)
    require_loopback(args.endpoint)
    snapshot = load_snapshot(args.snapshot)
    stats = TraceStats()
    for body in iter_otlp_requests(
        snapshot,
        offset_seconds=args.offset_seconds,
        batch_size=args.batch_size,
        stats=stats,
    ):
        post_json_loopback(args.endpoint, body)
    json.dump(asdict(stats), sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
