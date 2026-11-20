"""Shared fakes for the C6c contract tests (#570): a recording stand-in for
the MongoDB driver seam and the worker built around it.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「C6c 公开接口（MongoDB，2026-11-20）」), without reading the implementation.
The seam is the one the record names: ``MongoTransport(config, *,
connector=None, request_timeout_seconds=30.0)`` with ``connector(config,
timeout_seconds) -> MongoConnection`` and ``MongoConnection.command(database,
spec) -> dict``, ``.cancel()``, ``.close()``. Every command the tool sends
goes through ``command``; this fake keeps each one, in order, with the
database it was sent to and a deep copy of the ordered command document.

Seams assumed beyond the record (each also marked ``# ASSUMPTION:``):

* The module is ``opspilot.tools.mongodb_query`` and the worker constructs
  its transport by calling ``MongoTransport(...)`` (looked up when the
  executor factory is built), so a subclass patched over every module
  attribute that names the original class receives the fake connector.
* The command name is the first key of ``spec`` (MongoDB's own rule), and
  ``spec`` iterates in its key order (``dict`` or ``bson.SON``).
* A reply is the plain server answer: cursors as ``{"cursor": {"firstBatch":
  [...], "id": <int>, "ns": ...}, "ok": 1.0}``.

This module imports neither pymongo nor bson at import time (the dependency
lands with the implementation); tests that need driver types import them
inside the test.

All synthetic credentials are low-entropy placeholders.
"""

from __future__ import annotations

import copy
import itertools
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

EXT_PROFILE = "otel-demo-ext"
FIND = "mongodb_find"
AGGREGATE = "mongodb_aggregate"
LIST_COLLECTIONS = "mongodb_list_collections"
SCHEMA = "mongodb_collection_schema"
SERVER_STATUS = "mongodb_server_status"
CURRENT_OP = "mongodb_current_op"
MONGO_TOOLS = (FIND, AGGREGATE, LIST_COLLECTIONS, SCHEMA, SERVER_STATUS, CURRENT_OP)

#: The documented minimal call of each tool.
MINIMAL: dict[str, dict[str, object]] = {
    FIND: {"collection": "orders", "filter": "{}"},
    AGGREGATE: {"collection": "orders", "pipeline": '[{"$match": {}}]'},
    LIST_COLLECTIONS: {},
    SCHEMA: {"collection": "orders"},
    SERVER_STATUS: {},
    CURRENT_OP: {},
}

#: The only commands the tool may ever send (C6c 「命令面写死」).
ALLOWED_COMMANDS = frozenset(
    {
        "find",
        "aggregate",
        "listCollections",
        "listIndexes",
        "count",
        "serverStatus",
        "connectionStatus",
        "killCursors",
    }
)

# Nothing listens here: a transport that ignored the fake connector would
# fail as a source outage, never pass.
MONGO_URL = "mongodb://127.0.0.1:17017"
MONGO_DB = "c6tdb"
MONGO_USERNAME = "c6t-mreader"
MONGO_PASSWORD = "c6t-mpw-" + "aaaa570"
K8S_TOKEN = "eeeeeeee570"
K8S_API = "https://127.0.0.1:16443"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "dddddddd570"
CANARY = "ffffffff570"

MONGO_ENV_NAMES = (
    "OPSPILOT_MONGODB_URL",
    "OPSPILOT_MONGODB_DATABASE",
    "OPSPILOT_MONGODB_USERNAME",
    "OPSPILOT_MONGODB_PASSWORD",
    "OPSPILOT_MONGODB_AUTH_SOURCE",
    "OPSPILOT_MONGODB_TLS",
    "OPSPILOT_MONGODB_CA_FILE",
)

