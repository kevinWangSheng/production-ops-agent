"""Shared fakes for the Redis/Valkey tool tests (#773): a recording RESP2
server on 127.0.0.1 and the worker built around it.

Written by the independent test author from the #773 contract (R1-R18),
without reading the implementation.

The seam is the wire itself. The contract fixes ``RedisTransport(config, *,
connector=None, request_timeout_seconds=30.0)`` but not the connector's
signature, so these tests leave the default (standard-library) connector in
place and point ``OPSPILOT_REDIS_URL`` at ``FakeRedis``, a real TCP server
that keeps every command array each connection sends, in order. "No command
reached the server" is therefore observed on the socket, not inside the tool.

Assumptions beyond the contract (each also marked ``# ASSUMPTION:``):

* The worker builds its transport by calling ``RedisTransport(...)`` looked
  up as a module attribute, so a subclass patched over every module
  attribute bound to the original class sees each request (``fetch``) and can
  shorten the budget (``request_timeout_seconds``), as for C6c MongoDB.
* Tool parameter names: ``sections``, ``pattern``, ``type``, ``key`` and
  ``limit`` (the contract says "sections, glob patterns, key names and
  limits" without naming them). They are kept in ``MINIMAL`` and
  ``PARAMS`` here so one edit adapts every test.
* ``sections`` is a comma-separated string, as ``mongodb_server_status``.

All synthetic credentials are low-entropy placeholders.
"""

from __future__ import annotations

import fnmatch
import select
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tests.c6c_mongo_support import base_env, call, require_jq, worker

INFO_TOOL = "redis_info"
CONFIG_TOOL = "redis_config_get"
SCAN_TOOL = "redis_scan_keys"
KEY_INFO_TOOL = "redis_key_info"
GET_VALUE_TOOL = "redis_get_value"
SLOWLOG_TOOL = "redis_slowlog"
REDIS_TOOLS = (
    INFO_TOOL,
    CONFIG_TOOL,
    SCAN_TOOL,
    KEY_INFO_TOOL,
    GET_VALUE_TOOL,
    SLOWLOG_TOOL,
)

# ASSUMPTION: parameter names (see the module docstring).
PARAMS: dict[str, set[str]] = {
    INFO_TOOL: {"sections"},
    CONFIG_TOOL: {"pattern"},
    SCAN_TOOL: {"pattern", "type", "limit"},
    KEY_INFO_TOOL: {"key"},
    GET_VALUE_TOOL: {"key", "limit"},
    SLOWLOG_TOOL: {"limit"},
}
MINIMAL: dict[str, dict[str, object]] = {
    INFO_TOOL: {},
    CONFIG_TOOL: {"pattern": "maxmemory*"},
    SCAN_TOOL: {"pattern": "orders:*"},
    KEY_INFO_TOOL: {"key": "orders:1"},
    GET_VALUE_TOOL: {"key": "orders:1"},
    SLOWLOG_TOOL: {},
}

REDIS_USERNAME = "r773-reader"
REDIS_PASSWORD = "r773-pw-" + "aaaa773"
CANARY = "ffffffff773"
#: The SHA-256 the server reports under ``passwords``; never echoed.
PASSWORD_HASH = "ab" * 32
#: A key pattern only the GETUSER reply carries; never echoed.
GETUSER_MARKER = "~r773marker*"

#: R4: the lab/CI grant, as ``ACL GETUSER`` reports its ``commands`` field.
LAB_GRANT = (
    "-@all +@read +info +ping +hello +auth +select +client|setname +config|get "
    "+slowlog|get +slowlog|len +acl|whoami +acl|getuser"
)

