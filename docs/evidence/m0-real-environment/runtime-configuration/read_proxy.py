"""One-experiment, read-only API for the isolated OTel Demo target.

No arbitrary URL, backend path, shell, SQL, or mutation is accepted. This is a
lab boundary, not the product Tool Gateway or a claim of production security.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = "m0-otel-20260909"
SERVICES = (
    "accounting ad cart checkout currency email fraud-detection "
    "frontend frontend-proxy image-provider kafka load-generator payment "
    "product-catalog quote recommendation shipping"
).split()
BASES = {
    "metrics": os.getenv("PROMETHEUS_URL", "http://127.0.0.1:19090"),
    "traces": os.getenv("JAEGER_URL", "http://127.0.0.1:16686"),
    "logs": os.getenv("OPENSEARCH_URL", "http://127.0.0.1:19200"),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
MAX_BYTES = 1024 * 1024
DEADLINE = 1789060470  # 2026-09-10T17:14:30Z, original round deadline


class Handler(BaseHTTPRequestHandler):
    calls = 0
    budget_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass  # No request content or authorization data in transport logs.

    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.reply(405, {"error": "READ_ONLY"})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        prefix = "/integrations/" + TARGET + "/"
        if not parsed.path.startswith(prefix):
            return self.reply(403, {"error": "TARGET_DENIED"})
        if len(self.path) > 4096 or time.time() > DEADLINE:
            return self.reply(403, {"error": "QUERY_BUDGET_DENIED"})
        route = parsed.path[len(prefix) :]
        try:
            params = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            return self.reply(400, {"error": "QUERY_DENIED"})
        if route == "services" and not params:
            return self.reply(200, {"integration_id": TARGET, "services": SERVICES})
        try:
            allowed = (
                {"start", "end", "query"}
                if route == "metrics"
                else {"start", "end", "service"}
            )
            if (
                route not in BASES
                or set(params) != allowed
                or any(len(v) != 1 for v in params.values())
            ):
                raise ValueError("PARAMETERS_DENIED")
            start, end = float(params["start"][0]), float(params["end"][0])
            if not (time.time() - 3600 <= start < end <= time.time() + 5):
                raise ValueError("TIME_WINDOW_DENIED")
            body = None
            if route == "metrics":
                if end - start < 300:
                    raise ValueError("LOOKBACK_OUTSIDE_WINDOW")
                query = params["query"][0]
                # Do not permit selectors that escape the declared absolute window.
                ranges = re.findall(r"\[([^]]+)\]", query)
                durations = {"s": 1, "m": 60, "h": 3600}
                bad_range = any(
                    not re.fullmatch(r"[1-9][0-9]*[smh]", value)
                    or int(value[:-1]) * durations[value[-1]] > end - start
                    for value in ranges
                )
                if (
                    not query
                    or len(query) > 2000
                    or "@" in query
                    or re.search(r"\boffset\b", query)
                    or bad_range
                ):
                    raise ValueError("QUERY_DENIED")
                path = "/api/v1/query?" + urllib.parse.urlencode(
                    {"query": query, "time": end, "timeout": "5s"}
                )
            else:
                service = params["service"][0]
                if service not in SERVICES:
                    raise ValueError("SERVICE_DENIED")
                if route == "traces":
                    path = "/jaeger/ui/api/traces?" + urllib.parse.urlencode(
                        {
                            "service": service,
                            "start": int(start * 1e6),
                            "end": int(end * 1e6),
                            "limit": 20,
                        }
                    )
                else:
                    path = "/otel/_search"
                    body = json.dumps(
                        {
                            "size": 20,
                            "sort": [{"@timestamp": "desc"}],
                            "query": {
                                "bool": {
                                    "filter": [
                                        {
                                            "term": {
                                                "resource.service.name.keyword": service
                                            }
                                        },
                                        {
                                            "range": {
                                                "@timestamp": {
                                                    "gte": int(start * 1000),
                                                    "lte": int(end * 1000),
                                                    "format": "epoch_millis",
                                                }
                                            }
                                        },
                                    ]
                                }
                            },
                        }
                    ).encode()
            with Handler.budget_lock:
                if Handler.calls >= 200:
                    return self.reply(429, {"error": "QUERY_BUDGET_EXHAUSTED"})
                Handler.calls += 1
            url = BASES[route] + path
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with OPENER.open(request, timeout=10) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return self.reply(
                    413, {"error": "RESULT_TOO_LARGE", "incomplete": True}
                )
            self.reply(
                200,
                {
                    "integration_id": TARGET,
                    "source": route,
                    "query": params,
                    "collected_at": time.time(),
                    "data": json.loads(raw),
                },
            )
        except (ValueError, KeyError):
            self.reply(400, {"error": "QUERY_DENIED"})
        except urllib.error.HTTPError as error:
            raw_error = error.read(8192)
            try:
                details = json.loads(raw_error)
            except (ValueError, UnicodeDecodeError):
                details = {"message": "Non-JSON backend error omitted"}
            self.reply(
                400 if error.code in (400, 422) else 502,
                {
                    "error": "SOURCE_QUERY_REJECTED"
                    if error.code in (400, 422)
                    else "SOURCE_REQUEST_FAILED",
                    "source_status": error.code,
                    "source_error": details,
                    "incomplete": True,
                },
            )
        except (urllib.error.URLError, TimeoutError):
            self.reply(502, {"error": "SOURCE_UNAVAILABLE", "incomplete": True})


if __name__ == "__main__":
    ThreadingHTTPServer(
        (os.getenv("BIND_ADDRESS", "127.0.0.1"), 18081), Handler
    ).serve_forever()