COMMENT = re.compile(
    r"^opspilot-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

FRAME = Window(NOW - timedelta(hours=6), NOW)
_INDEX = itertools.count()

# -- connectionStatus answers ---------------------------------------------------

READ_ACTIONS = [
    "changeStream",
    "collStats",
    "dbHash",
    "dbStats",
    "find",
    "killCursors",
    "listCollections",
    "listIndexes",
    "listSearchIndexes",
]
MONITOR_ACTIONS = [
    "checkFreeMonitoringStatus",
    "connPoolStats",
    "getCmdLineOpts",
    "getDefaultRWConcern",
    "getLog",
    "getParameter",
    "getShardMap",
    "hostInfo",
    "inprog",
    "listDatabases",
    "listSessions",
    "listShards",
    "netstat",
    "replSetGetConfig",
    "replSetGetStatus",
    "serverStatus",
    "shardingState",
    "top",
]


def status(
    roles: list[tuple[str, str]] | None = None,
    actions: list[str] | None = None,
    *,
    users: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A ``connectionStatus`` reply with ``showPrivileges: true``.

    Defaults to the C6-21 account: ``read`` on the configured database and
    ``clusterMonitor`` on admin, with the privileges those roles carry."""
    if roles is None:
        roles = [("read", MONGO_DB), ("clusterMonitor", "admin")]
    if users is None:
        users = [{"user": MONGO_USERNAME, "db": "admin"}]
    privileges: list[dict[str, Any]] = [
        {"resource": {"db": MONGO_DB, "collection": ""}, "actions": list(READ_ACTIONS)},
        {"resource": {"cluster": True}, "actions": list(MONITOR_ACTIONS)},
    ]
    if actions:
        privileges.append(
            {"resource": {"db": MONGO_DB, "collection": ""}, "actions": list(actions)}
        )
    return {
        "authInfo": {
            "authenticatedUsers": users,
            "authenticatedUserRoles": [{"role": r, "db": d} for r, d in roles],
            "authenticatedUserPrivileges": privileges,
        },
        "ok": 1.0,
    }


# -- the recording driver -------------------------------------------------------


@dataclass
class Sent:
    database: str
    spec: list[tuple[str, Any]]  # the ordered command document, deep-copied
    at: float

    @property
    def name(self) -> str:
        return self.spec[0][0] if self.spec else ""

    @property
    def doc(self) -> dict[str, Any]:
        return dict(self.spec)

    @property
    def keys(self) -> list[str]:
        return [k for k, _ in self.spec]


def _cursor(database: str, collection: object, batch: list[Any], cursor_id: int):
    return {
        "cursor": {
            "firstBatch": batch,
            "id": cursor_id,
            "ns": f"{database}.{collection}",
        },
        "ok": 1.0,
    }


class FakeMongoConnection:
    def __init__(self, driver: FakeMongo, config: Any, timeout_seconds: float):
        self.driver = driver
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.sent: list[Sent] = []
        self.cancelled = threading.Event()
        self.cancel_calls = 0
        self.closed = 0

    def command(self, database: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        items = [(k, copy.deepcopy(v)) for k, v in spec.items()]
        item = Sent(database, items, time.monotonic())
        with self.driver.lock:
            self.sent.append(item)
        name = item.name
        error = self.driver.errors.get(name)
        if error is not None:
            raise error
        if name in self.driver.block_on:
            self._block()
        doc = item.doc
        if name == "connectionStatus":
            return copy.deepcopy(self.driver.status)
        if name == "find":
            docs = list(self.driver.docs.get(str(doc.get("find")), []))
            limit = doc.get("limit")
            if isinstance(limit, int) and limit > 0:
                docs = docs[:limit]
            return _cursor(database, doc.get("find"), docs, 0)
        if name == "aggregate":
            if doc.get("aggregate") == 1:
                docs = list(self.driver.ops)
            else:
                docs = list(self.driver.aggregate_docs)
            pipeline = doc.get("pipeline") or []
            last = pipeline[-1] if pipeline else {}
            if isinstance(last, Mapping) and isinstance(last.get("$limit"), int):
                docs = docs[: last["$limit"]]
            return _cursor(database, doc.get("aggregate"), docs, self.driver.cursor_id)
        if name == "listCollections":
            return _cursor(
                database, "$cmd.listCollections", list(self.driver.collections), 0
            )
        if name == "listIndexes":
            return _cursor(
                database, doc.get("listIndexes"), list(self.driver.indexes), 0
            )
        if name == "count":
            return {"n": self.driver.count, "ok": 1.0}
        if name == "serverStatus":
            return copy.deepcopy(self.driver.server_status)
        if name == "killCursors":
            return {"cursorsKilled": list(doc.get("cursors") or []), "ok": 1.0}
        return {"ok": 0.0, "errmsg": f"no such command: '{name}'", "code": 59}

    def _block(self) -> None:
        """A long command: ends when the client is cancelled or closed, or
        when the server's limit (``block_seconds``) passes."""
        end = time.monotonic() + self.driver.block_seconds
        while time.monotonic() < end:
            if self.cancelled.wait(0.02) or self.closed:
                break
        try:
            from pymongo.errors import ExecutionTimeout, NetworkTimeout
        except ImportError:  # pragma: no cover - pymongo lands with C6c
            from opspilot.tools.sql_query import SqlDriverError

            raise SqlDriverError("timeout", "50", "operation exceeded time limit")
        if self.cancelled.is_set() or self.closed:
            raise NetworkTimeout("timed out")
        raise ExecutionTimeout(
            "operation exceeded time limit",
            code=50,
            details={"code": 50, "codeName": "MaxTimeMSExpired", "ok": 0.0},
        )

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()

    def close(self) -> None:
        self.closed += 1
        self.cancelled.set()


class FakeMongo:
    """The connector: one ``FakeMongoConnection`` per call, every command kept."""

    def __init__(self) -> None:
        self.connections: list[FakeMongoConnection] = []
        self.lock = threading.Lock()
        self.status: dict[str, Any] = status()
        self.docs: dict[str, list[Any]] = {"orders": [{"_id": 1, "n": 1}]}
        self.aggregate_docs: list[Any] = [{"_id": "a", "total": 3}]
        self.collections: list[dict[str, Any]] = [
            {"name": "orders", "type": "collection"},
            {"name": "customers", "type": "collection"},
            {"name": "orders_view", "type": "view"},
        ]
        self.indexes: list[dict[str, Any]] = [
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
            {
                "v": 2,
                "key": {"customer_id": 1, "at": -1},
                "name": "cust_at",
                "unique": True,
            },
        ]
        self.count = 1234
        self.server_status: dict[str, Any] = server_status_reply()
        self.ops: list[dict[str, Any]] = [
            {"opid": 7, "op": "query", "ns": f"{MONGO_DB}.orders", "active": True,
             "secs_running": 0, "microsecs_running": 5}
        ]  # fmt: skip
        self.cursor_id = 0
        self.errors: dict[str, BaseException] = {}  # command name -> raised on it
        self.connect_error: BaseException | None = None
        self.block_on: set[str] = set()
        self.block_seconds = 0.0
        self.configs: list[Any] = []
        self.timeouts: list[float] = []
        self.requests: list[Any] = []  # TransportRequest objects, in order

    def connect(self, config: Any, timeout_seconds: float) -> FakeMongoConnection:
        with self.lock:
            self.configs.append(config)
            self.timeouts.append(timeout_seconds)
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeMongoConnection(self, config, timeout_seconds)
        with self.lock:
            self.connections.append(connection)
        return connection

    @property
    def sent(self) -> list[Sent]:
        return [item for c in self.connections for item in c.sent]

    def names(self) -> list[str]:
        return [item.name for item in self.sent]

    def tool_commands(self) -> list[Sent]:
        return [item for item in self.sent if item.name != "connectionStatus"]

    def reset(self) -> None:
        self.connections.clear()
        self.configs.clear()
        self.timeouts.clear()


def server_status_reply() -> dict[str, Any]:
    return {
        "host": "c6t-mongo:27017",
        "version": "7.0.17",
        "process": "mongod",
        "pid": 1,
        "uptime": 1234.0,
        "uptimeMillis": 1234000,
        "localTime": "2026-11-20T12:00:00Z",
        "connections": {"current": 7, "available": 800, "totalCreated": 40},
        "opcounters": {
            "insert": 1,
            "query": 2,
            "update": 3,
            "delete": 4,
            "getmore": 5,
            "command": 6,
        },
        "mem": {"bits": 64, "resident": 120, "virtual": 1500},
        "locks": {"Global": {"acquireCount": {"r": 10, "w": 2}}},
        "globalLock": {"totalTime": 1000, "currentQueue": {"total": 0}},
        "network": {"bytesIn": 100, "bytesOut": 200, "numRequests": 30},
        "wiredTiger": {"cache": {"bytes currently in the cache": 1}},
        "metrics": {"document": {"returned": 9}},
        "opLatencies": {"reads": {"latency": 10, "ops": 2}},
        "tcmalloc": {"generic": {"heap_size": 1}},
        "flowControl": {"enabled": True},
        "asserts": {"regular": 0, "warning": 0},
        "transactions": {"currentActive": 0},
        "ok": 1.0,
    }


def install(
    monkeypatch, driver: FakeMongo, *, request_timeout_seconds: float | None = None
):
    """Route every ``MongoTransport`` the worker builds to ``driver``.

    # ASSUMPTION: ``opspilot.tools.mongodb_query.MongoTransport`` is the class
    # the worker instantiates (directly, or through a module that imported
    # the name); patching each module attribute bound to it reaches both.
    """
    from opspilot.tools import mongodb_query

    original = mongodb_query.MongoTransport

    class Routed(original):  # type: ignore[misc, valid-type]
        def __init__(self, config, *args, **kwargs):
            kwargs["connector"] = driver.connect
            if request_timeout_seconds is not None:
                kwargs["request_timeout_seconds"] = request_timeout_seconds
            super().__init__(config, *args, **kwargs)

        def fetch(self, request, *args, **kwargs):
            driver.requests.append(request)
            return super().fetch(request, *args, **kwargs)

    for name, module in list(sys.modules.items()):
        if not name.startswith("opspilot") or module is None:
            continue
        if getattr(module, "MongoTransport", None) is original:
            monkeypatch.setattr(module, "MongoTransport", Routed)
    return Routed


def set_request_timeout(monkeypatch, seconds: float) -> None:
    """Shorten the per-call budget without replacing the real connector."""
    from opspilot.tools import mongodb_query

    original = mongodb_query.MongoTransport

    class Short(original):  # type: ignore[misc, valid-type]
        def __init__(self, config, *args, **kwargs):
            kwargs["request_timeout_seconds"] = seconds
            super().__init__(config, *args, **kwargs)

    for name, module in list(sys.modules.items()):
        if not name.startswith("opspilot") or module is None:
            continue
        if getattr(module, "MongoTransport", None) is original:
            monkeypatch.setattr(module, "MongoTransport", Short)


# -- the worker ------------------------------------------------------------------


def base_env() -> dict[str, str]:
    return {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["C6C_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }


def mongo_env(url: str | None = MONGO_URL, **overrides: str) -> dict[str, str]:
    """A worker environment; with a URL, the database and the mongodb-ro
    credential too (TLS ``disable`` as in the lab). An override of ``""``
    removes that key."""
    values = base_env()
    if url is not None:
        values["OPSPILOT_MONGODB_URL"] = url
        values["OPSPILOT_MONGODB_DATABASE"] = MONGO_DB
        values["OPSPILOT_MONGODB_USERNAME"] = MONGO_USERNAME
        values["OPSPILOT_MONGODB_PASSWORD"] = MONGO_PASSWORD
        values["OPSPILOT_MONGODB_TLS"] = "disable"
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def require_jq() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def worker(env: Mapping[str, str], *, deadline_seconds: float = 600):
    """The executor a worker of ``env`` builds, for one fresh Run."""
    require_jq()
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    if env.get("OPSPILOT_MONGODB_URL"):
        # Positive control: refusal tests are vacuous without the tools.
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        assert set(MONGO_TOOLS) <= names, names
    lease = _lease()
    deadline = NOW + timedelta(seconds=deadline_seconds)
    store = FakeStore(lease, deadline=deadline)
    sink = RecordingSink()
    factory = profile.executor_factory(store, sink, clock)
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why are checkout orders slow?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input), sink


def call(tool: str, params: Mapping[str, object]) -> ToolRequest:
    return ToolRequest(
        step_id="c6c-step",
        tool_index=next(_INDEX),
        tool_name=tool,
        target_ref=TARGET_ID,
        params=dict(params),
        window=FRAME.as_json(),
    )


@dataclass
class Tools:
    """One worker wired to one fake driver."""

    driver: FakeMongo
    env: dict[str, str]
    executors: list[Any] = field(default_factory=list)

    def run(self, tool: str, params: Mapping[str, object] | None = None):
        executor, _ = worker(self.env)
        self.executors.append(executor)
        return executor.execute(call(tool, MINIMAL[tool] if params is None else params))

    def find(self, filter: str = "{}", collection: str = "orders", **params: object):
        return self.run(FIND, {"collection": collection, "filter": filter, **params})

    def aggregate(self, pipeline: str, collection: str = "orders", **params: object):
        return self.run(
            AGGREGATE, {"collection": collection, "pipeline": pipeline, **params}
        )


def make_tools(
    monkeypatch, *, request_timeout_seconds: float | None = None, **env: str
) -> Tools:
    driver = FakeMongo()
    install(monkeypatch, driver, request_timeout_seconds=request_timeout_seconds)
    return Tools(driver, mongo_env(**env))


# -- outcome helpers -------------------------------------------------------------


def view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def notes(outcome) -> list[str]:
    return [str(n) for n in (view(outcome).get("notes") or [])]


def rows(outcome) -> list[Mapping[str, Any]]:
    content = view(outcome)["content"]
    assert isinstance(content, list), view(outcome)
    return content


def ok(outcome) -> None:
    assert outcome.status == "ok", (outcome.status, outcome.reason, view(outcome))


def refused_before_request(outcome, driver: FakeMongo, *codes: str) -> None:
    """INVALID_PARAMS with one of ``codes`` first in notes; no command sent."""
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), view(
        outcome
    )
    first = (notes(outcome) or [""])[0]
    assert any(first.startswith(code) for code in codes), (codes, view(outcome))
    assert driver.sent == [], [(s.database, s.spec) for s in driver.sent]


def forbidden(outcome, code: str) -> None:
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    first = (notes(outcome) or [""])[0]
    assert first.startswith(code), view(outcome)


def wait_until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
