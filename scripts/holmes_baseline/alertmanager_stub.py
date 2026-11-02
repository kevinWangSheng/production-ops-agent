"""Read-only Alertmanager API stand-in for F14 step 3 (#385, R5).

``holmes investigate alertmanager --alertmanager-file`` fails at a0009195
before any model call (``plugin.py`` passes the file ``Path`` as the
``Issue.source_instance_id`` string), so the raw-alert case uses the other
upstream entry, ``--alertmanager-url``, against this stub on the internal
network. It answers ``GET /api/v2/alerts`` with the JSON file it was given
and logs each request as one JSON line; everything else is 404.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALERTS: bytes = b"[]"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        found = self.path.split("?", 1)[0] == "/api/v2/alerts"
        print(json.dumps({"path": self.path, "served": found}), flush=True)
        payload = ALERTS if found else b'{"error":"not found"}'
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> int:
    global ALERTS
    port, path = (sys.argv[1:] if argv is None else argv)[:2]
    with open(path, "rb") as handle:
        ALERTS = handle.read()
    json.loads(ALERTS)
    ThreadingHTTPServer(("0.0.0.0", int(port)), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
