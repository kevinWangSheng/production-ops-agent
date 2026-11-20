"""Shared fakes for the C6b contract tests (#570): ClickHouse and SQL Server
dialects of the SQL tools.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「C6b 公开接口」), without reading the implementation. Two stand-ins:

* ``FakeSqlB`` -- the documented connector seam (``SqlTransport(config, *,
  connector=...)``), extended to the ``clickhouse`` and ``mssql`` dialects.
  "ClickHouse 每个 GET 是一次 query": every ClickHouse request reaches it as
  one ``query`` call; every SQL Server statement as ``execute`` or ``query``.
* ``FakeClickHouse`` -- a local ``http.server`` speaking the HTTP shape the
  record fixes (GET only, fixed query keys, ``X-ClickHouse-User``/``-Key``,
  ``JSONCompactEachRowWithNames``), so the real ClickHouse connection is
  tested without a container ("ClickHouse 的真实连接另可用本地 http.server
  替身按上面的 HTTP 形状测试").

Seams assumed beyond the record (each also marked ``# ASSUMPTION:``):

* As in C6a, ``query`` returns ``(columns, rows, more)`` with ``columns`` a
  sequence of names and ``rows`` a sequence of tuples.
* The SQL Server account check answers one problem code per row (0 rows =
  pass), like PostgreSQL's; ClickHouse ``SHOW GRANTS`` answers one grant
  line per row and the readonly probe one row ``(readonly, version)``.

All synthetic credentials are low-entropy placeholders.
"""

from __future__ import annotations

import json
import re
import select
import socket
import ssl
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.c6a_sql_support import (
    QUERY,
    SQL_USERNAME,
    install,
    sql_env,
    worker,
)

CH_URL = "clickhouse+http://127.0.0.1:18123/c6tdb"  # nothing listens here
MSSQL_URL = "mssql://127.0.0.1:11433/c6tdb"  # nothing listens here
CH_VERSION = "24.1.8.22"
MSSQL_VERSION = "16.0.4135.4"

#: The C6-21 read-only ClickHouse account's SHOW GRANTS.
CH_GOOD_GRANTS = (
    "GRANT SELECT ON c6tdb.* TO c6t_ro",
    "GRANT SELECT ON system.* TO c6t_ro",
)

CH_FIXED_KEYS = {
    "query",
    "database",
    "default_format",
    "max_execution_time",
    "timeout_overflow_mode",
    "cancel_http_readonly_queries_on_client_close",
    "output_format_json_quote_64bit_integers",
    "query_id",
}


def env_for(dialect: str, url: str | None = None, **overrides: str) -> dict[str, str]:
    """A worker environment for one C6b dialect with its only allowed TLS."""
    if dialect == "clickhouse":
        chosen = url or CH_URL
        tls = "disable" if chosen.startswith("clickhouse+http://") else "verify-full"
    else:
        chosen = url or MSSQL_URL
        tls = "disable"
    values = {"OPSPILOT_SQL_TLS": tls}
    values.update(overrides)
    return sql_env(chosen, **values)


# -- statement labels -----------------------------------------------------------