#: R2: every (command, subcommand) the tool may send; ``None`` = no subcommand.
HANDSHAKE = frozenset(
    {
        ("AUTH", None),
        ("CLIENT", "SETNAME"),
        ("SELECT", None),
        ("ACL", "WHOAMI"),
        ("ACL", "GETUSER"),
    }
)
TOOL_COMMANDS = frozenset(
    {
        ("INFO", None),
        ("CONFIG", "GET"),
        ("SCAN", None),
        ("TYPE", None),
        ("PTTL", None),
        ("OBJECT", "ENCODING"),
        ("MEMORY", "USAGE"),
        ("STRLEN", None),
        ("LLEN", None),
        ("HLEN", None),
        ("SCARD", None),
        ("ZCARD", None),
        ("XLEN", None),
        ("GETRANGE", None),
        ("HSCAN", None),
        ("LRANGE", None),
        ("SSCAN", None),
        ("ZRANGE", None),
        ("XREVRANGE", None),
        ("SLOWLOG", "GET"),
    }
)
ALLOWED = HANDSHAKE | TOOL_COMMANDS
_WITH_SUBCOMMAND = {"CLIENT", "ACL", "CONFIG", "OBJECT", "MEMORY", "SLOWLOG"}


def shape(argv: list[str]) -> tuple[str, str | None]:
    name = argv[0].upper() if argv else ""
    sub = argv[1].upper() if name in _WITH_SUBCOMMAND and len(argv) > 1 else None
    return name, sub


# -- RESP2 encoding -------------------------------------------------------------


class Simple(str):
    """``+text``"""


class Error(str):
    """``-text``"""


def encode(value: Any) -> bytes:
    if isinstance(value, Error):
        return b"-" + value.encode() + b"\r\n"
    if isinstance(value, Simple):
        return b"+" + value.encode() + b"\r\n"
    if value is None:
        return b"$-1\r\n"
    if isinstance(value, bool):
        return b":%d\r\n" % int(value)
    if isinstance(value, int):
        return b":%d\r\n" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, (bytes, bytearray)):
        return b"$%d\r\n" % len(value) + bytes(value) + b"\r\n"
    if isinstance(value, (list, tuple)):
        return b"*%d\r\n" % len(value) + b"".join(encode(v) for v in value)
    raise TypeError(type(value))


def getuser_reply(
    commands: str = LAB_GRANT,
    *,
    flags: list[str] | None = None,
    selectors: list[Any] | None = None,
) -> list[Any]:
    """``ACL GETUSER`` as Redis 7 / Valkey answer it in RESP2."""
    return [
        "flags",
        flags if flags is not None else ["on", "sanitize-payload"],
        "passwords",
        [PASSWORD_HASH],
        "commands",
        commands,
        "keys",
        f"~* {GETUSER_MARKER}",
        "channels",
        "",
        "selectors",
        selectors if selectors is not None else [],
    ]


# -- the recording server ---------------------------------------------------------


@dataclass
class Connection:
    commands: list[list[str]] = field(default_factory=list)
    raw: list[list[bytes]] = field(default_factory=list)
    closed: threading.Event = field(default_factory=threading.Event)

    def tool_commands(self) -> list[list[str]]:
        return [c for c in self.commands if shape(c) not in HANDSHAKE]


def _info_text(sections: Mapping[str, Mapping[str, object]]) -> str:
    parts = []
    for name, items in sections.items():
        parts.append(f"# {name.capitalize()}")
        parts.extend(f"{k}:{v}" for k, v in items.items())
        parts.append("")
    return "\r\n".join(parts)


DEFAULT_INFO: dict[str, dict[str, object]] = {
    "server": {"redis_version": "7.2.4", "valkey_version": "9.0.0", "uptime_in_seconds": 1234},
    "clients": {"connected_clients": 7, "blocked_clients": 0},
    "memory": {"used_memory": 1048576, "used_memory_human": "1.00M", "maxmemory": 0},
    "persistence": {"loading": 0, "rdb_changes_since_last_save": 0},
    "stats": {"total_commands_processed": 99, "evicted_keys": 0},
    "replication": {"role": "master", "connected_slaves": 0},
    "cpu": {"used_cpu_sys": "1.0"},
    "keyspace": {"db0": "keys=3,expires=0,avg_ttl=0"},
}  # fmt: skip


