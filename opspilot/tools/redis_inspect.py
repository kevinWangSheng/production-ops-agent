"""Read-only Redis/Valkey inspection tools (#773): ``redis_info``,
``redis_config_get``, ``redis_scan_keys``, ``redis_key_info``,
``redis_get_value`` and ``redis_slowlog``.

One configured node and database; the model supplies only sections, glob
patterns, a key name, a type filter and limits -- never a command. The public
interface is ``docs/tasks/2026-11-22-redis-tool-773.md``; the precedent is the
C6 database tools (``opspilot/tools/mongodb_query.py``).

Read-only is the tool's guarantee (PRODUCT-CONSTRAINTS line 18), in two
layers:

* the operator's read-only account (``redis-ro``), checked after every
  connect from its own ``ACL GETUSER`` (``account_problem``): no
  ``allcommands`` flag, no selectors, and every ``+`` rule is ``+@read`` or a
  command this tool sends;
* a frozen command table asserted in ``RedisTransport._send`` before any byte
  is written: only the handshake and each tool's fixed read commands
  (``GETRANGE``, never ``GET``; ``HSCAN``/``SSCAN``, never ``HGETALL``/
  ``SMEMBERS``; ``SCAN``, never ``KEYS``).

The client is a standard-library RESP2 client (socket, ssl): a new connection
per call, no pool, no retry, a watchdog that closes the socket at the call's
deadline. No cluster or Sentinel support: a ``MOVED``/``ASK`` reply is
``REDIS_CLUSTER_UNSUPPORTED``.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opspilot.redaction import REDACTED_CREDENTIAL, _credential_name, _session_word
from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.http_deadline import resolve
from opspilot.tools.registry import (
    MAX_RESULT_BYTES,
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolContractError,
    ToolDescription,
    ToolRegistration,
    canonical,
)
from opspilot.tools.sql_query import _dsn_endpoints, _host_key

__all__ = [
    "ALLOWED_COMMANDS",
    "CONFIG_TOOL",
    "CREDENTIAL_REF",
    "ENDPOINT_SELECTOR",
    "INFO_TOOL",
    "KEY_INFO_TOOL",
    "SCAN_TOOL",
    "SLOWLOG_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "URL_ENV",
    "VALUE_TOOL",
    "RedisConfig",
    "RedisConnection",
    "RedisProtocolError",
    "RedisReplyError",
    "RedisTransport",
    "account_problem",
    "overflow_summary",
    "redis_enabled",
    "redis_registrations",
]

INFO_TOOL = "redis_info"
CONFIG_TOOL = "redis_config_get"
SCAN_TOOL = "redis_scan_keys"
KEY_INFO_TOOL = "redis_key_info"
VALUE_TOOL = "redis_get_value"
SLOWLOG_TOOL = "redis_slowlog"
TOOL_NAMES: tuple[str, ...] = (
    INFO_TOOL,
    CONFIG_TOOL,
    SCAN_TOOL,
    KEY_INFO_TOOL,
    VALUE_TOOL,
    SLOWLOG_TOOL,
)

CREDENTIAL_REF = "redis-ro"
ENDPOINT_SELECTOR = "redis_endpoint"

URL_ENV = "OPSPILOT_REDIS_URL"
DATABASE_ENV = "OPSPILOT_REDIS_DATABASE"
USERNAME_ENV = "OPSPILOT_REDIS_USERNAME"
PASSWORD_ENV = "OPSPILOT_REDIS_PASSWORD"
TLS_ENV = "OPSPILOT_REDIS_TLS"
CA_FILE_ENV = "OPSPILOT_REDIS_CA_FILE"
_ENV_PREFIX = "OPSPILOT_REDIS_"
_TLS_MODES = ("verify-full", "require", "disable")
DEFAULT_PORT = 6379
_MAX_DATABASE = 2**31 - 1

REQUEST_TIMEOUT_SECONDS = 30.0
_CONNECT_MARGIN = 2.0
_MAX_CONNECT_SECONDS = 10.0
_MIN_CONNECT_SECONDS = 1.0
# Hostname lookups get their own slots so a stuck Redis resolver cannot
# queue other data sources' lookups (the #444 pattern).
_RESOLVER_SLOTS = threading.BoundedSemaphore(2)
READ_LIMIT_BYTES = MAX_RESULT_BYTES
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SLOWLOG_DEFAULT_LIMIT = 50
SLOWLOG_MAX_LIMIT = 128
MAX_CONFIG_ROWS = 500
SCAN_COUNT = 1000
MAX_SCAN_ITERATIONS = 50
STRING_READ_BYTES = 8192
MAX_VALUE_CHARS = 2000
MAX_NAME_CHARS = 1024
_MAX_REPLY_DEPTH = 8
_CLIENT_NAME = "opspilot-redis-tool"

_INFO_SECTIONS = (
    "server",
    "clients",
    "memory",
    "persistence",
    "stats",
    "replication",
    "cpu",
    "modules",
    "errorstats",
    "cluster",
    "keyspace",
    "commandstats",
    "latencystats",
)
_KEY_TYPES = ("string", "list", "set", "zset", "hash", "stream")
_LENGTH_COMMANDS: Mapping[str, str] = {
    "string": "STRLEN",
    "list": "LLEN",
    "hash": "HLEN",
    "set": "SCARD",
    "zset": "ZCARD",
    "stream": "XLEN",
}


# -- configuration ------------------------------------------------------------


def redis_enabled(env: Mapping[str, str]) -> bool:
    """Whether the extended profile shows the tools: the address alone (the
    web calls this too and never reads a credential)."""
    return bool(env.get(URL_ENV))


def _url_parts(value: str) -> tuple[str, int]:
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ToolContractError("REDIS_CONFIG_INVALID") from None
    if (
        parts.scheme.lower() != "redis"
        or not host
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or "," in parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or not value.isprintable()
        or any(ch.isspace() for ch in value)
    ):
        raise ToolContractError("REDIS_CONFIG_INVALID")
    return host, port or DEFAULT_PORT


def _database(text: str | None) -> int:
    if not text:
        return 0
    if re.fullmatch(r"[0-9]{1,10}", text) is None or int(text) > _MAX_DATABASE:
        raise ToolContractError("REDIS_CONFIG_INVALID")
    return int(text)


@dataclass(frozen=True)
class RedisConfig:
    """The worker's ``redis-ro`` connection. The password is never
    rendered."""

    url: str
    username: str
    password: str = field(repr=False)
    database: int = 0
    tls: str = "verify-full"
    ca_file: str | None = None
    host: str = field(init=False)
    port: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ToolContractError("REDIS_CONFIG_MISSING")
        host, port = _url_parts(self.url)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        for value in (self.username, self.password):
            if not isinstance(value, str) or not value:
                raise ToolContractError("REDIS_CONFIG_MISSING")
            if not value.isprintable():
                raise ToolContractError("REDIS_CONFIG_INVALID")
        if any(ch.isspace() for ch in self.username):
            raise ToolContractError("REDIS_CONFIG_INVALID")
        if type(self.database) is not int or not 0 <= self.database <= _MAX_DATABASE:
            raise ToolContractError("REDIS_CONFIG_INVALID")
        if self.tls not in _TLS_MODES:
            raise ToolContractError("REDIS_CONFIG_INVALID")
        if self.ca_file is not None:
            if self.tls == "disable":
                raise ToolContractError("REDIS_CONFIG_INVALID")
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ToolContractError("REDIS_CONFIG_INVALID")

    @property
    def endpoint(self) -> str:
        return self.url

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RedisConfig | None:
        url = env.get(URL_ENV) or ""
        if not url:
            if any(
                name.startswith(_ENV_PREFIX) and value for name, value in env.items()
            ):
                raise ToolContractError("REDIS_CONFIG_INVALID")
            return None
        _url_parts(url)
        username = env.get(USERNAME_ENV) or ""
        password = env.get(PASSWORD_ENV) or ""
        if not (username and password):
            raise ToolContractError("REDIS_CONFIG_MISSING")
        config = cls(
            url=url,
            username=username,
            password=password,
            database=_database(env.get(DATABASE_ENV)),
            tls=env.get(TLS_ENV) or "verify-full",
            ca_file=env.get(CA_FILE_ENV) or None,
        )
        if (_host_key(config.host), config.port) in _dsn_endpoints(env):
            raise ToolContractError("REDIS_TARGETS_OPSPILOT_DB")
        return config


# -- the command table (the second layer) -------------------------------------

_SUBCOMMANDS: Mapping[str, frozenset[str]] = {
    "ACL": frozenset({"WHOAMI", "GETUSER"}),
    "CLIENT": frozenset({"SETNAME"}),
    "CONFIG": frozenset({"GET"}),
    "MEMORY": frozenset({"USAGE"}),
    "OBJECT": frozenset({"ENCODING"}),
    "SLOWLOG": frozenset({"GET"}),
}
_PLAIN_COMMANDS = frozenset(
    {
        "AUTH",
        "SELECT",
        "INFO",
        "SCAN",
        "TYPE",
        "PTTL",
        "STRLEN",
        "LLEN",
        "HLEN",
        "SCARD",
        "ZCARD",
        "XLEN",
        "GETRANGE",
        "HSCAN",
        "LRANGE",
        "SSCAN",
        "ZRANGE",
        "XREVRANGE",
    }
)
#: Every (command, subcommand) the tool may put on the wire; frozen.
ALLOWED_COMMANDS: frozenset[tuple[str, str | None]] = frozenset(
    {(name, None) for name in _PLAIN_COMMANDS}
    | {(name, sub) for name, subs in _SUBCOMMANDS.items() for sub in subs}
)


def _command_allowed(argv: Sequence[str]) -> bool:
    if not argv or not all(isinstance(arg, str) for arg in argv):
        return False
    name = argv[0].upper()
    if name in _SUBCOMMANDS:
        return len(argv) >= 2 and (name, argv[1].upper()) in ALLOWED_COMMANDS
    return (name, None) in ALLOWED_COMMANDS


class RedisCommandRefused(TransportError):
    """A command outside :data:`ALLOWED_COMMANDS` was about to be sent."""


# -- account check -------------------------------------------------------------

#: The ``+`` rules a read-only account may hold: ``+@read``, each command the
#: tool sends, and the three harmless reads of the documented lab grant
#: (``+ping``, ``+hello``, ``+slowlog|len``).
_ACCOUNT_RULES = frozenset(
    {"+@read", "+ping", "+hello", "+slowlog|len"}
    | {
        "+" + (name if sub is None else f"{name}|{sub}").lower()
        for name, sub in ALLOWED_COMMANDS
    }
)


def _word(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        return value
    return None


def _getuser_fields(reply: object) -> dict[str, Any] | None:
    """The ``ACL GETUSER`` reply as ``{field: value}``: a RESP2 flat
    ``[name, value, ...]`` list or a mapping; ``None`` when it is neither."""
    if isinstance(reply, Mapping):
        pairs = list(reply.items())
    elif isinstance(reply, (list, tuple)) and len(reply) % 2 == 0 and reply:
        pairs = list(zip(reply[0::2], reply[1::2], strict=True))
    else:
        return None
    fields: dict[str, Any] = {}
    for name, value in pairs:
        text = _word(name)
        if text is None or text.lower() in fields:
            return None
        fields[text.lower()] = value
    return fields


def _rule_text(rule: str) -> str:
    shown = rule[:64]
    return shown if shown.isprintable() else "an unreadable rule"


def _account_check(getuser_reply: object) -> str | None:
    unreadable = (
        "REDIS_ACCOUNT_NOT_READ_ONLY: the account's ACL GETUSER reply could not "
        "be read; nothing was run."
    )
    fields = _getuser_fields(getuser_reply)
    if fields is None:
        return unreadable
    flags = fields.get("flags")
    commands = _word(fields.get("commands"))
    if (
        not isinstance(flags, (list, tuple))
        or any(_word(flag) is None for flag in flags)
        or commands is None
        or not commands.strip()
    ):
        return unreadable
    problems: list[str] = []
    if any(str(_word(flag)).lower() == "allcommands" for flag in flags):
        problems.append("flag allcommands")
    if "selectors" in fields:
        selectors = fields["selectors"]
        if not isinstance(selectors, (list, tuple)):
            return unreadable
        if selectors:
            problems.append("a selector")
    for rule in commands.split():
        lowered = rule.lower()
        if lowered.startswith("-"):
            continue
        if not lowered.startswith("+"):
            return unreadable
        if lowered not in _ACCOUNT_RULES:
            problems.append(_rule_text(rule))
    if problems:
        unique = sorted(set(problems))
        return (
            "REDIS_ACCOUNT_NOT_READ_ONLY: the configured account may run more "
            "than reads (" + ", ".join(unique[:8]) + "); nothing was run."
        )
    return None


def account_problem(getuser_reply: object) -> str | None:
    """The refusal note of the post-connect account check over the account's
    own ``ACL GETUSER`` reply, or ``None`` when the account looks read-only.
    Fails closed on anything it cannot read. The note names only the
    offending rule, never the reply."""
    return _account_check(getuser_reply)


def _legacy_server(getuser_reply: object) -> bool:
    """A server older than 7 (no ACL selectors): ``INFO`` takes one section."""
    fields = _getuser_fields(getuser_reply)
    return fields is not None and "selectors" not in fields


# -- the RESP2 client -------------------------------------------------------------


class RedisReplyError(Exception):
    """An error reply (``-ERR ...``, ``-NOPERM ...``). The text is the
    server's and is never shown; only its first word is classified."""

    def __init__(self, message: str) -> None:
        super().__init__("REDIS_ERROR_REPLY")
        self.message = message

    @property
    def word(self) -> str:
        return self.message.split(" ", 1)[0].upper() if self.message else ""


