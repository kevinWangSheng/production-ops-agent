"""Independent observation of the OTel Demo lab for one 300-second window.

Engineer-side script, separate from the product Run: it reads Prometheus and
Jaeger directly and reports (a) the control-window precondition (>= 2 distinct
checkout traces, positive call increments for checkout and its dependency
calls) and (b) for the fault case, >= 2 distinct checkout traces whose
failure is associated with a failed dependency call. Output is JSON; no
secrets are involved (the lab has no authentication).

usage: observe.py <start_iso> <end_iso> <out.json> [--expect fault|normal]
"""

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM = "http://127.0.0.1:19090"
JAEGER = "http://127.0.0.1:16686/jaeger/ui"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=30):
    with OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read())


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def prom_instant(expr, at):
    q = urllib.parse.urlencode({"query": expr, "time": f"{at.timestamp():.3f}"})
    return get(f"{PROM}/api/v1/query?{q}")["data"]["result"]


def main():
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
    # Jaeger: every checkout trace in the window (limit high enough for 300 s of load).
    q = urllib.parse.urlencode(
        {
            "service": "checkout",
            "start": int(start.timestamp() * 1_000_000),
            "end": int(end.timestamp() * 1_000_000),
            "limit": 500,
        }
    )
    payload = get(f"{JAEGER}/api/traces?{q}", timeout=120)
    traces = []
    for trace in payload.get("data") or []:
        procs = {k: v["serviceName"] for k, v in trace["processes"].items()}
        checkout_err = []
        failed_calls = []
        in_window = False
        for span in trace["spans"]:
            svc = procs.get(span["processID"])
            tags = {t["key"]: t["value"] for t in span.get("tags") or []}
            st = span["startTime"] / 1_000_000
            if start.timestamp() <= st <= end.timestamp():
                in_window = True
            err = (
                tags.get("error") is True
                or tags.get("otel.status_code") == "ERROR"
                or str(tags.get("rpc.grpc.status_code", "0")) not in {"0", "None"}
                or str(
                    tags.get(
                        "http.status_code", tags.get("http.response.status_code", "0")
                    )
                ).startswith("5")
            )
            if svc == "checkout" and err:
                checkout_err.append(
                    {
                        "span_id": span["spanID"],
                        "operation": span["operationName"],
                        "status": {
                            k: tags[k]
                            for k in (
                                "otel.status_code",
                                "otel.status_description",
                                "rpc.grpc.status_code",
                                "error",
                            )
                            if k in tags
                        },
                    }
                )
            if err and svc != "checkout":
                failed_calls.append(
                    {
                        "service": svc,
                        "span_id": span["spanID"],
                        "operation": span["operationName"],
                        "status": {
                            k: tags[k]
                            for k in (
                                "otel.status_code",
                                "otel.status_description",
                                "rpc.grpc.status_code",
                                "error",
                            )
                            if k in tags
                        },
                    }
                )
        traces.append(
            {
                "trace_id": trace["traceID"],
                "span_count": len(trace["spans"]),
                "any_span_in_window": in_window,
                "checkout_error_spans": checkout_err,
                "failed_non_checkout_spans": failed_calls,
            }
        )
    distinct = {t["trace_id"] for t in traces}
    failing = [
        t
        for t in traces
        if t["checkout_error_spans"] and t["failed_non_checkout_spans"]
    ]
    failed_dep_services = sorted(
        {c["service"] for t in failing for c in t["failed_non_checkout_spans"]}
    )
    positive = {
        n: sum(1 for s in m["series"] if s["value"] > 0) for n, m in metrics.items()
    }
    result = {
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "seconds": seconds,
        },
        "observed_at": observed_at.isoformat(),
        "sources": {"prometheus": PROM, "jaeger": JAEGER},
        "expect": expect,
        "metrics": metrics,
        "positive_series_count": positive,
        "jaeger": {
            "query": {"service": "checkout", "limit": 500},
            "returned_traces": len(traces),
            "distinct_trace_ids": len(distinct),
            "traces_with_checkout_error_and_failed_dependency_call": len(failing),
            "failed_dependency_services": failed_dep_services,
            "traces": traces,
        },
        "precondition": {
            "control_window_ok": len(distinct) >= 2
            and positive["checkout_calls_by_span_status"] > 0,
            "fault_confirmed": len(failing) >= 2,
        },
    }
    with open(out, "w") as f:
        json.dump(result, f, indent=1)
    summary = {
        k: v
        for k, v in result.items()
        if k in ("window", "observed_at", "positive_series_count", "precondition")
    }
    summary["jaeger"] = {k: v for k, v in result["jaeger"].items() if k != "traces"}
    summary["error_calls_by_service"] = metrics["all_services_error_calls"]["series"]
    print(json.dumps(summary, indent=1))


main()