class FakeRedis:
    """A single-node RESP2 server keeping every command, per connection."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connections: list[Connection] = []
        self.username = REDIS_USERNAME
        self.password = REDIS_PASSWORD
        self.getuser: Any = getuser_reply()
        self.info: dict[str, dict[str, object]] = {
            k: dict(v) for k, v in DEFAULT_INFO.items()
        }
        self.config: dict[str, str] = {
            "maxmemory": "0",
            "maxmemory-policy": "noeviction",
            "maxclients": "10000",
        }
        #: key -> (type, value): string bytes, hash dict, list, set (list),
        #: zset [(member, score)], stream [(id, {f: v})].
        self.data: dict[str, tuple[str, Any]] = {
            "orders:1": ("string", b"paid"),
            "orders:2": ("string", b"open"),
            "cart:9": ("hash", {"sku": "A1"}),
        }
        self.slowlog: list[list[Any]] = [
            [1, 1763812800, 15000, ["GET", "orders:1"], "10.0.0.7:5555", "checkout"]
        ]
        #: command or "COMMAND SUB" -> error text returned instead of a reply.
        self.errors: dict[str, str] = {}
        #: An error for every command except AUTH (a server needing a login).
        self.error_after_auth: str | None = None
        #: Commands that never answer (until the client closes).
        self.stall_on: set[str] = set()
        #: SCAN never finishes: every page has a non-zero cursor; only the
        #: first page carries a key.
        self.scan_forever = False
        self.page = 10  # keys/fields per SCAN/HSCAN/SSCAN page
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self.url = f"redis://127.0.0.1:{self.port}"
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    # -- views over what was sent

    @property
    def commands(self) -> list[list[str]]:
        return [c for conn in self.connections for c in conn.commands]

    def names(self) -> list[str]:
        return [shape(c)[0] for c in self.commands]

    def tool_commands(self) -> list[list[str]]:
        return [c for c in self.commands if shape(c) not in HANDSHAKE]

    def all_closed(self, seconds: float = 3.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if all(c.closed.is_set() for c in self.connections):
                return True
            time.sleep(0.02)
        return all(c.closed.is_set() for c in self.connections)

    def close(self) -> None:
        self._stop.set()
        self._server.close()

    # -- server loop

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            conn = Connection()
            with self.lock:
                self.connections.append(conn)
            threading.Thread(target=self._serve, args=(sock, conn), daemon=True).start()

    def _serve(self, sock: socket.socket, conn: Connection) -> None:
        buffer = b""
        authed = False
        try:
            while True:
                parsed = _parse(buffer)
                if parsed is None:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    continue
                argv_raw, buffer = parsed
                argv = [a.decode("utf-8", "replace") for a in argv_raw]
                with self.lock:
                    conn.raw.append(argv_raw)
                    conn.commands.append(argv)
                name, sub = shape(argv)
                if name in self.stall_on:
                    _wait_for_close(sock)
                    return
                if name == "AUTH":
                    authed = True
                reply = self._reply(argv, name, sub, authed)
                sock.sendall(encode(reply))
        except OSError:
            return
        finally:
            conn.closed.set()
            try:
                sock.close()
            except OSError:
                pass

    def _reply(self, argv: list[str], name: str, sub: str | None, authed: bool) -> Any:
        for label in (f"{name} {sub}" if sub else None, name):
            if label and label in self.errors:
                return Error(self.errors[label])
        if self.error_after_auth and name != "AUTH":
            return Error(self.error_after_auth)
        if name == "AUTH":
            if argv[1:] == [self.username, self.password]:
                return Simple("OK")
            return Error(
                "WRONGPASS invalid username-password pair or user is disabled."
            )
        if (name, sub) in {("CLIENT", "SETNAME"), ("SELECT", None)}:
            return Simple("OK")
        if (name, sub) == ("ACL", "WHOAMI"):
            return self.username
        if (name, sub) == ("ACL", "GETUSER"):
            return self.getuser if argv[2:] == [self.username] else None
        if name == "INFO":
            wanted = [a.lower() for a in argv[1:]] or ["default"]
            if any(w in ("default", "all", "everything") for w in wanted):
                chosen = self.info
            else:
                chosen = {k: v for k, v in self.info.items() if k in wanted}
            return _info_text(chosen)
        if (name, sub) == ("CONFIG", "GET"):
            out: list[str] = []
            for key, value in self.config.items():
                if any(fnmatch.fnmatchcase(key, p) for p in argv[2:]):
                    out += [key, value]
            return out
        if name == "SCAN":
            return self._scan(argv)
        key = argv[1] if len(argv) > 1 else ""
        if name == "OBJECT" or name == "MEMORY":
            key = argv[2] if len(argv) > 2 else ""
        kind, value = self.data.get(key, ("none", None))
        if name == "TYPE":
            return Simple(kind)
        if name == "PTTL":
            return -1 if kind != "none" else -2
        if (name, sub) == ("OBJECT", "ENCODING"):
            return {"string": "embstr", "hash": "listpack", "list": "quicklist"}.get(
                kind, "listpack"
            )
        if (name, sub) == ("MEMORY", "USAGE"):
            return 72
        lengths = {"STRLEN": "string", "LLEN": "list", "HLEN": "hash",
                   "SCARD": "set", "ZCARD": "zset", "XLEN": "stream"}  # fmt: skip
        if name in lengths:
            if kind != lengths[name]:
                return 0
            return len(value)
        if name == "GETRANGE" and kind == "string":
            start, end = int(argv[2]), int(argv[3])
            return value[start : end + 1]
        if name == "LRANGE" and kind == "list":
            start, end = int(argv[2]), int(argv[3])
            return list(value[start : end + 1])
        if name == "ZRANGE" and kind == "zset":
            start, end = int(argv[2]), int(argv[3])
            flat: list[Any] = []
            for member, score in value[start : end + 1]:
                flat += [member, str(score)]
            return flat
        if name == "XREVRANGE" and kind == "stream":
            count = len(value)
            upper = [a.upper() for a in argv]
            if "COUNT" in upper:
                count = int(argv[upper.index("COUNT") + 1])
            entries = list(reversed(value))[:count]
            return [
                [eid, [x for kv in fields.items() for x in kv]]
                for eid, fields in entries
            ]
        if name in ("HSCAN", "SSCAN") and kind in ("hash", "set"):
            items = (
                list(value.items()) if kind == "hash" else [(m, None) for m in value]
            )
            cursor = int(argv[2])
            page = items[cursor : cursor + self.page]
            nxt = cursor + self.page if cursor + self.page < len(items) else 0
            flat = []
            for k, v in page:
                flat += [k] if v is None else [k, v]
            return [str(nxt), flat]
        if (name, sub) == ("SLOWLOG", "GET"):
            count = int(argv[2]) if len(argv) > 2 else 10
            return [list(e) for e in self.slowlog[:count]]
        return Error(f"ERR unknown command '{argv[0] if argv else ''}'")

    def _scan(self, argv: list[str]) -> list[Any]:
        if self.scan_forever:
            return ["1", ["orders:1"] if argv[1] == "0" else []]
        upper = [a.upper() for a in argv]
        pattern = argv[upper.index("MATCH") + 1] if "MATCH" in upper else "*"
        kind = argv[upper.index("TYPE") + 1].lower() if "TYPE" in upper else None
        keys = sorted(self.data)
        cursor = int(argv[1])
        page = keys[cursor : cursor + self.page]
        nxt = cursor + self.page if cursor + self.page < len(keys) else 0
        hits = [
            k
            for k in page
            if fnmatch.fnmatchcase(k, pattern)
            and (kind is None or self.data[k][0] == kind)
        ]
        return [str(nxt), hits]


def _parse(buffer: bytes) -> tuple[list[bytes], bytes] | None:
    """One RESP array of bulk strings from ``buffer``, or None if incomplete.
    Anything else is kept as a one-word inline command."""
    if not buffer:
        return None
    if not buffer.startswith(b"*"):
        line, sep, rest = buffer.partition(b"\r\n")
        if not sep:
            return None
        return line.split(), rest
    head, sep, rest = buffer.partition(b"\r\n")
    if not sep:
        return None
    count = int(head[1:])
    items: list[bytes] = []
    for _ in range(count):
        line, sep, after = rest.partition(b"\r\n")
        if not sep:
            return None
        size = int(line[1:])
        if len(after) < size + 2:
            return None
        items.append(after[:size])
        rest = after[size + 2 :]
    return items, rest


def _wait_for_close(sock: socket.socket, seconds: float = 60.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([sock], [], [], 0.05)
        if ready:
            try:
                if not sock.recv(65536):
                    return
            except OSError:
                return


# -- the worker -------------------------------------------------------------------


def redis_env(url: str | None, **overrides: str) -> dict[str, str]:
    """A worker environment; with a URL, the redis-ro credential too (TLS
    ``disable`` as in the lab). An override of ``""`` removes that key."""
    values = base_env()
    if url is not None:
        values["OPSPILOT_REDIS_URL"] = url
        values["OPSPILOT_REDIS_USERNAME"] = REDIS_USERNAME
        values["OPSPILOT_REDIS_PASSWORD"] = REDIS_PASSWORD
        values["OPSPILOT_REDIS_TLS"] = "disable"
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def route(monkeypatch, *, request_timeout_seconds: float | None = None) -> list[Any]:
    """Record every ``TransportRequest`` the Redis transport receives.

    # ASSUMPTION: ``opspilot.tools.redis_inspect.RedisTransport`` is the
    # class the worker instantiates (directly or through a module that
    # imported the name); patching every module attribute bound to it
    # reaches both. The connector stays the default one.
    """
    from opspilot.tools import redis_inspect

    original = redis_inspect.RedisTransport
    requests: list[Any] = []

    class Routed(original):  # type: ignore[misc, valid-type]
        def __init__(self, config, *args, **kwargs):
            if request_timeout_seconds is not None:
                kwargs["request_timeout_seconds"] = request_timeout_seconds
            super().__init__(config, *args, **kwargs)

        def fetch(self, request, *args, **kwargs):
            requests.append(request)
            return super().fetch(request, *args, **kwargs)

    for name, module in list(sys.modules.items()):
        if not name.startswith("opspilot") or module is None:
            continue
        if getattr(module, "RedisTransport", None) is original:
            monkeypatch.setattr(module, "RedisTransport", Routed)
    return requests


@dataclass
class Tools:
    """One fake server and the workers pointed at it."""

    server: FakeRedis
    env: dict[str, str]
    requests: list[Any]
    sinks: list[Any] = field(default_factory=list)

    def run(self, tool: str, params: Mapping[str, object] | None = None):
        executor, sink = worker(self.env)
        self.sinks.append(sink)
        return executor.execute(call(tool, MINIMAL[tool] if params is None else params))


def make_tools(
    monkeypatch, *, request_timeout_seconds: float | None = None, **env: str
) -> Tools:
    require_jq()
    server = FakeRedis()
    requests = route(monkeypatch, request_timeout_seconds=request_timeout_seconds)
    return Tools(server, redis_env(server.url, **env), requests)


def free_port() -> int:
    """A port nothing listens on (bound, then released)."""
    with socket.create_server(("127.0.0.1", 0)) as probe:
        return probe.getsockname()[1]


def wait_until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
