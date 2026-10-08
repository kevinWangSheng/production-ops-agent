"""Independent observation of the OTel Demo lab for one window (engineer script).

Separate from the product Run: it reads Prometheus and Jaeger directly and
reports (a) the control-window precondition (>= 2 distinct checkout traces
with at least one span in the window, positive call increments for checkout,
payment and checkout's client calls) and (b) for the fault case, >= 2 distinct
in-window checkout traces in which a checkout span failed *and* a failed span
of an authorized checkout dependency service is a direct child of a checkout
span. Output is JSON; the lab has no authentication, so no secret is involved.

This is the reusable successor of the packet's
``docs/evidence/m1-01-v4-acceptance/observe.py`` (left untouched as merged
evidence). The fault predicate is tighter (PR #55 follow-up): the earlier
script accepted any failed non-checkout span in the trace, which would also
count a failure unrelated to checkout's own calls; here the failed span must
belong to one of the dependency services the v4 question authorizes and hang
off a checkout span by its ``CHILD_OF`` reference.

usage: otel_demo_observe.py <start_iso> <end_iso> <out.json> [--expect fault|normal]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROM = os.environ.get("OPSPILOT_OTEL_PROMETHEUS_URL", "http://127.0.0.1:19090")
JAEGER = os.environ.get("OPSPILOT_OTEL_JAEGER_URL", "http://127.0.0.1:16686/jaeger/ui")
# The dependency services the v4 packet question names for checkout.
AUTHORIZED_DEPENDENCIES = frozenset(
    {"payment", "product-catalog", "cart", "currency", "shipping", "email"}
)
STATUS_KEYS = (
    "otel.status_code",
    "otel.status_description",
    "rpc.grpc.status_code",
    "error",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.kind_lab import OPENER, lab_authorization  # noqa: E402


def get(url: str, timeout: int = 30) -> dict:
    """GET JSON. Prometheus (M1-02 step 4: basic auth) gets the lab account's
    header from ``kind_lab.lab_authorization``; Jaeger stays anonymous. The
    header value is never printed."""
    headers = {}
    if url.startswith(PROM):
        authorization = lab_authorization()
        if authorization is not None:
            headers["Authorization"] = authorization
    request = urllib.request.Request(url, headers=headers)
    with OPENER.open(request, timeout=timeout) as r:
        return json.loads(r.read())


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def prom_instant(expr: str, at: datetime) -> list:
    q = urllib.parse.urlencode({"query": expr, "time": f"{at.timestamp():.3f}"})
    return get(f"{PROM}/api/v1/query?{q}")["data"]["result"]


def span_failed(tags: dict) -> bool:
    return (
        tags.get("error") is True
        or tags.get("otel.status_code") == "ERROR"
        or str(tags.get("rpc.grpc.status_code", "0")) not in {"0", "None"}
        or str(
            tags.get("http.status_code", tags.get("http.response.status_code", "0"))
        ).startswith("5")
    )


def classify_trace(trace: dict, start: datetime, end: datetime) -> dict:
    procs = {k: v["serviceName"] for k, v in trace["processes"].items()}
    spans = trace["spans"]
    service_of = {s["spanID"]: procs.get(s["processID"]) for s in spans}
    # Jaeger returns whole traces that intersect the interval; a span counts
    # as fault evidence only when it itself started inside the window, and a
    # dependency failure only when its checkout parent did too (PR #56 bot
    # review P1: an in-window sibling span must not validate an out-of-window
    # failure). ``start_us`` is recorded so the predicate can be recomputed
    # offline from the output.
    started_in_window = {
        s["spanID"]: start.timestamp() <= s["startTime"] / 1_000_000 <= end.timestamp()
        for s in spans
    }
    checkout_err = []
    failed_dependency_children = []
    in_window = any(started_in_window.values())
    for span in spans:
        svc = service_of[span["spanID"]]
        tags = {t["key"]: t["value"] for t in span.get("tags") or []}
        if not span_failed(tags) or not started_in_window[span["spanID"]]:
            continue
        status = {k: tags[k] for k in STATUS_KEYS if k in tags}
        entry = {
            "span_id": span["spanID"],
            "operation": span["operationName"],
            "start_us": span["startTime"],
            "status": status,
        }
        if svc == "checkout":
            checkout_err.append(entry)
            continue
        if svc not in AUTHORIZED_DEPENDENCIES:
            continue
        parents = [
            r["spanID"]
            for r in span.get("references") or []
            if r.get("refType") == "CHILD_OF"
        ]
        checkout_parents = [
            p
            for p in parents
            if service_of.get(p) == "checkout" and started_in_window.get(p, False)
        ]
        if checkout_parents:
            failed_dependency_children.append(
                {**entry, "service": svc, "parent_span_ids": checkout_parents}
            )
    return {
        "trace_id": trace["traceID"],
        "span_count": len(spans),
        "any_span_in_window": in_window,
        "checkout_error_spans": checkout_err,
        "failed_dependency_child_spans": failed_dependency_children,
    }


def main() -> None:
    start, end, out = parse(sys.argv[1]), parse(sys.argv[2]), sys.argv[3]
    expect = (
        sys.argv[sys.argv.index("--expect") + 1] if "--expect" in sys.argv else None
    )
    seconds = int((end - start).total_seconds())
    observed_at = datetime.now(timezone.utc)  # noqa: TID251 (engineer script)
    rng = f"[{seconds}s]"
    metrics = {}
    for name, expr in {
        "checkout_calls_by_span_status": f'increase(traces_span_metrics_calls_total{{service_name="checkout"}}{rng})',
        "payment_calls_by_span_status": f'increase(traces_span_metrics_calls_total{{service_name="payment"}}{rng})',
        "checkout_client_calls_by_peer": f'sum by (span_name, status_code) (increase(traces_span_metrics_calls_total{{service_name="checkout", span_kind="SPAN_KIND_CLIENT"}}{rng}))',
        "all_services_error_calls": f'sum by (service_name) (increase(traces_span_metrics_calls_total{{status_code="STATUS_CODE_ERROR"}}{rng}))',
    }.items():
        rows = prom_instant(expr, end)
        metrics[name] = {
            "expr": expr,
            "evaluated_at": end.isoformat(),
            "series": [
                {"metric": r["metric"], "value": float(r["value"][1])} for r in rows
            ],
        }
    q = urllib.parse.urlencode(
        {
            "service": "checkout",
            "start": int(start.timestamp() * 1_000_000),
            "end": int(end.timestamp() * 1_000_000),
            "limit": 500,
        }
    )
    payload = get(f"{JAEGER}/api/traces?{q}", timeout=120)
    traces = [classify_trace(t, start, end) for t in payload.get("data") or []]
    # Only traces with at least one span inside the requested window count.
    in_window_traces = [t for t in traces if t["any_span_in_window"]]
    distinct = {t["trace_id"] for t in in_window_traces}
    failing = [
        t
        for t in in_window_traces
        if t["checkout_error_spans"] and t["failed_dependency_child_spans"]
    ]
    failed_dep_services = sorted(
        {c["service"] for t in failing for c in t["failed_dependency_child_spans"]}
    )
    positive = {
        n: sum(1 for s in m["series"] if s["value"] > 0) for n, m in metrics.items()
    }
    dependency_traffic = (
        positive["checkout_calls_by_span_status"] > 0
        and positive["payment_calls_by_span_status"] > 0
        and positive["checkout_client_calls_by_peer"] > 0
    )
    result = {
        "script": "scripts/otel_demo_observe.py",
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "seconds": seconds,
        },
        "observed_at": observed_at.isoformat(),
        "sources": {"prometheus": PROM, "jaeger": JAEGER},
        "expect": expect,
        "authorized_dependencies": sorted(AUTHORIZED_DEPENDENCIES),
        "metrics": metrics,
        "positive_series_count": positive,
        "jaeger": {
            "query": {"service": "checkout", "limit": 500},
            "returned_traces": len(traces),
            "traces_in_window": len(in_window_traces),
            "distinct_trace_ids": len(distinct),
            "traces_with_checkout_error_and_failed_dependency_child": len(failing),
            "failed_dependency_services": failed_dep_services,
            "traces": traces,
        },
        "precondition": {
            "control_window_ok": len(distinct) >= 2 and dependency_traffic,
            "dependency_traffic": dependency_traffic,
            "fault_confirmed": len(failing) >= 2,
        },
    }
    with open(out, "w") as f:
        json.dump(result, f, indent=1)
    summary = {
        k: v
        for k, v in result.items()
        if k in ("window", "positive_series_count", "precondition")
    }
    summary["jaeger"] = {
        k: v for k, v in result["jaeger"].items() if k not in ("traces", "query")
    }
    summary["error_calls_by_service"] = metrics["all_services_error_calls"]["series"]
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
