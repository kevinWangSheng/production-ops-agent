"""Read-only MongoDB tools (C6c, #570): ``mongodb_find``,
``mongodb_aggregate``, ``mongodb_list_collections``,
``mongodb_collection_schema``, ``mongodb_server_status`` and
``mongodb_current_op``.

The upstream HolmesGPT ``mongodb`` toolset's default tool names
(``holmes/plugins/toolsets/mongodb/mongodb.py`` at ``a0009195``) for one
configured database; the model cannot pick the database (no ``database``
parameter, no ``mongodb_list_databases``). The public interface is the
"C6c" section of ``docs/tasks/2026-11-20-tool-expansion-c6.md``.

Read-only is the tool's guarantee (PRODUCT-CONSTRAINTS line 18):

* the operator's read-only account (``mongodb-ro``), checked after every
  connect from ``connectionStatus`` (C6-U1: roles ``read``,
  ``readAnyDatabase``, ``clusterMonitor`` only; ``account_problem``);
* a fixed command surface -- the tool only ever sends ``find``,
  ``aggregate``, ``listCollections``, ``listIndexes``, ``count``,
  ``serverStatus``, ``connectionStatus`` and ``killCursors``; the model only
  supplies a filter, projection, sort or pipeline;
* a content check before anything is sent (``query_problem`` /
  ``pipeline_problem``): JavaScript and write operators at any depth, and
  pipeline stages outside a whitelist at every nesting level, are refused.

MongoDB has no read-only session; with a write-capable account only the
command surface and the content check stand in the way, which is why the
account check is not optional.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
from opspilot.tools.sql_query import SqlDriverError, _dsn_endpoints, _host_key

__all__ = [
    "AGGREGATE_TOOL",
    "CREDENTIAL_REF",
    "CURRENT_OP_TOOL",
    "ENDPOINT_SELECTOR",
    "FIND_TOOL",
    "LIST_TOOL",
    "SCHEMA_TOOL",
    "STATUS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "URL_ENV",
    "MongoConfig",
    "MongoConnection",
    "MongoTransport",
    "account_problem",
    "mongodb_enabled",
    "mongodb_registrations",
    "overflow_summary",
    "pipeline_problem",
    "query_problem",
]

FIND_TOOL = "mongodb_find"
AGGREGATE_TOOL = "mongodb_aggregate"
LIST_TOOL = "mongodb_list_collections"
SCHEMA_TOOL = "mongodb_collection_schema"
STATUS_TOOL = "mongodb_server_status"
CURRENT_OP_TOOL = "mongodb_current_op"
TOOL_NAMES: tuple[str, ...] = (
    FIND_TOOL,
    AGGREGATE_TOOL,
    LIST_TOOL,
    SCHEMA_TOOL,
    STATUS_TOOL,
    CURRENT_OP_TOOL,
)

CREDENTIAL_REF = "mongodb-ro"
ENDPOINT_SELECTOR = "mongodb_endpoint"

URL_ENV = "OPSPILOT_MONGODB_URL"
DATABASE_ENV = "OPSPILOT_MONGODB_DATABASE"
USERNAME_ENV = "OPSPILOT_MONGODB_USERNAME"
PASSWORD_ENV = "OPSPILOT_MONGODB_PASSWORD"
AUTH_SOURCE_ENV = "OPSPILOT_MONGODB_AUTH_SOURCE"
TLS_ENV = "OPSPILOT_MONGODB_TLS"
CA_FILE_ENV = "OPSPILOT_MONGODB_CA_FILE"
_OTHER_ENVS = (
    DATABASE_ENV,
    USERNAME_ENV,
    PASSWORD_ENV,
    AUTH_SOURCE_ENV,
    TLS_ENV,
    CA_FILE_ENV,
)
_TLS_MODES = ("verify-full", "require", "disable")
DEFAULT_PORT = 27017

REQUEST_TIMEOUT_SECONDS = 30.0
_SERVER_TIMEOUT_MARGIN = 2.0
_MIN_MAX_TIME_MS = 1000
READ_LIMIT_BYTES = MAX_RESULT_BYTES
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_LIST_ROWS = 500
MAX_OPS = 200  # about 100 tokens per operation row: 200 fit the 25k view
SCHEMA_SAMPLE = 10
MAX_JSON_CHARS = 20_000
MAX_DEPTH = 30
MAX_VALUE_CHARS = 2000
_ERROR_TEXT_CHARS = 500
_APPLICATION_NAME = "opspilot-mongodb-tool"

_COLLECTION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}")
_DATABASE = re.compile(r"[^/\\. \"$]{1,63}")
_SECTIONS = (
    "connections",
    "opcounters",
    "mem",
    "locks",
    "globalLock",
    "network",
    "wiredTiger",
    "metrics",
    "opLatencies",
    "tcmalloc",
    "flowControl",
    "repl",
    "asserts",
    "transactions",
)
_DEFAULT_SECTIONS = (
    "connections",
    "opcounters",
    "mem",
    "locks",
    "globalLock",
    "network",
)


# -- configuration ------------------------------------------------------------


def mongodb_enabled(env: Mapping[str, str]) -> bool:
    """Whether the extended profile shows the tools: the address alone (the
    web calls this too and never reads a credential)."""
    return bool(env.get(URL_ENV))


def _url_parts(value: str) -> tuple[str, int]:
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ToolContractError("MONGODB_CONFIG_INVALID") from None
    if (
        parts.scheme.lower() != "mongodb"
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
        raise ToolContractError("MONGODB_CONFIG_INVALID")
    return host, port or DEFAULT_PORT


@dataclass(frozen=True)
class MongoConfig:
    """The worker's ``mongodb-ro`` connection. The password is never
    rendered."""

    url: str
    database: str
    username: str
    password: str = field(repr=False)
    auth_source: str = "admin"
    tls: str = "verify-full"
    ca_file: str | None = None
    host: str = field(init=False)
    port: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ToolContractError("MONGODB_CONFIG_MISSING")
        host, port = _url_parts(self.url)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        for value in (self.database, self.username, self.password):
            if not isinstance(value, str) or not value:
                raise ToolContractError("MONGODB_CONFIG_MISSING")
            if not value.isprintable():
                raise ToolContractError("MONGODB_CONFIG_INVALID")
        for value in (self.database, self.auth_source):
            if _DATABASE.fullmatch(value) is None:
                raise ToolContractError("MONGODB_CONFIG_INVALID")
        if self.tls not in _TLS_MODES:
            raise ToolContractError("MONGODB_CONFIG_INVALID")
        if self.ca_file is not None:
            if self.tls == "disable":
                raise ToolContractError("MONGODB_CONFIG_INVALID")
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ToolContractError("MONGODB_CONFIG_INVALID")

    @property
    def endpoint(self) -> str:
        return self.url

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MongoConfig | None:
        url = env.get(URL_ENV) or ""
        values = {name: env.get(name) or None for name in _OTHER_ENVS}
        if not url:
            if any(values.values()):
                raise ToolContractError("MONGODB_CONFIG_INVALID")
            return None
        _url_parts(url)
        if not (values[DATABASE_ENV] and values[USERNAME_ENV] and values[PASSWORD_ENV]):
            raise ToolContractError("MONGODB_CONFIG_MISSING")
        config = cls(
            url=url,
            database=str(values[DATABASE_ENV]),
            username=str(values[USERNAME_ENV]),
            password=str(values[PASSWORD_ENV]),
            auth_source=values[AUTH_SOURCE_ENV] or "admin",
            tls=values[TLS_ENV] or "verify-full",
            ca_file=values[CA_FILE_ENV],
        )
        if (_host_key(config.host), config.port) in _dsn_endpoints(env):
            raise ToolContractError("MONGODB_TARGETS_OPSPILOT_DB")
        return config


# -- content check (the second layer) -----------------------------------------

_FORBIDDEN_KEYS = frozenset(
    {"$where", "$function", "$accumulator", "$code", "$out", "$merge"}
)
#: compared case-insensitively (``$WHERE``, ``$Out``), fail-closed
_FORBIDDEN_KEYS_LOWER = frozenset(key.lower() for key in _FORBIDDEN_KEYS)
_ALLOWED_STAGES = frozenset(
    {
        "$match",
        "$project",
        "$addFields",
        "$set",
        "$unset",
        "$group",
        "$sort",
        "$limit",
        "$skip",
        "$count",
        "$unwind",
        "$lookup",
        "$graphLookup",
        "$unionWith",
        "$facet",
        "$bucket",
        "$bucketAuto",
        "$sortByCount",
        "$replaceRoot",
        "$replaceWith",
        "$sample",
        "$redact",
        "$densify",
        "$fill",
        "$setWindowFields",
        "$geoNear",
    }
)


class _Reject(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _parse(text: object, what: str) -> Any:
    if not isinstance(text, str) or not text.strip():
        raise _Reject("INVALID_QUERY", f"{what} must be a non-empty JSON string")
    if len(text) > MAX_JSON_CHARS:
        raise _Reject(
            "INVALID_QUERY", f"{what} must be at most {MAX_JSON_CHARS} characters"
        )
    from bson import json_util

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise _Reject("INVALID_JSON", f"{what} repeats a key")
        return dict(pairs)

    try:
        # plain JSON first: repeated keys are refused, and operators such as
        # $code are seen as keys before json_util turns them into values
        plain = json.loads(text, object_pairs_hook=unique)
    except _Reject:
        raise
    except (ValueError, RecursionError):
        raise _Reject("INVALID_JSON", f"{what} is not valid JSON") from None
    _walk(plain)
    try:
        return json_util.loads(text, json_options=json_util.RELAXED_JSON_OPTIONS)
    except (ValueError, TypeError) as error:
        raise _Reject(
            "INVALID_JSON", f"{what} is not valid JSON ({type(error).__name__})"
        ) from None
    except Exception:
        raise _Reject("INVALID_JSON", f"{what} is not valid JSON") from None


def _walk(value: Any, depth: int = 0) -> None:
    """Refuse forbidden operators at any depth (and over-deep documents)."""
    if depth > MAX_DEPTH:
        raise _Reject("INVALID_QUERY", f"nested more than {MAX_DEPTH} levels")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _Reject("INVALID_QUERY", "a key that is not a string")
            if key.lower() in _FORBIDDEN_KEYS_LOWER:
                raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", key)
            _walk(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, depth + 1)
    elif type(value).__name__ == "Code":
        raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", "JavaScript code value")


def _check_pipeline(pipeline: Any, depth: int = 0) -> None:
    if not isinstance(pipeline, list):
        raise _Reject("INVALID_QUERY", "a pipeline must be a JSON array of stages")
    for stage in pipeline:
        if not isinstance(stage, Mapping) or len(stage) != 1:
            raise _Reject("INVALID_QUERY", "every stage must be an object with one key")
        ((name, body),) = stage.items()
        if name not in _ALLOWED_STAGES:
            raise _Reject("QUERY_CONSTRUCT_NOT_ALLOWED", f"stage {name}")
        if name in ("$lookup", "$graphLookup"):
            if not isinstance(body, Mapping) or not isinstance(body.get("from"), str):
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED",
                    f"{name} must name a collection of this database in 'from'",
                )
            if "db" in body:
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED", f"{name} into another database"
                )
            if name == "$lookup" and "pipeline" in body:
                _check_pipeline(body["pipeline"], depth + 1)
        elif name == "$unionWith":
            if isinstance(body, str):
                pass
            elif (
                isinstance(body, Mapping)
                and isinstance(body.get("coll"), str)
                and "db" not in body
            ):
                if "pipeline" in body:
                    _check_pipeline(body["pipeline"], depth + 1)
            else:
                raise _Reject(
                    "QUERY_CONSTRUCT_NOT_ALLOWED",
                    "$unionWith must name a collection of this database",
                )
        elif name == "$facet":
            if not isinstance(body, Mapping):
                raise _Reject("INVALID_QUERY", "$facet must be an object of pipelines")
            for branch in body.values():
                _check_pipeline(branch, depth + 1)


def _problem_text(reject: _Reject) -> str:
    if reject.code == "QUERY_CONSTRUCT_NOT_ALLOWED":
        return (
            f"QUERY_CONSTRUCT_NOT_ALLOWED: {reject.detail}. Only reads are "
            "allowed: JavaScript ($where, $function, $accumulator), write stages "
            "($out, $merge), cross-database lookups and stages outside the "
            "read-only list are refused."
        )
    return f"{reject.code}: {reject.detail}."


def query_problem(text: object, what: str = "filter") -> str | None:
    """The refusal note for a find filter, projection or sort, or ``None``."""
    try:
        value = _parse(text, what)
        if not isinstance(value, Mapping):
            raise _Reject("INVALID_QUERY", f"{what} must be a JSON object")
        _walk(value)
    except _Reject as reject:
        return _problem_text(reject)
    return None


def pipeline_problem(text: object) -> str | None:
    """The refusal note for an aggregation pipeline, or ``None``."""
    try:
        value = _parse(text, "pipeline")
        _walk(value)
        _check_pipeline(value)
    except _Reject as reject:
        return _problem_text(reject)
    return None


# -- account check (C6-U1) ------------------------------------------------------

_ALLOWED_ROLES = frozenset({"read", "readAnyDatabase", "clusterMonitor"})
_WRITE_ACTIONS = frozenset(
    {
        "insert",
        "update",
        "remove",
        "createCollection",
        "createIndex",
        "dropCollection",
        "dropDatabase",
        "dropIndex",
        "renameCollectionSameDB",
        "convertToCapped",
        "collMod",
        "compact",
        "enableProfiler",
        "createUser",
        "dropUser",
        "updateUser",
        "grantRole",
        "revokeRole",
        "createRole",
        "dropRole",
        "updateRole",
        "changePassword",
        "changeOwnPassword",
        "changeCustomData",
        "setAuthenticationRestriction",
        "killop",
        "killAnySession",
        "shutdown",
        "fsync",
        "setParameter",
        "replSetConfigure",
        "applicationMessage",
        "bypassDocumentValidation",
        "anyAction",
        "internal",
        "reIndex",
        "dropAllRolesFromDatabase",
        "dropAllUsersFromDatabase",
        "planCacheWrite",
        "splitVector",
        "cleanupOrphaned",
        "moveChunk",
        "splitChunk",
        "enableSharding",
        "flushRouterConfig",
        "logRotate",
        "resync",
    }
)


def account_problem(status: Mapping[str, Any]) -> str | None:
    """The refusal note of the post-connect account check (C6-U1) over a
    ``connectionStatus`` answer, or ``None`` when the account looks
    read-only. Fails closed: a reply without the expected shape (roles a
    non-empty list of ``{role, db}``, privileges a list of objects with an
    ``actions`` list) is refused rather than read as "nothing bad found"."""
    info = status.get("authInfo") if isinstance(status, Mapping) else None
    if not isinstance(info, Mapping):
        info = {}
    users = info.get("authenticatedUsers")
    if not isinstance(users, list) or not users:
        return (
            "MONGODB_ACCOUNT_NOT_READ_ONLY: no authenticated user (the server "
            "may have authentication off); nothing was run."
        )
    roles = info.get("authenticatedUserRoles")
    privileges = info.get("authenticatedUserPrivileges")
    if (
        not isinstance(roles, list)
        or not roles
        or not all(
            isinstance(role, Mapping)
            and isinstance(role.get("role"), str)
            and isinstance(role.get("db"), str)
            for role in roles
        )
        or not isinstance(privileges, list)
        or not all(
            isinstance(privilege, Mapping)
            and isinstance(privilege.get("actions"), list)
            and all(isinstance(action, str) for action in privilege["actions"])
            for privilege in privileges
        )
    ):
        return (
            "MONGODB_ACCOUNT_NOT_READ_ONLY: the connectionStatus reply does not "
            "have the expected roles and privileges; nothing was run."
        )
    problems: list[str] = []
    for role in roles:
        if role["role"] not in _ALLOWED_ROLES:
            problems.append(f"role {role['role'][:64]}@{role['db'][:64]}")
    for privilege in privileges:
        for action in privilege["actions"]:
            if action in _WRITE_ACTIONS:
                problems.append(f"action {action}")
    if problems:
        unique = sorted(set(problems))
        return (
            "MONGODB_ACCOUNT_NOT_READ_ONLY: the configured account is not "
            "read-only (" + ", ".join(unique[:8]) + "); nothing was run."
        )
    return None


# -- driver seam -------------------------------------------------------------------


class MongoConnection(Protocol):
    def command(self, database: str, spec: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


Connector = Callable[[MongoConfig, float], MongoConnection]


class _PyMongoConnection:
    """One ``MongoClient`` to the configured host only (``directConnection``),
    no retries. Driver errors pass through to the transport."""

    def __init__(self, config: MongoConfig, timeout: float) -> None:
        from pymongo import MongoClient
        from pymongo.read_preferences import SecondaryPreferred

        ms = max(1000, int(timeout * 1000))
        # pymongo keeps retrying server selection until this runs out; end it
        # before the call's deadline so an unreachable server is reported as
        # unavailable, not as a timeout
        select_ms = max(
            1000, min(10_000, int((timeout - _SERVER_TIMEOUT_MARGIN) * 1000))
        )
        kwargs: dict[str, Any] = {
            "host": config.host,
            "port": config.port,
            "username": config.username,
            "password": config.password,
            "authSource": config.auth_source,
            "directConnection": True,
            "retryReads": False,
            "retryWrites": False,
            "appname": _APPLICATION_NAME,
            "readPreference": "secondaryPreferred",
            "serverSelectionTimeoutMS": select_ms,
            "connectTimeoutMS": select_ms,
            "socketTimeoutMS": ms,
            "maxPoolSize": 1,
            "connect": False,
        }
        if config.tls == "disable":
            kwargs["tls"] = False
        else:
            kwargs["tls"] = True
            if config.ca_file:
                kwargs["tlsCAFile"] = config.ca_file
            if config.tls == "require":
                kwargs["tlsAllowInvalidCertificates"] = True
                kwargs["tlsAllowInvalidHostnames"] = True
        self._client: Any = MongoClient(**kwargs)
        self._read_preference = SecondaryPreferred()

    def command(self, database: str, spec: Mapping[str, Any]) -> Mapping[str, Any]:
        from bson import SON

        document = SON(list(spec.items()))
        reply: Mapping[str, Any] = self._client[database].command(
            document, read_preference=self._read_preference
        )
        return reply

    def cancel(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def default_connector(config: MongoConfig, timeout: float) -> MongoConnection:
    return _PyMongoConnection(config, timeout)


_INVALID_CODES = frozenset(
    {
        2,
        9,
        14,
        15,
        16,
        17,
        26,
        40324,
        168,
        31249,
        31254,
        40353,
        51091,
        28769,
        40323,
        16410,
        16411,
        15983,
        40352,
        4822819,
    }
)
_FORBIDDEN_CODES = frozenset({13, 18, 11})


def _classify(error: BaseException, *, cancelled: bool) -> SqlDriverError:
    if isinstance(error, SqlDriverError):
        if cancelled and error.kind in ("unavailable", "error"):
            return SqlDriverError("timeout", error.code, "")
        return error
    try:
        from pymongo import errors
    except ImportError:  # pragma: no cover - a locked dependency
        return SqlDriverError("error")
    if isinstance(error, errors.OperationFailure):
        code = int(error.code or 0)
        details = getattr(error, "details", None)
        errmsg = details.get("errmsg") if isinstance(details, Mapping) else None
        message = str(errmsg or error)
        if code == 50:
            return SqlDriverError("timeout", str(code), "")
        if code in _FORBIDDEN_CODES:
            return SqlDriverError("forbidden", str(code), "")
        if code in _INVALID_CODES:
            return SqlDriverError("invalid", str(code), message)
        if cancelled:
            return SqlDriverError("timeout", str(code), "")
        return SqlDriverError("error", str(code), "")
    if isinstance(error, errors.NetworkTimeout):
        return SqlDriverError("timeout" if cancelled else "unavailable")
    if isinstance(
        error, (errors.ConnectionFailure, errors.ServerSelectionTimeoutError)
    ):
        return SqlDriverError("timeout" if cancelled else "unavailable")
    if isinstance(error, errors.InvalidOperation) and cancelled:
        return SqlDriverError("timeout")
    if cancelled:
        return SqlDriverError("timeout")
    return SqlDriverError("error")


# -- projection --------------------------------------------------------------------


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, Decimal):
        return str(value)
    name = type(value).__name__
    if name == "Binary":
        return "0x" + bytes(value).hex()
    return str(value)  # ObjectId, Decimal128, Regex, Code, Timestamp, ...


def _safe_text(value: str) -> str:
    return value.encode("utf-8", "replace").decode("utf-8")


def _cut(value: Any, path: str, cut: list[str]) -> Any:
    if isinstance(value, str):
        text = _safe_text(value)
        if len(text) > MAX_VALUE_CHARS:
            cut.append(path)
            return text[:MAX_VALUE_CHARS]
        return text
    if isinstance(value, dict):
        return {k: _cut(v, f"{path}.{k}" if path else k, cut) for k, v in value.items()}
    if isinstance(value, list):
        return [
            _cut(v, f"{path}.{i}" if path else str(i), cut) for i, v in enumerate(value)
        ]
    return value


def _project_documents(docs: Sequence[Any]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    total = 0
    raw = 0
    for doc in docs:
        value = _json_value(doc)
        raw += len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        if raw > READ_LIMIT_BYTES:
            raise TransportResultTooLarge(str(READ_LIMIT_BYTES))
        cut: list[str] = []
        row = _cut(value, "", cut) if isinstance(value, dict) else {"value": value}
        if cut:
            row["_truncated_fields"] = cut
            total += len(cut)
        rows.append(row)
    return rows, total


# -- transport ------------------------------------------------------------------------


def _iso(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refused(note: str) -> TransportResponse:
    code = note.split(":", 1)[0]
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [note]},
    )


def _answered(note: str, *, text: str | None = None) -> TransportResponse:
    code = note.split(":", 1)[0]
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        view_fields={"notes": [note]},
        http_status=400 if text is not None else None,
        source_error_text=text,
    )


def _driver_failure(error: SqlDriverError) -> TransportResponse:
    label = f"code {error.code}" if error.code else "no code"
    if error.kind == "timeout":
        raise TransportTimeout("TIMEOUT", source_contact="confirmed")
    if error.kind == "unavailable":
        raise TransportUnavailable("MONGODB_UNAVAILABLE")
    if error.kind == "invalid":
        return _answered(
            f"MONGODB_INVALID: the server rejected the request ({label}); fix "
            "it using the server's error text.",
            text=_safe_text(error.message)[:_ERROR_TEXT_CHARS],
        )
    if error.kind == "forbidden":
        return _answered(
            f"MONGODB_FORBIDDEN: the server refused the account ({label}): "
            "authentication failed or the account may not read this."
        )
    return _answered(f"MONGODB_SOURCE_ERROR: the server failed the call ({label}).")


class MongoTransport:
    """Serves the six tools over a new client per call (no pool, no retry).
    Every refusal of the model's input is decided before connecting."""

    def __init__(
        self,
        config: MongoConfig,
        *,
        connector: Connector | None = None,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._connector = connector or default_connector
        self._timeout = request_timeout_seconds
        self._monotonic = monotonic or time.monotonic

    @staticmethod
    def _limit(params: Mapping[str, object]) -> int | TransportResponse:
        limit = params.get("limit", DEFAULT_LIMIT)
        if limit is None:
            limit = DEFAULT_LIMIT
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            return _refused(
                f"INVALID_LIMIT: limit must be an integer from 1 to {MAX_LIMIT}."
            )
        return limit

    @staticmethod
    def _collection(params: Mapping[str, object]) -> str | TransportResponse:
        name = params.get("collection")
        if not isinstance(name, str) or _COLLECTION.fullmatch(name) is None:
            return _refused(
                "INVALID_IDENTIFIER: collection must be 1 to 120 characters of "
                "letters, digits, _ . - (not starting with '.')."
            )
        return name

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        p = request.params
        plan: Callable[[MongoConnection, Callable[[], int]], TransportResponse]
        collection = ""
        if request.tool in (FIND_TOOL, AGGREGATE_TOOL, SCHEMA_TOOL):
            checked = self._collection(p)
            if isinstance(checked, TransportResponse):
                return checked
            collection = checked
        if request.tool == FIND_TOOL:
            find_limit = self._limit(p)
            if isinstance(find_limit, TransportResponse):
                return find_limit
            limit = find_limit
            problem = query_problem(p.get("filter"), "filter")
            for key in ("projection", "sort"):
                if problem is None and p.get(key) is not None:
                    problem = query_problem(p.get(key), key)
            if problem is not None:
                return _refused(problem)
            from bson import json_util

            spec_filter = json_util.loads(str(p["filter"]))
            projection = (
                json_util.loads(str(p["projection"])) if p.get("projection") else None
            )
            sort = json_util.loads(str(p["sort"])) if p.get("sort") else None
            if sort is not None and not all(
                v in (1, -1) or (isinstance(v, Mapping) and set(v) == {"$meta"})
                for v in sort.values()
            ):
                return _refused(
                    'INVALID_QUERY: sort values must be 1, -1 or {"$meta": ...}.'
                )
            plan = lambda conn, ms: self._find(  # noqa: E731
                request, conn, ms, collection, spec_filter, projection, sort, limit
            )
        elif request.tool == AGGREGATE_TOOL:
            agg_limit = self._limit(p)
            if isinstance(agg_limit, TransportResponse):
                return agg_limit
            limit = agg_limit
            problem = pipeline_problem(p.get("pipeline"))
            if problem is not None:
                return _refused(problem)
            from bson import json_util

            pipeline = json_util.loads(str(p["pipeline"]))
            plan = lambda conn, ms: self._aggregate(  # noqa: E731
                request, conn, ms, collection, pipeline, limit
            )
        elif request.tool == LIST_TOOL:
            plan = lambda conn, ms: self._list(request, conn, ms)  # noqa: E731
        elif request.tool == SCHEMA_TOOL:
            plan = lambda conn, ms: self._schema(request, conn, ms, collection)  # noqa: E731
        elif request.tool == STATUS_TOOL:
            raw = p.get("sections")
            if raw is None or raw == "":
                sections = list(_DEFAULT_SECTIONS)
            elif isinstance(raw, str):
                sections = [part.strip() for part in raw.split(",") if part.strip()]
                if not sections or any(part not in _SECTIONS for part in sections):
                    return _refused(
                        "INVALID_PARAMS: sections must be a comma-separated list of "
                        + ", ".join(_SECTIONS)
                        + "."
                    )
            else:
                return _refused("INVALID_PARAMS: sections must be a string.")
            plan = lambda conn, ms: self._status(request, conn, ms, sections)  # noqa: E731
        else:
            minimum = p.get("min_duration_ms")
            if minimum is not None and (type(minimum) is not int or minimum < 0):
                return _refused(
                    "INVALID_PARAMS: min_duration_ms must be an integer of 0 or more."
                )
            active = p.get("active_only", True)
            if active is None:
                active = True
            if type(active) is not bool:
                return _refused("INVALID_PARAMS: active_only must be true or false.")
            plan = lambda conn, ms: self._current_op(  # noqa: E731
                request, conn, ms, minimum, active
            )
        return self._session(plan)

    def _session(
        self, plan: Callable[[MongoConnection, Callable[[], int]], TransportResponse]
    ) -> TransportResponse:
        budget = self._timeout
        deadline = self._monotonic() + budget
        state = {"cancelled": False}

        def remaining() -> float:
            left = deadline - self._monotonic()
            if left <= 0:
                raise TransportTimeout("TIMEOUT", source_contact="possible")
            return left

        def max_time_ms() -> int:
            return max(
                _MIN_MAX_TIME_MS, int((remaining() - _SERVER_TIMEOUT_MARGIN) * 1000)
            )

        try:
            conn = self._connector(self._config, remaining())
        except TransportError:
            raise
        except Exception as error:
            return _driver_failure(_classify(error, cancelled=False))

        def on_deadline() -> None:
            state["cancelled"] = True
            conn.cancel()

        timer = threading.Timer(max(0.0, deadline - self._monotonic()), on_deadline)
        timer.daemon = True
        timer.start()
        try:
            status = conn.command(
                "admin", {"connectionStatus": 1, "showPrivileges": True}
            )
            problem = account_problem(status)
            if problem is not None:
                return _answered(problem)
            remaining()
            return plan(conn, max_time_ms)
        except TransportError:
            raise
        except Exception as error:
            late = state["cancelled"] or self._monotonic() >= deadline - 0.05
            return _driver_failure(_classify(error, cancelled=late))
        finally:
            timer.cancel()
            conn.close()

    # -- tools ----------------------------------------------------------------

    def _view(self, request: TransportRequest, **extra: Any) -> dict[str, Any]:
        end = request.window.end
        return {
            "database": self._config.database,
            "notes": [
                "Read at query time (this call's observed time): the database "
                "as it is now, not as it was in the authorized window ending "
                f"{_iso(end)}."
            ],
            "cannot_prove": _CANNOT_PROVE,
            **extra,
        }

    @staticmethod
    def _comment() -> str:
        return f"opspilot-{uuid.uuid4()}"

    def _batch(
        self,
        conn: MongoConnection,
        database: str,
        collection: str,
        reply: Mapping[str, Any],
    ) -> list[Any]:
        cursor = reply.get("cursor") or {}
        batch = list(cursor.get("firstBatch") or [])
        cursor_id = cursor.get("id") or 0
        if cursor_id:
            try:
                conn.command(
                    database,
                    {
                        "killCursors": collection,
                        "cursors": [cursor_id],
                        "comment": self._comment(),
                    },
                )
            except Exception:
                pass
        return batch

    def _respond(
        self,
        rows: list[dict[str, Any]],
        unit: str,
        view: dict[str, Any],
        incomplete: bool,
        body_extra: Mapping[str, Any] | None = None,
    ) -> TransportResponse:
        body = json.dumps(
            {"results": rows, **(body_extra or {})},
            separators=(",", ":"),
            ensure_ascii=False,
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

    def _documents(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        collection: str,
        reply: Mapping[str, Any],
        limit: int,
    ) -> TransportResponse:
        docs = self._batch(conn, self._config.database, collection, reply)
        more = len(docs) > limit
        rows, cut = _project_documents(docs[:limit])
        view = self._view(
            request,
            collection=collection,
            limit=limit,
            long_values_cut=cut,
            incomplete_reason=(
                "reached limit; the collection has more matching documents"
                if more
                else None
            ),
        )
        return self._respond(rows, "documents", view, more)

    def _find(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        max_time_ms: Callable[[], int],
        collection: str,
        spec_filter: Any,
        projection: Any,
        sort: Any,
        limit: int,
    ) -> TransportResponse:
        spec: dict[str, Any] = {"find": collection, "filter": spec_filter}
        if projection is not None:
            spec["projection"] = projection
        if sort is not None:
            spec["sort"] = sort
        spec.update(
            {
                "limit": limit + 1,
                "batchSize": limit + 1,
                "singleBatch": True,
                "maxTimeMS": max_time_ms(),
                "comment": self._comment(),
            }
        )
        reply = conn.command(self._config.database, spec)
        return self._documents(request, conn, collection, reply, limit)

    def _aggregate(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        max_time_ms: Callable[[], int],
        collection: str,
        pipeline: list[Any],
        limit: int,
    ) -> TransportResponse:
        spec = {
            "aggregate": collection,
            "pipeline": [*pipeline, {"$limit": limit + 1}],
            "cursor": {"batchSize": limit + 1},
            "maxTimeMS": max_time_ms(),
            "comment": self._comment(),
        }
        reply = conn.command(self._config.database, spec)
        return self._documents(request, conn, collection, reply, limit)

    def _list(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        max_time_ms: Callable[[], int],
    ) -> TransportResponse:
        reply = conn.command(
            self._config.database,
            {
                "listCollections": 1,
                "nameOnly": True,
                "authorizedCollections": True,
                "maxTimeMS": max_time_ms(),
                "comment": self._comment(),
            },
        )
        docs = (reply.get("cursor") or {}).get("firstBatch") or []
        rows = sorted(
            (
                {
                    "name": _safe_text(str(d.get("name"))),
                    "type": str(d.get("type") or "collection"),
                }
                for d in docs
                if isinstance(d, Mapping)
            ),
            key=lambda r: r["name"],
        )
        more = len(rows) > MAX_LIST_ROWS
        rows = rows[:MAX_LIST_ROWS]
        view = self._view(
            request,
            incomplete_reason=f"only the first {MAX_LIST_ROWS} collections are listed"
            if more
            else None,
        )
        return self._respond(rows, "collections", view, more)

    def _schema(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        max_time_ms: Callable[[], int],
        collection: str,
    ) -> TransportResponse:
        db = self._config.database
        sample = conn.command(
            db,
            {
                "find": collection,
                "filter": {},
                "limit": SCHEMA_SAMPLE,
                "batchSize": SCHEMA_SAMPLE,
                "singleBatch": True,
                "maxTimeMS": max_time_ms(),
                "comment": self._comment(),
            },
        )
        docs = (sample.get("cursor") or {}).get("firstBatch") or []
        indexes_reply = conn.command(
            db, {"listIndexes": collection, "comment": self._comment()}
        )
        index_docs = (indexes_reply.get("cursor") or {}).get("firstBatch") or []
        count_reply = conn.command(
            db,
            {
                "count": collection,
                "maxTimeMS": max_time_ms(),
                "comment": self._comment(),
            },
        )
        types: dict[str, set[str]] = {}

        def visit(value: Any, prefix: str) -> None:
            if not isinstance(value, Mapping):
                return
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                types.setdefault(path, set()).add(_type_name(item))
                if isinstance(item, Mapping):
                    visit(item, path)

        for doc in docs:
            visit(doc, "")
        rows = [
            {"field": name, "types": sorted(kinds)}
            for name, kinds in sorted(types.items())
        ]
        indexes = [
            {
                "name": _safe_text(str(d.get("name"))),
                "keys": _json_value(d.get("key")),
                "unique": bool(d.get("unique", False)),
            }
            for d in index_docs
            if isinstance(d, Mapping)
        ]
        estimated = int(count_reply.get("n") or 0)
        extra = {
            "collection": collection,
            "indexes": indexes,
            "estimated_document_count": estimated,
            "sample_size": len(docs),
        }
        view = self._view(request, **extra)
        return self._respond(rows, "fields", view, False, extra)

    def _status(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        max_time_ms: Callable[[], int],
        sections: list[str],
    ) -> TransportResponse:
        status = conn.command(
            "admin",
            {"serverStatus": 1, "maxTimeMS": max_time_ms(), "comment": self._comment()},
        )
        row: dict[str, Any] = {
            "host": status.get("host"),
            "version": status.get("version"),
            "uptime_seconds": status.get("uptimeEstimate", status.get("uptime")),
        }
        for section in sections:
            if section in status:
                row[section] = status[section]
        rows, cut = _project_documents([row])
        view = self._view(request, sections=sections, long_values_cut=cut)
        return self._respond(rows, "sections", view, False)

    def _current_op(
        self,
        request: TransportRequest,
        conn: MongoConnection,
        max_time_ms: Callable[[], int],
        minimum: int | None,
        active_only: bool,
    ) -> TransportResponse:
        match: dict[str, Any] = {}
        if active_only:
            match["active"] = True
        if minimum is not None:
            match["microsecs_running"] = {"$gte": minimum * 1000}
        pipeline: list[dict[str, Any]] = [
            {"$currentOp": {"allUsers": True, "idleConnections": not active_only}}
        ]
        if match:
            pipeline.append({"$match": match})
        pipeline.append({"$limit": MAX_OPS + 1})
        reply = conn.command(
            "admin",
            {
                "aggregate": 1,
                "pipeline": pipeline,
                "cursor": {"batchSize": MAX_OPS + 1},
                "maxTimeMS": max_time_ms(),
                "comment": self._comment(),
            },
        )
        ops = self._batch(conn, "admin", "$cmd.aggregate", reply)
        more = len(ops) > MAX_OPS
        keep = (
            "opid",
            "op",
            "ns",
            "active",
            "secs_running",
            "microsecs_running",
            "desc",
            "appName",
            "client",
            "waitingForLock",
            "planSummary",
            "command",
        )
        projected = [
            {key: op.get(key) for key in keep if key in op}
            for op in ops[:MAX_OPS]
            if isinstance(op, Mapping)
        ]
        rows, cut = _project_documents(projected)
        view = self._view(
            request,
            long_values_cut=cut,
            incomplete_reason=f"only the first {MAX_OPS} operations are shown"
            if more
            else None,
        )
        return self._respond(rows, "operations", view, more)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, _dt.datetime):
        return "date"
    return type(value).__name__


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    units = {
        FIND_TOOL: "documents",
        AGGREGATE_TOOL: "documents",
        LIST_TOOL: "collections",
        SCHEMA_TOOL: "fields",
        STATUS_TOOL: "sections",
        CURRENT_OP_TOOL: "operations",
    }
    summary: dict[str, object] = {"row_unit": units.get(tool, "documents")}
    if tool in (FIND_TOOL, AGGREGATE_TOOL):
        summary["suggestion"] = (
            "Project fewer fields, lower limit, add a filter, or summarise with "
            "$group/$count."
        )
    if tool == STATUS_TOOL:
        summary["suggestion"] = "Ask for fewer sections (wiredTiger is large)."
    return summary


# -- model-visible face and registrations ---------------------------------------------

_CANNOT_PROVE = (
    "This is the database as it is at query time, not its state during the "
    "incident window. A document that is not returned may lie past limit, or "
    "be invisible to the read-only account. Values are redacted by field name "
    "only; a secret under another name or a bare secret value is not "
    "recognised. Current operations may show other clients' query text."
)
_READ_ONLY_TEXT = (
    "Only reads are run on the configured database. Refused before sending "
    "(QUERY_CONSTRUCT_NOT_ALLOWED): $where, $function, $accumulator and $code "
    "anywhere, the write stages $out and $merge anywhere, pipeline stages "
    "outside $match, $project, $addFields, $set, $unset, $group, $sort, $limit, "
    "$skip, $count, $unwind, $lookup, $graphLookup, $unionWith, $facet, "
    "$bucket, $bucketAuto, $sortByCount, $replaceRoot, $replaceWith, $sample, "
    "$redact, $densify, $fill, $setWindowFields and $geoNear, and lookups or "
    "unions into another database. The tool first checks that the account "
    "only has read roles (MONGODB_ACCOUNT_NOT_READ_ONLY otherwise, nothing "
    "run). Write operations cannot be enabled."
)
_ERRORS_TEXT = (
    "Invalid JSON is INVALID_PARAMS before sending; an unknown operator or a "
    "bad value is INVALID_PARAMS with the server's text; a failed login or a "
    "missing privilege is SOURCE_FORBIDDEN; a server that cannot be reached is "
    "SOURCE_UNAVAILABLE; a call past its 30 s deadline is a timeout (the "
    "server ends it with maxTimeMS); no documents is no data."
)
_LIMITS = (
    f"At most {MAX_LIMIT} documents (default {DEFAULT_LIMIT}); string values "
    f"cut to {MAX_VALUE_CHARS} characters at any depth and marked. One call "
    "reads at most 2 MiB and the view is limited to 25,000 tokens (refused "
    "whole above it). 30 s per call on a new connection, no retry."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. The database is "
    "read at query time whatever the window says; no time filter is added."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}. The database "
    "and account come from configuration; the model cannot choose them. "
    "collection is 1 to 120 characters of letters, digits, _ . -."
)