class RedisProtocolError(Exception):
    """A reply that is not well-formed RESP2."""


class RedisConnection(Protocol):
    def command(self, argv: Sequence[str]) -> Any: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


Connector = Callable[["RedisConfig", float], RedisConnection]


def _encode(argv: Sequence[str]) -> bytes:
    out = bytearray(b"*%d\r\n" % len(argv))
    for arg in argv:
        data = arg.encode("utf-8")
        out += b"$%d\r\n" % len(data)
        out += data
        out += b"\r\n"
    return bytes(out)


class _SocketConnection:
    """One TCP (optionally TLS) connection to the configured node. Replies:
    simple string -> ``str``, bulk -> ``bytes``, integer -> ``int``, array ->
    ``list``, nil -> ``None``; an error reply raises :class:`RedisReplyError`.
    Reads stop at the 2 MiB per-call ceiling."""

    def __init__(self, config: RedisConfig, timeout: float) -> None:
        # #783: the lookup shares the connect timeout (getaddrinfo cannot be
        # interrupted, so it runs on a slot-limited thread); a stuck resolver
        # ends as a connect failure, before the call's deadline.
        deadline = time.monotonic() + timeout
        family, kind, proto, _, address = resolve(
            config.host, config.port, deadline, _RESOLVER_SLOTS
        )[0]
        raw = socket.socket(family, kind, proto)
        sock: socket.socket = raw
        try:
            raw.settimeout(max(0.001, deadline - time.monotonic()))
            raw.connect(address)
            if config.tls != "disable":
                context = ssl.create_default_context(cafile=config.ca_file)
                if config.tls == "require":
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(raw, server_hostname=config.host)
            # a backstop only: the transport's watchdog ends the call
            sock.settimeout(REQUEST_TIMEOUT_SECONDS)
        except BaseException:
            raw.close()
            raise
        self._sock = sock
        self._buffer = bytearray()
        self._read = 0

    def command(self, argv: Sequence[str]) -> Any:
        self._sock.sendall(_encode(argv))
        return self._reply(0)

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionResetError("closed")
        self._read += len(chunk)
        if self._read > READ_LIMIT_BYTES:
            raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
        self._buffer += chunk

    def _line(self) -> bytes:
        while True:
            end = self._buffer.find(b"\r\n")
            if end >= 0:
                line = bytes(self._buffer[:end])
                del self._buffer[: end + 2]
                return line
            self._fill()

    def _exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._fill()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _reply(self, depth: int) -> Any:
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode("utf-8", "replace")
        if kind == b"-":
            raise RedisReplyError(rest.decode("utf-8", "replace"))
        try:
            number = int(rest)
        except ValueError:
            raise RedisProtocolError("bad length") from None
        if kind == b":":
            return number
        if kind == b"$":
            if number < 0:
                return None
            if number > READ_LIMIT_BYTES:
                raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
            data = self._exact(number + 2)
            if data[-2:] != b"\r\n":
                raise RedisProtocolError("bad bulk")
            return data[:-2]
        if kind == b"*":
            if number < 0:
                return None
            if depth >= _MAX_REPLY_DEPTH or number > READ_LIMIT_BYTES:
                raise RedisProtocolError("too deep")
            return [self._reply(depth + 1) for _ in range(number)]
        raise RedisProtocolError("unknown type")

    def _shut(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def cancel(self) -> None:
        self._shut()

    def close(self) -> None:
        self._shut()


def default_connector(config: RedisConfig, timeout: float) -> RedisConnection:
    return _SocketConnection(config, timeout)


# -- projection -------------------------------------------------------------------


def _value(value: object) -> Any:
    """A reply value as JSON: UTF-8 text, else ``0x`` hex."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + data.hex()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return str(value)


def _prefix_value(data: bytes) -> Any:
    """A value read only up to the cap: a trailing UTF-8 character cut by the
    cap (at most 3 bytes) is dropped so text stays text (#783)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data" and len(data) - error.start <= 3:
            try:
                return data[: error.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
        return "0x" + data.hex()


def _text(value: object) -> str:
    shown = _value(value)
    return shown if isinstance(shown, str) else str(shown)


def _cut(value: Any, path: str, cut: list[str]) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            cut.append(path)
            return value[:MAX_VALUE_CHARS]
        return value
    if isinstance(value, dict):
        return {k: _cut(v, f"{path}.{k}" if path else k, cut) for k, v in value.items()}
    if isinstance(value, list):
        return [
            _cut(v, f"{path}.{i}" if path else str(i), cut) for i, v in enumerate(value)
        ]
    return value


def _rows(
    raw: Sequence[dict[str, Any]], marked: Mapping[int, list[str]] | None = None
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    total = 0
    for index, item in enumerate(raw):
        cut = list((marked or {}).get(index, []))
        row = _cut(item, "", cut)
        if cut:
            cut = list(dict.fromkeys(cut))
            row["_truncated_fields"] = cut
            total += len(cut)
        rows.append(row)
    return rows, total


def _iso(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config_secret(name: str) -> bool:
    """The tool-side ``CONFIG GET`` denylist: shown redacted even when empty."""
    lowered = name.lower()
    return (
        lowered in {"requirepass", "masterauth", "masteruser", "primaryuser"}
        or fnmatch.fnmatchcase(lowered, "tls-*-pass")
        or any(word in lowered for word in ("pass", "auth", "secret"))
        or _credential_name(name)
    )


_SLOWLOG_SECRET_COMMANDS = frozenset({"AUTH", "HELLO", "MIGRATE", "ACL"})


def _slowlog_args(args: Sequence[Any]) -> list[Any]:
    shown = [_value(arg) for arg in args]
    for index, arg in enumerate(shown):
        if isinstance(arg, str) and arg.upper() in _SLOWLOG_SECRET_COMMANDS:
            return [
                *shown[: index + 1],
                *([REDACTED_CREDENTIAL] * (len(shown) - index - 1)),
            ]
    return shown


def _score(value: object) -> Any:
    text = _text(value)
    try:
        number = float(text)
    except ValueError:
        return text
    return number if math.isfinite(number) else text


# -- transport --------------------------------------------------------------------


def _refused(note: str) -> TransportResponse:
    code = note.split(":", 1)[0]
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [note]},
    )


def _answered(note: str) -> TransportResponse:
    code = note.split(":", 1)[0]
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        view_fields={"notes": [note]},
    )


_AUTH_UNSUPPORTED = (
    "wrong number of arguments",
    "without any password configured",
    "no password is set",
)
_ACL_UNSUPPORTED = ("unknown command", "unknown subcommand")


def _reply_failure(error: RedisReplyError, stage: str) -> TransportResponse:
    """Only the reply's first word is read; the server text is never shown."""
    word = error.word
    lowered = error.message.lower()
    if word in ("LOADING", "BUSY"):
        raise TransportUnavailable("REDIS_UNAVAILABLE")
    if word == "NOAUTH":
        return _answered(
            "REDIS_AUTH_REQUIRED: the server requires authentication before "
            "this command."
        )
    if word == "WRONGPASS":
        return _answered(
            "REDIS_AUTH_FAILED: the server refused the configured username or password."
        )
    if word == "NOPERM":
        return _answered(
            "REDIS_FORBIDDEN: the account may not run this read or read this key."
        )
    if word in ("MOVED", "ASK"):
        return _answered(
            "REDIS_CLUSTER_UNSUPPORTED: the server is a cluster node and "
            "redirected the read; only a single node is supported."
        )
    if (stage == "auth" and any(text in lowered for text in _AUTH_UNSUPPORTED)) or (
        stage == "acl" and any(text in lowered for text in _ACL_UNSUPPORTED)
    ):
        return _answered(
            "REDIS_ACCOUNT_UNSUPPORTED: the server has no ACL users (password "
            "only, or older than Redis 6), so the account cannot be checked; "
            "nothing was run."
        )
    return _answered("REDIS_SOURCE_ERROR: the server failed the call.")


class _Wire:
    """One call's connection behind the single send point."""

    def __init__(self, conn: RedisConnection) -> None:
        self._conn = conn

    def _send(self, argv: Sequence[str]) -> Any:
        """Every command of every call passes here; anything outside the
        frozen table is refused before a byte is written."""
        command = list(argv)
        if not _command_allowed(command):
            raise RedisCommandRefused("REDIS_COMMAND_NOT_ALLOWED")
        return self._conn.command(command)


Send = Callable[[Sequence[str]], Any]
Plan = Callable[[Send, bool], TransportResponse]


class RedisTransport:
    """Serves the six tools over a new connection per call (no pool, no
    retry). Every refusal of the model's input is decided before
    connecting."""

    def __init__(
        self,
        config: RedisConfig,
        *,
        connector: Connector | None = None,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._connector = connector or default_connector
        self._timeout = request_timeout_seconds
        self._monotonic = monotonic or time.monotonic

    # -- input checks ---------------------------------------------------------

    @staticmethod
    def _limit(
        params: Mapping[str, object], default: int, maximum: int
    ) -> int | TransportResponse:
        limit = params.get("limit", default)
        if limit is None:
            limit = default
        if type(limit) is not int or not 1 <= limit <= maximum:
            return _refused(
                f"INVALID_LIMIT: limit must be an integer from 1 to {maximum}."
            )
        return limit

    @staticmethod
    def _name(
        params: Mapping[str, object], key: str, what: str, default: str | None
    ) -> str | TransportResponse:
        value = params.get(key)
        if value is None and default is not None:
            return default
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= MAX_NAME_CHARS
            or not value.isprintable()
        ):
            return _refused(
                f"INVALID_IDENTIFIER: {what} must be 1 to {MAX_NAME_CHARS} "
                "printable characters."
            )
        return value

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        p = request.params
        plan: Plan
        if request.tool == INFO_TOOL:
            raw = p.get("sections")
            if raw is None or raw == "":
                sections: list[str] = []
            elif isinstance(raw, str):
                sections = [
                    part.strip().lower() for part in raw.split(",") if part.strip()
                ]
                if not sections or any(part not in _INFO_SECTIONS for part in sections):
                    return _refused(
                        "INVALID_PARAMS: sections must be a comma-separated list "
                        "of " + ", ".join(_INFO_SECTIONS) + "."
                    )
                sections = list(dict.fromkeys(sections))
            else:
                return _refused("INVALID_PARAMS: sections must be a string.")
            plan = lambda send, legacy: self._info(request, send, legacy, sections)  # noqa: E731
        elif request.tool == CONFIG_TOOL:
            pattern = self._name(p, "pattern", "pattern", "*")
            if isinstance(pattern, TransportResponse):
                return pattern
            plan = lambda send, legacy: self._config_get(request, send, pattern)  # noqa: E731
        elif request.tool == SCAN_TOOL:
            match = self._name(p, "pattern", "pattern", "*")
            if isinstance(match, TransportResponse):
                return match
            kind = p.get("type")
            if kind is not None and kind not in _KEY_TYPES:
                return _refused(
                    "INVALID_PARAMS: type must be one of " + ", ".join(_KEY_TYPES) + "."
                )
            scan_limit = self._limit(p, DEFAULT_LIMIT, MAX_LIMIT)
            if isinstance(scan_limit, TransportResponse):
                return scan_limit
            plan = lambda send, legacy: self._scan(  # noqa: E731
                request,
                send,
                match,
                kind if isinstance(kind, str) else None,
                scan_limit,
            )
        elif request.tool == KEY_INFO_TOOL:
            key = self._name(p, "key", "key", None)
            if isinstance(key, TransportResponse):
                return key
            plan = lambda send, legacy: self._key_info(request, send, key)  # noqa: E731
        elif request.tool == VALUE_TOOL:
            value_key = self._name(p, "key", "key", None)
            if isinstance(value_key, TransportResponse):
                return value_key
            value_limit = self._limit(p, DEFAULT_LIMIT, MAX_LIMIT)
            if isinstance(value_limit, TransportResponse):
                return value_limit
            plan = lambda send, legacy: self._get_value(  # noqa: E731
                request, send, value_key, value_limit
            )
        else:
            slow_limit = self._limit(p, SLOWLOG_DEFAULT_LIMIT, SLOWLOG_MAX_LIMIT)
            if isinstance(slow_limit, TransportResponse):
                return slow_limit
            plan = lambda send, legacy: self._slowlog(request, send, slow_limit)  # noqa: E731
        return self._session(plan)

    # -- one call ----------------------------------------------------------------

    def _session(self, plan: Plan) -> TransportResponse:
        deadline = self._monotonic() + self._timeout
        state = {"cancelled": False}

        def remaining() -> float:
            left = deadline - self._monotonic()
            if left <= 0:
                raise TransportTimeout("TIMEOUT", source_contact="possible")
            return left

        connect_timeout = max(
            _MIN_CONNECT_SECONDS,
            min(remaining() - _CONNECT_MARGIN, _MAX_CONNECT_SECONDS),
        )
        try:
            conn = self._connector(self._config, connect_timeout)
        except TransportError:
            raise
        except Exception:
            # refused, DNS, TLS handshake, connect timeout: all before the
            # deadline (the connect timeout ends at least 2 s before it)
            raise TransportUnavailable("REDIS_UNAVAILABLE") from None

        def on_deadline() -> None:
            state["cancelled"] = True
            conn.cancel()

        timer = threading.Timer(max(0.0, deadline - self._monotonic()), on_deadline)
        timer.daemon = True
        timer.start()
        stage = "auth"
        wire = _Wire(conn)

        def send(argv: Sequence[str]) -> Any:
            remaining()
            return wire._send(argv)

        try:
            config = self._config
            send(["AUTH", config.username, config.password])
            stage = "handshake"
            send(["CLIENT", "SETNAME", _CLIENT_NAME])
            if config.database != 0:
                send(["SELECT", str(config.database)])
            stage = "acl"
            whoami = _word(send(["ACL", "WHOAMI"]))
            if (
                whoami is None
                or not 1 <= len(whoami) <= MAX_NAME_CHARS
                or not whoami.isprintable()
                or any(ch.isspace() for ch in whoami)
            ):
                return _answered(account_problem(None) or "REDIS_ACCOUNT_NOT_READ_ONLY")
            getuser = send(["ACL", "GETUSER", whoami])
            problem = account_problem(getuser)
            if problem is not None:
                return _answered(problem)
            stage = "tool"
            return plan(send, _legacy_server(getuser))
        except TransportError:
            if state["cancelled"]:
                raise TransportTimeout("TIMEOUT", source_contact="possible") from None
            raise
        except RedisReplyError as error:
            if state["cancelled"]:
                raise TransportTimeout("TIMEOUT", source_contact="possible") from None
            return _reply_failure(error, stage)
        except Exception as error:
            late = state["cancelled"] or self._monotonic() >= deadline - 0.05
            if late:
                raise TransportTimeout("TIMEOUT", source_contact="possible") from None
            if isinstance(error, (OSError, EOFError)):
                # reset, TLS error, socket timeout before the deadline
                raise TransportUnavailable("REDIS_UNAVAILABLE") from None
            return _answered("REDIS_SOURCE_ERROR: the server failed the call.")
        finally:
            timer.cancel()
            conn.close()

    # -- views ------------------------------------------------------------------

    def _view(self, request: TransportRequest, **extra: Any) -> dict[str, Any]:
        end = request.window.end
        return {
            "database": self._config.database,
            "notes": [
                "Read at query time (this call's observed time): the server as it "
                "is now, not as it was in the authorized window ending "
                f"{_iso(end)}.",
                *extra.pop("extra_notes", ()),
            ],
            "cannot_prove": _CANNOT_PROVE,
            **extra,
        }

    @staticmethod
    def _respond(
        rows: list[dict[str, Any]],
        unit: str,
        view: dict[str, Any],
        incomplete: bool,
    ) -> TransportResponse:
        body = json.dumps(
            {"results": rows}, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(body) > READ_LIMIT_BYTES:
            raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
        return TransportResponse(
            body=body,
            row_unit=unit,
            backend_rows_returned=len(rows),
            incomplete=incomplete,
            view_fields=view,
        )

    def _not_found(
        self, request: TransportRequest, key: str, unit: str
    ) -> TransportResponse:
        view = self._view(
            request,
            key=key,
            extra_notes=[
                "KEY_NOT_FOUND: no key with this name in the configured database."
            ],
        )
        return self._respond([], unit, view, False)

    # -- tools ------------------------------------------------------------------

    def _info(
        self, request: TransportRequest, send: Send, legacy: bool, sections: list[str]
    ) -> TransportResponse:
        if not sections:
            texts = [send(["INFO"])]
        elif legacy:
            texts = [send(["INFO", section]) for section in sections]
        else:
            texts = [send(["INFO", *sections])]
        found: dict[str, dict[str, str]] = {}
        for text in texts:
            current = "default"
            for line in _text(text).splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    current = line[1:].strip().lower() or "default"
                    found.setdefault(current, {})
                elif ":" in line:
                    name, value = line.split(":", 1)
                    found.setdefault(current, {})[name] = value
        raw = [{"section": name, "fields": values} for name, values in found.items()]
        rows, cut = _rows(raw)
        view = self._view(
            request, sections=sections or ["default"], long_values_cut=cut
        )
        return self._respond(rows, "sections", view, False)

    def _config_get(
        self, request: TransportRequest, send: Send, pattern: str
    ) -> TransportResponse:
        reply = send(["CONFIG", "GET", pattern])
        if isinstance(reply, Mapping):
            pairs = list(reply.items())
        elif isinstance(reply, (list, tuple)) and len(reply) % 2 == 0:
            pairs = list(zip(reply[0::2], reply[1::2], strict=True))
        else:
            raise RedisProtocolError("CONFIG GET reply")
        items: list[dict[str, Any]] = []
        for raw_name, raw_value in pairs:
            name = _text(raw_name)
            if _config_secret(name):
                value: Any = REDACTED_CREDENTIAL
            else:
                value = _value(raw_value)
            items.append({"name": name, "value": value})
        items.sort(key=lambda row: str(row["name"]))
        more = len(items) > MAX_CONFIG_ROWS
        rows, cut = _rows(items[:MAX_CONFIG_ROWS])
        view = self._view(
            request,
            pattern=pattern,
            long_values_cut=cut,
            incomplete_reason=f"only the first {MAX_CONFIG_ROWS} parameters are shown"
            if more
            else None,
        )
        return self._respond(rows, "parameters", view, more)

    @staticmethod
    def _cursor_reply(reply: Any) -> tuple[str, list[Any]]:
        if (
            not isinstance(reply, (list, tuple))
            or len(reply) != 2
            or not isinstance(reply[1], (list, tuple))
        ):
            raise RedisProtocolError("cursor reply")
        return _text(reply[0]), list(reply[1])

    def _scan(
        self,
        request: TransportRequest,
        send: Send,
        pattern: str,
        kind: str | None,
        limit: int,
    ) -> TransportResponse:
        keys: dict[str, None] = {}
        cursor = "0"
        iterations = 0
        more = stopped = False
        while True:
            argv = ["SCAN", cursor, "MATCH", pattern, "COUNT", str(SCAN_COUNT)]
            if kind is not None:
                argv += ["TYPE", kind]
            cursor, batch = self._cursor_reply(send(argv))
            iterations += 1
            for item in batch:
                keys.setdefault(_text(item))
            if len(keys) > limit:
                more = True
                break
            if cursor == "0":
                break
            if iterations >= MAX_SCAN_ITERATIONS:
                stopped = True
                break
        rows, cut = _rows([{"key": key} for key in list(keys)[:limit]])
        reason = None
        if more:
            reason = "reached limit; more keys match"
        elif stopped:
            reason = (
                f"the scan stopped after {MAX_SCAN_ITERATIONS} iterations; more "
                "keys may match"
            )
        view = self._view(
            request,
            pattern=pattern,
            key_type=kind,
            limit=limit,
            long_values_cut=cut,
            incomplete_reason=reason,
        )
        return self._respond(rows, "keys", view, more or stopped)

    def _key_info(
        self, request: TransportRequest, send: Send, key: str
    ) -> TransportResponse:
        kind = _text(send(["TYPE", key])).lower()
        if kind == "none":
            return self._not_found(request, key, "keys")
        pttl = send(["PTTL", key])
        encoding = send(["OBJECT", "ENCODING", key])
        memory = send(["MEMORY", "USAGE", key])
        command = _LENGTH_COMMANDS.get(kind)
        length = send([command, key]) if command is not None else None
        if pttl == -2:
            return self._not_found(request, key, "keys")
        row = {
            "key": key,
            "type": kind,
            "ttl_ms": pttl if isinstance(pttl, int) and pttl >= 0 else None,
            "encoding": None if encoding is None else _text(encoding),
            "memory_usage_bytes": memory if isinstance(memory, int) else None,
            "length": length if isinstance(length, int) else None,
        }
        rows, cut = _rows([row])
        view = self._view(request, key=key, long_values_cut=cut)
        return self._respond(rows, "keys", view, False)

    def _scan_members(
        self, send: Send, command: str, key: str, limit: int, pairs: bool
    ) -> tuple[list[Any], bool, bool]:
        seen: dict[Any, Any] = {}
        cursor = "0"
        iterations = 0
        while True:
            cursor, batch = self._cursor_reply(
                send([command, key, cursor, "COUNT", str(limit + 1)])
            )
            iterations += 1
            if pairs:
                if len(batch) % 2:
                    raise RedisProtocolError("HSCAN reply")
                for name, value in zip(batch[0::2], batch[1::2], strict=True):
                    seen.setdefault(_text(name), value)
            else:
                for member in batch:
                    seen.setdefault(_text(member), None)
            if len(seen) > limit:
                return list(seen.items()), True, False
            if cursor == "0":
                return list(seen.items()), False, False
            if iterations >= MAX_SCAN_ITERATIONS:
                return list(seen.items()), False, True

    def _get_value(
        self, request: TransportRequest, send: Send, key: str, limit: int
    ) -> TransportResponse:
        kind = _text(send(["TYPE", key])).lower()
        if kind == "none":
            return self._not_found(request, key, "elements")
        marked: dict[int, list[str]] = {}
        more = stopped = False
        raw: list[dict[str, Any]]
        unit = "elements"
        if kind == "string":
            data = send(["GETRANGE", key, "0", str(STRING_READ_BYTES - 1)])
            if data is None:
                return self._not_found(request, key, "elements")
            full = isinstance(data, (bytes, str)) and len(data) >= STRING_READ_BYTES
            value: Any = (
                _prefix_value(bytes(data))
                if full and isinstance(data, bytes)
                else _value(data)
            )
            if _credential_name(key) or _session_word(key):
                value = REDACTED_CREDENTIAL
            elif full:
                # only the first 8 KiB were read: the value may go on
                marked[0] = ["value"]
            raw = [{"value": value}]
        elif kind == "hash":
            unit = "fields"
            items, more, stopped = self._scan_members(send, "HSCAN", key, limit, True)
            # A field named for a session (``sessionAttr:…``) keeps the
            # pre-#366 rule: its value is a stored session, never shown.
            raw = [
                {name: REDACTED_CREDENTIAL if _session_word(name) else _value(value)}
                for name, value in items[:limit]
            ]
        elif kind == "set":
            items, more, stopped = self._scan_members(send, "SSCAN", key, limit, False)
            raw = [{"member": name} for name, _ in items[:limit]]
        elif kind == "list":
            reply = send(["LRANGE", key, "0", str(limit)])
            values = list(reply) if isinstance(reply, (list, tuple)) else []
            more = len(values) > limit
            raw = [
                {"index": index, "value": _value(item)}
                for index, item in enumerate(values[:limit])
            ]
        elif kind == "zset":
            reply = send(["ZRANGE", key, "0", str(limit), "WITHSCORES"])
            flat = list(reply) if isinstance(reply, (list, tuple)) else []
            if len(flat) % 2:
                raise RedisProtocolError("ZRANGE reply")
            members = list(zip(flat[0::2], flat[1::2], strict=True))
            more = len(members) > limit
            raw = [
                {"member": _value(member), "score": _score(score)}
                for member, score in members[:limit]
            ]
        elif kind == "stream":
            unit = "entries"
            reply = send(["XREVRANGE", key, "+", "-", "COUNT", str(limit + 1)])
            entries = list(reply) if isinstance(reply, (list, tuple)) else []
            more = len(entries) > limit
            raw = []
            for entry in entries[:limit]:
                if (
                    not isinstance(entry, (list, tuple))
                    or len(entry) != 2
                    or not isinstance(entry[1], (list, tuple))
                    or len(entry[1]) % 2
                ):
                    raise RedisProtocolError("XREVRANGE reply")
                fields = {
                    _text(name): _value(value)
                    for name, value in zip(entry[1][0::2], entry[1][1::2], strict=True)
                }
                raw.append({"id": _text(entry[0]), "fields": fields})
        else:
            return _answered(
                f"REDIS_TYPE_UNSUPPORTED: keys of type {kind[:40]} cannot be "
                "read by this tool; use redis_key_info."
            )
        rows, cut = _rows(raw, marked)
        reason = None
        if more:
            reason = f"reached limit; the {kind} has more {unit}"
        elif stopped:
            reason = (
                f"the scan stopped after {MAX_SCAN_ITERATIONS} iterations; the "
                f"{kind} may have more {unit}"
            )
        view = self._view(
            request,
            key=key,
            key_type=kind,
            limit=limit,
            long_values_cut=cut,
            incomplete_reason=reason,
        )
        return self._respond(rows, unit, view, more or stopped)

    def _slowlog(
        self, request: TransportRequest, send: Send, limit: int
    ) -> TransportResponse:
        reply = send(["SLOWLOG", "GET", str(limit + 1)])
        entries = list(reply) if isinstance(reply, (list, tuple)) else []
        more = len(entries) > limit
        raw: list[dict[str, Any]] = []
        for entry in entries[:limit]:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) < 4
                or not isinstance(entry[1], int)
                or not isinstance(entry[3], (list, tuple))
            ):
                raise RedisProtocolError("SLOWLOG reply")
            raw.append(
                {
                    "id": entry[0],
                    "time": _iso(
                        _dt.datetime.fromtimestamp(entry[1], tz=_dt.timezone.utc)
                    ),
                    "duration_us": entry[2],
                    "command": _slowlog_args(entry[3]),
                    "client": _text(entry[4]) if len(entry) > 4 else None,
                    "client_name": _text(entry[5]) if len(entry) > 5 else None,
                }
            )
        rows, cut = _rows(raw)
        view = self._view(
            request,
            limit=limit,
            long_values_cut=cut,
            incomplete_reason=f"reached limit; the slow log has more than {limit} entries"
            if more
            else None,
        )
        return self._respond(rows, "entries", view, more)


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    units = {
        INFO_TOOL: "sections",
        CONFIG_TOOL: "parameters",
        SCAN_TOOL: "keys",
        KEY_INFO_TOOL: "keys",
        VALUE_TOOL: "elements",
        SLOWLOG_TOOL: "entries",
    }
    summary: dict[str, object] = {"row_unit": units.get(tool, "elements")}
    if tool in (SCAN_TOOL, VALUE_TOOL, SLOWLOG_TOOL):
        summary["suggestion"] = "Lower limit or narrow the pattern."
    elif tool == INFO_TOOL:
        summary["suggestion"] = "Ask for fewer sections (commandstats is large)."
    elif tool == CONFIG_TOOL:
        summary["suggestion"] = "Use a narrower pattern."
    return summary


# -- model-visible face and registrations ---------------------------------------------

_CANNOT_PROVE = (
    "This is the server as it is at query time, not its state during the "
    "incident window. A key that is not listed may lie past limit or the scan "
    "budget, or be invisible to the read-only account. Values are redacted by "
    "name only; a secret under another name or a bare secret value is not "
    "recognised. The slow log and INFO can show other clients' commands and "
    "addresses."
)
_READ_ONLY_TEXT = (
    "Only fixed read commands are sent to one configured Redis or Valkey node "
    "and database; there is no command parameter and no way to write. The tool "
    "first checks that the account's ACL allows only reads "
    "(REDIS_ACCOUNT_NOT_READ_ONLY otherwise, nothing run)."
)
_ERRORS_TEXT = (
    "Bad input is INVALID_PARAMS before sending; a failed login or a missing "
    "permission is SOURCE_FORBIDDEN; a server that cannot be reached or is "
    "loading is SOURCE_UNAVAILABLE; a cluster redirect is INVALID_PARAMS "
    "(REDIS_CLUSTER_UNSUPPORTED); a call past its 30 s deadline is a timeout; "
    "a missing key is no data (KEY_NOT_FOUND)."
)
_LIMITS = (
    f"At most {MAX_LIMIT} keys, fields or elements (default {DEFAULT_LIMIT}); "
    f"the slow log at most {SLOWLOG_MAX_LIMIT} entries (default "
    f"{SLOWLOG_DEFAULT_LIMIT}); config at most {MAX_CONFIG_ROWS} parameters. "
    f"Strings are read up to {STRING_READ_BYTES} bytes and every value is cut "
    f"to {MAX_VALUE_CHARS} characters and marked; non-UTF-8 values are 0x hex. "
    f"A key scan stops after {MAX_SCAN_ITERATIONS} SCAN calls and is marked "
    "incomplete. One call reads at most 2 MiB and the view is limited to "
    "25,000 tokens (refused whole above it). 30 s per call on a new "
    "connection, no retry."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. The server is "
    "read at query time whatever the window says; there are no time "
    "parameters."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. The node, "
    "database and account come from configuration; the model cannot choose "
    f"them. key and pattern are 1 to {MAX_NAME_CHARS} printable characters."
)


def _schema_entry(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> Mapping[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _limit_property(what: str, maximum: int, default: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "description": f"Most {what} to return, 1 to {maximum} (default {default}).",
    }


_KEY_PROPERTY = {"type": "string", "description": "The exact key name."}

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    _schema_entry(
        INFO_TOOL,
        "Server status of the configured Redis or Valkey node from INFO: "
        "memory, clients, replication, persistence, stats and more. "
        + _READ_ONLY_TEXT
        + " "
        + _ERRORS_TEXT,
        {
            "sections": {
                "type": "string",
                "description": (
                    "Comma-separated sections: " + ", ".join(_INFO_SECTIONS) + ". "
                    "Default: the server's default set."
                ),
            }
        },
        [],
    ),
    _schema_entry(
        CONFIG_TOOL,
        "Configuration parameters of the configured Redis or Valkey node "
        "(CONFIG GET), sorted by name; password-like parameters are always "
        "redacted. " + _READ_ONLY_TEXT,
        {
            "pattern": {
                "type": "string",
                "description": "Glob pattern of parameter names, e.g. maxmemory* (default *).",
            }
        },
        [],
    ),
    _schema_entry(
        SCAN_TOOL,
        "List key names matching a glob pattern with SCAN (never KEYS), "
        "optionally of one type. " + _READ_ONLY_TEXT,
        {
            "pattern": {
                "type": "string",
                "description": "Glob pattern of key names, e.g. session:* (default *).",
            },
            "type": {
                "type": "string",
                "description": "Only keys of this type: " + ", ".join(_KEY_TYPES) + ".",
            },
            "limit": _limit_property("keys", MAX_LIMIT, DEFAULT_LIMIT),
        },
        [],
    ),
    _schema_entry(
        KEY_INFO_TOOL,
        "Metadata of one key: type, TTL, encoding, memory usage and length. "
        + _READ_ONLY_TEXT,
        {"key": _KEY_PROPERTY},
        ["key"],
    ),
    _schema_entry(
        VALUE_TOOL,
        "Read one key's value by its type: the first 8 KiB of a string, hash "
        "fields, list or set elements, sorted-set members with scores, or the "
        "newest stream entries. " + _READ_ONLY_TEXT + " " + _ERRORS_TEXT,
        {
            "key": _KEY_PROPERTY,
            "limit": _limit_property("fields or elements", MAX_LIMIT, DEFAULT_LIMIT),
        },
        ["key"],
    ),
    _schema_entry(
        SLOWLOG_TOOL,
        "The newest entries of the slow log (SLOWLOG GET): time, duration and "
        "command of slow calls; credential arguments are redacted. " + _READ_ONLY_TEXT,
        {"limit": _limit_property("entries", SLOWLOG_MAX_LIMIT, SLOWLOG_DEFAULT_LIMIT)},
        [],
    ),
)

_ERROR_CLASSES: Mapping[str, str] = {
    "INVALID_LIMIT": "INVALID_PARAMS",
    "INVALID_IDENTIFIER": "INVALID_PARAMS",
    "INVALID_PARAMS": "INVALID_PARAMS",
    "REDIS_CLUSTER_UNSUPPORTED": "INVALID_PARAMS",
    "REDIS_TYPE_UNSUPPORTED": "INVALID_PARAMS",
    "REDIS_AUTH_REQUIRED": "SOURCE_FORBIDDEN",
    "REDIS_AUTH_FAILED": "SOURCE_FORBIDDEN",
    "REDIS_FORBIDDEN": "SOURCE_FORBIDDEN",
    "REDIS_ACCOUNT_NOT_READ_ONLY": "SOURCE_FORBIDDEN",
    "REDIS_ACCOUNT_UNSUPPORTED": "SOURCE_FORBIDDEN",
    "REDIS_SOURCE_ERROR": "SOURCE_ERROR",
}

_RETURNS: Mapping[str, str] = {
    INFO_TOOL: "Sections under results, each {section, fields} with the INFO fields as strings.",
    CONFIG_TOOL: "Parameters under results, each {name, value}, sorted by name.",
    SCAN_TOOL: "Key names under results, each {key}.",
    KEY_INFO_TOOL: "One row under results: key, type, ttl_ms (null without expiry), encoding, memory_usage_bytes and length.",
    VALUE_TOOL: "The value under results by type: string {value}; hash one {field: value} per field; list {index, value}; set {member}; zset {member, score}; stream {id, fields}, newest first.",
    SLOWLOG_TOOL: "Slow log entries under results, newest first, each {id, time, duration_us, command, client, client_name}.",
}
_PARAMETERS: Mapping[str, Mapping[str, ParameterSpec]] = {
    INFO_TOOL: {"sections": ParameterSpec("string", description="Sections.")},
    CONFIG_TOOL: {"pattern": ParameterSpec("string", description="Name pattern.")},
    SCAN_TOOL: {
        "pattern": ParameterSpec("string", description="Key pattern."),
        "type": ParameterSpec("string", description="Key type."),
        "limit": ParameterSpec("integer", description="Most keys."),
    },
    KEY_INFO_TOOL: {"key": ParameterSpec("string", required=True, description="Key.")},
    VALUE_TOOL: {
        "key": ParameterSpec("string", required=True, description="Key."),
        "limit": ParameterSpec("integer", description="Most elements."),
    },
    SLOWLOG_TOOL: {"limit": ParameterSpec("integer", description="Most entries.")},
}


def redis_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The six registrations (``source="otel-demo"``, the lab target's;
    ``may_contain_secrets=True`` -- values, the slow log and INFO can carry
    other clients' data)."""
    return tuple(
        ToolRegistration(
            name=name,
            version="v1",
            source=otel_demo.SOURCE,
            verb="get",
            parameters=dict(_PARAMETERS[name]),
            result_path=("results",),
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=READ_LIMIT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            description=ToolDescription(
                returns=_RETURNS[name],
                window_format=_WINDOW_FORMAT,
                values_format=_VALUES_FORMAT,
                limits=_LIMITS,
                cannot_prove=_CANNOT_PROVE,
            ),
            may_contain_secrets=True,
            error_classes=dict(_ERROR_CLASSES),
        )
        for name in TOOL_NAMES
    )
