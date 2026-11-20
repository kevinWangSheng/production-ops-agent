"""Stand-in SREGym conductor for contract tests (#667 PR-A).

Follows ``conductor_api.py`` (ac60c642): ``GET /status``, ``GET /get_app``,
``POST /submit``. Stages ``setup -> diagnosis -> tearing_down -> done`` are
advanced by the test with :meth:`FakeConductor.advance`, except that an
accepted diagnosis submission moves the stage to ``tearing_down``. Every
request is recorded (method, path, JSON body).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STAGES: tuple[str, ...] = ("setup", "diagnosis", "tearing_down", "done")
_SUBMITTABLE = frozenset({"diagnosis", "mitigation"})


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    body: Any


class FakeConductor:
    def __init__(
        self,
        app_info: Mapping[str, Any],
        stages: Sequence[str] = ("diagnosis",),
        *,
        started: bool = True,
    ) -> None:
        self.app_info = dict(app_info)
        self.stage_names = list(stages)
        self.started = started
        self.stage: str | None = "setup"
        self.remaining_s: float | None = 1800.0
        self.requests: list[Recorded] = []
        self.accepted: list[str] = []
        self._fail: list[int] = []
        self._lock = threading.Lock()

    def advance(self, stage: str | None) -> None:
        with self._lock:
            self.stage = stage

    def fail_next(self, status: int) -> None:
        """The next request (any endpoint) answers ``status`` instead."""
        with self._lock:
            self._fail.append(status)

    # -- handling ------------------------------------------------------------------------------

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parsed = "<invalid json>"
        with self._lock:
            self.requests.append(Recorded(method, path, parsed))
            if self._fail:
                return self._fail.pop(0), {"detail": "injected failure"}
            if method == "GET" and path == "/status":
                return self._status()
            if method == "GET" and path == "/get_app":
                return self._get_app()
            if method == "POST" and path == "/submit":
                return self._submit(parsed)
            return 404, {"detail": "Not Found"}

    def _status(self) -> tuple[int, Any]:
        if not self.started:
            return 400, {"detail": "No problem has been started"}
        out: dict[str, Any] = {"stage": self.stage}
        if self.remaining_s is not None:
            out["agent_remaining_seconds"] = self.remaining_s
        return 200, out

    def _get_app(self) -> tuple[int, Any]:
        if not self.started:
            return 400, {"detail": "No problem has been started"}
        return 200, {**self.app_info, "stages": list(self.stage_names)}

    def _submit(self, body: Any) -> tuple[int, Any]:
        if not self.started:
            return 400, {"detail": "No problem has been started"}
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("solution"), str)
            or body.get("stage") not in (None, *_SUBMITTABLE)
        ):
            return 422, {"detail": "validation error"}
        requested = body.get("stage") or self.stage
        if self.stage in ("setup", None):
            return 409, {"detail": "Cannot submit before an agent stage is ready"}
        if self.stage in ("tearing_down", "done"):
            return 409, {"detail": "This benchmark attempt is already closed."}
        if self.stage not in _SUBMITTABLE:
            return 400, {"detail": f"Cannot submit at stage: {self.stage!r}"}
        if requested != self.stage:
            return 409, {"detail": "Stage mismatch"}
        self.accepted.append(body["solution"])
        stage = self.stage
        self.stage = "tearing_down"
        return 200, {"status": "200", "message": "accepted", "stage": stage}


def serve(
    conductor: FakeConductor, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """Start serving on a daemon thread; ``server.server_address`` has the port."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("fake conductor binds loopback only")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = conductor.handle(method, self.path, body)
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self._reply("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._reply("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