_JSON_HINT = (
    'Relaxed extended JSON is accepted, e.g. {"_id": {"$oid": "..."}} or '
    '{"ts": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}.'
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


_LIMIT_PROPERTY = {
    "type": "integer",
    "description": f"Most documents to return, 1 to {MAX_LIMIT} (default {DEFAULT_LIMIT}).",
}
_COLLECTION_PROPERTY = {"type": "string", "description": "Name of the collection."}

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    _schema_entry(
        FIND_TOOL,
        "Find documents in a collection of the configured MongoDB database, with "
        "an optional projection and sort. " + _READ_ONLY_TEXT + " " + _ERRORS_TEXT,
        {
            "collection": _COLLECTION_PROPERTY,
            "filter": {
                "type": "string",
                "description": (
                    'JSON filter document, e.g. {"status": "error", "level": '
                    '{"$gte": 3}}; {} matches all. ' + _JSON_HINT
                ),
            },
            "projection": {
                "type": "string",
                "description": 'JSON projection, e.g. {"_id": 0, "name": 1}.',
            },
            "sort": {
                "type": "string",
                "description": 'JSON sort, e.g. {"timestamp": -1}.',
            },
            "limit": _LIMIT_PROPERTY,
        },
        ["collection", "filter"],
    ),
    _schema_entry(
        AGGREGATE_TOOL,
        "Run an aggregation pipeline on a collection of the configured MongoDB "
        "database; the tool appends a $limit. Prefer $match early and $group/"
        "$count to summarise. " + _READ_ONLY_TEXT + " " + _ERRORS_TEXT,
        {
            "collection": _COLLECTION_PROPERTY,
            "pipeline": {
                "type": "string",
                "description": (
                    'JSON array of stages, e.g. [{"$match": {"status": "error"}}, '
                    '{"$group": {"_id": "$service", "n": {"$sum": 1}}}]. ' + _JSON_HINT
                ),
            },
            "limit": _LIMIT_PROPERTY,
        },
        ["collection", "pipeline"],
    ),
    _schema_entry(
        LIST_TOOL,
        "List the collections and views of the configured MongoDB database.",
        {},
        [],
    ),
    _schema_entry(
        SCHEMA_TOOL,
        "Infer a collection's fields and their types from a sample of "
        f"{SCHEMA_SAMPLE} documents, with its indexes and estimated document "
        "count.",
        {"collection": _COLLECTION_PROPERTY},
        ["collection"],
    ),
    _schema_entry(
        STATUS_TOOL,
        "Server diagnostics from serverStatus: host, version, uptime and the "
        "chosen sections.",
        {
            "sections": {
                "type": "string",
                "description": (
                    "Comma-separated sections: " + ", ".join(_SECTIONS) + ". "
                    "Default: "
                    + ", ".join(_DEFAULT_SECTIONS)
                    + " (wiredTiger is large)."
                ),
            }
        },
        [],
    ),
    _schema_entry(
        CURRENT_OP_TOOL,
        "Operations running on the server now ($currentOp), to find slow or "
        "blocked queries. Shows other clients' operations and query text.",
        {
            "min_duration_ms": {
                "type": "integer",
                "description": "Only operations running at least this long.",
            },
            "active_only": {
                "type": "boolean",
                "description": "Only active operations (default true).",
            },
        },
        [],
    ),
)