def _norm(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip().upper()


def kind(statement: str, model: str | None = None) -> str:
    """A coarse label of one statement the tool sent (C6b dialects)."""
    if model is not None and statement == model:
        return "model"
    if "opspilot:account-check" in statement:
        text = _norm(statement)
        if "SHOW GRANTS" in text:
            return "check:grants"
        if "GETSETTING" in text:
            return "check:readonly"
        return "check"
    if "opspilot:catalog" in statement:
        return "catalog"
    text = _norm(statement).rstrip(";").strip()
    match = re.match(r"^SET (LOCK_TIMEOUT|XACT_ABORT|IMPLICIT_TRANSACTIONS)\b", text)
    if match:
        return f"set:{match.group(1).lower()}"
    if re.match(r"^BEGIN (TRAN|TRANSACTION)$", text):
        return "begin"
    if re.match(r"^ROLLBACK( TRAN| TRANSACTION| WORK)?$", text):
        return "rollback"
    return "other"


def is_check(label: str) -> bool:
    return label.startswith("check")


# -- the recording connector -------------------------------------------------------


@dataclass
class Sent:
    method: str
    statement: str
    params: Any
    max_rows: int | None
    at: float


class FakeConnectionB:
    def __init__(self, driver: FakeSqlB, config: Any, timeout_seconds: float):
        self.driver = driver
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.model = driver.model
        self.sent: list[Sent] = []
        self.cancelled = threading.Event()
        self.cancel_calls = 0
        self.closed = 0

    @property
    def server_version(self) -> str:
        return self.driver.version

    def _record(self, method, statement, params, max_rows) -> str:
        with self.driver.lock:
            self.sent.append(
                Sent(method, statement, params, max_rows, time.monotonic())
            )
        label = kind(statement, self.model)
        error = self.driver.errors.get(label)
        if error is None and is_check(label):
            error = self.driver.errors.get("check")
        if error is not None:
            raise error
        return label

    def execute(self, statement: str, params: Sequence[Any] = ()) -> None:
        self._record("execute", statement, params, None)

    def query(
        self,
        statement: str,
        params: Any = (),
        max_rows: int | None = None,
        **extra: Any,
    ):
        if max_rows is None:
            max_rows = extra.get("max_rows")
        label = self._record("query", statement, params, max_rows)
        if label == "check:grants":
            return (
                [f"GRANTS FOR {SQL_USERNAME}"],
                [(line,) for line in self.driver.grants],
                False,
            )
        if label == "check:readonly":
            return (
                ["getSetting('readonly')", "version()"],
                [(self.driver.readonly, self.driver.version)],
                False,
            )
        if is_check(label):
            return ["problem"], [(code,) for code in self.driver.problems], False
        if label == "catalog":
            columns, rows = self.driver.catalog
            return list(columns), list(rows), False
        if label == "model" and self.driver.block_seconds:
            self.cancelled.wait(self.driver.block_seconds)
            raise self.driver.block_error or RuntimeError("query cancelled")
        columns, rows = self.driver.result
        rows = list(rows)
        if max_rows is not None and len(rows) > max_rows:
            return list(columns), rows[:max_rows], True
        return list(columns), rows, False

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()

    def close(self) -> None:
        self.closed += 1


class FakeSqlB:
    """The connector for ``clickhouse`` and ``mssql``: one connection per
    call, every statement kept."""

    def __init__(self, dialect: str, version: str | None = None) -> None:
        assert dialect in ("clickhouse", "mssql")
        self.dialect = dialect
        self.version = version or (
            CH_VERSION if dialect == "clickhouse" else MSSQL_VERSION
        )
        self.connections: list[FakeConnectionB] = []
        self.lock = threading.Lock()
        self.grants: list[str] = list(CH_GOOD_GRANTS)
        self.readonly: Any = 2
        self.problems: list[str] = []
        self.catalog: tuple[Sequence[str], Sequence[tuple]] = ([], [])
        self.result: tuple[Sequence[str], Sequence[tuple]] = (["n"], [(1,)])
        self.model: str | None = None
        self.errors: dict[str, BaseException] = {}
        self.connect_error: BaseException | None = None
        self.block_seconds = 0.0
        self.block_error: BaseException | None = None
        self.configs: list[Any] = []
        self.timeouts: list[float] = []

    def connect(self, config: Any, timeout_seconds: float) -> FakeConnectionB:
        with self.lock:
            self.configs.append(config)
            self.timeouts.append(timeout_seconds)
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnectionB(self, config, timeout_seconds)
        with self.lock:
            self.connections.append(connection)
        return connection

    @property
    def sent(self) -> list[Sent]:
        return [item for c in self.connections for item in c.sent]

    def kinds(self, connection: FakeConnectionB | None = None) -> list[str]:
        chosen = [connection] if connection is not None else list(self.connections)
        return [kind(item.statement, c.model) for c in chosen for item in c.sent]


@dataclass
class ToolsB:
    driver: FakeSqlB
    env: dict[str, str]
    executors: list[Any] = field(default_factory=list)

    def run(self, tool: str, params: dict[str, object]):
        from tests.c6a_sql_support import call

        if tool == QUERY and isinstance(params.get("sql"), str):
            self.driver.model = params["sql"]  # type: ignore[assignment]
        executor, _ = worker(self.env)
        self.executors.append(executor)
        return executor.execute(call(tool, params))

    def query(self, sql: str, **params: object):
        return self.run(QUERY, {"sql": sql, **params})


def make_tools_b(
    monkeypatch,
    dialect: str,
    *,
    url: str | None = None,
    request_timeout_seconds: float | None = None,
    **env: str,
) -> ToolsB:
    driver = FakeSqlB(dialect)
    install(monkeypatch, driver, request_timeout_seconds=request_timeout_seconds)
    return ToolsB(driver, env_for(dialect, url, **env))


def set_request_timeout(monkeypatch, seconds: float) -> None:
    """Shorten the per-call budget without replacing the real connector."""
    from opspilot.tools import sql_query

    original = sql_query.SqlTransport

    class Short(original):  # type: ignore[misc, valid-type]
        def __init__(self, config, *args, **kwargs):
            kwargs["request_timeout_seconds"] = seconds
            super().__init__(config, *args, **kwargs)

    for name, module in list(sys.modules.items()):
        if not name.startswith("opspilot") or module is None:
            continue
        if getattr(module, "SqlTransport", None) is original:
            monkeypatch.setattr(module, "SqlTransport", Short)


def pytds_error(cls_name: str, number: int, message: str):
    """A python-tds exception carrying a server message number.

    # ASSUMPTION: python-tds 1.17 exposes the server message number as
    # ``msg_no`` (and ``number``) on ``tds_base`` DatabaseError subclasses;
    # the transport "读消息号" from it. Skipped until the dependency lands.
    """
    pytds = pytest.importorskip("pytds")
    from pytds import tds_base

    cls = getattr(tds_base, cls_name, None) or getattr(pytds, cls_name, None)
    if cls is None:
        pytest.skip(f"python-tds has no {cls_name}")
    try:
        error = cls(message)
    except TypeError:  # e.g. ClosedConnectionError() takes no message
        error = cls()
    if number:
        error.msg_no = number
        error.number = number
    error.text = message
    return error


# -- the ClickHouse HTTP stand-in ---------------------------------------------------


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    at: float

    def one(self, key: str) -> str:
        values = self.params.get(key) or [""]
        return values[0]

    @property
    def query(self) -> str:
        return self.one("query")


@dataclass
class Reply:
    """What the stand-in answers to one request."""

    status: int = 200
    names: Sequence[str] = ("n",)
    rows: Sequence[Sequence[Any]] = ((1,),)
    code: int | None = None  # X-ClickHouse-Exception-Code
    message: str = ""
    midstream_error: str | None = None  # a non-JSON line after the first row
    block_seconds: float = 0.0
    location: str | None = None  # a redirect
    raw_lines: Sequence[str] | None = None


def ch_error(code: int, message: str, status: int) -> Reply:
    return Reply(status=status, code=code, message=message)


class FakeClickHouse:
    """A ClickHouse HTTP interface stand-in on 127.0.0.1 (optionally TLS)."""

    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        self.requests: list[Request] = []
        self.grants: list[str] = list(CH_GOOD_GRANTS)
        self.readonly: Any = 2
        self.catalog = Reply(names=("x",), rows=())
        self.model = Reply()
        self.replies: dict[str, Reply] = {}  # kind -> override
        self.disconnected = threading.Event()
        self.lock = threading.Lock()
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                split = urlsplit(self.path)
                request = Request(
                    self.command,
                    split.path,
                    parse_qs(split.query, keep_blank_values=True),
                    {k.lower(): v for k, v in self.headers.items()},
                    body,
                    time.monotonic(),
                )
                with stand_in.lock:
                    stand_in.requests.append(request)
                stand_in._answer(self, request)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_HEAD = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls is not None:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, database: str = "c6tdb", scheme: str = "clickhouse+http") -> str:
        return f"{scheme}://127.0.0.1:{self.port}/{database}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- answering ----------------------------------------------------------------

    def label(self, request: Request) -> str:
        return kind(request.query, None) if "opspilot:" in request.query else "model"

    def _reply_for(self, request: Request) -> Reply:
        label = self.label(request)
        if label in self.replies:
            return self.replies[label]
        if label == "check:grants":
            return Reply(
                names=(f"GRANTS FOR {SQL_USERNAME}",), rows=[(g,) for g in self.grants]
            )
        if label == "check:readonly":
            return Reply(
                names=("getSetting('readonly')", "version()"),
                rows=[(self.readonly, CH_VERSION)],
            )
        if label == "catalog":
            return self.catalog
        return self.model

    def _answer(self, handler: BaseHTTPRequestHandler, request: Request) -> None:
        reply = self._reply_for(request)
        if reply.location is not None:
            handler.send_response(reply.status if reply.status != 200 else 302)
            handler.send_header("Location", reply.location)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if reply.code is not None:
            text = (
                f"Code: {reply.code}. DB::Exception: {reply.message}. "
                f"(version {CH_VERSION} (official build))\n"
            ).encode()
            handler.send_response(reply.status)
            handler.send_header("X-ClickHouse-Exception-Code", str(reply.code))
            handler.send_header("Content-Type", "text/plain; charset=UTF-8")
            handler.send_header("Content-Length", str(len(text)))
            handler.end_headers()
            handler.wfile.write(text)
            return
        handler.send_response(reply.status)
        handler.send_header("Content-Type", "application/json; charset=UTF-8")
        handler.send_header("X-ClickHouse-Format", "JSONCompactEachRowWithNames")
        handler.send_header("X-ClickHouse-Query-Id", request.one("query_id"))
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        if reply.block_seconds:
            self._wait_for_disconnect(handler, reply.block_seconds)
            return
        lines = (
            list(reply.raw_lines)
            if reply.raw_lines is not None
            else [json.dumps(list(reply.names))]
            + [json.dumps(list(row)) for row in reply.rows]
        )
        try:
            for n, line in enumerate(lines):
                self._chunk(handler, line + "\n")
                if n == 1 and reply.midstream_error is not None:
                    self._chunk(handler, reply.midstream_error + "\n")
            if len(lines) <= 1 and reply.midstream_error is not None:
                self._chunk(handler, reply.midstream_error + "\n")
            handler.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            self.disconnected.set()

    @staticmethod
    def _chunk(handler: BaseHTTPRequestHandler, text: str) -> None:
        data = text.encode()
        handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def _wait_for_disconnect(self, handler, seconds: float) -> None:
        sock = handler.connection
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                readable, _, _ = select.select([sock], [], [], 0.05)
                if readable:
                    peek = sock.recv(1, socket.MSG_PEEK)
                    if not peek:
                        self.disconnected.set()
                        return
            except (OSError, ValueError, ssl.SSLError):
                self.disconnected.set()
                return
        try:
            handler.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.disconnected.set()

    # -- views ------------------------------------------------------------------

    def labels(self) -> list[str]:
        return [self.label(r) for r in self.requests]


def uuid4_query_id(value: str) -> bool:
    match = re.fullmatch(r"opspilot-(.+)", value)
    if not match:
        return False
    try:
        return uuid.UUID(match.group(1)).version == 4
    except ValueError:
        return False


def wait_until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
