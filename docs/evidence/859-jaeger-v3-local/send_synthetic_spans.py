import json
import time
import urllib.request

now = time.time_ns()
T1 = "4bf92f3577b34da6a3ce929d0e0e4736"
T2 = "00000000000000000000000000abc123"


def attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    return {"key": k, "value": {"stringValue": v}}


def span(
    tid, sid, parent, name, start_ms, dur_ms, kind=2, status=None, attrs=(), events=()
):
    s = {
        "traceId": tid,
        "spanId": sid,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(now - start_ms * 1_000_000),
        "endTimeUnixNano": str(now - start_ms * 1_000_000 + dur_ms * 1_000_000),
        "attributes": [attr(k, v) for k, v in attrs],
    }
    if parent:
        s["parentSpanId"] = parent
    if status:
        s["status"] = status
    if events:
        s["events"] = list(events)
    return s


body = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [
                    attr("service.name", "frontend"),
                    attr("service.version", "1.2.3"),
                ]
            },
            "scopeSpans": [
                {
                    "scope": {"name": "synthetic"},
                    "spans": [
                        span(
                            T1,
                            "00f067aa0ba902b7",
                            None,
                            "GET /api/cart",
                            5000,
                            120,
                            kind=2,
                            attrs=[("http.status_code", 500), ("http.method", "GET")],
                        ),
                        span(
                            T2,
                            "1111111111111111",
                            None,
                            "GET /health",
                            3000,
                            5,
                            kind=2,
                            status={"code": 1},
                        ),
                    ],
                }
            ],
        },
        {
            "resource": {"attributes": [attr("service.name", "cart")]},
            "scopeSpans": [
                {
                    "scope": {"name": "synthetic"},
                    "spans": [
                        span(
                            T1,
                            "b7ad6b7169203331",
                            "00f067aa0ba902b7",
                            "CartService/GetCart",
                            4990,
                            100,
                            kind=2,
                            status={"code": 2, "message": "redis timeout"},
                            attrs=[("rpc.grpc.status_code", 14), ("app.retry", True)],
                            events=[
                                {
                                    "timeUnixNano": str(now - 4950 * 1_000_000),
                                    "name": "exception",
                                    "attributes": [
                                        attr("exception.type", "TimeoutError"),
                                        attr(
                                            "exception.message",
                                            "connect to redis:6379 timed out",
                                        ),
                                    ],
                                }
                            ],
                        ),
                        span(
                            T1,
                            "c7ad6b7169203332",
                            "b7ad6b7169203331",
                            "redis GET",
                            4980,
                            80,
                            kind=3,
                        ),
                    ],
                }
            ],
        },
    ]
}
req = urllib.request.Request(
    "http://127.0.0.1:4318/v1/traces",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(urllib.request.urlopen(req).status)
print(now)
