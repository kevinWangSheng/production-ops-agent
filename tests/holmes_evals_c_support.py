"""Shared helpers for the #666 PR-C contract tests (C5 Loki / VictoriaLogs
questions in the HolmesGPT eval driver and the Holmes comparison runner).

Written from docs/tasks/2026-11-18-holmes-evals-666.md (section "PR-C 接口",
whole-day shift revision) without reading the PR-C implementation.

``FakeLoki`` is a small in-process stand-in for the Loki HTTP API the
engineer-side ``LokiAdmin`` talks to (``labels``/``label``, ``query_range``,
``push``). It records every request (path, query, headers, body size) so the
tests can assert what was sent. Times are nanoseconds since the epoch; a
``query_range`` returns entries with ``start <= ns < end`` in forward order,
``limit`` entries at most across all streams, as Loki does. Large synthetic
streams are computed, not stored, so 200 000+ entries stay cheap.

No real Loki, VictoriaLogs, kind, docker, model or network is used; every
HTTP client here bypasses proxies; credentials are low-entropy fake values.
"""

from __future__ import annotations

import gzip
import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Q100A = "100a_loki_historical_logs"
Q101 = "101_loki_historical_logs_pod_deleted"
Q102 = "102_loki_label_discovery"
Q102A = "102a_loki_logs_transparency"
Q102B = "102b_loki_multiple_pods"
Q259L = "259_loki_historical_logs_pod_deleted_docker"
Q259B = "259_wrong_cluster_logs_confusion"
Q282 = "282_loki_custom_stream_label"
Q257 = "257_victorialogs_log_search"
Q258 = "258_victorialogs_streams"
C5_LOKI = (Q100A, Q101, Q102, Q102A, Q102B, Q259L, Q282)
C5_VL = (Q257, Q258)
C5_ALL = C5_LOKI + C5_VL

# Upstream's own wording shape for 100a/101/259_loki (user_prompt is an
# allowed key; the anchor is "August 2, 2025 around 13:45 UTC").
PROMPT_FIXED = (
    "The payment-api pod in namespace {ns} had issues on August 2, 2025 "
    "around 13:45 UTC. What happened?"
)

LOKI_RO_USER = "loki-ro-fake-user"
LOKI_RO_PW = "loki-ro-fake-value-11"
VL_RO_USER = "vl-ro-fake-user"
VL_RO_PW = "vl-ro-fake-value-12"


def ns_of(moment: datetime) -> int:
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + (
        delta.microseconds * 1000
    )


