"""Read-only SQL queries: ``database_sql_query``, ``database_sql_list_tables``
and ``database_sql_describe_table`` (C6a, #570).

The upstream HolmesGPT ``database/sql`` toolset's default tool names
(``holmes/plugins/toolsets/database/database.py`` at ``a0009195``), for one
configured PostgreSQL, MySQL or MariaDB database. The public interface is
``docs/tasks/2026-11-20-tool-expansion-c6.md``; the decisions are r11 C6-U1=a
and the reversible defaults C6-0 to C6-22 of the C6 pre-review.

Read-only is the tool's guarantee (PRODUCT-CONSTRAINTS line 18), in four
layers:

* the operator's read-only account (``sql-ro``, the first layer);
* a statement check before anything is sent (``sql_statement_problem``, the
  second layer; a deny list, so never claimed to guarantee read-only alone);
* a read-only transaction around every statement the tool sends
  (``BEGIN READ ONLY`` / ``START TRANSACTION READ ONLY``), always rolled
  back;
* after every connect, before the model's statement, a fixed check of the
  account's own privileges (C6-U1): an account that does not look read-only
  is ``SQL_ACCOUNT_NOT_READ_ONLY`` and the model's statement is never sent.

OpsPilot's own database is kept apart three ways (C6-8): the worker refuses
to start when the address matches an ``OPSPILOT_*DSN`` host and port; the
credential is only ever the ``sql-ro`` slot; and a PostgreSQL target holding
an ``opspilot_runs`` relation is ``SQL_TARGETS_OPSPILOT_DB`` on every call.

The tool reads the database as it is at query time (C3 section 8, current
state): no source time, no time filter added to the model's SQL.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.registry import (
    MAX_RESULT_BYTES,
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolContractError,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "CA_FILE_ENV",
    "CREDENTIAL_REF",
    "DESCRIBE_TOOL",
    "DIALECTS",
    "ENDPOINT_SELECTOR",
    "LIST_TOOL",
    "PASSWORD_ENV",
    "QUERY_TOOL",
    "TLS_ENV",
    "TOOL_NAMES",
    "URL_ENV",
    "USERNAME_ENV",
    "SqlConfig",
    "SqlConnection",
    "SqlDriverError",
    "SqlTransport",
    "overflow_summary",
    "session_problem",
    "sql_dialect",
    "sql_registrations",
    "sql_statement_problem",
    "tool_schemas",
]

QUERY_TOOL = "database_sql_query"
LIST_TOOL = "database_sql_list_tables"
DESCRIBE_TOOL = "database_sql_describe_table"
TOOL_NAMES: tuple[str, ...] = (QUERY_TOOL, LIST_TOOL, DESCRIBE_TOOL)

#: C6-7: this transport's own credential slot; never another backend's.
CREDENTIAL_REF = "sql-ro"
#: C6-10: where the address (no credential) travels in the target selector.
ENDPOINT_SELECTOR = "sql_endpoint"

URL_ENV = "OPSPILOT_SQL_URL"
USERNAME_ENV = "OPSPILOT_SQL_USERNAME"
PASSWORD_ENV = "OPSPILOT_SQL_PASSWORD"
CA_FILE_ENV = "OPSPILOT_SQL_CA_FILE"
TLS_ENV = "OPSPILOT_SQL_TLS"
_OTHER_ENVS = (USERNAME_ENV, PASSWORD_ENV, CA_FILE_ENV, TLS_ENV)

#: URL scheme -> dialect (C6-7). MySQL and MariaDB share one dialect.
_SCHEMES: Mapping[str, str] = {
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    # C6b
    "clickhouse+http": "clickhouse",
    "clickhouse+https": "clickhouse",
    "mssql": "mssql",
    "mssql+pytds": "mssql",
}
DIALECTS: tuple[str, ...] = ("postgresql", "mysql", "clickhouse", "mssql")
_DEFAULT_PORTS: Mapping[str, int] = {
    "postgresql": 5432,
    "mysql": 3306,
    "clickhouse": 8123,
    "mssql": 1433,
}
_CLICKHOUSE_HTTPS_PORT = 8443
_TLS_MODES = ("verify-full", "require", "disable")

REQUEST_TIMEOUT_SECONDS = 30.0
#: C6-6: the database-side statement timeout is this much shorter than the
#: time left, and never below the floor.
_SERVER_TIMEOUT_MARGIN = 2.0
_SERVER_TIMEOUT_FLOOR = 1.0
READ_LIMIT_BYTES = MAX_RESULT_BYTES
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_LIST_ROWS = 500
MAX_QUERY_CHARS = 20_000
MAX_VALUE_CHARS = 2000
MAX_IDENTIFIER_CHARS = 128
_ERROR_TEXT_CHARS = 500
_APPLICATION_NAME = "opspilot-sql-tool"

ACCOUNT_CHECK_MARKER = "/* opspilot:account-check */"
CATALOG_MARKER = "/* opspilot:catalog */"


# -- configuration (C6-7, C6-8) -----------------------------------------------


def _url_parts(value: str) -> tuple[str, str, int | None, str]:
    """``(dialect, host, port, database)`` of a valid address, else
    ``SQL_CONFIG_INVALID``."""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ToolContractError("SQL_CONFIG_INVALID") from None
    dialect = _SCHEMES.get(parts.scheme.lower())
    if (
        dialect is None
        or not host
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or "," in parts.netloc
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or not value.isprintable()
        or any(ch.isspace() for ch in value)
    ):
        raise ToolContractError("SQL_CONFIG_INVALID")
    database = parts.path[1:] if parts.path.startswith("/") else ""
    if not database or "/" in database:
        raise ToolContractError("SQL_CONFIG_INVALID")
    database = urllib.parse.unquote(database)
    if not database.isprintable() or len(database) > MAX_IDENTIFIER_CHARS:
        raise ToolContractError("SQL_CONFIG_INVALID")
    return dialect, host, port, database


def sql_dialect(env: Mapping[str, str]) -> str | None:
    """C6-13: the dialect the profile shows, from the address's scheme only
    -- the web calls this too and never reads a credential. Unset is
    ``None``; an address this tool does not support ends the process with
    ``SQL_CONFIG_INVALID`` in both processes."""
    url = env.get(URL_ENV) or ""
    if not url:
        return None
    try:
        return _url_parts(url)[0]
    except ToolContractError as error:
        raise SystemExit(str(error)) from None


_LOOPBACK = "loopback"


def _host_key(host: str) -> str:
    """One spelling per host for the C6-8 comparison: every loopback
    address, ``localhost`` and a Unix socket directory are one host."""
    value = host.strip().lower().rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if (
        not value
        or value == "localhost"
        or value.startswith("/")
        or value.startswith("@")
    ):
        return _LOOPBACK
    if value.endswith(".localhost") or value == "localhost.localdomain":
        return _LOOPBACK
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            # libpq and MySQL clients also accept inet_aton shorthands
            # (127.1, 2130706433); compare them as the address they name.
            address = ipaddress.ip_address(socket.inet_aton(value))
        except (OSError, ValueError):
            return value
    if address.is_loopback or address.is_unspecified:
        return _LOOPBACK
    return str(address)


_DSN_ENV = re.compile(r"OPSPILOT_[A-Z0-9_]*DSN")


def _dsn_endpoints(env: Mapping[str, str]) -> set[tuple[str, int]]:
    """Every (host, port) an ``OPSPILOT_*DSN`` of this process points at
    (C6-8 first layer). A DSN with several hosts contributes each; one that
    does not parse contributes the libpq defaults, so it can only widen the
    refusal."""
    from psycopg.conninfo import conninfo_to_dict

    found: set[tuple[str, int]] = set()
    default_host = env.get("PGHOST") or ""
    default_port_text = env.get("PGPORT") or "5432"
    # The worker's mapping is its process environment; both are read so a
    # DSN in either widens the refusal (fail closed).
    for name, value in [*env.items(), *os.environ.items()]:
        if not _DSN_ENV.fullmatch(name) or not value:
            continue
        try:
            info = conninfo_to_dict(value)
        except Exception:
            info = {}
        # host and hostaddr both count when both are given (fail closed)
        hosts = [
            *str(info.get("host") or default_host).split(","),
            *(str(info["hostaddr"]).split(",") if info.get("hostaddr") else []),
        ]
        ports = str(info.get("port") or default_port_text).split(",")
        for index, host in enumerate(hosts):
            port_text = ports[index] if index < len(ports) else ports[-1]
            try:
                port = int(port_text or default_port_text)
            except ValueError:
                port = 5432
            found.add((_host_key(host), port))
    return found


@dataclass(frozen=True)
class SqlConfig:
    """The worker's ``sql-ro`` connection (C6-7): address, user name and
    password (both required), TLS mode and CA file. Read only by the worker;
    the web reads the address's scheme alone (``sql_dialect``). The password
    is never rendered."""

    url: str
    username: str
    password: str = field(repr=False)
    tls: str = "verify-full"
    ca_file: str | None = None
    dialect: str = field(init=False)
    #: ClickHouse over HTTPS (``clickhouse+https``).
    secure: bool = field(init=False)
    host: str = field(init=False)
    port: int = field(init=False)
    database: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ToolContractError("SQL_CONFIG_MISSING")
        dialect, host, port, database = _url_parts(self.url)
        secure = self.url.lower().startswith("clickhouse+https:")
        object.__setattr__(self, "dialect", dialect)
        object.__setattr__(self, "secure", secure)
        object.__setattr__(self, "host", host)
        default_port = _CLICKHOUSE_HTTPS_PORT if secure else _DEFAULT_PORTS[dialect]
        object.__setattr__(self, "port", port or default_port)
        object.__setattr__(self, "database", database)
        for value in (self.username, self.password):
            if not isinstance(value, str) or not value:
                raise ToolContractError("SQL_CONFIG_MISSING")
            if not value.isprintable():
                raise ToolContractError("SQL_CONFIG_INVALID")
        if self.tls not in _TLS_MODES:
            raise ToolContractError("SQL_CONFIG_INVALID")
        # C6b: ClickHouse's TLS follows the scheme; SQL Server has no TLS in
        # this build (python-tds needs pyOpenSSL for it, not a dependency).
        if dialect == "clickhouse" and (self.tls == "disable") == secure:
            raise ToolContractError("SQL_CONFIG_INVALID")
        if dialect == "mssql" and self.tls != "disable":
            raise ToolContractError("SQL_CONFIG_INVALID")
        if self.ca_file is not None:
            if self.tls == "disable":
                raise ToolContractError("SQL_CONFIG_INVALID")
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ToolContractError("SQL_CONFIG_INVALID")

    @property
    def endpoint(self) -> str:
        """The address without credentials, for the target selector."""
        return self.url

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SqlConfig | None:
        """``OPSPILOT_SQL_*`` (C6-7). No address: ``None`` -- unless another
        ``OPSPILOT_SQL_*`` is set (``SQL_CONFIG_INVALID``). An address whose
        host and port are those of an ``OPSPILOT_*DSN`` in this process is
        ``SQL_TARGETS_OPSPILOT_DB`` (C6-8 first layer)."""
        url = env.get(URL_ENV) or ""
        values = {name: env.get(name) or None for name in _OTHER_ENVS}
        if not url:
            if any(values.values()):
                raise ToolContractError("SQL_CONFIG_INVALID")
            return None
        if values[USERNAME_ENV] is None or values[PASSWORD_ENV] is None:
            _url_parts(url)  # an invalid address is reported as such first
            raise ToolContractError("SQL_CONFIG_MISSING")
        config = cls(
            url=url,
            username=str(values[USERNAME_ENV]),
            password=str(values[PASSWORD_ENV]),
            tls=values[TLS_ENV] or "verify-full",
            ca_file=values[CA_FILE_ENV],
        )
        if (_host_key(config.host), config.port) in _dsn_endpoints(env):
            raise ToolContractError("SQL_TARGETS_OPSPILOT_DB")
        return config


# -- statement check (C6-3, the second layer) --------------------------------

_ALLOWED_FIRST = frozenset({"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC"})
_FORBIDDEN_WORDS = frozenset(
    {
        # upstream ``_WRITE_ANYWHERE_PATTERN``
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "REPLACE",
        "MERGE",
        "GRANT",
        "REVOKE",
        "VACUUM",
        # C6-3 additions
        "INTO",
        "COPY",
        "CALL",
        "EXEC",
        "EXECUTE",
        "DO",
        "SET",
        "LOAD",
        "HANDLER",
        "LOCK",
        "OUTFILE",
        "DUMPFILE",
    }
)
#: ``FOR UPDATE`` / ``FOR SHARE`` / ``FOR NO KEY UPDATE`` / ``FOR KEY SHARE``.
_LOCKING_AFTER_FOR = frozenset({"UPDATE", "SHARE", "NO", "KEY"})
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "set_config",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_notify",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_switch_wal",
        "pg_logical_emit_message",
        "sleep",
        "benchmark",
        "get_lock",
        "load_file",
        "master_pos_wait",
        "source_pos_wait",
        "wait_for_executed_gtid_set",
        "wait_until_sql_thread_after_gtids",
        "txid_current",
        # run the SQL text they are given
        "ts_stat",
        "ts_rewrite",
        "pg_current_xact_id",
        "openrowset",
        "openquery",
        "opendatasource",
        "url",
        "file",
        "s3",
        "remote",
        "remotesecure",
        "mysql",
        "postgresql",
        "executable",
    }
)
_FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_sleep",
    "lo_",
    "dblink",
    "pg_advisory",
    "pg_try_advisory",
    "pg_stat_reset",
    "xp_",
    "sp_",
)
#: C6b: words refused only in one dialect (C6a dialects unchanged).
_DIALECT_WORDS: Mapping[str, frozenset[str]] = {
    "clickhouse": frozenset(
        {
            "SETTINGS",
            "FORMAT",
            "SYSTEM",  # the ``system`` database must be written `system`
            "KILL",
            "OPTIMIZE",
            "ATTACH",
            "DETACH",
            "RENAME",
            "EXCHANGE",
            "UNDROP",
            "BACKUP",
            "RESTORE",
            "WATCH",
            "MOVE",
            "COMMIT",
            "ROLLBACK",
        }
    ),
    "mssql": frozenset(
        {
            "DECLARE",
            "WAITFOR",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SAVE",
            "GO",
            "GOTO",
            "USE",
            "DBCC",
            "KILL",
            "SHUTDOWN",
            "RECONFIGURE",
            "CHECKPOINT",
            "BACKUP",
            "RESTORE",
            "BULK",
            "DENY",
            "ENABLE",
            "DISABLE",
            "WRITETEXT",
            "UPDATETEXT",
            "READTEXT",
            "RAISERROR",
            "THROW",
            "REVERT",
            "SETUSER",
            "OPENXML",
            # control flow: T-SQL needs no ';' between statements
            "WHILE",
            "IF",
            "PRINT",
            # locking hints, inside WITH (...) or the old ``FROM t (HINT)``
            # form, refused as whole words anywhere outside quotes
            "UPDLOCK",
            "XLOCK",
            "HOLDLOCK",
            "TABLOCK",
            "TABLOCKX",
            "PAGLOCK",
            "ROWLOCK",
            "SERIALIZABLE",
            "REPEATABLEREAD",
            "READPAST",
        }
    ),
}
#: C6b: ClickHouse table functions and side-effect functions (lower case).
_CLICKHOUSE_FUNCTIONS = frozenset(
    name.lower()
    for name in (
        "url",
        "urlCluster",
        "file",
        "fileCluster",
        "s3",
        "s3Cluster",
        "gcs",
        "azureBlobStorage",
        "azureBlobStorageCluster",
        "hdfs",
        "hdfsCluster",
        "remote",
        "remoteSecure",
        "cluster",
        "clusterAllReplicas",
        "mysql",
        "postgresql",
        "sqlite",
        "mongodb",
        "redis",
        "odbc",
        "jdbc",
        "executable",
        "input",
        "iceberg",
        "icebergS3",
        "icebergAzure",
        "icebergHDFS",
        "deltaLake",
        "hudi",
        "sleep",
        "sleepEachRow",
    )
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$]")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


class _Reject(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _ident_before(sql: str, index: int) -> bool:
    return index > 0 and _IDENT_CHAR.fullmatch(sql[index - 1]) is not None


def _lex(sql: str, dialect: str, backslash: bool) -> tuple[str, list[tuple[int, str]]]:
    """The statement with every comment, string literal and quoted
    identifier replaced by one space, and the quoted identifiers (position
    after the closing quote, content). ``backslash``: whether a backslash
    escapes the next character in a plain ``'...'`` (and MySQL ``"..."``)
    literal -- both readings are checked for PostgreSQL and MySQL (C6-3);
    ClickHouse always and SQL Server never take backslash escapes (C6b)."""
    if dialect == "clickhouse":
        return _lex_clickhouse(sql)
    if dialect == "mssql":
        return _lex_mssql(sql)
    return _lex_pg_mysql(sql, dialect, backslash)


def _quoted_end(sql: str, i: int, quote: str, *, backslash: bool, what: str) -> int:
    """Index just after the literal or identifier that starts at ``i``:
    ``quote`` doubled is an escaped quote; with ``backslash``, a backslash
    escapes the next character."""
    j = i + 1
    n = len(sql)
    while True:
        if j >= n:
            raise _Reject("INVALID_QUERY", f"unterminated {what}")
        c = sql[j]
        if backslash and c == "\\":
            j += 2
            continue
        if c == quote:
            if j + 1 < n and sql[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1


def _lex_clickhouse(sql: str) -> tuple[str, list[tuple[int, str]]]:
    out: list[str] = []
    quoted: list[tuple[int, str]] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise _Reject("INVALID_QUERY", "unterminated comment")
            if "/*" in sql[i + 2 : end]:
                raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", "a nested comment")
            i = end + 2
            out.append(" ")
            continue
        if ch in ("#", "$"):
            raise _Reject(
                "QUERY_CONSTRUCT_NOT_ALLOWED",
                f"'{ch}' outside quotes (comment and heredoc forms are not accepted)",
            )
        if ch == "'":
            i = _quoted_end(sql, i, "'", backslash=True, what="string literal")
            out.append(" ")
            continue
        if ch in ('"', "`"):
            j = _quoted_end(sql, i, ch, backslash=True, what="quoted identifier")
            content = sql[i + 1 : j - 1].replace(ch * 2, ch)
            i = j
            out.append(" ")
            quoted.append((len(out), content))
            continue
        out.append(ch)
        i += 1
    return "".join(out), quoted


def _lex_mssql(sql: str) -> tuple[str, list[tuple[int, str]]]:
    out: list[str] = []
    quoted: list[tuple[int, str]] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                elif sql.startswith("/*", j):
                    depth += 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise _Reject("INVALID_QUERY", "unterminated comment")
            i = j
            out.append(" ")
            continue
        if ch == "'":
            i = _quoted_end(sql, i, "'", backslash=False, what="string literal")
            out.append(" ")
            continue
        if ch in ('"', "["):
            close = '"' if ch == '"' else "]"
            j = i + 1
            content: list[str] = []
            while True:
                if j >= n:
                    raise _Reject("INVALID_QUERY", "unterminated quoted identifier")
                c = sql[j]
                if c == close:
                    if j + 1 < n and sql[j + 1] == close:
                        content.append(close)
                        j += 2
                        continue
                    break
                content.append(c)
                j += 1
            i = j + 1
            out.append(" ")
            quoted.append((len(out), "".join(content)))
            continue
        out.append(ch)
        i += 1
    return "".join(out), quoted


def _lex_pg_mysql(
    sql: str, dialect: str, backslash: bool
) -> tuple[str, list[tuple[int, str]]]:
    """C6a lexing, PostgreSQL and MySQL/MariaDB (unchanged)."""
    out: list[str] = []
    quoted: list[tuple[int, str]] = []
    i = 0
    n = len(sql)
    mysql = dialect == "mysql"
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            after = sql[i + 2] if i + 2 < n else ""
            if mysql and after and not after.isspace():
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED",
                    "'--' must be followed by a space to start a comment",
                )
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if mysql and ch == "#":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            if mysql and (sql.startswith("/*!", i) or sql.startswith("/*M!", i)):
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED", "executable comment /*! ... */"
                )
            if mysql and sql.startswith("/*+", i):
                # optimizer hints can override MAX_EXECUTION_TIME or SET_VAR
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED", "optimizer hint /*+ ... */"
                )
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                elif not mysql and sql.startswith("/*", j):
                    depth += 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise _Reject("INVALID_QUERY", "unterminated comment")
            i = j
            out.append(" ")
            continue
        if ch == "'" or (mysql and ch == '"'):
            escaped = backslash
            if (
                not mysql
                and ch == "'"
                and out
                and out[-1] in ("E", "e")
                and not (len(out) > 1 and _IDENT_CHAR.fullmatch(out[-2] or " "))
            ):
                escaped = True  # E'...' always takes backslash escapes
            j = i + 1
            while True:
                if j >= n:
                    raise _Reject("INVALID_QUERY", "unterminated string literal")
                c = sql[j]
                if escaped and c == "\\":
                    j += 2
                    continue
                if c == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            out.append(" ")
            continue
        if not mysql and ch == "$" and not _ident_before(sql, i):
            match = _DOLLAR_TAG.match(sql, i)
            if match is not None:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                if end < 0:
                    raise _Reject("INVALID_QUERY", "unterminated dollar-quoted string")
                i = end + len(tag)
                out.append(" ")
                continue
        if (ch == '"' and not mysql) or (mysql and ch == "`"):
            if not mysql and len(out) >= 2 and "".join(out[-2:]).upper() == "U&":
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED", 'Unicode-escaped identifier U&"..."'
                )
            j = i + 1
            content: list[str] = []
            while True:
                if j >= n:
                    raise _Reject("INVALID_QUERY", "unterminated quoted identifier")
                c = sql[j]
                if c == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        content.append(ch)
                        j += 2
                        continue
                    break
                content.append(c)
                j += 1
            i = j + 1
            out.append(" ")
            quoted.append((len(out), "".join(content)))
            continue
        out.append(ch)
        i += 1
    # ``quoted`` holds indexes into ``out``; every replaced region is one
    # character, so the joined text keeps those positions.
    return "".join(out), quoted


def _function_problem(name: str, dialect: str = "") -> bool:
    lowered = name.lower()
    if dialect == "clickhouse" and lowered in _CLICKHOUSE_FUNCTIONS:
        return True
    return (
        lowered in _FORBIDDEN_FUNCTIONS
        or lowered.startswith(_FORBIDDEN_FUNCTION_PREFIXES)
        # query_to_xml, cursor_to_xml, table_to_xml, ...: they run SQL text
        or "_to_xml" in lowered
    )


def _next_char(text: str, index: int) -> str:
    while index < len(text) and text[index].isspace():
        index += 1
    return text[index] if index < len(text) else ""


def _scan(code: str, quoted: Sequence[tuple[int, str]], dialect: str) -> None:
    # PostgreSQL and MySQL accept non-ASCII letters in identifiers and
    # PostgreSQL in dollar-quote tags; this lexer does not, so any non-ASCII
    # character outside literals, comments and quoted identifiers is refused
    # rather than lexed differently from the server (PR #746 review B1).
    for ch in code:
        if ord(ch) > 127:
            raise _Reject(
                "QUERY_CONSTRUCT_NOT_ALLOWED",
                "a non-ASCII character outside quotes (quote the identifier)",
            )
    if dialect == "mysql" and ":=" in code:
        raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", "variable assignment :=")
    body = code.rstrip()
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", "more than one statement")
    words = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(body)]
    first = _WORD.match(re.sub(r"^[\s(]*", "", body))
    if first is None or first.group(0).upper() not in _ALLOWED_FIRST:
        start = "" if first is None else f"it starts with {first.group(0).upper()}; "
        raise _Reject(
            "QUERY_CONSTRUCT_NOT_ALLOWED",
            f"{start}the statement must start with SELECT, WITH, EXPLAIN, SHOW, "
            "DESCRIBE or DESC",
        )
    extra = _DIALECT_WORDS.get(dialect, frozenset())
    for index, (word, _, end) in enumerate(words):
        upper = word.upper()
        if upper in _FORBIDDEN_WORDS:
            raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", upper)
        if upper in extra:
            raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", upper)
        if (
            dialect == "mssql"
            and upper == "NEXT"
            and index + 1 < len(words)
            and words[index + 1][0].upper() == "VALUE"
        ):
            raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", "NEXT VALUE")
        if (
            upper == "FOR"
            and index + 1 < len(words)
            and words[index + 1][0].upper() in _LOCKING_AFTER_FOR
        ):
            raise _Reject(
                "QUERY_CONSTRUCT_NOT_ALLOWED", f"FOR {words[index + 1][0].upper()}"
            )
        if _next_char(body, end) == "(" and _function_problem(word, dialect):
            raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", f"{word}()")
    for end, content in quoted:
        if _next_char(code, end) == "(" and _function_problem(content, dialect):
            raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", f'"{content}"()')


def sql_statement_problem(sql: object, dialect: str) -> str | None:
    """The refusal note of the C6-3 statement check (``CODE: detail``), or
    ``None`` when the statement may be sent. Decided before connecting."""
    if not isinstance(sql, str) or not sql.strip():
        return "INVALID_QUERY: sql must be a non-empty SQL statement."
    if len(sql) > MAX_QUERY_CHARS:
        return f"INVALID_QUERY: sql must be at most {MAX_QUERY_CHARS} characters."
    if any(not (ch.isprintable() or ch in "\n\r\t") for ch in sql):
        return "INVALID_QUERY: sql must contain only printable characters."
    try:
        readings = (
            (True,)
            if dialect == "clickhouse"
            else (False,)
            if dialect == "mssql"
            else (True, False)
        )
        for backslash in readings:
            code, quoted = _lex(sql, dialect, backslash)
            _scan(code, quoted, dialect)
    except _Reject as reject:
        if reject.code == "INVALID_QUERY":
            return f"INVALID_QUERY: {reject.detail}."
        return (
            f"QUERY_CONSTRUCT_NOT_ALLOWED: {reject.detail}. Only one read-only "
            "statement is allowed; write, locking, session and side-effect "
            "constructs are refused (quote a column that has the same name)."
        )
    return None


# -- account check (C6-U1) and OpsPilot-database probe (C6-8 third layer) ----

_PG_READ_ROLES = (
    "pg_read_all_data",
    "pg_read_all_stats",
    "pg_read_all_settings",
    "pg_stat_scan_tables",
    "pg_monitor",
)
_PG_USER_SCHEMA = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'"
)
_PG_ACCOUNT_CHECK = (
    ACCOUNT_CHECK_MARKER
    + " WITH r AS (SELECT oid, rolname, rolsuper, rolcreaterole, rolcreatedb, "
    "rolreplication, rolbypassrls FROM pg_catalog.pg_roles "
    "WHERE pg_catalog.pg_has_role(current_user, oid, 'MEMBER')) "
    "SELECT 'role_attribute:superuser' WHERE EXISTS (SELECT 1 FROM r WHERE rolsuper) "
    "UNION ALL SELECT 'role_attribute:createrole' WHERE EXISTS "
    "(SELECT 1 FROM r WHERE rolcreaterole) "
    "UNION ALL SELECT 'role_attribute:createdb' WHERE EXISTS "
    "(SELECT 1 FROM r WHERE rolcreatedb) "
    "UNION ALL SELECT 'role_attribute:replication' WHERE EXISTS "
    "(SELECT 1 FROM r WHERE rolreplication) "
    "UNION ALL SELECT 'role_attribute:bypassrls' WHERE EXISTS "
    "(SELECT 1 FROM r WHERE rolbypassrls) "
    "UNION ALL SELECT 'predefined_role:' || rolname FROM r "
    "WHERE rolname LIKE 'pg\\_%' AND rolname NOT IN ("
    + ", ".join(f"'{name}'" for name in _PG_READ_ROLES)
    + ") "
    "UNION ALL SELECT 'relation_write' WHERE EXISTS (SELECT 1 FROM "
    "pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "CROSS JOIN r WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND "
    + _PG_USER_SCHEMA
    + " AND (pg_catalog.has_table_privilege(r.oid, c.oid, "
    "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') OR (CASE WHEN "
    "pg_catalog.current_setting('server_version_num')::int >= 170000 THEN "
    "pg_catalog.has_table_privilege(r.oid, c.oid, 'MAINTAIN') ELSE false END) OR "
    "pg_catalog.has_any_column_privilege(r.oid, c.oid, 'INSERT, UPDATE, REFERENCES'))) "
    "UNION ALL SELECT 'sequence_write' WHERE EXISTS (SELECT 1 FROM "
    "pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "CROSS JOIN r WHERE c.relkind = 'S' AND "
    + _PG_USER_SCHEMA
    + " AND pg_catalog.has_sequence_privilege(r.oid, c.oid, 'UPDATE, USAGE')) "
    "UNION ALL SELECT 'schema_create' WHERE EXISTS (SELECT 1 FROM "
    "pg_catalog.pg_namespace n CROSS JOIN r WHERE "
    + _PG_USER_SCHEMA
    + " AND pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE')) "
    "UNION ALL SELECT 'database_create' WHERE EXISTS (SELECT 1 FROM r WHERE "
    "pg_catalog.has_database_privilege(r.oid, current_database(), 'CREATE')) "
    "UNION ALL SELECT 'database_owner' WHERE EXISTS (SELECT 1 FROM "
    "pg_catalog.pg_database d JOIN r ON d.datdba = r.oid "
    "WHERE d.datname = current_database()) "
    "UNION ALL SELECT 'opspilot_schema' WHERE EXISTS (SELECT 1 FROM "
    "pg_catalog.pg_class WHERE relname = 'opspilot_runs')"
)
_MYSQL_ACCOUNT_CHECK = ACCOUNT_CHECK_MARKER + " SHOW GRANTS"
_MYSQL_ALLOWED_PRIVILEGES = frozenset(
    {
        "USAGE",
        "SELECT",
        "SHOW VIEW",
        "PROCESS",
        "SHOW DATABASES",
        "REPLICATION CLIENT",
        "BINLOG MONITOR",
        "SLAVE MONITOR",
        "REPLICA MONITOR",
    }
)
_MYSQL_GRANT = re.compile(
    r"GRANT (?P<privs>.+?) ON (?P<scope>.+?) TO (?P<rest>.+)", re.S
)
_COLUMN_LIST = re.compile(r"\s*\([^()]*\)")

#: C6b: ClickHouse -- two fixed GETs; the grants answer one line per row.
_CLICKHOUSE_GRANTS_CHECK = ACCOUNT_CHECK_MARKER + " SHOW GRANTS"
_CLICKHOUSE_READONLY_CHECK = (
    ACCOUNT_CHECK_MARKER + " SELECT getSetting('readonly'), version()"
)
_CLICKHOUSE_ALLOWED_PRIVILEGES = frozenset(
    {
        "SELECT",
        "SHOW",
        "SHOW DATABASES",
        "SHOW TABLES",
        "SHOW COLUMNS",
        "SHOW DICTIONARIES",
    }
)
_MSSQL_SERVER_ROLES = (
    "sysadmin",
    "serveradmin",
    "securityadmin",
    "processadmin",
    "setupadmin",
    "bulkadmin",
    "diskadmin",
    "dbcreator",
)
_MSSQL_DATABASE_ROLES = (
    "db_owner",
    "db_datawriter",
    "db_ddladmin",
    "db_securityadmin",
    "db_accessadmin",
    "db_backupoperator",
)


#: C6b: SQL Server -- one problem code per row, none is a pass.
def _ms_text(expr: str) -> str:
    """One problem code in one collation: SQL Server refuses a UNION whose
    branches mix catalog and database collations (error 451)."""
    return f"CAST({expr} AS NVARCHAR(300)) COLLATE DATABASE_DEFAULT"


_MSSQL_ACCOUNT_CHECK = (
    ACCOUNT_CHECK_MARKER + " SELECT p FROM ("
    f"SELECT {_ms_text(''''server_role:' + r''')} AS p FROM (VALUES "
    + ", ".join(f"('{name}')" for name in _MSSQL_SERVER_ROLES)
    + ") v(r) WHERE IS_SRVROLEMEMBER(r) = 1 "
    f"UNION ALL SELECT {_ms_text(''''database_role:' + r''')} FROM (VALUES "
    + ", ".join(f"('{name}')" for name in _MSSQL_DATABASE_ROLES)
    + ") v(r) WHERE IS_ROLEMEMBER(r) = 1 "
    f"UNION ALL SELECT {_ms_text(''''server_permission:' + permission_name''')} FROM "
    "sys.fn_my_permissions(NULL, 'SERVER') WHERE permission_name NOT IN "
    "('CONNECT SQL', 'VIEW ANY DATABASE') AND permission_name NOT LIKE 'VIEW %' "
    f"UNION ALL SELECT {_ms_text(''''database_permission:' + permission_name''')} FROM "
    "sys.fn_my_permissions(NULL, 'DATABASE') WHERE permission_name NOT IN "
    "('CONNECT', 'SELECT', 'SHOWPLAN') AND permission_name NOT LIKE 'VIEW %' "
    f"UNION ALL SELECT {_ms_text(''''granted_permission:' + dp.permission_name''')} FROM "
    "sys.database_permissions dp JOIN sys.database_principals pr ON "
    "pr.principal_id = dp.grantee_principal_id WHERE dp.state IN ('G', 'W') AND "
    "(dp.grantee_principal_id = DATABASE_PRINCIPAL_ID() OR IS_MEMBER(pr.name) = 1) "
    "AND dp.permission_name NOT IN ('CONNECT', 'SELECT', 'SHOWPLAN') AND "
    "dp.permission_name NOT LIKE 'VIEW %' "
    f"UNION ALL SELECT {_ms_text(''''schema_owner:' + s.name''')} FROM sys.schemas s JOIN "
    "sys.database_principals pr ON pr.principal_id = s.principal_id WHERE "
    "(s.principal_id = DATABASE_PRINCIPAL_ID() OR (pr.type = 'R' AND "
    "pr.is_fixed_role = 0 AND IS_MEMBER(pr.name) = 1))"
    ") x"
)


def _mysql_grant_problem(
    line: str, allowed: frozenset[str] = _MYSQL_ALLOWED_PRIVILEGES
) -> str | None:
    text = line.strip()
    if ";" in text or len(re.findall(r"\bGRANT\b", text, re.I)) != 1:
        return "a grant line this check cannot read"
    match = _MYSQL_GRANT.fullmatch(text)
    if match is None:
        return "a grant that is not a plain privilege grant (role or proxy)"
    if "WITH GRANT OPTION" in match.group("rest").upper():
        return f"WITH GRANT OPTION on {_scope_text(match.group('scope'))}"
    privileges = _COLUMN_LIST.sub("", match.group("privs"))
    if "(" in privileges or ")" in privileges:
        return "a grant this check cannot read"
    for raw in privileges.split(","):
        name = " ".join(raw.split()).upper()
        if name not in allowed:
            return f"{name[:64]} on {_scope_text(match.group('scope'))}"
    return None


def _scope_text(scope: str) -> str:
    text = scope.strip()
    if text.upper().startswith(("PROCEDURE ", "FUNCTION ", "TABLE ")):
        text = text.split(None, 1)[1]
    return text[:128] if re.fullmatch(r"[A-Za-z0-9_`*.$-]{1,128}", text) else "a scope"


def session_problem(dialect: str, rows: Sequence[Sequence[object]]) -> str | None:
    """The refusal note of the post-connect check (C6-U1 and C6-8 third
    layer), or ``None`` when the account looks read-only. ``rows`` is what
    the fixed check returned: PostgreSQL and SQL Server one problem code per
    row (none is a pass), MySQL/MariaDB and ClickHouse the ``SHOW GRANTS``
    lines (never echoed whole)."""
    if dialect in ("postgresql", "mssql"):
        problems = sorted({str(row[0]) for row in rows if row})
        if "opspilot_schema" in problems:
            return (
                "SQL_TARGETS_OPSPILOT_DB: the configured database holds "
                "OpsPilot's own tables; the tool never reads it."
            )
        if problems:
            return (
                "SQL_ACCOUNT_NOT_READ_ONLY: the configured account is not "
                "read-only (" + ", ".join(problems[:8]) + "); nothing was run."
            )
        return None
    lines = [str(row[0]) for row in rows if row]
    if not lines:
        return (
            "SQL_ACCOUNT_NOT_READ_ONLY: the account's grants could not be "
            "read; nothing was run."
        )
    allowed = (
        _CLICKHOUSE_ALLOWED_PRIVILEGES
        if dialect == "clickhouse"
        else _MYSQL_ALLOWED_PRIVILEGES
    )
    for line in lines:
        problem = _mysql_grant_problem(line, allowed)
        if problem is not None:
            return (
                "SQL_ACCOUNT_NOT_READ_ONLY: the configured account is not "
                f"read-only ({problem}); nothing was run."
            )
    return None


# -- driver seam ----------------------------------------------------------------


class SqlDriverError(Exception):
    """A classified database failure. The transport classifies raw
    psycopg/PyMySQL errors into this itself; a connection may also raise it.

    ``kind``: ``invalid`` (syntax, unknown object, bad data), ``forbidden``
    (authentication, privilege), ``read_only`` (the read-only transaction
    refused a write), ``unavailable`` (connect, TLS, lost connection),
    ``timeout`` (database or client deadline) or ``error``. ``code`` is the
    SQLSTATE or error number; ``message`` the database's text (untrusted)."""

    def __init__(self, kind: str, code: str = "", message: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.code = code
        self.message = message


class SqlConnection(Protocol):
    """One connection; every statement the tool sends goes through
    ``execute`` (no rows wanted) or ``query``."""

    server_version: str

    def execute(self, statement: str, params: Sequence[object] = ()) -> None: ...

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        """Column names, at most ``max_rows`` rows, and whether more rows
        were left unread (the rest is not read)."""
        ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


Connector = Callable[[SqlConfig, float], SqlConnection]


def _approx_size(row: Sequence[object]) -> int:
    size = 0
    for value in row:
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            size += len(value)
        else:
            size += 16
    return size


def _pg_kind(sqlstate: str) -> str:
    if sqlstate == "57014":
        return "timeout"
    if sqlstate == "25006":
        return "read_only"
    if sqlstate == "42501" or sqlstate.startswith("28"):
        return "forbidden"
    if sqlstate.startswith(("42", "22", "0A", "2B", "34", "3D", "3F")):
        return "invalid"
    if sqlstate.startswith(("08", "57P", "53")):
        return "unavailable"
    return "error"


_AUTH_PHRASES = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "does not exist",
    "permission denied",
    "authentication failed",
)


