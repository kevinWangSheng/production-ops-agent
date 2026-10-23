"""Loopback stand-in for the OTel Demo Prometheus + Jaeger read APIs (#309 evidence).

Serves ``/api/v1/query_range`` and ``<prefix>/api/traces`` on one loopback
port with synthetic checkout/payment data inside whatever window the request
names, after a fixed per-request latency, so a real-model Run under the
``otel-demo`` tool profile can be driven without the kind lab (another
session owns it). It is a test double: no credentials, no real telemetry,
and the evidence README says so.

    .venv/bin/python scripts/m1_otel_stub.py --port 29317 --latency-seconds 1.5

Point the profile at it with ``OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:29317``
and ``OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:29317/jaeger/ui``.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SERVICES = ("frontend", "checkout", "payment")


def prometheus_body(query: dict[str, list[str]]) -> bytes:
    start = float(query["start"][0])
    end = min(float(query["end"][0]), time.time())
    step = max(1, int(float(query.get("step", ["30"])[0])))
    series = []
    for offset, (service, status, base) in enumerate(
        (
            ("checkout", "STATUS_CODE_UNSET", 30.0),
            ("checkout", "STATUS_CODE_ERROR", 6.0),
            ("payment", "STATUS_CODE_ERROR", 9.0),
        )
    ):
        values = []
        t = start
        k = 0
        while t <= end:
            values.append([round(t, 3), f"{base + (k % 5) * (1 + offset):.4f}"])
            t += step
            k += 1
        series.append(
            {
                "metric": {"service_name": service, "status_code": status},
                "values": values,
            }
        )
    return json.dumps(
        {"status": "success", "data": {"resultType": "matrix", "result": series}}
    ).encode("utf-8")


def jaeger_body(query: dict[str, list[str]]) -> bytes:
    service = query.get("service", ["checkout"])[0]
    start = int(query["start"][0]) + 10_000_000
    trace_id = "30930930930930930930930930930901"
    processes = {
        f"p{i}": {"serviceName": n, "tags": []} for i, n in enumerate(SERVICES)
    }
    spans = []
    for i, name in enumerate(SERVICES):
        tags = [{"key": "span.kind", "type": "string", "value": "server"}]
        if name == "payment":
            tags += [
                {"key": "otel.status_code", "type": "string", "value": "ERROR"},
                {"key": "error", "type": "bool", "value": True},
            ]
        spans.append(
            {
                "traceID": trace_id,
                "spanID": f"30930900000000{i:02d}",
                "operationName": f"{name}/handle",
                "references": []
                if i == 0
                else [
                    {
                        "refType": "CHILD_OF",
                        "traceID": trace_id,
                        "spanID": "3093090000000000",
                    }
                ],
                "startTime": start + i * 1_000,
                "duration": 5_000 - i * 1_000,
                "tags": tags,
                "logs": [],
                "processID": f"p{i}",
                "warnings": None,
            }
        )
    body = {
        "data": [{"traceID": trace_id, "spans": spans, "processes": processes}]
        if service in SERVICES
        else [],
        "total": 0,
        "limit": 0,
        "offset": 0,
        "errors": None,
    }
    return json.dumps(body).encode("utf-8")


def handler(latency: float) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            time.sleep(latency)
            if parts.path.endswith("/api/v1/query_range"):
                body = prometheus_body(query)
            elif parts.path.endswith("/api/traces"):
                body = jaeger_body(query)
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{time.time():.3f} {self.path.split('?')[0]}", flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=29317)
    parser.add_argument("--latency-seconds", type=float, default=1.5)
    args = parser.parse_args()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler(args.latency_seconds)
    )
    print(f"stub listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