def at_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_time(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    if re.fullmatch(r"\d+", value):
        n = int(value)
        if len(value) >= 16:
            return n
        if len(value) >= 13:
            return n * 1_000_000
        return n * 1_000_000_000
    if re.fullmatch(r"\d+\.\d*", value):
        return int(float(value) * 1e9)
    text = value.replace("Z", "+00:00")
    return ns_of(datetime.fromisoformat(text))


_SELECTOR = re.compile(
    r'^\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>=~|=)\s*"(?P<value>[^"]*)"\s*\}$'
)


@dataclass
class _Stream:
    labels: dict[str, str]
    entries: list[tuple[int, str]] = field(default_factory=list)
    # Synthetic: entry i is (base + i * step, f"{prefix}{i}"), never stored.
    synthetic: tuple[int, int, int, str] | None = None

    def key(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.labels.items()))

    def window(self, start: int, end: int, limit: int) -> list[tuple[int, str]]:
        if self.synthetic is not None:
            base, count, step, prefix = self.synthetic
            first = max(0, -(-(start - base) // step))
            out = []
            i = first
            while i < count and len(out) < limit:
                ns = base + i * step
                if ns >= end:
                    break
                out.append((ns, f"{prefix}{i}"))
                i += 1
            return out
        rows = sorted(self.entries)
        lo = bisect_left(rows, (start, ""))
        out = []
        for ns, line in rows[lo:]:
            if ns >= end or len(out) >= limit:
                break
            out.append((ns, line))
        return out


class FakeLoki:
    """Loki's HTTP API, in process, recording every request."""

    def __init__(self) -> None:
        self.streams: dict[tuple[tuple[str, str], ...], _Stream] = {}
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        #: Like Loki's ``max_query_length`` (2.9 default 30d1h): a labels or
        #: query_range request spanning more than this gets HTTP 400.
        self.max_query_ns: int | None = None

    # -- direct seeding (the stand-in for a question's before_test)
    def add(self, labels: Mapping[str, str], rows: Sequence[tuple[int, str]]) -> None:
        with self.lock:
            s = self._stream(labels)
            have = set(s.entries)
            for row in rows:
                if row not in have:
                    s.entries.append(row)
                    have.add(row)

    def add_synthetic(
        self,
        labels: Mapping[str, str],
        base: int,
        count: int,
        step: int = 1,
        prefix: str = "s",
    ) -> None:
        with self.lock:
            s = _Stream(dict(labels), synthetic=(base, count, step, prefix))
            self.streams[s.key()] = s

    def _stream(self, labels: Mapping[str, str]) -> _Stream:
        key = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
        if key not in self.streams:
            self.streams[key] = _Stream(dict(key))
        return self.streams[key]

    def entries(self, labels: Mapping[str, str]) -> list[tuple[int, str]]:
        key = tuple(sorted(labels.items()))
        s = self.streams.get(key)
        return [] if s is None else sorted(s.entries)

    def all_entries(self) -> list[tuple[dict[str, str], int, str]]:
        return [
            (dict(s.labels), ns, line)
            for s in self.streams.values()
            for ns, line in sorted(s.entries)
        ]

    # -- HTTP
    def handle(
        self, method: str, path: str, query: Mapping[str, list[str]],
        headers: Mapping[str, str], body: bytes,
    ) -> tuple[int, Any]:  # fmt: skip
        self.requests.append(
            {
                "method": method,
                "path": path,
                "query": {k: list(v) for k, v in query.items()},
                "headers": {k.lower(): v for k, v in headers.items()},
                "body_bytes": len(body),
            }
        )
        if method == "GET" and self.max_query_ns is not None and path in (
            "/loki/api/v1/labels", "/loki/api/v1/label", "/loki/api/v1/query_range",
        ):  # fmt: skip
            span = _parse_time((query.get("end") or [""])[0], 2**63 - 1) - _parse_time(
                (query.get("start") or [""])[0], 0
            )
            if span > self.max_query_ns:
                return 400, {"error": "query length exceeds limit"}
        if method == "GET" and path in ("/loki/api/v1/labels", "/loki/api/v1/label"):
            names = sorted({n for s in self.streams.values() for n in s.labels})
            return 200, {"status": "success", "data": names}
        if method == "GET" and path == "/loki/api/v1/query_range":
            return self._query_range(query)
        if method == "GET" and path == "/loki/api/v1/series":
            return self._series(query)
        if method == "POST" and path == "/loki/api/v1/push":
            return self._push(headers, body)
        if method == "GET" and path == "/ready":
            return 200, "ready"
        return 404, {"error": "not found"}

    def _query_range(self, query: Mapping[str, list[str]]) -> tuple[int, Any]:
        def one(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values else None

        m = _SELECTOR.match((one("query") or "").strip())
        if not m:
            return 400, {"error": "unsupported selector"}
        start = _parse_time(one("start"), 0)
        end = _parse_time(one("end"), 2**63 - 1)
        limit = int(one("limit") or 100)
        direction = one("direction") or "backward"
        if direction != "forward":
            return 400, {"error": "fake supports forward only"}
        name, op, value = m["name"], m["op"], m["value"]
        rows: list[tuple[int, str, tuple[tuple[str, str], ...]]] = []
        with self.lock:
            for s in self.streams.values():
                if name not in s.labels:
                    continue
                if op == "=" and s.labels[name] != value:
                    continue
                if op == "=~" and not re.fullmatch(value, s.labels[name]):
                    continue
                for ns, line in s.window(start, end, limit):
                    rows.append((ns, line, s.key()))
        rows.sort(key=lambda r: (r[0], r[2], r[1]))
        rows = rows[:limit]
        grouped: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
        for ns, line, key in rows:
            grouped.setdefault(key, []).append([str(ns), line])
        return 200, {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {"stream": dict(k), "values": v} for k, v in grouped.items()
                ],
            },
        }

    def _series(self, query: Mapping[str, list[str]]) -> tuple[int, Any]:
        start = _parse_time((query.get("start") or [""])[0], 0)
        end = _parse_time((query.get("end") or [""])[0], 2**63 - 1)
        matches = [_SELECTOR.match(m.strip()) for m in query.get("match[]") or []]
        if not matches or not all(matches):
            return 400, {"error": "unsupported match[]"}
        out = []
        with self.lock:
            for s in self.streams.values():
                hit = any(
                    m["name"] in s.labels
                    and (
                        s.labels[m["name"]] == m["value"]
                        if m["op"] == "="
                        else re.fullmatch(m["value"], s.labels[m["name"]])
                    )
                    for m in matches
                    if m
                )
                if hit and s.window(start, end, 1):
                    out.append(dict(s.labels))
        return 200, {"status": "success", "data": out}

    def _push(self, headers: Mapping[str, str], body: bytes) -> tuple[int, Any]:
        lower = {k.lower(): v for k, v in headers.items()}
        if lower.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        if "json" not in lower.get("content-type", ""):
            return 415, {"error": "fake accepts JSON pushes only"}
        doc = json.loads(body)
        for stream in doc.get("streams", []):
            rows = [(int(v[0]), str(v[1])) for v in stream.get("values", [])]
            self.add(stream.get("stream", {}), rows)
        self.requests[-1]["values"] = sum(
            len(s.get("values", [])) for s in doc.get("streams", [])
        )
        return 204, None


@contextmanager
def served_loki(store: FakeLoki, port: int = 0) -> Iterator[str]:
    """``store`` on 127.0.0.1 (``port`` 0 = ephemeral); yields the base URL."""

    class Handler(BaseHTTPRequestHandler):
        def _do(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = store.handle(
                self.command,
                parts.path,
                urllib.parse.parse_qs(parts.query, keep_blank_values=True),
                dict(self.headers.items()),
                body,
            )
            raw = b"" if payload is None else (
                payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            )  # fmt: skip
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = _do

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {})
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def find(name: str, *modules: str) -> Any:
    """``name`` from the first of ``modules`` (``scripts.holmes_evals.<m>``)
    that defines it. The interface lists some functions under a section
    rather than a module (reported as a spec gap); the tests accept either."""
    import importlib

    for m in modules:
        try:
            module = importlib.import_module(f"scripts.holmes_evals.{m}")
        except ModuleNotFoundError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"{name} not found in {modules}")