def _classify(
    error: BaseException, *, connecting: bool, cancelled: bool
) -> SqlDriverError:
    """One driver failure as a ``SqlDriverError`` (C6-14). The transport
    classifies raw psycopg/PyMySQL errors itself, so a connection only
    passes them through; a ``SqlDriverError`` is taken as it is."""
    if isinstance(error, SqlDriverError):
        if cancelled and error.kind in ("unavailable", "error"):
            return SqlDriverError("timeout", error.code, "")
        return error
    if _is_pytds_error(error):
        return _classify_mssql(error, connecting=connecting, cancelled=cancelled)
    if isinstance(error, TimeoutError):  # a driver socket timeout
        return SqlDriverError(
            "unavailable" if connecting and not cancelled else "timeout"
        )
    sqlstate = getattr(error, "sqlstate", None)
    args: tuple[Any, ...] = tuple(getattr(error, "args", ()))
    errno = args[0] if args and type(args[0]) is int else None
    if errno is not None and _is_mysql_error(error):
        message = str(args[1]) if len(args) > 1 else ""
        if cancelled:
            return SqlDriverError("timeout", str(errno), "")
        if connecting:
            if errno in (1044, 1045, 1698):
                return SqlDriverError("forbidden", str(errno), "")
            return SqlDriverError("unavailable", str(errno), "")
        if errno == 2013:
            return SqlDriverError("unavailable", str(errno), "")
        return SqlDriverError(_mysql_kind(errno), str(errno), message)
    # PostgreSQL by SQLSTATE; checked after MySQL, whose errors carry a
    # SQLSTATE too but are classified by their error number.
    if isinstance(sqlstate, str) and sqlstate:
        diag = getattr(error, "diag", None)
        message = str(getattr(diag, "message_primary", None) or error)
        if cancelled and sqlstate == "57014":
            return SqlDriverError("timeout", sqlstate, "")
        return SqlDriverError(_pg_kind(sqlstate), sqlstate, message)
    if _is_psycopg_error(error):
        if cancelled:
            return SqlDriverError("timeout")
        text = str(error).lower()
        if connecting and any(phrase in text for phrase in _AUTH_PHRASES):
            return SqlDriverError("forbidden", "28000", "")
        return SqlDriverError("unavailable")
    if cancelled:
        return SqlDriverError("timeout")
    return SqlDriverError("unavailable" if connecting else "error")


