"""Data-driven stand-in Loki and VictoriaLogs for the local run (#666 PR-C).

One process plays the question's backend *and* the read-only gateway in front
of it (C5-U1), on two ``127.0.0.1`` ports:

* the **backend port** has no authentication and accepts writes, like the
  fixtures' own Loki / VictoriaLogs (the evaluation's engineer side: setup,
  the Loki shift);
* the **gateway port** requires the product's Basic account (else 401) and
  forwards only ``GET`` of the backend's read paths -- Loki
  ``/loki/api/v1/query_range``, VictoriaLogs ``/select/logsql/{query,
  streams,field_names,field_values,hits}`` (as
  ``scripts/loki_readonly_gateway.py``); everything else is 403.

Query semantics are a small documented subset; anything else is a 400, never
an approximation:

* LogQL: one stream selector (``=``, ``!=``, ``=~``, ``!~``) followed by line
  filters (``|=``, ``!=``, ``|~``, ``!~`` with a double-quoted string).
* LogsQL: space-separated AND of ``*``, a stream filter ``{k="v",...}``,
  ``field:value`` / ``field:"value"`` (exact), ``_time:<anything>`` (ignored:
  start/end already bound the query) and bare or quoted words (substring of
  ``_msg``, case-insensitive).

Usage::

    python -m scripts.holmes_evals.fake_logs --backend loki \\
        --backend-port A --gateway-port B --account-file <user:password, 600>

The first stdout line is ``{"backend_url": ..., "gateway_url": ...}``.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
LOKI_READ_PATHS = ("/loki/api/v1/query_range",)
VL_READ_PATHS = (
    "/select/logsql/query",
    "/select/logsql/streams",
    "/select/logsql/field_names",
    "/select/logsql/field_values",
    "/select/logsql/hits",
)

Answer = tuple[int, Any]


def _ns(value: str | None, default: int) -> int:
    """Nanoseconds from an epoch (s/ns) or RFC3339 value."""
    if value is None or value == "":
        return default
    if re.fullmatch(r"-?\d+", value):
        number = int(value)
        return number if abs(number) > 10**14 else number * 1_000_000_000
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _BadRequest(f"invalid time {value!r}") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = moment - epoch
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def _now_ns() -> int:
    return _ns(datetime.now(UTC).isoformat(), 0)


class _BadRequest(Exception):
    pass


# -- Loki ---------------------------------------------------------------------------------------

_MATCHER = re.compile(
    r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*'
)
_FILTER = re.compile(r'\s*(\|=|!=|\|~|!~)\s*"((?:[^"\\]|\\.)*)"')


def _unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _matchers(body: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    rest = body.strip()
    while rest:
        m = _MATCHER.match(rest)
        if not m:
            raise _BadRequest("parse error: stream selector")
        out.append((m[1], m[2], _unquote(m[3])))
        rest = rest[m.end() :]
        if rest.startswith(","):
            rest = rest[1:]
        elif rest:
            raise _BadRequest("parse error: stream selector")
    if not out:
        raise _BadRequest("parse error: empty selector")
    return out


def _non_empty(op: str, value: str) -> bool:
    """A matcher that cannot match the empty label value (Loki requires one)."""
    if op == "=":
        return value != ""
    if op == "=~":
        return re.fullmatch(value, "") is None
    return False


def _label_ok(labels: Mapping[str, str], op: str, name: str, value: str) -> bool:
    have = labels.get(name, "")
    if op == "=":
        return have == value
    if op == "!=":
        return have != value
    hit = re.fullmatch(value, have) is not None
    return hit if op == "=~" else not hit


@dataclass
class FakeLoki:
    streams: dict[tuple[tuple[str, str], ...], list[tuple[int, str]]] = field(
        default_factory=dict
    )
    lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, body: bytes) -> Answer:
        try:
            doc = json.loads(body or b"{}")
            for stream in doc["streams"]:
                key = tuple(
                    sorted((str(k), str(v)) for k, v in stream["stream"].items())
                )
                rows = [(int(ts), str(line)) for ts, line in stream["values"]]
                with self.lock:
                    self.streams.setdefault(key, []).extend(rows)
        except (ValueError, KeyError, TypeError):
            return 400, "error parsing push request"
        return 204, ""

    def _select(
        self, query: str
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
        query = query.strip()
        if not query.startswith("{") or "}" not in query:
            raise _BadRequest("parse error: only log stream queries are supported")
        end = query.index("}")
        matchers = _matchers(query[1:end])
        if not any(_non_empty(op, v) for _, op, v in matchers):
            raise _BadRequest("queries require at least one non-empty matcher")
        filters: list[tuple[str, str]] = []
        rest = query[end + 1 :]
        while rest.strip():
            m = _FILTER.match(rest)
            if not m:
                raise _BadRequest("parse error: unsupported pipeline in fake Loki")
            filters.append((m[1], _unquote(m[2])))
            rest = rest[m.end() :]
        return matchers, filters

    def query_range(self, q: Mapping[str, list[str]]) -> Answer:
        def one(name: str) -> str | None:
            return q.get(name, [None])[0]

        try:
            matchers, filters = self._select(one("query") or "")
            end = _ns(one("end"), _now_ns())
            start = _ns(one("start"), end - 3600 * 10**9)
            limit = int(one("limit") or 100)
        except (_BadRequest, ValueError) as error:
            return 400, str(error)
        backward = (one("direction") or "backward") != "forward"
        rows: list[tuple[int, tuple[tuple[str, str], ...], str]] = []
        with self.lock:
            items = [(k, list(v)) for k, v in self.streams.items()]
        for key, values in items:
            labels = dict(key)
            if not all(_label_ok(labels, op, n, v) for n, op, v in matchers):
                continue
            for ts, line in values:
                if not start <= ts < end:
                    continue
                keep = True
                for op, needle in filters:
                    if op == "|=":
                        keep = needle in line
                    elif op == "!=":
                        keep = needle not in line
                    else:
                        found = re.search(needle, line) is not None
                        keep = found if op == "|~" else not found
                    if not keep:
                        break
                if keep:
                    rows.append((ts, key, line))
        rows.sort(key=lambda r: r[0], reverse=backward)
        rows = rows[:limit]
        grouped: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
        for ts, key, line in rows:
            grouped.setdefault(key, []).append([str(ts), line])
        result = [{"stream": dict(k), "values": v} for k, v in grouped.items()]
        return 200, {
            "status": "success",
            "data": {"resultType": "streams", "result": result, "stats": {}},
        }

    def series(self, q: Mapping[str, list[str]]) -> Answer:
        """Label sets of streams matching any ``match[]`` with an entry in
        ``[start, end)`` (the shift lists streams before reading them)."""
        try:
            selectors = [self._select(m)[0] for m in q.get("match[]") or []]
            end = _ns(q.get("end", [None])[0], _now_ns())
            start = _ns(q.get("start", [None])[0], end - 3600 * 10**9)
        except (_BadRequest, ValueError) as error:
            return 400, str(error)
        if not selectors:
            return 400, "match[] is required"
        with self.lock:
            items = [(k, list(v)) for k, v in self.streams.items()]
        out = []
        for key, values in items:
            labels = dict(key)
            if not any(
                all(_label_ok(labels, op, n, v) for n, op, v in matchers)
                for matchers in selectors
            ):
                continue
            if any(start <= ts < end for ts, _ in values):
                out.append(labels)
        return 200, {"status": "success", "data": out}

    def labels(self, q: Mapping[str, list[str]]) -> Answer:
        with self.lock:
            names = sorted({k for key in self.streams for k, _ in key})
        return 200, {"status": "success", "data": names}

    def handle(
        self, method: str, path: str, q: Mapping[str, list[str]], body: bytes
    ) -> Answer:
        if path == "/ready" and method == "GET":
            return 200, "ready"
        if path == "/loki/api/v1/push" and method == "POST":
            return self.push(body)
        if path == "/loki/api/v1/query_range" and method == "GET":
            return self.query_range(q)
        if path == "/loki/api/v1/labels" and method == "GET":
            return self.labels(q)
        if path == "/loki/api/v1/series" and method == "GET":
            return self.series(q)
        return 404, "404 page not found"


# -- VictoriaLogs -------------------------------------------------------------------------------

_TOKEN = re.compile(
    r'\s*(\{[^}]*\}|[A-Za-z_][\w.]*:"(?:[^"\\]|\\.)*"|"(?:[^"\\]|\\.)*"|\S+)'
)


@dataclass
class FakeVictoriaLogs:
    rows: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def insert(self, q: Mapping[str, list[str]], body: bytes) -> Answer:
        stream_fields = [f for f in (q.get("_stream_fields", [""])[0]).split(",") if f]
        msg_field = q.get("_msg_field", ["_msg"])[0]
        time_field = q.get("_time_field", ["_time"])[0]
        added = []
        for raw in body.decode("utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                return 400, "cannot parse json line"
            row: dict[str, Any] = {
                k: str(v) for k, v in doc.items() if k not in (msg_field, time_field)
            }
            row["_msg"] = str(doc.get(msg_field, ""))
            try:
                row["_time_ns"] = _ns(str(doc.get(time_field) or ""), _now_ns())
            except _BadRequest as error:
                return 400, str(error)
            row["_stream"] = (
                "{"
                + ",".join(f'{f}="{doc[f]}"' for f in stream_fields if f in doc)
                + "}"
            )
            added.append(row)
        with self.lock:
            self.rows.extend(added)
        return 200, ""

    @staticmethod
    def _predicate(query: str) -> Callable[[Mapping[str, Any]], bool]:
        tokens = [m.group(1) for m in _TOKEN.finditer(query or "*")]
        checks: list[Callable[[Mapping[str, Any]], bool]] = []

        def stream(
            pairs: list[tuple[str, str, str]],
        ) -> Callable[[Mapping[str, Any]], bool]:
            return lambda r: all(_label_ok(r, op, n, v) for n, op, v in pairs)

        def exact(name: str, value: str) -> Callable[[Mapping[str, Any]], bool]:
            return lambda r: str(r.get(name, "")).lower() == value.lower()

        def word(needle: str) -> Callable[[Mapping[str, Any]], bool]:
            return lambda r: needle in str(r["_msg"]).lower()

        for token in tokens:
            if token == "*" or token.startswith("_time:"):
                continue
            if token.startswith("{"):
                checks.append(stream(_matchers(token[1:-1])))
            elif re.match(r"[A-Za-z_][\w.]*:", token):
                name, _, value = token.partition(":")
                value = _unquote(value[1:-1]) if value.startswith('"') else value
                checks.append(exact(name, value))
            elif token.startswith('"'):
                checks.append(word(_unquote(token[1:-1]).lower()))
            elif re.fullmatch(r"[\w.-]+", token):
                checks.append(word(token.lower()))
            else:
                raise _BadRequest(f"fake VictoriaLogs: unsupported LogsQL {token!r}")
        return lambda row: all(c(row) for c in checks)

    def select(self, kind: str, q: Mapping[str, list[str]]) -> Answer:
        def one(name: str, default: str | None = None) -> str | None:
            return q.get(name, [default])[0]

        try:
            keep = self._predicate(one("query", "*") or "*")
            end = _ns(one("end"), _now_ns())
            start = _ns(one("start"), 0)
            limit = int(one("limit") or 1000)
        except (_BadRequest, ValueError) as error:
            return 400, str(error)
        with self.lock:
            rows = [r for r in self.rows if start <= r["_time_ns"] < end and keep(r)]
        rows.sort(key=lambda r: r["_time_ns"], reverse=True)

        def public(row: Mapping[str, Any]) -> dict[str, Any]:
            out = {k: v for k, v in row.items() if k != "_time_ns"}
            moment = datetime.fromtimestamp(row["_time_ns"] / 1e9, UTC)
            out["_time"] = moment.isoformat().replace("+00:00", "Z")
            return out

        if kind == "query":
            return 200, "".join(json.dumps(public(r)) + "\n" for r in rows[:limit])

        def counted(values: Sequence[str]) -> dict[str, Any]:
            hits: dict[str, int] = {}
            for v in values:
                hits[v] = hits.get(v, 0) + 1
            ordered = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
            return {"values": [{"value": v, "hits": n} for v, n in ordered]}

        if kind == "streams":
            return 200, counted([r["_stream"] for r in rows])
        if kind == "field_names":
            names = [k for r in rows for k in public(r)]
            return 200, counted(names)
        if kind == "field_values":
            name = one("field")
            if not name:
                return 400, "missing field"
            return 200, counted([str(public(r).get(name, "")) for r in rows])
        # hits: one series over the whole window (the fake does not bucket).
        return 200, {
            "hits": [
                {
                    "fields": {},
                    "timestamps": [
                        datetime.fromtimestamp(start / 1e9, UTC)
                        .isoformat()
                        .replace("+00:00", "Z")
                    ],
                    "values": [len(rows)],
                    "total": len(rows),
                }
            ]
            if rows
            else []
        }

    def handle(
        self, method: str, path: str, q: Mapping[str, list[str]], body: bytes
    ) -> Answer:
        if path == "/health" and method == "GET":
            return 200, "OK"
        if path == "/insert/jsonline" and method == "POST":
            return self.insert(q, body)
        if path in VL_READ_PATHS and method in ("GET", "POST"):
            return self.select(path.rsplit("/", 1)[1], q)
        return 400, f"unsupported path {path}"


# -- servers ------------------------------------------------------------------------------------


def _authorized(headers: Mapping[str, str], account: tuple[str, str]) -> bool:
    value = headers.get("Authorization") or headers.get("authorization") or ""
    if not value.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(value[6:]).decode("utf-8").partition(":")
    except ValueError:
        return False
    return (user, password) == account


def gateway_handle(
    store: FakeLoki | FakeVictoriaLogs,
    account: tuple[str, str],
    method: str,
    path: str,
    q: Mapping[str, list[str]],
    headers: Mapping[str, str],
) -> Answer:
    """The read-only gateway: login first, then only GET of the read paths
    (no body forwarded)."""
    if not _authorized(headers, account):
        return 401, "401 Authorization Required"
    reads = LOKI_READ_PATHS if isinstance(store, FakeLoki) else VL_READ_PATHS
    if method != "GET" or path not in reads:
        return 403, "403 Forbidden"
    return store.handle("GET", path, q, b"")


def serve(handle: Any, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, keep_blank_values=True)
            if (
                body
                and self.command == "POST"
                and "form" in (self.headers.get("Content-Type") or "")
            ):
                query |= parse_qs(body.decode("utf-8"), keep_blank_values=True)
            status, answer = handle(
                self.command, parts.path, query, dict(self.headers.items()), body
            )
            if isinstance(answer, str):
                data, kind = answer.encode("utf-8"), "text/plain; charset=utf-8"
            else:
                data, kind = json.dumps(answer).encode("utf-8"), "application/json"
            self.send_response(status)
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="fake gateway"')
            if status != 204:
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if status != 204 and self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server


def start(
    backend: str, backend_port: int, gateway_port: int, account: tuple[str, str]
) -> tuple[FakeLoki | FakeVictoriaLogs, list[ThreadingHTTPServer]]:
    """Both servers running in daemon threads (for tests and the CLI)."""
    store: FakeLoki | FakeVictoriaLogs = (
        FakeLoki() if backend == "loki" else FakeVictoriaLogs()
    )
    direct = serve(lambda m, p, q, h, b: store.handle(m, p, q, b), backend_port)
    gateway = serve(
        lambda m, p, q, h, b: gateway_handle(store, account, m, p, q, h), gateway_port
    )
    for server in (direct, gateway):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return store, [direct, gateway]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", choices=("loki", "victorialogs"), required=True)
    parser.add_argument("--backend-port", type=int, required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--account-file", type=Path, required=True)
    args = parser.parse_args(argv)
    user, _, password = args.account_file.read_text().strip().partition(":")
    _, servers = start(
        args.backend, args.backend_port, args.gateway_port, (user, password)
    )
    ports = [s.server_address[1] for s in servers]
    print(
        json.dumps(
            {
                "backend_url": f"http://{HOST}:{ports[0]}",
                "gateway_url": f"http://{HOST}:{ports[1]}",
            }
        ),
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