_ERROR_CLASSES: Mapping[str, str] = {
    "INVALID_JSON": "INVALID_PARAMS",
    "INVALID_QUERY": "INVALID_PARAMS",
    "INVALID_LIMIT": "INVALID_PARAMS",
    "INVALID_IDENTIFIER": "INVALID_PARAMS",
    "INVALID_PARAMS": "INVALID_PARAMS",
    "QUERY_CONSTRUCT_NOT_ALLOWED": "INVALID_PARAMS",
    "MONGODB_INVALID": "INVALID_PARAMS",
    "MONGODB_FORBIDDEN": "SOURCE_FORBIDDEN",
    "MONGODB_ACCOUNT_NOT_READ_ONLY": "SOURCE_FORBIDDEN",
    "MONGODB_TARGETS_OPSPILOT_DB": "SOURCE_FORBIDDEN",
    "MONGODB_SOURCE_ERROR": "SOURCE_ERROR",
}

_RETURNS: Mapping[str, str] = {
    FIND_TOOL: "Matching documents under results (JSON-safe values). The view adds database, collection, limit, long_values_cut, notes, incomplete_reason and cannot_prove.",
    AGGREGATE_TOOL: "Output documents of the pipeline under results. The view adds database, collection, limit, long_values_cut, notes, incomplete_reason and cannot_prove.",
    LIST_TOOL: "Collections under results, each {name, type}, sorted by name.",
    SCHEMA_TOOL: "Fields under results, each {field, types}; the view adds collection, indexes {name, keys, unique}, estimated_document_count and sample_size.",
    STATUS_TOOL: "One row under results: host, version, uptime_seconds and each chosen section.",
    CURRENT_OP_TOOL: "Operations under results, each {opid, op, ns, active, secs_running, microsecs_running, desc, appName, client, waitingForLock, planSummary, command}.",
}
_PARAMETERS: Mapping[str, Mapping[str, ParameterSpec]] = {
    FIND_TOOL: {
        "collection": ParameterSpec("string", required=True, description="Collection."),
        "filter": ParameterSpec("string", required=True, description="JSON filter."),
        "projection": ParameterSpec("string", description="JSON projection."),
        "sort": ParameterSpec("string", description="JSON sort."),
        "limit": ParameterSpec("integer", description="Most documents."),
    },
    AGGREGATE_TOOL: {
        "collection": ParameterSpec("string", required=True, description="Collection."),
        "pipeline": ParameterSpec(
            "string", required=True, description="JSON pipeline."
        ),
        "limit": ParameterSpec("integer", description="Most documents."),
    },
    LIST_TOOL: {},
    SCHEMA_TOOL: {
        "collection": ParameterSpec("string", required=True, description="Collection."),
    },
    STATUS_TOOL: {"sections": ParameterSpec("string", description="Sections.")},
    CURRENT_OP_TOOL: {
        "min_duration_ms": ParameterSpec("integer", description="Minimum duration."),
        "active_only": ParameterSpec("boolean", description="Active only."),
    },
}


def mongodb_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The six registrations (``source="otel-demo"``, the lab target's;
    ``may_contain_secrets=True`` -- current operations show other clients'
    query text)."""
    return tuple(
        ToolRegistration(
            name=name,
            version="v1",
            source=otel_demo.SOURCE,
            verb="query" if name in (FIND_TOOL, AGGREGATE_TOOL) else "get",
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
                limits=_LIMITS
                + (
                    " " + _READ_ONLY_TEXT if name in (FIND_TOOL, AGGREGATE_TOOL) else ""
                ),
                cannot_prove=_CANNOT_PROVE,
            ),
            may_contain_secrets=True,
            error_classes=dict(_ERROR_CLASSES),
        )
        for name in TOOL_NAMES
    )