#: SQL Server message numbers (C6b).
_MSSQL_INVALID = frozenset(
    {
        102,
        156,
        170,
        207,
        208,
        4104,
        8134,
        245,
        8114,
        105,
        195,
        515,
        8115,
        241,
        242,
        102,
        319,
        1038,
        4145,
        8120,
        8127,
        209,
    }
)
_MSSQL_FORBIDDEN = frozenset({229, 230, 262, 297, 300, 916, 18456, 4060})


def _mssql_kind(number: int) -> str:
    if number in _MSSQL_FORBIDDEN:
        return "forbidden"
    if number == 1222:
        return "timeout"
    if number in _MSSQL_INVALID:
        return "invalid"
    if number in (3906, 3908):
        return "read_only"
    return "error"


def _classify_mssql(
    error: BaseException, *, connecting: bool, cancelled: bool
) -> SqlDriverError:
    number = getattr(error, "number", 0) or getattr(error, "msg_no", 0) or 0
    number = int(number) if isinstance(number, int) else 0
    message = str(getattr(error, "message", "") or error)
    if cancelled:
        return SqlDriverError("timeout", str(number or ""), "")
    if connecting and number != 18456:
        # python-tds retries a refused or silent server until login_timeout
        # and then raises a timeout: the server was not reachable.
        return SqlDriverError("unavailable", str(number or ""), "")
    if isinstance(error, socket.timeout):
        return SqlDriverError("timeout", str(number or ""), "")
    if number == 18456 or (connecting and number in _MSSQL_FORBIDDEN):
        return SqlDriverError("forbidden", str(number), "")
    if number == 0:
        import pytds  # type: ignore[import-untyped]

        lost = isinstance(error, pytds.tds_base.ClosedConnectionError)
        return SqlDriverError("unavailable" if connecting or lost else "error")
    kind = _mssql_kind(number)
    return SqlDriverError(kind, str(number), message if kind == "invalid" else "")


def _is_pytds_error(error: BaseException) -> bool:
    try:
        import pytds
    except ImportError:  # pragma: no cover - a locked dependency
        return False
    return isinstance(error, pytds.tds_base.Error)


def _is_mysql_error(error: BaseException) -> bool:
    try:
        import pymysql  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - a locked dependency
        return False
    return isinstance(error, pymysql.MySQLError)


def _is_psycopg_error(error: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - a locked dependency
        return False
    return isinstance(error, psycopg.Error)


class _PostgresConnection:
    """psycopg, autocommit (the tool sends ``BEGIN READ ONLY`` itself);
    rows are streamed over the extended protocol, so the server refuses a
    second statement too. Driver errors pass through to the transport."""

    def __init__(self, config: SqlConfig, timeout: float) -> None:
        import psycopg

        kwargs: dict[str, Any] = {
            "host": config.host,
            "port": config.port,
            "dbname": config.database,
            "user": config.username,
            "password": config.password,
            "connect_timeout": max(1, math.ceil(timeout)),
            "application_name": _APPLICATION_NAME,
            "sslmode": config.tls,
        }
        if config.tls == "verify-full":
            kwargs["sslrootcert"] = config.ca_file or "system"
        self._conn = psycopg.connect(autocommit=True, prepare_threshold=None, **kwargs)
        self.server_version = str(self._conn.info.server_version)

    def execute(self, statement: str, params: Sequence[object] = ()) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(statement, tuple(params) if params else None)

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        rows: list[tuple[Any, ...]] = []
        columns: list[str] | None = None
        more = False
        size = 0
        with self._conn.cursor() as cursor:
            stream = cursor.stream(statement, tuple(params) if params else None)
            try:
                for row in stream:
                    if columns is None:
                        columns = [c.name for c in cursor.description or ()]
                    if len(rows) >= max_rows:
                        more = True
                        break
                    size += _approx_size(row)
                    if size > READ_LIMIT_BYTES:
                        raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
                    rows.append(tuple(row))
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()  # cancels and drains what is left
            if columns is None:
                columns = [c.name for c in cursor.description or ()]
        return columns, rows, more

    def cancel(self) -> None:
        try:
            self._conn.cancel_safe(timeout=3.0)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _mysql_kind(errno: int) -> str:
    if errno in (3024, 1969, 1317):
        return "timeout"
    if errno == 1792:
        return "read_only"
    if errno in (1044, 1045, 1142, 1143, 1227, 1370, 1698):
        return "forbidden"
    if errno in (
        2003,
        2005,
        2006,
        2013,
        2026,
        2055,
        1040,
        1053,
        1081,
        1152,
        1153,
        1184,
    ):
        return "unavailable"
    if errno in _MYSQL_INVALID:
        return "invalid"
    return "error"


#: Syntax, unknown object and bad-value errors (MySQL and MariaDB).
_MYSQL_INVALID = frozenset(
    {1051, 1052, 1054, 1060, 1064, 1066, 1109, 1146, 1210, 1222, 1241,
     1242, 1247, 1248, 1250, 1292, 1305, 1349, 1356, 1365, 1366, 1367,
     1406, 1582, 1583, 1584, 1630, 1690, 3140, 3141, 3143, 3146}
)  # fmt: skip


def _mysql_connect(config: SqlConfig, timeout: float) -> Any:
    import pymysql

    kwargs: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "user": config.username,
        "password": config.password,
        "database": config.database,
        "connect_timeout": max(1.0, timeout),
        "read_timeout": max(1.0, timeout),
        "write_timeout": max(1.0, timeout),
        "autocommit": True,
        "charset": "utf8mb4",
        "local_infile": False,
        "client_flag": 0,
        "program_name": _APPLICATION_NAME,
    }
    if config.tls == "verify-full":
        kwargs["ssl"] = ssl.create_default_context(cafile=config.ca_file)
    elif config.tls == "require":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["ssl"] = context
    return pymysql.connect(**kwargs)


class _MySqlConnection:
    """PyMySQL, autocommit with an explicit read-only transaction, no
    ``MULTI_STATEMENTS``, no ``LOAD DATA LOCAL``; result rows are read
    unbuffered and an early stop kills the query from a second connection
    before the rest is drained. Driver errors pass through; the transport
    kills a query past its deadline itself."""

    def __init__(self, config: SqlConfig, timeout: float) -> None:
        self._config = config
        self._conn = _mysql_connect(config, timeout)
        self.server_version = str(self._conn.get_server_info())
        self._thread_id: int | None = self._conn.thread_id()

    def execute(self, statement: str, params: Sequence[object] = ()) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(statement, tuple(params) if params else None)

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        import pymysql.cursors  # type: ignore[import-untyped]

        rows: list[tuple[Any, ...]] = []
        more = False
        size = 0
        cursor = self._conn.cursor(pymysql.cursors.SSCursor)
        try:
            cursor.execute(statement, tuple(params) if params else None)
            columns = [str(item[0]) for item in cursor.description or ()]
            while True:
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) >= max_rows:
                    more = True
                    break
                size += _approx_size(row)
                if size > READ_LIMIT_BYTES:
                    self._kill()
                    raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
                rows.append(tuple(row))
            if more:
                self._kill()
        finally:
            try:
                cursor.close()  # drains what is left: little once killed
            except Exception:
                pass
        return columns, rows, more

    def _kill(self) -> None:
        if self._thread_id is None:
            return
        try:
            other = _mysql_connect(self._config, 3.0)
        except Exception:
            return
        try:
            with other.cursor() as cursor:
                cursor.execute(f"KILL QUERY {int(self._thread_id)}")
        except Exception:
            pass
        finally:
            try:
                other.close()
            except Exception:
                pass

    def cancel(self) -> None:
        """The transport kills a late query with ``KILL QUERY``."""

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            try:
                self._conn._force_close()
            except Exception:
                pass


#: ClickHouse exception codes (C6b, C6-14).
_CLICKHOUSE_INVALID = frozenset(
    {
        62,
        60,
        47,
        46,
        81,
        16,
        43,
        53,
        42,
        6,
        27,
        38,
        41,
        69,
        70,
        72,
        36,
        44,
        48,
        184,
        215,
    }
)
_CLICKHOUSE_FORBIDDEN = frozenset({497, 516, 192, 194})
_CLICKHOUSE_CODE = re.compile(r"Code:\s*(\d+)")


def _clickhouse_kind(code: int) -> str:
    if code in _CLICKHOUSE_INVALID:
        return "invalid"
    if code in _CLICKHOUSE_FORBIDDEN:
        return "forbidden"
    if code == 164:
        return "read_only"
    if code in (159, 394):
        return "timeout"
    if code in (241, 202, 210):
        return "unavailable"
    return "error"


class _ClickHouseConnection:
    """ClickHouse over its HTTP interface (C6b): GET only, so the server
    forces ``readonly=2`` on an account whose own ``readonly`` is 0
    (``HTTPHandler.cpp``); every request is one statement, its own session.
    No proxy, no redirects; credentials in the ``X-ClickHouse-*`` headers.
    Failures are raised as ``SqlDriverError``."""

    def __init__(self, config: SqlConfig, timeout: float) -> None:
        self._config = config
        self._deadline = time.monotonic() + timeout
        self._lock = threading.Lock()
        self._active: Any = None
        self._cancelled = False
        self.server_version = ""

    def execute(self, statement: str, params: Sequence[object] = ()) -> None:
        self.query(statement, params, max_rows=0)

    def _settings(self) -> list[tuple[str, str]]:
        left = self._deadline - time.monotonic()
        seconds = max(1, int(_server_timeout(left)))
        return [
            ("database", self._config.database),
            ("default_format", "JSONCompactEachRowWithNames"),
            ("max_execution_time", str(seconds)),
            ("timeout_overflow_mode", "throw"),
            ("cancel_http_readonly_queries_on_client_close", "1"),
            ("output_format_json_quote_64bit_integers", "0"),
            ("query_id", f"opspilot-{uuid.uuid4()}"),
        ]

    def _open(self, timeout: float) -> Any:
        import http.client

        config = self._config
        if config.secure:
            context = ssl.create_default_context(cafile=config.ca_file)
            if config.tls == "require":
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(
                config.host, config.port, timeout=timeout, context=context
            )
        return http.client.HTTPConnection(config.host, config.port, timeout=timeout)

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise SqlDriverError("timeout")
        keys = [("query", statement), *self._settings()]
        keys += [
            (f"param_p{i}", "" if v is None else str(v)) for i, v in enumerate(params)
        ]
        path = "/?" + urllib.parse.urlencode(keys)
        conn = self._open(left)
        with self._lock:
            self._active = conn
        try:
            try:
                conn.request(
                    "GET",
                    path,
                    headers={
                        "X-ClickHouse-User": self._config.username,
                        "X-ClickHouse-Key": self._config.password,
                    },
                )
                response = conn.getresponse()
            except (OSError, ssl.SSLError) as error:
                late = self._cancelled or isinstance(error, TimeoutError)
                raise SqlDriverError("timeout" if late else "unavailable") from None
            if response.status != 200:
                body = response.read(8192).decode("utf-8", "replace")
                header = response.getheader("X-ClickHouse-Exception-Code") or ""
                code = int(header) if header.isdigit() else None
                if code is None:
                    match = _CLICKHOUSE_CODE.search(body)
                    code = int(match.group(1)) if match else None
                if code is None:
                    kind = (
                        "forbidden"
                        if response.status in (401, 403)
                        else "unavailable"
                        if response.status >= 500
                        else "error"
                    )
                    raise SqlDriverError(kind, str(response.status), "")
                raise SqlDriverError(_clickhouse_kind(code), str(code), body)
            return self._read(response, max_rows)
        except TimeoutError:
            raise SqlDriverError("timeout") from None
        finally:
            with self._lock:
                self._active = None
            conn.close()

    def _read(
        self, response: Any, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        rows: list[tuple[Any, ...]] = []
        columns: list[str] = []
        size = 0
        first = True
        while True:
            try:
                line = response.readline(READ_LIMIT_BYTES + 1)
            except OSError as error:
                late = self._cancelled or isinstance(error, TimeoutError)
                raise SqlDriverError("timeout" if late else "unavailable") from None
            if not line:
                return columns, rows, False
            size += len(line)
            if size > READ_LIMIT_BYTES:
                raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except ValueError:
                match = _CLICKHOUSE_CODE.search(text)
                if match:
                    code = int(match.group(1))
                    raise SqlDriverError(
                        _clickhouse_kind(code), str(code), text
                    ) from None
                raise SqlDriverError("error") from None
            if not isinstance(value, list):
                raise SqlDriverError("error")
            if first:
                columns = [str(item) for item in value]
                first = False
                continue
            if len(rows) >= max_rows:
                return columns, rows, True
            rows.append(tuple(value))

    def cancel(self) -> None:
        self._cancelled = True
        with self._lock:
            conn = self._active
        if conn is not None and getattr(conn, "sock", None) is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        """Each request closes its own HTTP connection."""


class _MssqlConnection:
    """python-tds, ``autocommit`` (the tool sends ``BEGIN TRANSACTION`` and
    ``ROLLBACK TRANSACTION`` itself), no TLS (C6b), plain language batches
    for the model's statement. Driver errors pass through to the
    transport."""

    def __init__(self, config: SqlConfig, timeout: float) -> None:
        import pytds

        self._conn = pytds.connect(
            server=config.host,
            port=config.port,
            database=config.database,
            user=config.username,
            password=config.password,
            login_timeout=max(1, int(timeout)),
            timeout=max(1.0, _server_timeout(timeout)),
            appname=_APPLICATION_NAME,
            autocommit=True,
            disable_connect_retry=True,
        )
        self.server_version = ""
        self._cursor: Any = None

    def execute(self, statement: str, params: Sequence[object] = ()) -> None:
        with self._conn.cursor() as cursor:
            # ``None``, not ``()``: python-tds %-formats any non-None params,
            # which would break a ``%`` in the model's own SQL.
            cursor.execute(statement, tuple(params) if params else None)

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        rows: list[tuple[Any, ...]] = []
        more = False
        size = 0
        cursor = self._conn.cursor()
        self._cursor = cursor
        try:
            # ``None``, not ``()``: python-tds %-formats any non-None params,
            # which would break a ``%`` in the model's own SQL.
            cursor.execute(statement, tuple(params) if params else None)
            columns = [str(item[0]) for item in cursor.description or ()]
            if cursor.description is None:
                return columns, rows, False
            while True:
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) >= max_rows:
                    more = True
                    break
                size += _approx_size(row)
                if size > READ_LIMIT_BYTES:
                    raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
                rows.append(tuple(row))
            if more:
                try:
                    cursor.cancel()
                except Exception:
                    pass
        finally:
            self._cursor = None
            try:
                cursor.close()
            except Exception:
                pass
        return columns, rows, more

    def cancel(self) -> None:
        cursor = self._cursor
        if cursor is not None:
            try:
                cursor.cancel()
            except Exception:
                pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def default_connector(config: SqlConfig, timeout: float) -> SqlConnection:
    if config.dialect == "postgresql":
        return _PostgresConnection(config, timeout)
    if config.dialect == "clickhouse":
        return _ClickHouseConnection(config, timeout)
    if config.dialect == "mssql":
        return _MssqlConnection(config, timeout)
    return _MySqlConnection(config, timeout)


# -- projection (C6-10) ------------------------------------------------------


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value.total_seconds())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
    return str(value)


def _safe_text(value: str) -> str:
    """Drop lone surrogates so the view stays encodable."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _column_keys(columns: Sequence[str]) -> list[str]:
    """Result column names as unique row keys: a repeated name gets the
    next ``#n`` suffix that no other key uses (``a, a, a#2`` is
    ``a, a#2, a#2#2``... never two equal keys; PR #746 review R1)."""
    used: set[str] = {"_truncated_fields"}
    counts: dict[str, int] = {}
    keys: list[str] = []
    for column in columns:
        name = _safe_text(str(column))
        if name not in used:
            key = name
        else:
            n = counts.get(name, 1)
            while True:
                n += 1
                key = f"{name}#{n}"
                if key not in used:
                    break
            counts[name] = n
        used.add(key)
        keys.append(key)
    return keys


def _project_rows(
    columns: Sequence[str], rows: Iterable[Sequence[object]]
) -> tuple[list[str], list[dict[str, object]], int]:
    keys = _column_keys(columns)
    projected: list[dict[str, object]] = []
    cut = 0
    raw = 0
    for row in rows:
        item: dict[str, object] = {}
        truncated: list[str] = []
        for index, key in enumerate(keys):
            value = _json_value(row[index] if index < len(row) else None)
            if isinstance(value, str):
                value = _safe_text(value)
                raw += len(value.encode("utf-8"))
                if len(value) > MAX_VALUE_CHARS:
                    value = value[:MAX_VALUE_CHARS]
                    truncated.append(key)
            else:
                raw += 16
            item[key] = value
        if truncated:
            item["_truncated_fields"] = truncated
            cut += len(truncated)
        projected.append(item)
        if raw > READ_LIMIT_BYTES:
            raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
    return keys, projected, cut


# -- transport ---------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.$-]{1,128}")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refused(note: str) -> TransportResponse:
    """A fixed-code refusal decided before anything was sent (B1-6)."""
    code = note.split(":", 1)[0]
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [note]},
    )


def _answered(note: str, *, text: str | None = None) -> TransportResponse:
    """A fixed-code failure after the database was contacted."""
    code = note.split(":", 1)[0]
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        view_fields={"notes": [note]},
        http_status=400 if text is not None else None,
        source_error_text=text,
    )


def _driver_failure(error: SqlDriverError, dialect: str) -> TransportResponse:
    label = (
        (f"SQLSTATE {error.code}" if dialect == "postgresql" else f"error {error.code}")
        if error.code
        else "no code"
    )
    if error.kind == "timeout":
        raise TransportTimeout("TIMEOUT", source_contact="confirmed")
    if error.kind == "unavailable":
        raise TransportUnavailable("SQL_UNAVAILABLE")
    if error.kind == "invalid":
        text = _safe_text(error.message)[:_ERROR_TEXT_CHARS] or None
        return _answered(
            f"SQL_INVALID: the database rejected the statement ({label}); "
            "fix it using the database's error text.",
            text=text or "",
        )
    if error.kind == "forbidden":
        return _answered(
            f"SQL_FORBIDDEN: the database refused the account ({label}): "
            "authentication failed or the account may not read this object."
        )
    if error.kind == "read_only":
        return _answered(
            f"SESSION_READ_ONLY: the read-only transaction refused a write ({label})."
        )
    return _answered(f"SQL_SOURCE_ERROR: the database failed the call ({label}).")


_CLICKHOUSE_READONLY_1_NOTE = (
    "SQL_ACCOUNT_UNSUPPORTED: the ClickHouse account has readonly=1, which "
    "blocks the tool's own timeout setting; use readonly=2 (or leave it unset "
    "with SELECT-only grants). Nothing was run."
)


def _server_timeout(remaining: float) -> float:
    return max(_SERVER_TIMEOUT_FLOOR, remaining - _SERVER_TIMEOUT_MARGIN)


class SqlTransport:
    """Serves the three tools over a new connection per call (no pool, no
    retry). Every refusal of the model's input is decided before
    connecting."""

    def __init__(
        self,
        config: SqlConfig,
        *,
        connector: Connector | None = None,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._connector = connector or default_connector
        self._timeout = request_timeout_seconds
        self._monotonic = monotonic or time.monotonic

    # -- input checks --------------------------------------------------------

    def _limit(self, params: Mapping[str, object]) -> int | TransportResponse:
        limit = params.get("limit", DEFAULT_LIMIT)
        if limit is None:
            limit = DEFAULT_LIMIT
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            return _refused(
                f"INVALID_LIMIT: limit must be an integer from 1 to {MAX_LIMIT}."
            )
        return limit

    @staticmethod
    def _identifier(value: object, name: str) -> str | None | TransportResponse:
        if value is None:
            return None
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            return _refused(
                f"INVALID_IDENTIFIER: {name} must be 1 to {MAX_IDENTIFIER_CHARS} "
                "characters of letters, digits, _ . $ -."
            )
        return value

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        params = request.params
        dialect = self._config.dialect
        plan: Callable[[SqlConnection], TransportResponse]
        if request.tool == QUERY_TOOL:
            limit = self._limit(params)
            if isinstance(limit, TransportResponse):
                return limit
            sql = params.get("sql")
            problem = sql_statement_problem(sql, dialect)
            if problem is not None:
                return _refused(problem)
            assert isinstance(sql, str)
            plan = lambda conn: self._query(request, conn, sql, limit)  # noqa: E731
        elif request.tool == LIST_TOOL:
            schema = self._identifier(params.get("schema"), "schema")
            if isinstance(schema, TransportResponse):
                return schema
            include_views = params.get("include_views", True)
            if include_views is None:
                include_views = True
            if type(include_views) is not bool:
                return _refused("INVALID_PARAMS: include_views must be true or false.")
            list_schema: str | None = schema
            plan = lambda conn: self._list(request, conn, list_schema, include_views)  # noqa: E731
        else:
            table = self._identifier(params.get("table_name"), "table_name")
            if isinstance(table, TransportResponse):
                return table
            if table is None:
                return _refused("INVALID_IDENTIFIER: table_name is required.")
            schema = self._identifier(params.get("schema"), "schema")
            if isinstance(schema, TransportResponse):
                return schema
            if schema is None and table.count(".") == 1:
                schema, table = table.split(".")
                if not schema or not table:
                    return _refused("INVALID_IDENTIFIER: table_name must name a table.")
            assert isinstance(table, str)
            plan = lambda conn: self._describe(request, conn, schema, table)  # noqa: E731
        return self._session(request, plan)

    # -- one session -----------------------------------------------------------

    def _session(
        self,
        request: TransportRequest,
        plan: Callable[[SqlConnection], TransportResponse],
    ) -> TransportResponse:
        budget = min(float(request.timeout_seconds), self._timeout)
        deadline = self._monotonic() + budget
        dialect = self._config.dialect

        def remaining() -> float:
            left = deadline - self._monotonic()
            if left <= 0:
                raise TransportTimeout("TIMEOUT", source_contact="possible")
            return left

        try:
            conn = self._connector(self._config, remaining())
        except TransportError:
            raise
        except Exception as error:
            return _driver_failure(
                _classify(error, connecting=True, cancelled=False), dialect
            )
        state: dict[str, Any] = {"cancelled": False, "killed": False, "thread": None}
        lock = threading.Lock()

        def kill_late_query() -> None:
            """MySQL/MariaDB: ``KILL QUERY`` from a second connection (an
            account may always kill its own threads); best effort."""
            with lock:
                if state["killed"] or state["thread"] is None:
                    return
                state["killed"] = True
            try:
                other = self._connector(self._config, 3.0)
            except Exception:
                return
            try:
                other.execute(f"KILL QUERY {int(state['thread'])}")
            except Exception:
                pass
            finally:
                other.close()

        def on_deadline() -> None:
            state["cancelled"] = True
            if dialect == "mysql":
                kill_late_query()
            else:
                conn.cancel()

        timer = threading.Timer(max(0.0, deadline - self._monotonic()), on_deadline)
        timer.daemon = True
        timer.start()
        began = False

        def rollback() -> None:
            try:
                conn.execute(
                    "ROLLBACK TRANSACTION" if dialect == "mssql" else "ROLLBACK"
                )
            except Exception:
                pass  # read-only work is discarded with the connection

        try:
            if dialect == "postgresql":
                ms = int(_server_timeout(remaining()) * 1000)
                conn.execute("BEGIN READ ONLY")
                began = True
                conn.execute(f"SET LOCAL statement_timeout = {ms}")
                conn.execute(f"SET LOCAL lock_timeout = {ms}")
                conn.execute(f"SET LOCAL idle_in_transaction_session_timeout = {ms}")
                _, rows, _ = conn.query(_PG_ACCOUNT_CHECK, max_rows=64)
                problem = session_problem(dialect, rows)
                if problem is not None:
                    return _answered(problem)
            elif dialect == "clickhouse":
                # C6b: every GET is its own session; nothing to begin or roll
                # back -- the server makes a GET read-only.
                try:
                    _, rows, _ = conn.query(_CLICKHOUSE_GRANTS_CHECK, max_rows=256)
                except SqlDriverError as error:
                    if error.kind != "read_only":
                        raise
                    # 164 on a read: the account's readonly=1 refused the
                    # tool's own settings (max_execution_time and the rest)
                    return _answered(_CLICKHOUSE_READONLY_1_NOTE)
                problem = session_problem(dialect, rows)
                if problem is not None:
                    return _answered(problem)
                _, setting_rows, _ = conn.query(_CLICKHOUSE_READONLY_CHECK, max_rows=1)
                readonly = str(setting_rows[0][0]) if setting_rows else ""
                if readonly == "1":
                    return _answered(_CLICKHOUSE_READONLY_1_NOTE)
                if readonly != "2":
                    return _answered(
                        "SQL_ACCOUNT_NOT_READ_ONLY: ClickHouse did not report a "
                        "read-only session for this account; nothing was run."
                    )
            elif dialect == "mssql":
                ms = int(_server_timeout(remaining()) * 1000)
                conn.execute(f"SET LOCK_TIMEOUT {ms}")
                conn.execute("SET XACT_ABORT ON")
                conn.execute("SET IMPLICIT_TRANSACTIONS OFF")
                _, rows, _ = conn.query(_MSSQL_ACCOUNT_CHECK, max_rows=64)
                problem = session_problem(dialect, rows)
                if problem is not None:
                    return _answered(problem)
                conn.execute("BEGIN TRANSACTION")
                began = True
            else:
                _, version_rows, _ = conn.query(
                    "SELECT VERSION(), CONNECTION_ID()", max_rows=1
                )
                version = str(version_rows[0][0]) if version_rows else ""
                if version_rows and len(version_rows[0]) > 1:
                    try:
                        state["thread"] = int(version_rows[0][1])
                    except (TypeError, ValueError):
                        state["thread"] = None
                seconds = _server_timeout(remaining())
                if "mariadb" in version.lower():
                    conn.execute(f"SET SESSION max_statement_time = {seconds:.3f}")
                else:
                    conn.execute(
                        f"SET SESSION max_execution_time = {int(seconds * 1000)}"
                    )
                conn.execute(f"SET SESSION lock_wait_timeout = {max(1, int(seconds))}")
                _, rows, _ = conn.query(_MYSQL_ACCOUNT_CHECK, max_rows=256)
                problem = session_problem(dialect, rows)
                if problem is not None:
                    return _answered(problem)
                conn.execute("START TRANSACTION READ ONLY")
                began = True
            remaining()
            response = plan(conn)
            began = False
            if dialect != "clickhouse":  # a ClickHouse GET has no transaction
                rollback()
            return response
        except TransportError:
            if began:
                began = False
                rollback()
            raise
        except Exception as error:
            late = state["cancelled"] or self._monotonic() >= deadline - 0.05
            failure = _classify(error, connecting=False, cancelled=late)
            if failure.kind == "timeout" and dialect != "postgresql":
                kill_late_query()
            if began:
                began = False
                rollback()
            return _driver_failure(failure, dialect)
        finally:
            timer.cancel()
            if began:
                rollback()
            conn.close()

    # -- the three tools ---------------------------------------------------

    def _base_view(self, request: TransportRequest) -> dict[str, Any]:
        end = request.window.end.astimezone(timezone.utc).replace(microsecond=0)
        return {
            "dialect": self._config.dialect,
            "notes": [
                "Read at query time (this call's observed time): the database "
                "as it is now, not as it was in the authorized window ending "
                f"{_iso(end)}."
            ],
            "cannot_prove": _CANNOT_PROVE,
        }

    def _query(
        self, request: TransportRequest, conn: SqlConnection, sql: str, limit: int
    ) -> TransportResponse:
        columns, rows, more = conn.query(sql, max_rows=limit + 1)
        if len(rows) > limit:
            rows = rows[:limit]
            more = True
        keys, projected, cut = _project_rows(columns, rows)
        view = self._base_view(request)
        reason = "reached limit; the result set has more rows" if more else None
        view.update(
            {
                "columns": keys,
                "limit": limit,
                "long_values_cut": cut,
                "incomplete_reason": reason,
            }
        )
        return self._respond(
            request, {"results": projected}, projected, "rows", view, more
        )

    def _list(
        self,
        request: TransportRequest,
        conn: SqlConnection,
        schema: str | None,
        include_views: bool,
    ) -> TransportResponse:
        if self._config.dialect == "postgresql":
            kinds = ["r", "p", "f"] + (["v", "m"] if include_views else [])
            if schema is None:
                statement = (
                    CATALOG_MARKER
                    + " SELECT n.nspname, c.relname, c.relkind::text FROM "
                    "pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON "
                    "n.oid = c.relnamespace WHERE c.relkind::text = ANY(%s) AND "
                    + _PG_USER_SCHEMA.replace("%", "%%")
                    + " ORDER BY 1, 2 LIMIT %s"
                )
                params: tuple[object, ...] = (kinds, MAX_LIST_ROWS + 1)
            else:
                statement = (
                    CATALOG_MARKER
                    + " SELECT n.nspname, c.relname, c.relkind::text FROM "
                    "pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON "
                    "n.oid = c.relnamespace WHERE c.relkind::text = ANY(%s) AND "
                    "n.nspname = %s ORDER BY 1, 2 LIMIT %s"
                )
                params = (kinds, schema, MAX_LIST_ROWS + 1)
        elif self._config.dialect == "clickhouse":
            where = (
                "database = {p0:String}" if schema else "database = currentDatabase()"
            )
            views = (
                ""
                if include_views
                else " AND engine NOT IN ('View', 'MaterializedView')"
            )
            statement = (
                CATALOG_MARKER + " SELECT database, name, multiIf(engine = 'View', "
                "'VIEW', engine = 'MaterializedView', 'm', 'BASE TABLE') FROM "
                f"`system`.tables WHERE {where}{views} ORDER BY database, name "
                f"LIMIT {MAX_LIST_ROWS + 1}"
            )
            params = (schema,) if schema else ()
        elif self._config.dialect == "mssql":
            kinds_sql = "('U', 'V')" if include_views else "('U')"
            statement = (
                CATALOG_MARKER + f" SELECT TOP {MAX_LIST_ROWS + 1} s.name, o.name, "
                "CASE WHEN o.type = 'V' THEN 'VIEW' ELSE 'BASE TABLE' END FROM "
                "sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
                f"WHERE o.type IN {kinds_sql} AND s.name = "
                + ("%s" if schema else "SCHEMA_NAME()")
                + " ORDER BY s.name, o.name"
            )
            params = (schema,) if schema else ()
        else:
            types = (
                "('BASE TABLE', 'VIEW', 'SYSTEM VIEW')"
                if include_views
                else "('BASE TABLE')"
            )
            statement = (
                CATALOG_MARKER + " SELECT table_schema, table_name, table_type FROM "
                "information_schema.tables WHERE table_schema = COALESCE(%s, DATABASE()) "
                f"AND table_type IN {types} ORDER BY 1, 2 LIMIT %s"
            )
            params = (schema, MAX_LIST_ROWS + 1)
        _, rows, more = conn.query(statement, params, max_rows=MAX_LIST_ROWS + 1)
        if len(rows) > MAX_LIST_ROWS:
            rows = rows[:MAX_LIST_ROWS]
            more = True
        results = [
            {
                "schema": _safe_text(str(row[0])),
                "name": _safe_text(str(row[1])),
                "type": _TABLE_TYPES.get(str(row[2]), "table"),
            }
            for row in rows
        ]
        results.sort(key=lambda item: (str(item["schema"]), str(item["name"])))
        view = self._base_view(request)
        view["incomplete_reason"] = (
            f"only the first {MAX_LIST_ROWS} tables are listed" if more else None
        )
        return self._respond(
            request, {"results": results}, results, "tables", view, more
        )

    def _describe(
        self,
        request: TransportRequest,
        conn: SqlConnection,
        schema: str | None,
        table: str,
    ) -> TransportResponse:
        if self._config.dialect == "postgresql":
            found = self._pg_describe(conn, schema, table)
        elif self._config.dialect == "clickhouse":
            found = self._clickhouse_describe(conn, schema, table)
        elif self._config.dialect == "mssql":
            found = self._mssql_describe(conn, schema, table)
        else:
            found = self._mysql_describe(conn, schema, table)
        if found is None:
            return _answered(
                "TABLE_NOT_FOUND: no table or view of that name is visible to "
                "the account; list tables first."
            )
        body, columns = found
        view = self._base_view(request)
        view.update(
            {
                "schema": body["schema"],
                "table": body["table"],
                "primary_key": body["primary_key"],
                "foreign_keys": body["foreign_keys"],
                "indexes": body["indexes"],
            }
        )
        return self._respond(request, body, columns, "columns", view, False)

    @staticmethod
    def _pg_describe(
        conn: SqlConnection, schema: str | None, table: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if schema is None:
            _, found, _ = conn.query(
                CATALOG_MARKER
                + " SELECT c.oid::bigint, n.nspname, c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE "
                "c.relname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND "
                "pg_catalog.pg_table_is_visible(c.oid) LIMIT 1",
                (table,),
                max_rows=1,
            )
        else:
            _, found, _ = conn.query(
                CATALOG_MARKER
                + " SELECT c.oid::bigint, n.nspname, c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE "
                "c.relname = %s AND n.nspname = %s AND c.relkind IN "
                "('r', 'p', 'v', 'm', 'f') LIMIT 1",
                (table, schema),
                max_rows=1,
            )
        if not found:
            return None
        oid, nspname, relname = found[0]
        _, column_rows, _ = conn.query(
            CATALOG_MARKER
            + " SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
            "NOT a.attnotnull, pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attnum "
            "FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_attrdef d ON "
            "d.adrelid = a.attrelid AND d.adnum = a.attnum WHERE a.attrelid = "
            "%s::oid AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            (oid,),
            max_rows=2000,
        )
        _, constraint_rows, _ = conn.query(
            CATALOG_MARKER
            + " SELECT con.conname, con.contype::text, ARRAY(SELECT att.attname FROM "
            "unnest(con.conkey) WITH ORDINALITY k(num, ord) JOIN pg_catalog.pg_attribute "
            "att ON att.attrelid = con.conrelid AND att.attnum = k.num ORDER BY k.ord)::text[], "
            "fn.nspname, fc.relname, ARRAY(SELECT att.attname FROM unnest(con.confkey) "
            "WITH ORDINALITY k(num, ord) JOIN pg_catalog.pg_attribute att ON "
            "att.attrelid = con.confrelid AND att.attnum = k.num ORDER BY k.ord)::text[] "
            "FROM pg_catalog.pg_constraint con LEFT JOIN pg_catalog.pg_class fc ON "
            "fc.oid = con.confrelid LEFT JOIN pg_catalog.pg_namespace fn ON "
            "fn.oid = fc.relnamespace WHERE con.conrelid = %s::oid AND con.contype "
            "IN ('p', 'f') ORDER BY con.conname",
            (oid,),
            max_rows=2000,
        )
        _, index_rows, _ = conn.query(
            CATALOG_MARKER
            + " SELECT ic.relname, i.indisunique, i.indisprimary, ARRAY(SELECT "
            "a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(num, ord) JOIN "
            "pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.num "
            "ORDER BY k.ord)::text[], pg_catalog.pg_get_indexdef(i.indexrelid) FROM "
            "pg_catalog.pg_index i JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = %s::oid ORDER BY ic.relname",
            (oid,),
            max_rows=2000,
        )
        primary: list[str] = []
        foreign: list[dict[str, Any]] = []
        for name, kind, cols, ref_schema, ref_table, ref_cols in constraint_rows:
            if kind == "p":
                primary = [str(item) for item in (cols or [])]
            else:
                foreign.append(
                    {
                        "name": str(name),
                        "columns": [str(item) for item in (cols or [])],
                        "referred_schema": None
                        if ref_schema is None
                        else str(ref_schema),
                        "referred_table": None if ref_table is None else str(ref_table),
                        "referred_columns": [str(item) for item in (ref_cols or [])],
                    }
                )
        indexes = [
            {
                "name": str(name),
                "columns": [str(item) for item in (cols or [])],
                "unique": bool(unique),
                "primary": bool(is_primary),
                "definition": None if definition is None else str(definition),
            }
            for name, unique, is_primary, cols, definition in index_rows
        ]
        columns = [
            {
                "name": str(name),
                "type": str(kind),
                "nullable": bool(nullable),
                "default": None if default is None else str(default),
                "ordinal": int(ordinal),
            }
            for name, kind, nullable, default, ordinal in column_rows
        ]
        return _describe_body(
            str(nspname), str(relname), columns, primary, foreign, indexes
        )

    @staticmethod
    def _clickhouse_describe(
        conn: SqlConnection, schema: str | None, table: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        where_db = (
            "database = {p1:String}" if schema else "database = currentDatabase()"
        )
        params: tuple[object, ...] = (table, schema) if schema else (table,)
        _, found, _ = conn.query(
            CATALOG_MARKER + " SELECT database, name, sorting_key FROM `system`.tables "
            f"WHERE name = {{p0:String}} AND {where_db} LIMIT 1",
            params,
            max_rows=1,
        )
        if not found:
            return None
        nspname, relname, sorting_key = str(found[0][0]), str(found[0][1]), found[0][2]
        _, column_rows, _ = conn.query(
            CATALOG_MARKER + " SELECT name, type, default_expression, position, "
            "is_in_primary_key FROM `system`.columns WHERE database = {p0:String} AND "
            "table = {p1:String} ORDER BY position",
            (nspname, relname),
            max_rows=5000,
        )
        _, index_rows, _ = conn.query(
            CATALOG_MARKER + " SELECT name, expr, type FROM "
            "`system`.data_skipping_indices WHERE database = {p0:String} AND "
            "table = {p1:String} ORDER BY name",
            (nspname, relname),
            max_rows=5000,
        )
        columns = []
        primary: list[str] = []
        for name, kind, default, ordinal, in_primary in column_rows:
            text = str(kind)
            columns.append(
                {
                    "name": str(name),
                    "type": text,
                    "nullable": text.startswith(
                        ("Nullable(", "LowCardinality(Nullable(")
                    ),
                    "default": default if default else None,
                    "ordinal": int(ordinal),
                }
            )
            if in_primary in (1, True, "1"):
                primary.append(str(name))
        indexes: list[dict[str, Any]] = [
            {
                "name": str(name),
                "columns": [],
                "unique": False,
                "primary": False,
                "definition": f"{expr} TYPE {kind}",
            }
            for name, expr, kind in index_rows
        ]
        if sorting_key:
            indexes.insert(
                0,
                {
                    "name": "sorting_key",
                    "columns": [p.strip() for p in str(sorting_key).split(",")],
                    "unique": False,
                    "primary": True,
                    "definition": str(sorting_key),
                },
            )
        return _describe_body(nspname, relname, columns, primary, [], indexes)

    @staticmethod
    def _mssql_describe(
        conn: SqlConnection, schema: str | None, table: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        where_schema = "s.name = %s" if schema else "s.name = SCHEMA_NAME()"
        params: tuple[object, ...] = (table, schema) if schema else (table,)
        _, found, _ = conn.query(
            CATALOG_MARKER + " SELECT TOP 1 o.object_id, s.name, o.name FROM "
            "sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE "
            f"o.name = %s AND {where_schema} AND o.type IN ('U', 'V')",
            params,
            max_rows=1,
        )
        if not found:
            return None
        oid, nspname, relname = int(found[0][0]), str(found[0][1]), str(found[0][2])
        _, column_rows, _ = conn.query(
            CATALOG_MARKER + " SELECT c.name, TYPE_NAME(c.user_type_id), "
            "c.is_nullable, dc.definition, c.column_id FROM sys.columns c LEFT JOIN "
            "sys.default_constraints dc ON dc.parent_object_id = c.object_id AND "
            "dc.parent_column_id = c.column_id WHERE c.object_id = %s ORDER BY "
            "c.column_id",
            (oid,),
            max_rows=5000,
        )
        _, index_rows, _ = conn.query(
            CATALOG_MARKER + " SELECT i.name, i.is_unique, i.is_primary_key, "
            "COL_NAME(ic.object_id, ic.column_id) FROM sys.indexes i JOIN "
            "sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = "
            "i.index_id WHERE i.object_id = %s AND i.index_id > 0 AND "
            "ic.is_included_column = 0 ORDER BY i.name, ic.key_ordinal",
            (oid,),
            max_rows=5000,
        )
        _, fk_rows, _ = conn.query(
            CATALOG_MARKER + " SELECT fk.name, COL_NAME(fkc.parent_object_id, "
            "fkc.parent_column_id), OBJECT_SCHEMA_NAME(fkc.referenced_object_id), "
            "OBJECT_NAME(fkc.referenced_object_id), COL_NAME(fkc.referenced_object_id, "
            "fkc.referenced_column_id) FROM sys.foreign_keys fk JOIN "
            "sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "WHERE fk.parent_object_id = %s ORDER BY fk.name, fkc.constraint_column_id",
            (oid,),
            max_rows=5000,
        )
        columns = [
            {
                "name": str(name),
                "type": str(kind),
                "nullable": bool(nullable),
                "default": None if default is None else str(default),
                "ordinal": int(ordinal),
            }
            for name, kind, nullable, default, ordinal in column_rows
        ]
        index_by_name: dict[str, dict[str, Any]] = {}
        for name, unique, is_primary, column in index_rows:
            entry = index_by_name.setdefault(
                str(name),
                {
                    "name": str(name),
                    "columns": [],
                    "unique": bool(unique),
                    "primary": bool(is_primary),
                    "definition": None,
                },
            )
            entry["columns"].append(str(column))
        primary = next(
            (list(e["columns"]) for e in index_by_name.values() if e["primary"]), []
        )
        foreign_by_name: dict[str, dict[str, Any]] = {}
        for name, column, ref_schema, ref_table, ref_column in fk_rows:
            entry = foreign_by_name.setdefault(
                str(name),
                {
                    "name": str(name),
                    "columns": [],
                    "referred_schema": None if ref_schema is None else str(ref_schema),
                    "referred_table": None if ref_table is None else str(ref_table),
                    "referred_columns": [],
                },
            )
            entry["columns"].append(str(column))
            entry["referred_columns"].append(str(ref_column))
        return _describe_body(
            nspname,
            relname,
            columns,
            primary,
            sorted(foreign_by_name.values(), key=lambda item: str(item["name"])),
            sorted(index_by_name.values(), key=lambda item: str(item["name"])),
        )

    @staticmethod
    def _mysql_describe(
        conn: SqlConnection, schema: str | None, table: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        _, found, _ = conn.query(
            CATALOG_MARKER
            + " SELECT table_schema, table_name FROM information_schema.tables WHERE "
            "table_schema = COALESCE(%s, DATABASE()) AND table_name = %s LIMIT 1",
            (schema, table),
            max_rows=1,
        )
        if not found:
            return None
        nspname, relname = str(found[0][0]), str(found[0][1])
        _, column_rows, _ = conn.query(
            CATALOG_MARKER
            + " SELECT column_name, column_type, is_nullable, column_default, "
            "ordinal_position FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = %s ORDER BY ordinal_position",
            (nspname, relname),
            max_rows=5000,
        )
        _, key_rows, _ = conn.query(
            CATALOG_MARKER
            + " SELECT constraint_name, column_name, referenced_table_schema, "
            "referenced_table_name, referenced_column_name FROM "
            "information_schema.key_column_usage WHERE table_schema = %s AND "
            "table_name = %s ORDER BY constraint_name, ordinal_position",
            (nspname, relname),
            max_rows=5000,
        )
        _, index_rows, _ = conn.query(
            CATALOG_MARKER + " SELECT index_name, non_unique, column_name FROM "
            "information_schema.statistics WHERE table_schema = %s AND table_name = %s "
            "ORDER BY index_name, seq_in_index",
            (nspname, relname),
            max_rows=5000,
        )
        primary: list[str] = []
        foreign_by_name: dict[str, dict[str, Any]] = {}
        for name, column, ref_schema, ref_table, ref_column in key_rows:
            if str(name) == "PRIMARY":
                primary.append(str(column))
            elif ref_table is not None:
                entry = foreign_by_name.setdefault(
                    str(name),
                    {
                        "name": str(name),
                        "columns": [],
                        "referred_schema": None
                        if ref_schema is None
                        else str(ref_schema),
                        "referred_table": str(ref_table),
                        "referred_columns": [],
                    },
                )
                entry["columns"].append(str(column))
                entry["referred_columns"].append(
                    None if ref_column is None else str(ref_column)
                )
        index_by_name: dict[str, dict[str, Any]] = {}
        for name, non_unique, column in index_rows:
            entry = index_by_name.setdefault(
                str(name),
                {
                    "name": str(name),
                    "columns": [],
                    "unique": not bool(int(non_unique)),
                    "primary": str(name) == "PRIMARY",
                    "definition": None,
                },
            )
            if column is not None:
                entry["columns"].append(str(column))
        columns = [
            {
                "name": str(name),
                "type": _text(kind),
                "nullable": str(_text(nullable)).upper() == "YES",
                "default": None if default is None else _text(default),
                "ordinal": int(ordinal),
            }
            for name, kind, nullable, default, ordinal in column_rows
        ]
        return _describe_body(
            nspname,
            relname,
            columns,
            primary,
            sorted(foreign_by_name.values(), key=lambda item: str(item["name"])),
            sorted(index_by_name.values(), key=lambda item: str(item["name"])),
        )

    @staticmethod
    def _respond(
        request: TransportRequest,
        body_obj: Mapping[str, Any],
        rows: Sequence[Any],
        unit: str,
        view: Mapping[str, Any],
        incomplete: bool,
    ) -> TransportResponse:
        # Not ``canonical``: rows keep their columns in result order.
        body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            row_unit=unit,
            backend_rows_returned=len(rows),
            incomplete=incomplete,
            view_fields=dict(view),
        )


def _text(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return _safe_text(str(value))


def _describe_body(
    schema: str,
    table: str,
    columns: list[dict[str, Any]],
    primary: list[str],
    foreign: list[dict[str, Any]],
    indexes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cut_columns: list[dict[str, Any]] = []
    for column in columns:
        item = dict(column)
        default = item.get("default")
        if isinstance(default, str) and len(default) > MAX_VALUE_CHARS:
            item["default"] = default[:MAX_VALUE_CHARS]
            item["_truncated_fields"] = ["default"]
        cut_columns.append(item)
    for index in indexes:
        definition = index.get("definition")
        if isinstance(definition, str) and len(definition) > MAX_VALUE_CHARS:
            index["definition"] = definition[:MAX_VALUE_CHARS]
    body = {
        "results": cut_columns,
        "schema": _safe_text(schema),
        "table": _safe_text(table),
        "primary_key": primary,
        "foreign_keys": foreign,
        "indexes": indexes,
    }
    return body, cut_columns


_TABLE_TYPES: Mapping[str, str] = {
    "r": "table",
    "p": "partitioned_table",
    "f": "foreign_table",
    "v": "view",
    "m": "materialized_view",
    "BASE TABLE": "table",
    "VIEW": "view",
    "SYSTEM VIEW": "view",
}


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """The over-limit refusal summary (#308 U5) with what to change."""
    if tool == QUERY_TOOL:
        return {
            "row_unit": "rows",
            "suggestion": (
                "Select fewer columns, aggregate with COUNT/GROUP BY, add a "
                "WHERE clause or a smaller LIMIT, or cut long text with "
                "substring()/left()."
            ),
        }
    if tool == LIST_TOOL:
        return {"row_unit": "tables", "suggestion": "Give schema to list one schema."}
    return {"row_unit": "columns"}


# -- model-visible face and registrations (C6-15) -----------------------------

_DIALECT_TEXT: Mapping[str, str] = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL or MariaDB",
    "clickhouse": "ClickHouse",
    "mssql": "SQL Server",
}
_CANNOT_PROVE = (
    "This is the database as it is at query time, not its state during the "
    "incident window: rows may have changed since. A row that is not "
    "returned may lie past limit, or be invisible to the read-only account. "
    "Values are redacted by column name only; a secret under an alias or a "
    "bare secret value is not recognised."
)
_REFUSALS_TEXT = (
    "Only one read-only statement is run: it must start with SELECT, WITH, "
    "EXPLAIN, SHOW, DESCRIBE or DESC; at most one trailing semicolon. Refused "
    "before sending (QUERY_CONSTRUCT_NOT_ALLOWED): the words INSERT, UPDATE, "
    "DELETE, DROP, CREATE, ALTER, TRUNCATE, REPLACE, MERGE, GRANT, REVOKE, "
    "VACUUM, INTO, COPY, CALL, EXEC, EXECUTE, DO, SET, LOAD, HANDLER, LOCK, "
    "OUTFILE, DUMPFILE and FOR UPDATE/SHARE anywhere outside quotes (so the "
    "REPLACE() function and CHARACTER SET are refused too; use translate() "
    "or regexp_replace() instead), and calls of sleep, lock, file, "
    "notification and remote-access functions such as pg_sleep, "
    "pg_advisory_lock, pg_read_file, lo_export, dblink, SLEEP, BENCHMARK, "
    "GET_LOCK and LOAD_FILE. A column with one of those names must be "
    "quoted. EXPLAIN ANALYZE SELECT is allowed. Every call runs in a "
    "read-only transaction that is rolled back, and the tool first checks "
    "that the configured account is read-only (SQL_ACCOUNT_NOT_READ_ONLY "
    "otherwise, nothing run). Write operations cannot be enabled."
)
_ERRORS_TEXT = (
    "A syntax error, an unknown table or column, or a bad value is "
    "INVALID_PARAMS with the database's error text; an authentication "
    "failure or a missing privilege is SOURCE_FORBIDDEN; a database that "
    "cannot be reached is SOURCE_UNAVAILABLE; a call past its 30 s deadline "
    "is a timeout (the database statement is cancelled); no rows is no data."
)


#: C6b: the refusal texts of the two later dialects (C6a texts unchanged).
_CLICKHOUSE_REFUSALS_TEXT = (
    "Only one read-only statement is run: it must start with SELECT, WITH, "
    "EXPLAIN, SHOW, DESCRIBE or DESC; at most one trailing semicolon. Refused "
    "before sending (QUERY_CONSTRUCT_NOT_ALLOWED): write and DDL words "
    "(INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, RENAME, ATTACH, "
    "DETACH, OPTIMIZE, KILL, SYSTEM statements, INTO OUTFILE and more), "
    "SETTINGS and FORMAT clauses (so the format() function is refused too), "
    "'#' and '$' outside quotes, and the table functions that reach outside "
    "the database: url, file, s3, gcs, azureBlobStorage, hdfs, remote, "
    "remoteSecure, cluster, mysql, postgresql, sqlite, mongodb, redis, odbc, "
    "jdbc, executable, input, iceberg, deltaLake and hudi, plus sleep. "
    "Reading system tables is allowed, but write the database name with "
    "backticks: `system`.query_log, `system`.parts, `system`.processes (an "
    "unquoted SYSTEM is refused). A column with one of those names must be quoted. The tool "
    "sends HTTP GET only, which ClickHouse runs read-only, and first checks "
    "that the configured account only has read grants "
    "(SQL_ACCOUNT_NOT_READ_ONLY otherwise, nothing run). Write operations "
    "cannot be enabled."
)
_MSSQL_REFUSALS_TEXT = (
    "Only one read-only statement is run: it must start with SELECT or WITH "
    "(use TOP n, not LIMIT); at most one trailing semicolon. Refused before "
    "sending (QUERY_CONSTRUCT_NOT_ALLOWED): write and DDL words (INSERT, "
    "UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, MERGE, SELECT ... INTO "
    "and more), EXEC/EXECUTE, DECLARE, WAITFOR, BEGIN, COMMIT, ROLLBACK, USE, "
    "DBCC, KILL, BULK, BACKUP, RESTORE, SET, GO, WHILE, IF, PRINT, NEXT VALUE "
    "FOR, the locking hints UPDLOCK, XLOCK, HOLDLOCK, TABLOCK, TABLOCKX, "
    "PAGLOCK, ROWLOCK, SERIALIZABLE, REPEATABLEREAD and READPAST (NOLOCK is "
    "allowed), and calls of "
    "xp_*, sp_*, OPENROWSET, OPENQUERY, OPENDATASOURCE and OPENXML. A column "
    "with one of those names must be quoted with [] . There is no EXPLAIN "
    "and SET SHOWPLAN is refused: read plans and query statistics from DMVs "
    "such as sys.dm_exec_query_stats, sys.dm_exec_requests and "
    "sys.dm_exec_query_plan. SQL Server has no read-only session: the tool "
    "first checks that the configured account is read-only "
    "(SQL_ACCOUNT_NOT_READ_ONLY otherwise, nothing run) and runs the "
    "statement inside a transaction that is always rolled back. Write "
    "operations cannot be enabled."
)


def _refusals(dialect: str) -> str:
    if dialect == "clickhouse":
        return _CLICKHOUSE_REFUSALS_TEXT
    if dialect == "mssql":
        return _MSSQL_REFUSALS_TEXT
    return _REFUSALS_TEXT


def _query_description(dialect: str) -> str:
    if dialect in ("clickhouse", "mssql"):
        limit_hint = "TOP n" if dialect == "mssql" else "a LIMIT"
        return (
            f"Run one read-only SQL statement against the configured "
            f"{_DIALECT_TEXT[dialect]} database and return the rows. Use the "
            f"{_DIALECT_TEXT[dialect]} dialect. Start by listing tables and "
            "describing the relevant ones; summarise with COUNT(*), GROUP BY "
            f"and aggregates instead of reading every row, and put {limit_hint} "
            "in the SQL (the tool reads at most limit rows, default "
            f"{DEFAULT_LIMIT}, at most {MAX_LIMIT}, and marks the result "
            "incomplete when more were left). "
            + _refusals(dialect)
            + " Results are the data at query time. Each row is an object of "
            f"column name to value; string values are cut to {MAX_VALUE_CHARS} "
            "characters and marked. " + _ERRORS_TEXT
        )
    return (
        f"Run one read-only SQL statement against the configured "
        f"{_DIALECT_TEXT[dialect]} database and return the rows. Use the "
        f"{_DIALECT_TEXT[dialect]} dialect. Start by listing tables and "
        "describing the relevant ones; summarise with COUNT(*), GROUP BY and "
        "aggregates instead of reading every row, and put a LIMIT in the SQL "
        "(the tool reads at most limit rows, default "
        f"{DEFAULT_LIMIT}, at most {MAX_LIMIT}, and marks the result "
        "incomplete when more were left). Use EXPLAIN (ANALYZE) to see a "
        "query plan, for example to find a missing index. "
        + _REFUSALS_TEXT
        + " Results are the data at query time. Each row is an object of "
        f"column name to value; string values are cut to {MAX_VALUE_CHARS} "
        "characters and marked. " + _ERRORS_TEXT
    )


_LIST_DESCRIPTION = (
    "List the tables (and, unless include_views is false, the views) of the "
    "configured database. Give schema to list one schema; omitted lists "
    "every user schema (PostgreSQL) or the configured database (MySQL, "
    f"MariaDB). At most {MAX_LIST_ROWS} rows."
)
_DESCRIBE_DESCRIPTION = (
    "Describe one table or view of the configured database: columns (name, "
    "type, nullable, default), primary key, foreign keys and indexes (with "
    "the index definition on PostgreSQL). table_name may be schema.table."
)


def tool_schemas(dialect: str) -> tuple[Mapping[str, Any], ...]:
    """The three tools' model-visible schemas for ``dialect`` (C6-13: the
    dialect is in ``database_sql_query``'s description)."""
    return (
        {
            "type": "function",
            "function": {
                "name": QUERY_TOOL,
                "description": _query_description(dialect),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": (
                                "One read-only SQL statement (SELECT, WITH, "
                                "EXPLAIN, SHOW, DESCRIBE), at most "
                                f"{MAX_QUERY_CHARS} characters. Example: SELECT "
                                "status, count(*) FROM orders GROUP BY status "
                                "LIMIT 50"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Most rows to return, 1 to {MAX_LIMIT} "
                                f"(default {DEFAULT_LIMIT})."
                            ),
                        },
                    },
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": LIST_TOOL,
                "description": _LIST_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schema": {
                            "type": "string",
                            "description": "Schema (database on MySQL/MariaDB) to list.",
                        },
                        "include_views": {
                            "type": "boolean",
                            "description": "Include views (default true).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": DESCRIBE_TOOL,
                "description": _DESCRIBE_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Table or view name, optionally schema.table.",
                        },
                        "schema": {
                            "type": "string",
                            "description": "Schema the table belongs to (optional).",
                        },
                    },
                    "required": ["table_name"],
                },
            },
        },
    )


_ERROR_CLASSES: Mapping[str, str] = {
    "INVALID_QUERY": "INVALID_PARAMS",
    "INVALID_LIMIT": "INVALID_PARAMS",
    "INVALID_IDENTIFIER": "INVALID_PARAMS",
    "INVALID_PARAMS": "INVALID_PARAMS",
    "QUERY_CONSTRUCT_NOT_ALLOWED": "INVALID_PARAMS",
    "TABLE_NOT_FOUND": "INVALID_PARAMS",
    "SQL_INVALID": "INVALID_PARAMS",
    "SQL_FORBIDDEN": "SOURCE_FORBIDDEN",
    "SESSION_READ_ONLY": "SOURCE_FORBIDDEN",
    "SQL_ACCOUNT_NOT_READ_ONLY": "SOURCE_FORBIDDEN",
    "SQL_TARGETS_OPSPILOT_DB": "SOURCE_FORBIDDEN",
    "SQL_SOURCE_ERROR": "SOURCE_ERROR",
}

_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. The database is "
    "read at query time whatever the window says; the first note states when "
    "it was read relative to the window's end. No time filter is added to "
    "the SQL."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. The "
    "database and account come from configuration; the model cannot choose "
    "them. schema and table_name are 1 to 128 characters of letters, digits, "
    "_ . $ - (INVALID_IDENTIFIER otherwise)."
)
_LIMITS = (
    f"At most {MAX_LIMIT} rows (list: {MAX_LIST_ROWS} tables); string values "
    f"cut to {MAX_VALUE_CHARS} characters and marked. One call reads at most "
    "2 MiB and returns at most 2 MiB (RESULT_TOO_LARGE otherwise, whole "
    "call); the view is limited to 25,000 tokens and refused whole above it. "
    "30 s per call on a new connection, no retry."
)


def sql_registrations(
    dialect: str, *, max_window_seconds: int
) -> tuple[ToolRegistration, ...]:
    """The three registrations (C6-10: ``source="otel-demo"``, the lab
    target's; ``may_contain_secrets=True``; verbs query/get/get)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "may_contain_secrets": True,
        "result_path": ("results",),
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": READ_LIMIT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        # C6b: the ClickHouse-only code joins only that dialect's table, so
        # the C6a registrations (and revisions) are unchanged.
        "error_classes": {
            **_ERROR_CLASSES,
            **(
                {"SQL_ACCOUNT_UNSUPPORTED": "SOURCE_FORBIDDEN"}
                if dialect == "clickhouse"
                else {}
            ),
        },
    }
    query = ToolRegistration(
        name=QUERY_TOOL,
        verb="query",
        parameters={
            "sql": ParameterSpec(
                "string", required=True, description="One read-only SQL statement."
            ),
            "limit": ParameterSpec(
                "integer",
                description=f"Most rows, 1 to {MAX_LIMIT} (default {DEFAULT_LIMIT}).",
            ),
        },
        description=ToolDescription(
            returns=(
                f"Rows of the statement on the configured {_DIALECT_TEXT[dialect]} "
                "database under results, each an object of column name to "
                "value (a repeated column name gets #2, #3). The view adds "
                "dialect, columns (in result order), limit, "
                "long_values_cut, notes, incomplete_reason and cannot_prove."
            ),
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS + " " + _refusals(dialect),
            cannot_prove=_CANNOT_PROVE,
        ),
        **common,
    )
    listing = ToolRegistration(
        name=LIST_TOOL,
        verb="get",
        parameters={
            "schema": ParameterSpec("string", description="Schema to list."),
            "include_views": ParameterSpec("boolean", description="Include views."),
        },
        description=ToolDescription(
            returns=(
                "Tables and views under results, each {schema, name, type}, "
                "sorted by schema and name; type is table, view, "
                "materialized_view, partitioned_table or foreign_table."
            ),
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        **common,
    )
    describe = ToolRegistration(
        name=DESCRIBE_TOOL,
        verb="get",
        parameters={
            "table_name": ParameterSpec(
                "string", required=True, description="Table or view name."
            ),
            "schema": ParameterSpec("string", description="Schema of the table."),
        },
        description=ToolDescription(
            returns=(
                "The table's columns under results, each {name, type, "
                "nullable, default, ordinal}; the view adds schema, table, "
                "primary_key, foreign_keys {name, columns, referred_schema, "
                "referred_table, referred_columns} and indexes {name, columns, "
                "unique, primary, definition}."
            ),
            window_format=_WINDOW_FORMAT,
            values_format=_VALUES_FORMAT,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        **common,
    )
    return query, listing, describe
