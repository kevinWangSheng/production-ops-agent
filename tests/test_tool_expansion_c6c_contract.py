"""Independent contract tests for tool expansion C6c (#570): the MongoDB
read-only tools ``mongodb_find``, ``mongodb_aggregate``,
``mongodb_list_collections``, ``mongodb_collection_schema``,
``mongodb_server_status`` and ``mongodb_current_op``.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「C6c 公开接口（MongoDB，2026-11-20）」), r11 (C6-U1=a), PRODUCT-CONSTRAINTS
line 18 and the C6 pre-review, without reading the implementation. Red
until C6c lands; collection stays green.

MongoDB has no read-only session, so the tool's own command face is part of
the read-only guarantee: these tests pin every command document the tool
sends through the documented connector seam (``tests/c6c_mongo_support.py``)
-- name, database, key set, ``maxTimeMS``, ``comment`` -- and that nothing
the model writes reaches a command except ``filter``/``projection``/
``sort``/``pipeline``. The content check is in
``tests/test_tool_expansion_c6c_content_contract.py``; real servers in
``tests/integration/test_tool_expansion_c6c_mongo.py``.

Pinned surfaces (``tests/fixtures/c6c_mongo/pins.json``) were computed on
origin/main ``f08a074e`` + interface commits ``39069f8d``/``78d094e7``,
before any C6c code: every extension backend/switch set times each SQL
dialect (or none) times Kafka on/off, ``otel-demo`` and ``fixture``.

Tests that need driver types (``bson``, ``pymongo.errors``) import them in
the test and skip until the dependency lands. Assumptions beyond the record
are marked ``# ASSUMPTION:``. All synthetic credentials are low-entropy
placeholders.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import itertools
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from opspilot import tracing
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.c6c_mongo_support import (
    AGGREGATE,
    CANARY,
    COMMENT,
    CURRENT_OP,
    EXT_PROFILE,
    FIND,
    K8S_API,
    K8S_TOKEN,
    LIST_COLLECTIONS,
    MINIMAL,
    MONGO_DB,
    MONGO_PASSWORD,
    MONGO_TOOLS,
    MONGO_URL,
    MONGO_USERNAME,
    PROM_PASSWORD,
    SCHEMA,
    SERVER_STATUS,
    FakeMongo,
    call,
    forbidden,
    install,
    make_tools,
    mongo_env,
    notes,
    ok,
    refused_before_request,
    require_jq,
    rows,
    status,
    view,
    wait_until,
    worker,
)
from tests.m1_tool_support import FakeClock

ROOT = Path(__file__).resolve().parents[1]
PIN_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RUN_ID = "c6c-run"
MIB = 1024 * 1024
PINS_FILE = ROOT / "tests" / "fixtures" / "c6c_mongo" / "pins.json"
PINS: dict[str, list[str]] = json.loads(PINS_FILE.read_text())["pins"]

BACKEND_URLS = {
    "OPSPILOT_ES_URL": "http://127.0.0.1:9200",
    "OPSPILOT_LOKI_URL": "http://127.0.0.1:3100",
    "OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428",
}
SWITCHES = (
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
)
FLAG_NAMES = (
    "elasticsearch",
    "loki",
    "victorialogs",
    "live_metrics",
    "knowledge",
    "todo",
)
SQL_URLS = {
    "none": None,
    "postgresql": "postgresql://127.0.0.1:15432/c6tdb",
    "mysql": "mysql://127.0.0.1:13306/c6tdb",
    "clickhouse": "clickhouse+http://127.0.0.1:18123/c6tdb",
    "mssql": "mssql://127.0.0.1:11433/c6tdb",
}
KAFKA = "localhost:9092"
FLAG_SETS = list(itertools.product((False, True), repeat=6))
SURFACE_KEYS = sorted(k for k in PINS if not k.startswith("profile|"))

ROW_UNIT = {
    FIND: "documents",
    AGGREGATE: "documents",
    LIST_COLLECTIONS: "collections",
    SCHEMA: "fields",
    SERVER_STATUS: "sections",
    CURRENT_OP: "operations",
}
VERB = {
    FIND: "query",
    AGGREGATE: "query",
    LIST_COLLECTIONS: "get",
    SCHEMA: "get",
    SERVER_STATUS: "get",
    CURRENT_OP: "get",
}
DEFAULT_SECTIONS = (
    "connections",
    "opcounters",
    "mem",
    "locks",
    "globalLock",
    "network",
)
ALL_SECTIONS = (
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
OP_FIELDS = {
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
}

#: The record's write-and-administration action table (C6-U1 backstop).
WRITE_ACTIONS = (
    "insert", "update", "remove", "createCollection", "createIndex",
    "dropCollection", "dropDatabase", "dropIndex", "renameCollectionSameDB",
    "convertToCapped", "collMod", "compact", "enableProfiler", "createUser",
    "dropUser", "updateUser", "grantRole", "revokeRole", "createRole",
    "dropRole", "updateRole", "changePassword", "changeOwnPassword",
    "changeCustomData", "setAuthenticationRestriction", "killop",
    "killAnySession", "shutdown", "fsync", "setParameter", "replSetConfigure",
    "applicationMessage", "bypassDocumentValidation", "anyAction", "internal",
    "reIndex", "dropAllRolesFromDatabase", "dropAllUsersFromDatabase",
    "planCacheWrite", "splitVector", "cleanupOrphaned", "moveChunk",
    "splitChunk", "enableSharding", "flushRouterConfig", "logRotate", "resync",
)  # fmt: skip
#: Built-in roles outside {read, readAnyDatabase, clusterMonitor}, and
#: custom or misspelled ones.
BAD_ROLES = (
    "readWrite", "readWriteAnyDatabase", "dbAdmin", "dbAdminAnyDatabase",
    "dbOwner", "userAdmin", "userAdminAnyDatabase", "clusterAdmin",
    "clusterManager", "hostManager", "backup", "restore", "root", "__system",
    "enableSharding", "directShardOperations", "searchCoordinator",
    "c6tReader", "Read", "read ", "clustermonitor", "",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith(("OPSPILOT_MONGODB", "OPSPILOT_SQL", "OPSPILOT_KAFKA")) or (
            re.fullmatch(r"OPSPILOT_[A-Z0-9_]*DSN", name)
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("PGHOST", "PGPORT", "PGHOSTADDR", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6C_TEST_K8S_CA_FILE", str(ca_file))


def _env_for(key: str, *, mongo: bool = False) -> dict[str, str]:
    flags_text, sql, kafka = key.split("|")
    env = {PROFILE_ENV: EXT_PROFILE}
    flags = [c == "1" for c in flags_text]
    for on, name in zip(flags[:3], BACKEND_URLS):
        if on:
            env[name] = BACKEND_URLS[name]
    for on, name in zip(flags[3:], SWITCHES):
        if on:
            env[name] = "1"
    if SQL_URLS[sql]:
        env["OPSPILOT_SQL_URL"] = SQL_URLS[sql]  # type: ignore[assignment]
    if kafka == "1":
        env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] = KAFKA
    if mongo:
        env["OPSPILOT_MONGODB_URL"] = MONGO_URL
    return env


def _revision_kwargs(key: str) -> dict[str, Any]:
    flags_text, sql, kafka = key.split("|")
    kwargs: dict[str, Any] = {name: c == "1" for name, c in zip(FLAG_NAMES, flags_text)}
    kwargs["sql"] = None if sql == "none" else sql
    kwargs["kafka"] = kafka == "1"
    return kwargs


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=PIN_NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _available(env: Mapping[str, str]) -> Mapping[str, Any]:
    return _face(env).evidence_context(RUN_ID)["data_sources"]["available"]


def _surface_sha256(profile: ToolProfile) -> str:
    face = profile.face(FakeClock(start=PIN_NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context(RUN_ID)),
        "versions": profile.versions(),
    }
    return hashlib.sha256(canonical(blob).encode()).hexdigest()


def _revision(env: Mapping[str, str]) -> str:
    return select_profile(env).versions()["tool_schema_revision"]


def _schemas(env: Mapping[str, str]) -> dict[str, Mapping[str, Any]]:
    return {s["function"]["name"]: s["function"] for s in _face(env).tool_schemas}


def _web(**extra: str) -> dict[str, str]:
    return {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_MONGODB_URL": MONGO_URL, **extra}


def _text(outcome) -> str:
    return json.dumps(view(outcome), ensure_ascii=False, default=str)


# == ⑩ surfaces: existing bytes unchanged, six tools last with the address ========


@pytest.mark.parametrize("key", SURFACE_KEYS)
def test_without_a_mongodb_url_every_existing_surface_is_byte_identical(key):
    from opspilot.tools import otel_demo_ext

    revision, surface = PINS[key]
    env = _env_for(key)
    assert _revision(env) == revision
    assert _surface_sha256(select_profile(env)) == surface
    kwargs = _revision_kwargs(key)
    assert otel_demo_ext.tool_schema_revision(**kwargs) == revision
    assert otel_demo_ext.tool_schema_revision(**kwargs, mongodb=False) == revision


@pytest.mark.parametrize("profile", ["otel-demo", "fixture"])
def test_other_profiles_ignore_every_mongodb_setting(profile):
    revision, surface = PINS[f"profile|{profile}"]
    env = {
        PROFILE_ENV: profile,
        "OPSPILOT_MONGODB_URL": MONGO_URL,
        "OPSPILOT_MONGODB_DATABASE": MONGO_DB,
    }
    assert _revision(env) == revision
    assert _surface_sha256(select_profile(env)) == surface


def test_existing_revision_constants_keep_their_values():
    from opspilot.tools import otel_demo_ext as ext

    assert ext.TOOL_SCHEMA_REVISION == "otel-demo-ext-b90c54386594"
    assert ext.ES_TOOL_SCHEMA_REVISION == "otel-demo-ext-5b9fc1dc2f9d"
    assert ext.LOKI_TOOL_SCHEMA_REVISION == "otel-demo-ext-06a1afb380f4"
    assert ext.VICTORIALOGS_TOOL_SCHEMA_REVISION == "otel-demo-ext-4f224386bf21"
    assert ext.LIVE_METRICS_TOOL_SCHEMA_REVISION == "otel-demo-ext-0d20c18e8654"
    assert ext.KNOWLEDGE_TOOL_SCHEMA_REVISION == "otel-demo-ext-81a37085b36a"
    assert ext.TODO_TOOL_SCHEMA_REVISION == "otel-demo-ext-82ae2712ee17"
    assert ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION == "otel-demo-ext-cddae9d97c81"
    assert ext.SQL_MYSQL_TOOL_SCHEMA_REVISION == "otel-demo-ext-ddd14c20ac47"
    assert ext.SQL_CLICKHOUSE_TOOL_SCHEMA_REVISION == "otel-demo-ext-171220e85941"
    assert ext.SQL_MSSQL_TOOL_SCHEMA_REVISION == "otel-demo-ext-18fec849b01c"
    assert ext.KAFKA_TOOL_SCHEMA_REVISION == "otel-demo-ext-63e4bf70e153"


def test_mongodb_settings_without_the_address_change_no_web_surface():
    # The web reads only whether OPSPILOT_MONGODB_URL is set.
    for key in ("000000|none|0", "111111|mssql|1", "100100|postgresql|0"):
        plain = _surface_sha256(select_profile(_env_for(key)))
        for extra in (
            {"OPSPILOT_MONGODB_DATABASE": MONGO_DB},
            {"OPSPILOT_MONGODB_USERNAME": MONGO_USERNAME},
            {"OPSPILOT_MONGODB_PASSWORD": MONGO_PASSWORD},
            {"OPSPILOT_MONGODB_AUTH_SOURCE": "admin"},
            {"OPSPILOT_MONGODB_TLS": "require"},
            {"OPSPILOT_MONGODB_CA_FILE": "/run/secrets/mongo-ca.pem"},
        ):
            env = {**_env_for(key), **extra}
            assert _surface_sha256(select_profile(env)) == plain, (key, extra)
        assert not set(MONGO_TOOLS) & set(_tool_names(_env_for(key)))
        assert "mongodb" not in _available(_env_for(key))


@pytest.mark.parametrize("key", SURFACE_KEYS)
def test_the_address_appends_the_six_tools_last_and_one_source(key):
    from opspilot.tools import otel_demo_ext

    old = _tool_names(_env_for(key))
    new = _tool_names(_env_for(key, mongo=True))
    assert new[: len(old)] == old
    assert sorted(new[len(old) :]) == sorted(MONGO_TOOLS)
    assert len(new) == len(old) + 6
    available = _available(_env_for(key, mongo=True))
    assert available["mongodb"] == sorted(MONGO_TOOLS)
    assert {k: v for k, v in available.items() if k != "mongodb"} == dict(
        _available(_env_for(key))
    )
    revision = _revision(_env_for(key, mongo=True))
    assert revision.startswith("otel-demo-ext-")
    assert revision == otel_demo_ext.tool_schema_revision(
        **_revision_kwargs(key), mongodb=True
    )
    assert revision != PINS[key][0]


def test_every_mongodb_surface_has_its_own_revision():
    known = {pin[0] for pin in PINS.values()}
    seen = {_revision(_env_for(key, mongo=True)) for key in SURFACE_KEYS}
    assert len(seen) == len(SURFACE_KEYS)
    assert not seen & known


def test_mongodb_constants_and_names():
    from opspilot.tools import otel_demo_ext as ext

    assert ext.MONGODB_TOOL_SCHEMA_REVISION == ext.tool_schema_revision(mongodb=True)
    assert _revision(_web()) == ext.MONGODB_TOOL_SCHEMA_REVISION
    names = list(ext.MONGODB_TOOL_NAMES)
    assert len(names) == len(set(names))
    assert set(names) == set(ext.TOOL_NAMES) | set(MONGO_TOOLS)


def test_web_and_worker_agree_and_the_prompt_is_unchanged():
    web, full = _web(), mongo_env()
    assert select_profile(web).versions() == select_profile(full).versions()
    assert _surface_sha256(select_profile(web)) == _surface_sha256(select_profile(full))
    without = {PROFILE_ENV: EXT_PROFILE}
    old, new = select_profile(without).versions(), select_profile(web).versions()
    assert new["prompt_revision"] == old["prompt_revision"]
    assert new["context_policy_revision"] == old["context_policy_revision"]
    assert old != new


def test_schemas_take_exactly_the_documented_parameters():
    schemas = _schemas(_web())
    expected = {
        FIND: ({"collection", "filter", "projection", "sort", "limit"}, {"collection", "filter"}),
        AGGREGATE: ({"collection", "pipeline", "limit"}, {"collection", "pipeline"}),
        LIST_COLLECTIONS: (set(), set()),
        SCHEMA: ({"collection"}, {"collection"}),
        SERVER_STATUS: ({"sections"}, set()),
        CURRENT_OP: ({"min_duration_ms", "active_only"}, set()),
    }  # fmt: skip
    for tool, (properties, required) in expected.items():
        parameters = schemas[tool]["parameters"]
        assert set(parameters.get("properties") or {}) == properties, tool
        assert set(parameters.get("required") or []) == required, tool
    types = {
        (FIND, "collection"): "string",
        (FIND, "filter"): "string",
        (FIND, "projection"): "string",
        (FIND, "sort"): "string",
        (FIND, "limit"): "integer",
        (AGGREGATE, "pipeline"): "string",
        (AGGREGATE, "limit"): "integer",
        (SERVER_STATUS, "sections"): "string",
        (CURRENT_OP, "min_duration_ms"): "integer",
        (CURRENT_OP, "active_only"): "boolean",
    }
    for (tool, name), kind in types.items():
        assert schemas[tool]["parameters"]["properties"][name]["type"] == kind
    # C6-2 / C3 §8: the model never picks a database, a connection or a command.
    flat = json.dumps([schemas[t]["parameters"] for t in MONGO_TOOLS]).lower()
    for word in ("database", "db", "dsn", "connection", "host", "url", "command"):
        assert f'"{word}"' not in flat, word
    assert "mongodb_list_databases" not in _tool_names(_web())


def test_descriptions_promise_no_write_mode():
    schemas = _schemas(_web())
    for tool in MONGO_TOOLS:
        lower = schemas[tool]["description"].lower()
        for marker in ("allow_write", "enable write", "write mode", "read_only: false"):
            assert marker not in lower, (tool, marker)


def test_registrations_are_bounded_read_only_otel_demo_tools():
    from opspilot.tools import mongodb_query, otel_demo, otel_demo_ext

    # ASSUMPTION: the private registry seam takes ``mongodb=`` like
    # ``tool_schema_revision`` does (as ``sql=``/``kafka=`` before it).
    regs = {r.name: r for r in otel_demo_ext._registrations(mongodb=True)}
    for name in MONGO_TOOLS:
        reg = regs[name]
        assert reg.read_only is True, name
        assert reg.verb == VERB[name], name
        assert reg.source == otel_demo.SOURCE, name
        assert reg.result_path == ("results",), name
        assert reg.may_contain_secrets is True, name
        assert reg.max_result_bytes == 2 * MIB, name
        assert reg.max_view_tokens == 25_000, name
        assert 0 < reg.request_timeout_seconds <= 30, name
    assert not set(MONGO_TOOLS) & {r.name for r in otel_demo_ext._registrations()}
    assert sorted(mongodb_query.TOOL_NAMES) == sorted(MONGO_TOOLS)
    if hasattr(mongodb_query, "CREDENTIAL_REF"):
        assert mongodb_query.CREDENTIAL_REF == "mongodb-ro"


def test_unconfigured_extension_worker_has_no_mongodb_tool(monkeypatch):
    driver = FakeMongo()
    install(monkeypatch, driver)
    executor, _ = worker(mongo_env(None))
    for tool in MONGO_TOOLS:
        outcome = executor.execute(call(tool, MINIMAL[tool]))
        assert outcome.status in ("error", "denied"), (tool, view(outcome))
    assert driver.configs == [] and driver.sent == []


# == ① static guard: only the new module touches the driver =======================

MONGO_MODULE = ROOT / "opspilot" / "tools" / "mongodb_query.py"
WRITE_METHODS = {
    "insert_one", "insert_many", "update_one", "update_many", "replace_one",
    "delete_one", "delete_many", "bulk_write", "find_one_and_update",
    "find_one_and_replace", "find_one_and_delete", "drop", "drop_collection",
    "drop_database", "create_collection", "create_index", "create_indexes",
    "create_search_index", "drop_index", "drop_indexes", "rename", "watch",
    "start_session", "with_transaction", "map_reduce",
}  # fmt: skip


def test_only_the_mongodb_module_imports_the_driver_and_calls_no_write_method():
    importers = []
    for path in sorted((ROOT / "opspilot").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] in ("pymongo", "bson", "gridfs") for n in names):
                importers.append(path)
    assert set(importers) <= {MONGO_MODULE}, importers
    tree = ast.parse(MONGO_MODULE.read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & WRITE_METHODS, called & WRITE_METHODS
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "gridfs":
            pytest.fail("gridfs imported")


# == ② the fixed command face (每次调用的命令序列) ===================================

EXPECTED_COMMANDS = {
    FIND: ["find"],
    AGGREGATE: ["aggregate"],
    LIST_COLLECTIONS: ["listCollections"],
    SCHEMA: sorted(["find", "listIndexes", "count"]),
    SERVER_STATUS: ["serverStatus"],
    CURRENT_OP: ["aggregate"],
}
ADMIN_TOOLS = {SERVER_STATUS, CURRENT_OP}


@pytest.mark.parametrize("tool", MONGO_TOOLS)
def test_each_tool_sends_the_account_check_then_only_its_commands(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    ok(tools.run(tool))
    [connection] = tools.driver.connections
    sent = connection.sent
    first, rest = sent[0], sent[1:]
    assert first.name == "connectionStatus" and first.database == "admin"
    assert first.doc["connectionStatus"] == 1
    assert first.doc["showPrivileges"] is True
    assert set(first.keys) <= {
        "connectionStatus",
        "showPrivileges",
        "comment",
        "maxTimeMS",
    }
    assert sorted(s.name for s in rest) == EXPECTED_COMMANDS[tool]
    for item in rest:
        expected_db = "admin" if tool in ADMIN_TOOLS else MONGO_DB
        assert item.database == expected_db, (item.name, item.database)
    assert set(tools.driver.names()) <= {
        "connectionStatus", "find", "aggregate", "listCollections", "listIndexes",
        "count", "serverStatus", "killCursors",
    }  # fmt: skip
    assert "getMore" not in tools.driver.names()
    assert connection.closed >= 1


@pytest.mark.parametrize("tool", MONGO_TOOLS)
def test_every_tool_command_carries_max_time_and_a_fresh_comment(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    ok(tools.run(tool))
    first_comments = set()
    for item in tools.driver.tool_commands():
        doc = item.doc
        assert COMMENT.fullmatch(str(doc.get("comment"))), (item.name, doc)
        first_comments.add(doc["comment"])
        if item.name in ("listIndexes", "killCursors"):
            assert "maxTimeMS" not in doc, item.name
        else:
            assert 24_000 <= int(doc["maxTimeMS"]) <= 28_000, (item.name, doc)
    tools.driver.reset()
    ok(tools.run(tool))
    second = {s.doc["comment"] for s in tools.driver.tool_commands()}
    assert not first_comments & second  # a new uuid4 per call


def test_find_command_document(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.find('{"status": "paid"}', limit=7))
    [find] = tools.driver.tool_commands()
    assert find.keys[0] == "find"
    assert set(find.keys) == {
        "find", "filter", "limit", "batchSize", "singleBatch", "maxTimeMS", "comment"
    }  # fmt: skip
    doc = find.doc
    assert doc["find"] == "orders"
    assert doc["filter"] == {"status": "paid"}
    assert doc["limit"] == 8 and doc["batchSize"] == 8
    assert doc["singleBatch"] is True
    tools.driver.reset()
    ok(tools.find("{}", projection='{"name": 1}', sort='{"at": -1}'))
    [find] = tools.driver.tool_commands()
    assert set(find.keys) == {
        "find", "filter", "projection", "sort", "limit", "batchSize",
        "singleBatch", "maxTimeMS", "comment",
    }  # fmt: skip
    assert find.doc["projection"] == {"name": 1}
    assert find.doc["sort"] == {"at": -1}
    assert find.doc["limit"] == 101  # default limit 100, plus one


def test_aggregate_command_document_appends_the_tools_limit(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.aggregate('[{"$match": {"status": "paid"}}, {"$limit": 1000}]', limit=9))
    [aggregate] = tools.driver.tool_commands()
    assert aggregate.keys[0] == "aggregate"
    assert set(aggregate.keys) == {
        "aggregate",
        "pipeline",
        "cursor",
        "maxTimeMS",
        "comment",
    }
    doc = aggregate.doc
    assert doc["aggregate"] == "orders"
    assert [dict(s) for s in doc["pipeline"]] == [
        {"$match": {"status": "paid"}},
        {"$limit": 1000},
        {"$limit": 10},
    ]
    assert dict(doc["cursor"]) == {"batchSize": 10}


def test_list_collections_command_document(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(LIST_COLLECTIONS))
    [command] = tools.driver.tool_commands()
    assert command.keys[0] == "listCollections"
    required = {
        "listCollections",
        "nameOnly",
        "authorizedCollections",
        "maxTimeMS",
        "comment",
    }
    assert required <= set(command.keys) <= required | {"cursor"}
    assert command.doc["nameOnly"] is True
    assert command.doc["authorizedCollections"] is True


def test_collection_schema_command_documents(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(SCHEMA, {"collection": "orders"}))
    by_name = {s.name: s for s in tools.driver.tool_commands()}
    find = by_name["find"]
    assert find.doc["find"] == "orders" and find.doc["filter"] == {}
    assert find.doc["limit"] == 10
    assert {"find", "filter", "limit", "maxTimeMS", "comment"} <= set(find.keys) <= {
        "find", "filter", "limit", "batchSize", "singleBatch", "maxTimeMS", "comment"
    }  # fmt: skip
    indexes = by_name["listIndexes"]
    assert indexes.doc["listIndexes"] == "orders"
    assert {"listIndexes", "comment"} <= set(indexes.keys) <= {
        "listIndexes", "comment", "cursor"
    }  # fmt: skip
    count = by_name["count"]
    assert count.doc["count"] == "orders"
    assert set(count.keys) == {"count", "maxTimeMS", "comment"}  # estimate: no query


def test_server_status_command_document(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(SERVER_STATUS))
    [command] = tools.driver.tool_commands()
    assert command.database == "admin"
    assert command.keys[0] == "serverStatus" and command.doc["serverStatus"] == 1
    # Any other key may only toggle a section in or out.
    for key in set(command.keys) - {"serverStatus", "maxTimeMS", "comment"}:
        assert command.doc[key] in (0, 1, True, False), (key, command.doc[key])


@pytest.mark.parametrize("active_only", [True, False, None])
def test_current_op_command_document(monkeypatch, active_only):
    tools = make_tools(monkeypatch)
    params: dict[str, object] = {"min_duration_ms": 1500}
    if active_only is not None:
        params["active_only"] = active_only
    ok_or_empty = tools.run(CURRENT_OP, params)
    assert ok_or_empty.status in ("ok", "no_data"), view(ok_or_empty)
    [command] = tools.driver.tool_commands()
    assert command.database == "admin"
    assert command.keys[0] == "aggregate" and command.doc["aggregate"] == 1
    assert set(command.keys) == {
        "aggregate",
        "pipeline",
        "cursor",
        "maxTimeMS",
        "comment",
    }
    pipeline = [dict(s) for s in command.doc["pipeline"]]
    assert [next(iter(s)) for s in pipeline] == ["$currentOp", "$match", "$limit"]
    current = dict(pipeline[0]["$currentOp"])
    assert current["allUsers"] is True
    assert current["idleConnections"] is (active_only is False)
    match = pipeline[1]["$match"]
    assert dict(match["microsecs_running"]) == {"$gte": 1_500_000}
    # Lead ruling A, revised 2026-11-20 after the rental measurement: at
    # most 100 rows, read one more to detect truncation.
    assert pipeline[2] == {"$limit": 101}
    assert dict(command.doc["cursor"]) == {"batchSize": 101}


def test_a_left_over_cursor_is_killed_and_never_read_on(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.cursor_id = 4242570
    tools.driver.aggregate_docs = [{"n": n} for n in range(5)]
    ok(tools.aggregate('[{"$match": {}}]', limit=3))
    names = tools.driver.names()
    assert "getMore" not in names
    kills = [s for s in tools.driver.sent if s.name == "killCursors"]
    assert len(kills) == 1, names
    kill = kills[0]
    assert kill.database == MONGO_DB
    assert kill.doc["killCursors"] == "orders"
    assert [int(c) for c in kill.doc["cursors"]] == [4242570]
    assert set(kill.keys) == {"killCursors", "cursors", "comment"}
    assert names.index("killCursors") > names.index("aggregate")


def test_a_closed_cursor_sends_no_kill_cursors(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.aggregate('[{"$match": {}}]'))
    ok(tools.find("{}"))
    assert "killCursors" not in tools.driver.names()


@pytest.mark.parametrize("tool", [FIND, AGGREGATE, SCHEMA, LIST_COLLECTIONS])
def test_each_call_opens_its_own_connection_and_closes_it(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    ok(tools.run(tool))
    ok(tools.run(tool))
    assert len(tools.driver.connections) == 2
    assert all(c.closed >= 1 for c in tools.driver.connections)
    assert len(tools.driver.configs) == 2


INJECTED = {
    "database": "admin",
    "db": "local",
    "command": '{"dropDatabase": 1}',
    "maxTimeMS": 999_999_999,
    "comment": "not-opspilot",
    "hint": "_id_",
    "allowDiskUse": True,
    "let": '{"x": 1}',
    "collation": '{"locale": "en"}',
    "readConcern": '{"level": "majority"}',
    "writeConcern": '{"w": 1}',
    "bypassDocumentValidation": True,
    "batchSize": 100_000,
    "out": "stolen",
}


@pytest.mark.parametrize("tool", MONGO_TOOLS)
def test_parameters_outside_the_schema_never_reach_a_command(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {**MINIMAL[tool], **INJECTED})
    if outcome.status in ("error", "denied"):
        assert tools.driver.sent == [], view(outcome)
        return
    for item in tools.driver.sent:
        assert item.database in ("admin", MONGO_DB), item.database
        flat = json.dumps(item.spec, default=str)
        for value in (
            "dropDatabase",
            "999999999",
            "not-opspilot",
            "stolen",
            "majority",
        ):
            assert value not in flat, (item.name, value)
        assert item.doc.get("batchSize") != 100_000
        assert dict(item.doc.get("cursor") or {}).get("batchSize") != 100_000
        for key in ("hint", "allowDiskUse", "let", "collation", "readConcern",
                    "writeConcern", "bypassDocumentValidation"):  # fmt: skip
            assert key not in item.keys, (item.name, key)


@pytest.mark.parametrize(
    "collection",
    ["", ".orders", "a" * 121, "or$ders", "orders;", "a b", "a/b", "über", "orders\n",
     "$cmd", "a*", "a\\b"],
)  # fmt: skip
@pytest.mark.parametrize("tool", [FIND, AGGREGATE, SCHEMA])
def test_bad_collection_names_are_refused_before_any_command(
    monkeypatch, tool, collection
):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {**MINIMAL[tool], "collection": collection})
    refused_before_request(outcome, tools.driver, "INVALID_IDENTIFIER")


@pytest.mark.parametrize(
    "collection", ["a" * 120, "orders.archive", "c6t-orders_2", "A9"]
)
def test_collection_names_inside_the_pattern_are_accepted(monkeypatch, collection):
    tools = make_tools(monkeypatch)
    tools.driver.docs[collection] = [{"_id": 1}]
    ok(tools.find("{}", collection=collection))
    [find] = tools.driver.tool_commands()
    assert find.doc["find"] == collection


@pytest.mark.parametrize("tool", [FIND, AGGREGATE])
@pytest.mark.parametrize("limit", [0, 501, -1, 10_000])
def test_limit_outside_1_to_500_is_refused(monkeypatch, tool, limit):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {**MINIMAL[tool], "limit": limit})
    refused_before_request(outcome, tools.driver, "INVALID_LIMIT")


@pytest.mark.parametrize("limit", ["10", 1.5, True, None, [5]])
def test_limit_of_the_wrong_type_is_refused(monkeypatch, limit):
    tools = make_tools(monkeypatch)
    outcome = tools.run(FIND, {**MINIMAL[FIND], "limit": limit})
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []


@pytest.mark.parametrize(
    "sections",
    ["foo", "connections,foo", "wiredtiger", "connections;mem", "*", "serverStatus"],
)
def test_unknown_sections_are_refused_before_any_command(monkeypatch, sections):
    tools = make_tools(monkeypatch)
    outcome = tools.run(SERVER_STATUS, {"sections": sections})
    refused_before_request(outcome, tools.driver, "INVALID_PARAMS")


@pytest.mark.parametrize(
    "params",
    [
        {"min_duration_ms": -1},
        {"min_duration_ms": "5"},
        {"min_duration_ms": 1.5},
        {"min_duration_ms": True},
        {"active_only": "yes"},
        {"active_only": 1},
    ],
)
def test_bad_current_op_parameters_are_refused(monkeypatch, params):
    tools = make_tools(monkeypatch)
    outcome = tools.run(CURRENT_OP, params)
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []


# == timeouts and the session (每次调用的命令序列, 到期) ==============================


def test_server_time_limit_is_the_remaining_budget_minus_two_seconds(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.find("{}"))
    [timeout] = tools.driver.timeouts
    assert 25 <= timeout <= 30, timeout
    [find] = tools.driver.tool_commands()
    assert 24_000 <= int(find.doc["maxTimeMS"]) <= 28_000


def test_server_time_limit_is_at_least_one_second(monkeypatch):
    tools = make_tools(monkeypatch, request_timeout_seconds=2.5)
    ok(tools.find("{}"))
    [timeout] = tools.driver.timeouts
    assert 0 < timeout <= 2.5
    [find] = tools.driver.tool_commands()
    assert int(find.doc["maxTimeMS"]) == 1000


def test_watchdog_closes_the_client_past_the_deadline(monkeypatch):
    tools = make_tools(monkeypatch, request_timeout_seconds=2.0)
    tools.driver.block_on = {"aggregate"}
    tools.driver.block_seconds = 15
    started = time.monotonic()
    outcome = tools.aggregate('[{"$match": {}}]')
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )
    assert elapsed < 8, elapsed
    [connection] = tools.driver.connections
    assert wait_until(lambda: connection.closed >= 1 or connection.cancel_calls >= 1)


def test_server_max_time_expired_is_tool_timeout(monkeypatch):
    errors = pytest.importorskip("pymongo.errors")
    tools = make_tools(monkeypatch)
    tools.driver.errors["find"] = errors.ExecutionTimeout(
        "operation exceeded time limit", code=50
    )
    outcome = tools.find("{}")
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )


# == C6-U1: the account check ========================================================

GOOD_STATUSES = {
    "c6-21": status(),
    "read-only": status([("read", MONGO_DB)]),
    "read-any-database": status([("readAnyDatabase", "admin")]),
    "cluster-monitor-only": status([("clusterMonitor", "admin")]),
    "all-three": status(
        [("read", MONGO_DB), ("readAnyDatabase", "admin"), ("clusterMonitor", "admin")]
    ),
    "read-on-other-db": status([("read", "reporting"), ("clusterMonitor", "admin")]),
}


def _bad_statuses() -> dict[str, dict[str, Any]]:
    bad: dict[str, dict[str, Any]] = {
        "no-user": status(users=[]),
        "no-auth-info": {"ok": 1.0},
        "empty-auth-info": {"authInfo": {}, "ok": 1.0},
    }
    for role in BAD_ROLES:
        bad[f"role-{role or 'empty'}"] = status([("read", MONGO_DB), (role, MONGO_DB)])
    bad["only-read-write"] = status([("readWrite", MONGO_DB)])
    bad["root-alone"] = status([("root", "admin")])
    for action in WRITE_ACTIONS:
        bad[f"action-{action}"] = status(actions=[action])
    bad["action-among-reads"] = status(actions=["find", "insert", "listIndexes"])
    return bad


BAD_STATUSES = _bad_statuses()


@pytest.mark.parametrize("case", sorted(GOOD_STATUSES))
def test_account_problem_accepts_the_read_only_roles(case):
    from opspilot.tools import mongodb_query

    # ASSUMPTION: ``status`` is the whole connectionStatus reply document.
    assert mongodb_query.account_problem(GOOD_STATUSES[case]) is None


@pytest.mark.parametrize("case", sorted(BAD_STATUSES))
def test_account_problem_refuses_every_other_account(case):
    from opspilot.tools import mongodb_query

    problem = mongodb_query.account_problem(BAD_STATUSES[case])
    assert isinstance(problem, str) and problem, case


@pytest.mark.parametrize(
    "case", ["no-user", "role-readWrite", "role-root", "role-c6tReader", "action-insert",
             "action-anyAction", "action-killop"],
)  # fmt: skip
@pytest.mark.parametrize("tool", MONGO_TOOLS)
def test_a_not_read_only_account_is_refused_before_any_tool_command(
    monkeypatch, tool, case
):
    tools = make_tools(monkeypatch)
    tools.driver.status = BAD_STATUSES[case]
    outcome = tools.run(tool)
    forbidden(outcome, "MONGODB_ACCOUNT_NOT_READ_ONLY")
    assert tools.driver.names() == ["connectionStatus"]


@pytest.mark.parametrize(
    ("case", "named"),
    [
        ("role-readWrite", "readWrite"),
        ("role-c6tReader", "c6tReader"),
        ("action-dropDatabase", "dropDatabase"),
        ("action-bypassDocumentValidation", "bypassDocumentValidation"),
    ],
)
def test_the_refusal_names_the_role_or_action(monkeypatch, case, named):
    tools = make_tools(monkeypatch)
    tools.driver.status = BAD_STATUSES[case]
    outcome = tools.find("{}")
    forbidden(outcome, "MONGODB_ACCOUNT_NOT_READ_ONLY")
    assert named in " ".join(notes(outcome))
    assert MONGO_PASSWORD not in _text(outcome)


@pytest.mark.parametrize("case", sorted(GOOD_STATUSES))
def test_read_only_accounts_pass_the_check_through_the_tool(monkeypatch, case):
    tools = make_tools(monkeypatch)
    tools.driver.status = GOOD_STATUSES[case]
    ok(tools.find("{}"))
    assert tools.driver.names() == ["connectionStatus", "find"]


def test_a_failing_account_check_never_runs_the_tool_command(monkeypatch):
    from opspilot.tools.sql_query import SqlDriverError

    tools = make_tools(monkeypatch)
    tools.driver.errors["connectionStatus"] = SqlDriverError("error", "1", "boom")
    outcome = tools.find("{}")
    assert outcome.status == "error", view(outcome)
    assert tools.driver.names() == ["connectionStatus"]


def test_the_account_is_checked_on_every_call(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.find("{}"))
    tools.driver.status = BAD_STATUSES["role-readWrite"]
    forbidden(tools.find("{}"), "MONGODB_ACCOUNT_NOT_READ_ONLY")
    assert tools.driver.names() == ["connectionStatus", "find", "connectionStatus"]


# == projection, caps and views (上限与投影) ===========================================


@pytest.mark.parametrize("tool", MONGO_TOOLS)
def test_views_name_the_row_unit_and_are_current_state(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    tools.driver.ops = [{"opid": 1, "op": "query", "active": True}]
    outcome = tools.run(tool)
    ok(outcome)
    v = view(outcome)
    assert f"backend_{ROW_UNIT[tool]}_returned" in v, v
    assert v.get("source_start_at") is None and v.get("source_end_at") is None
    assert outcome.evidence is not None
    assert outcome.evidence.source_start_at is None
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", notes(outcome)[0]), notes(outcome)
    assert isinstance(v.get("cannot_prove"), str) and v["cannot_prove"]
    assert v["source"] == "otel-demo"


def test_documents_convert_to_json_safe_values(monkeypatch):
    pytest.importorskip("bson")
    from bson import Binary, Code, Decimal128, Int64, ObjectId, Regex

    oid = ObjectId("65f0c0ffee00000000000001")
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [
        {
            "_id": oid,
            "at": datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            "price": Decimal128("9.99"),
            "blob": Binary(b"\x01\x02\xff"),
            "big": Int64(5_000_000_000),
            "re": Regex("^cust", "i"),
            "code": Code("function() { return 1; }"),
            "nested": {"oid": oid, "list": [oid, 1, "x"]},
            "flag": True,
            "none": None,
            "ratio": 0.5,
        }
    ]
    outcome = tools.find("{}")
    ok(outcome)
    [row] = rows(outcome)
    assert row["_id"] == "65f0c0ffee00000000000001"
    assert str(row["at"]).startswith("2026-11-20T12:00")
    assert row["price"] == "9.99"
    assert row["blob"] == "0x0102ff"
    assert row["big"] == 5_000_000_000 and type(row["big"]) is int
    assert isinstance(row["re"], str) and "^cust" in row["re"]
    assert isinstance(row["code"], str) and "return 1" in row["code"]
    assert row["nested"]["oid"] == "65f0c0ffee00000000000001"
    assert row["nested"]["list"] == ["65f0c0ffee00000000000001", 1, "x"]
    assert row["flag"] is True and row["none"] is None and row["ratio"] == 0.5
    json.dumps(view(outcome), allow_nan=False)


def test_long_strings_at_any_depth_are_cut_and_listed_by_dotted_path(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [
        {"_id": 1, "body": "x" * 5000, "meta": {"text": "y" * 5000, "ok": "fine"}},
        {"_id": 2, "body": "short"},
    ]
    outcome = tools.find("{}")
    ok(outcome)
    first, second = rows(outcome)
    assert first["body"].startswith("x" * 1000) and len(first["body"]) <= 2000 + 64
    assert len(first["meta"]["text"]) <= 2000 + 64
    text = json.dumps(view(outcome))
    assert "x" * 2001 not in text and "y" * 2001 not in text
    assert set(first["_truncated_fields"]) == {"body", "meta.text"}
    assert not second.get("_truncated_fields")


def test_password_and_token_fields_are_redacted_at_any_depth(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [
        {
            "_id": 1,
            "password": f"pw-{CANARY}",
            "api_token": f"tok-{CANARY}",
            "profile": {"password": f"pw2-{CANARY}", "name": "c1"},
        }
    ]
    outcome = tools.find("{}")
    ok(outcome)
    text = _text(outcome)
    assert CANARY not in text
    assert "REDACTED" in text
    [row] = rows(outcome)
    assert row["_id"] == 1 and row["profile"]["name"] == "c1"


@pytest.mark.parametrize("limit", [1, 3, 500])
def test_find_reaching_the_limit_marks_incomplete(monkeypatch, limit):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [{"_id": n} for n in range(limit + 5)]
    outcome = tools.find("{}", limit=limit)
    ok(outcome)
    assert [r["_id"] for r in rows(outcome)] == list(range(limit))
    v = view(outcome)
    assert v["incomplete"] is True
    assert "limit" in json.dumps(v.get("incomplete_reason"), default=str).lower()


def test_exactly_limit_documents_are_complete(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [{"_id": n} for n in range(100)]
    outcome = tools.find("{}")
    ok(outcome)
    assert len(rows(outcome)) == 100
    assert view(outcome)["incomplete"] is False


def test_aggregate_reaching_the_limit_marks_incomplete(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.aggregate_docs = [{"n": n} for n in range(600)]
    outcome = tools.aggregate('[{"$match": {}}]', limit=50)
    ok(outcome)
    assert [r["n"] for r in rows(outcome)] == list(range(50))
    assert view(outcome)["incomplete"] is True


def test_list_collections_rows_are_sorted_and_capped_at_500(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(LIST_COLLECTIONS)
    ok(outcome)
    assert [(r["name"], r["type"]) for r in rows(outcome)] == [
        ("customers", "collection"),
        ("orders", "collection"),
        ("orders_view", "view"),
    ]
    tools.driver.collections = [
        {"name": f"c{n:04d}", "type": "collection"} for n in reversed(range(600))
    ]
    outcome = tools.run(LIST_COLLECTIONS)
    ok(outcome)
    names = [r["name"] for r in rows(outcome)]
    assert names == [f"c{n:04d}" for n in range(500)]
    assert view(outcome)["incomplete"] is True


def test_collection_schema_samples_fields_and_reports_indexes_and_count(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [
        {"_id": 1, "name": "a", "address": {"city": "x", "zip": 1}},
        {"_id": 2, "name": 3},
    ]
    outcome = tools.run(SCHEMA, {"collection": "orders"})
    ok(outcome)
    fields = {r["field"]: r for r in rows(outcome)}
    assert {"_id", "name", "address.city", "address.zip"} <= set(fields)
    for row in fields.values():
        assert isinstance(row["types"], list) and row["types"], row
    assert len(set(fields["name"]["types"])) == 2  # a string and an integer
    v = view(outcome)
    assert v["estimated_document_count"] == 1234
    assert v["sample_size"] == 2
    indexes = {i["name"]: i for i in v["indexes"]}
    assert set(indexes) == {"_id_", "cust_at"}
    assert dict(indexes["cust_at"]["keys"]) == {"customer_id": 1, "at": -1}
    assert indexes["cust_at"]["unique"] is True
    assert not indexes["_id_"]["unique"]


def test_server_status_returns_the_default_sections(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(SERVER_STATUS)
    ok(outcome)
    [row] = rows(outcome)
    assert row["host"] == "c6t-mongo:27017" and row["version"] == "7.0.17"
    assert row["uptime_seconds"] == 1234
    for section in DEFAULT_SECTIONS:
        assert section in row, section
    for section in set(ALL_SECTIONS) - set(DEFAULT_SECTIONS):
        assert section not in row, section
    assert row["connections"]["current"] == 7


def test_server_status_returns_only_the_chosen_sections(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(SERVER_STATUS, {"sections": "wiredTiger,opLatencies"})
    ok(outcome)
    [row] = rows(outcome)
    assert "wiredTiger" in row and "opLatencies" in row
    assert "connections" not in row and "mem" not in row
    assert {"host", "version", "uptime_seconds"} <= set(row)


def _ops(count: int) -> list[dict[str, Any]]:
    return [
        {
            "opid": n,
            "op": "query",
            "ns": f"{MONGO_DB}.orders",
            "active": True,
            "secs_running": 3,
            "microsecs_running": 3_000_000,
            "desc": "conn12",
            "appName": "checkout",
            "client": "10.0.0.7:5555",
            "waitingForLock": False,
            "planSummary": "COLLSCAN",
            "command": {"find": "users", "filter": {"password": f"pw-{CANARY}"}},
            "lsid": {"id": "session-secret-ish"},
            "effectiveUsers": [{"user": "someone", "db": "admin"}],
        }
        for n in range(count)
    ]


def test_current_op_rows_are_projected_redacted_and_capped(monkeypatch):
    # Lead ruling A, revised 2026-11-20: the row cap is 100 (a real 200-row
    # page on MongoDB 7.0.17 was 25,404 tokens, ~127 per row, over the 25k
    # view, so the capped page itself was refused whole).
    tools = make_tools(monkeypatch)
    tools.driver.ops = _ops(501)
    outcome = tools.run(CURRENT_OP, {"min_duration_ms": 0})
    ok(outcome)
    result = rows(outcome)
    assert len(result) == 100
    assert view(outcome)["incomplete"] is True
    for row in result:
        assert {k for k in row if not k.startswith("_")} <= OP_FIELDS, row
    assert result[0]["opid"] == 0 and result[0]["planSummary"] == "COLLSCAN"
    assert CANARY not in _text(outcome)


def test_exactly_100_current_operations_are_complete(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.ops = _ops(100)
    outcome = tools.run(CURRENT_OP, {"min_duration_ms": 0})
    ok(outcome)
    assert len(rows(outcome)) == 100
    assert view(outcome)["incomplete"] is False


def _real_ops(count: int) -> list[dict[str, Any]]:
    # The shape MongoDB 7.0.17 returned on the rental host (a ``$where``
    # find, ~127 tokens per projected row), with a longer command, the way a
    # real application's query carries more filter keys: about 240 tokens per
    # row, close to the most a 100-row page holds (~24k tokens; a 200-row
    # page of these is ~47.5k and refused whole).
    return [
        {
            "opid": 19511 + n,
            "op": "query",
            "ns": "shopdb.slow",
            "active": True,
            "secs_running": 59,
            "microsecs_running": 59_641_150 - n * 1_013,
            "desc": f"conn{15 + n}",
            "appName": "checkout-service-v2",
            "client": f"10.42.{n % 250}.17:{43722 + n}",
            "waitingForLock": False,
            "planSummary": "IXSCAN { customer_id: 1, status: 1, created_at: -1 }",
            "command": {
                "find": "orders",
                "filter": {
                    "customer_id": {"$in": [f"cust-{n:05d}-{k}" for k in range(2)]},
                    "status": {"$in": ["pending", "paid", "shipped"]},
                    "created_at": {"$gte": "2026-11-19T21:45:25Z"},
                    "$where": "sleep(15000); return true",
                },
                "sort": {"created_at": -1},
                "limit": 50,
                "comment": "lane-d-load",
                "lsid": {"id": f"8b0f6a1e-3c2d-4f5a-9e7b-{n:012d}"},
                "$db": "shopdb",
            },
        }
        for n in range(count)
    ]


def test_a_full_current_op_page_of_long_real_rows_fits_the_view(monkeypatch):
    # Revised ruling A: the cap page itself must fit the 25k view with room
    # to spare for real rows, not just the short synthetic ones above.
    from opspilot.tools.tokens import count_tokens

    tools = make_tools(monkeypatch)
    tools.driver.ops = _real_ops(201)
    outcome = tools.run(CURRENT_OP, {"min_duration_ms": 0})
    ok(outcome)
    assert len(rows(outcome)) == 100
    assert view(outcome)["incomplete"] is True
    assert count_tokens(canonical(view(outcome))) <= 25_000


def test_raw_reply_over_2_mib_is_refused_whole(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [{"_id": 1, "blob": "z" * (3 * MIB)}]
    outcome = tools.find("{}", limit=1)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    assert view(outcome)["content"] is None


def test_many_documents_over_2_mib_are_refused_whole(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.aggregate_docs = [{"n": n, "body": "q" * 5000} for n in range(500)]
    outcome = tools.aggregate('[{"$match": {}}]', limit=500)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    assert view(outcome)["content"] is None


def test_view_over_25k_tokens_is_refused_whole_with_a_narrowing_hint(monkeypatch):
    words = " ".join(f"word{n} checkout latency spike" for n in range(60))
    assert len(words) < 2000
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = [{"_id": n, "note": words} for n in range(500)]
    outcome = tools.find("{}", limit=500)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    v = view(outcome)
    assert v["content"] is None
    assert "limit" in json.dumps(v, default=str).lower()


@pytest.mark.parametrize("tool", [FIND, AGGREGATE, LIST_COLLECTIONS, CURRENT_OP])
def test_zero_rows_are_no_data(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    tools.driver.docs["orders"] = []
    tools.driver.aggregate_docs = []
    tools.driver.collections = []
    tools.driver.ops = []
    outcome = tools.run(tool)
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), view(outcome)


def test_the_selector_carries_the_address_and_no_credential(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.find("{}"))
    [request] = tools.driver.requests
    selector = dict(request.selector)
    assert "127.0.0.1:17017" in selector["mongodb_endpoint"]
    flat = json.dumps(selector)
    assert MONGO_PASSWORD not in flat and MONGO_USERNAME not in flat


# == error classification (错误分类) ===================================================


def _op_failure(code: int, message: str):
    errors = pytest.importorskip("pymongo.errors")
    return errors.OperationFailure(
        message, code=code, details={"ok": 0.0, "errmsg": message, "code": code}
    )


INVALID_CODES = [2, 9, 14, 15, 16, 17, 40324, 168, 31249, 31254, 40353, 51091, 26]


@pytest.mark.parametrize("code", INVALID_CODES)
def test_bad_query_errors_are_invalid_params_with_a_short_redacted_echo(
    monkeypatch, code
):
    tools = make_tools(monkeypatch)
    tools.driver.errors["aggregate"] = _op_failure(
        code, f"server said something password={CANARY} " + "w" * 3000
    )
    outcome = tools.aggregate('[{"$match": {}}]')
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), view(
        outcome
    )
    text = _text(outcome)
    assert "MONGODB_INVALID" in " ".join(notes(outcome))
    assert "server said something" in text
    assert CANARY not in text
    assert "w" * 600 not in text
    assert len(tools.driver.connections) == 1
    assert tools.driver.names().count("aggregate") == 1  # no retry


@pytest.mark.parametrize("code", [13, 18, 11])
def test_authorization_errors_are_forbidden_and_not_echoed(monkeypatch, code):
    tools = make_tools(monkeypatch)
    tools.driver.errors["find"] = _op_failure(code, f"not authorized {CANARY}")
    outcome = tools.find("{}")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    assert "MONGODB_FORBIDDEN" in " ".join(notes(outcome))
    assert CANARY not in _text(outcome)


def test_bad_password_at_connect_is_forbidden_and_not_echoed(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.driver.connect_error = _op_failure(18, f"Authentication failed. {CANARY}")
    outcome = tools.find("{}")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    assert CANARY not in _text(outcome) and MONGO_PASSWORD not in _text(outcome)
    assert len(tools.driver.configs) == 1


@pytest.mark.parametrize(
    "name",
    [
        "ServerSelectionTimeoutError",
        "ConnectionFailure",
        "AutoReconnect",
        "NetworkTimeout",
    ],
)
def test_unreachable_servers_are_source_unavailable(monkeypatch, name):
    errors = pytest.importorskip("pymongo.errors")
    tools = make_tools(monkeypatch)
    tools.driver.connect_error = getattr(errors, name)("connection refused")
    outcome = tools.find("{}")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )
    assert len(tools.driver.configs) == 1  # no retry


@pytest.mark.parametrize("code", [1, 12345])
def test_other_errors_are_source_error(monkeypatch, code):
    tools = make_tools(monkeypatch)
    tools.driver.errors["find"] = _op_failure(code, "something else")
    outcome = tools.find("{}")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), view(outcome)


@pytest.mark.parametrize(
    ("kind", "status_reason"),
    [
        ("invalid", ("error", "INVALID_PARAMS")),
        ("forbidden", ("error", "SOURCE_FORBIDDEN")),
        ("unavailable", ("error", "SOURCE_UNAVAILABLE")),
        ("timeout", ("timeout", "TOOL_TIMEOUT")),
        ("error", ("error", "SOURCE_ERROR")),
    ],
)
def test_a_connection_may_raise_the_classified_sql_driver_error(
    monkeypatch, kind, status_reason
):
    from opspilot.tools.sql_query import SqlDriverError

    tools = make_tools(monkeypatch)
    tools.driver.errors["find"] = SqlDriverError(kind, "0", "classified")
    outcome = tools.find("{}")
    assert (outcome.status, outcome.reason) == status_reason, view(outcome)


# == configuration, credentials, startup codes ======================================

CA_SENTINEL = "<minted-ca>"


def _mint_ca(tmp_path: Path) -> str:
    import shutil

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic CA")
    cert, key = tmp_path / "mongo-ca.pem", tmp_path / "mongo-ca-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(cert)


BAD_CONFIG = [
    ({"OPSPILOT_MONGODB_DATABASE": ""}, "MONGODB_CONFIG_MISSING"),
    ({"OPSPILOT_MONGODB_USERNAME": ""}, "MONGODB_CONFIG_MISSING"),
    ({"OPSPILOT_MONGODB_PASSWORD": ""}, "MONGODB_CONFIG_MISSING"),
    ({"OPSPILOT_MONGODB_URL": "mongodb+srv://cluster0.example.invalid"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://u:p@127.0.0.1:27017"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://u@127.0.0.1:27017"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017,127.0.0.2:27017"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017/c6tdb"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017/?authSource=admin"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017/?directConnection=false"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017?tls=false"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017/#x"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017//"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:notaport"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "http://127.0.0.1:27017"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_URL": "127.0.0.1:27017"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": "a/b"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": "a\\b"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": "a.b"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": "a b"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": 'a"b'}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": "a$b"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_DATABASE": "d" * 64}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_TLS": "prefer"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_TLS": "verify-ca"}, "MONGODB_CONFIG_INVALID"),
    ({"OPSPILOT_MONGODB_TLS": "true"}, "MONGODB_CONFIG_INVALID"),
    (
        {"OPSPILOT_MONGODB_TLS": "disable", "OPSPILOT_MONGODB_CA_FILE": "/run/secrets/ca.pem"},
        "MONGODB_CONFIG_INVALID",
    ),
    # Settings without the address are never silently ignored.
    ({"OPSPILOT_MONGODB_URL": ""}, "MONGODB_CONFIG_INVALID"),
    (
        {
            "OPSPILOT_MONGODB_URL": "",
            "OPSPILOT_MONGODB_USERNAME": "",
            "OPSPILOT_MONGODB_PASSWORD": "",
            "OPSPILOT_MONGODB_TLS": "",
        },
        "MONGODB_CONFIG_INVALID",
    ),
    (
        {
            "OPSPILOT_MONGODB_URL": "",
            "OPSPILOT_MONGODB_DATABASE": "",
            "OPSPILOT_MONGODB_USERNAME": "",
            "OPSPILOT_MONGODB_PASSWORD": "",
            "OPSPILOT_MONGODB_TLS": "",
            "OPSPILOT_MONGODB_AUTH_SOURCE": "admin",
        },
        "MONGODB_CONFIG_INVALID",
    ),
]  # fmt: skip


@pytest.mark.parametrize(("overrides", "code"), BAD_CONFIG)
def test_worker_refuses_to_start_with_bad_mongodb_configuration(overrides, code):
    require_jq()
    with pytest.raises(SystemExit, match=code):
        worker(mongo_env(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:17017/"},
        {"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1"},
        {"OPSPILOT_MONGODB_URL": "mongodb://mongo.c6t.svc:27017"},
        {"OPSPILOT_MONGODB_URL": "mongodb://localhost:27018"},
        {"OPSPILOT_MONGODB_TLS": ""},
        {"OPSPILOT_MONGODB_TLS": "verify-full"},
        {"OPSPILOT_MONGODB_TLS": "require"},
        {
            "OPSPILOT_MONGODB_TLS": "verify-full",
            "OPSPILOT_MONGODB_CA_FILE": CA_SENTINEL,
        },
        {"OPSPILOT_MONGODB_TLS": "require", "OPSPILOT_MONGODB_CA_FILE": CA_SENTINEL},
        {"OPSPILOT_MONGODB_AUTH_SOURCE": MONGO_DB},
        {"OPSPILOT_MONGODB_DATABASE": "d" * 63},
        {"OPSPILOT_MONGODB_DATABASE": "shop-db_2"},
    ],
)
def test_worker_starts_with_valid_mongodb_configuration(
    monkeypatch, tmp_path, overrides
):
    if overrides.get("OPSPILOT_MONGODB_CA_FILE") == CA_SENTINEL:
        overrides = {**overrides, "OPSPILOT_MONGODB_CA_FILE": _mint_ca(tmp_path)}
    driver = FakeMongo()
    install(monkeypatch, driver)
    executor, _ = worker(mongo_env(**overrides))
    assert executor is not None
    assert driver.configs == []  # no connection at startup


@pytest.mark.parametrize("tls", ["verify-full", "require"])
def test_an_unreadable_ca_file_is_config_invalid(tmp_path, tls):
    require_jq()
    missing = str(tmp_path / "no-such-ca.pem")
    with pytest.raises(SystemExit, match="MONGODB_CONFIG_INVALID"):
        worker(mongo_env(OPSPILOT_MONGODB_TLS=tls, OPSPILOT_MONGODB_CA_FILE=missing))


def test_startup_codes_end_the_process():
    from opspilot.tools import profiles

    assert {
        "MONGODB_CONFIG_MISSING",
        "MONGODB_CONFIG_INVALID",
        "MONGODB_TARGETS_OPSPILOT_DB",
    } <= set(profiles._STARTUP_CODES)


def _flat(config: Any) -> str:
    flat = repr(config)
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        flat += json.dumps(dataclasses.asdict(config), default=str)
    elif hasattr(config, "__dict__"):
        flat += json.dumps(vars(config), default=str)
    return flat


def test_credentials_come_only_from_the_mongodb_ro_slot(monkeypatch):
    sql_user, sql_password = "c6tsqluser", "c6t-sql-" + "bbbb570"
    dsn_user, dsn_password = "c6tdsnuser", "c6t-dsn-" + "cccc570"
    monkeypatch.setenv(
        "OPSPILOT_WORKER_DSN",
        f"postgresql://{dsn_user}:{dsn_password}@127.0.0.1:5432/o",
    )
    tools = make_tools(
        monkeypatch,
        OPSPILOT_SQL_URL="postgresql://127.0.0.1:15432/app",
        OPSPILOT_SQL_USERNAME=sql_user,
        OPSPILOT_SQL_PASSWORD=sql_password,
        OPSPILOT_WORKER_DSN=f"postgresql://{dsn_user}:{dsn_password}@127.0.0.1:5432/o",
    )
    ok(tools.find("{}"))
    [config] = tools.driver.configs
    flat = _flat(config)
    for foreign in (sql_user, sql_password, dsn_user, dsn_password):
        assert foreign not in flat, foreign
    assert MONGO_USERNAME in flat


def test_no_fallback_to_another_slot_without_the_mongodb_password(monkeypatch):
    require_jq()
    driver = FakeMongo()
    install(monkeypatch, driver)
    env = mongo_env(
        OPSPILOT_MONGODB_PASSWORD="",
        OPSPILOT_SQL_URL="postgresql://127.0.0.1:15432/app",
        OPSPILOT_SQL_USERNAME="c6tsqluser",
        OPSPILOT_SQL_PASSWORD="c6t-sql-" + "bbbb570",
        OPSPILOT_ES_URL="http://127.0.0.1:9",
        OPSPILOT_ES_API_KEY="es-ro-cccccccc570",
    )
    with pytest.raises(SystemExit, match="MONGODB_CONFIG_MISSING"):
        worker(env)
    assert driver.configs == []


class _TrackingEnv(Mapping[str, str]):
    def __init__(self, values: Mapping[str, str]):
        self._values = dict(values)
        self.read: set[str] = set()
        self.iterated = False

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return self._values[key]

    def __iter__(self):
        self.iterated = True
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_web_side_profile_reads_only_the_mongodb_address():
    env = _TrackingEnv(
        {
            **_web(),
            "OPSPILOT_MONGODB_DATABASE": MONGO_DB,
            "OPSPILOT_MONGODB_USERNAME": MONGO_USERNAME,
            "OPSPILOT_MONGODB_PASSWORD": MONGO_PASSWORD,
            "OPSPILOT_MONGODB_AUTH_SOURCE": "admin",
            "OPSPILOT_MONGODB_TLS": "require",
            "OPSPILOT_MONGODB_CA_FILE": "/nonexistent/mongo-ca.pem",
        }
    )
    profile = select_profile(env)
    face = profile.face(FakeClock(start=PIN_NOW))
    face.evidence_context(RUN_ID)
    profile.versions()
    assert set(MONGO_TOOLS) <= {s["function"]["name"] for s in face.tool_schemas}
    assert not env.read & (
        {
            "OPSPILOT_MONGODB_DATABASE",
            "OPSPILOT_MONGODB_USERNAME",
            "OPSPILOT_MONGODB_PASSWORD",
            "OPSPILOT_MONGODB_AUTH_SOURCE",
            "OPSPILOT_MONGODB_TLS",
            "OPSPILOT_MONGODB_CA_FILE",
        }
    ), env.read
    assert not env.iterated


def test_entrypoint_imports_mongodb_settings_from_the_worker_env_file(tmp_path):
    entrypoint = ROOT / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_MONGODB_URL=mongodb://127.0.0.1:10227\n"
        "OPSPILOT_MONGODB_DATABASE=shopdb\n"
        f"OPSPILOT_MONGODB_USERNAME={MONGO_USERNAME}\n"
        f"OPSPILOT_MONGODB_PASSWORD='{MONGO_PASSWORD}'\n"
        "OPSPILOT_MONGODB_AUTH_SOURCE=admin\n"
        "OPSPILOT_MONGODB_TLS=disable\n"
        "OPSPILOT_MONGODBX=not-a-setting\n"
        "OPSPILOT_MONGODB_$(touch pwned)=x\n"
    )
    result = subprocess.run(
        ["sh", str(entrypoint), "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(worker_env)},
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env["OPSPILOT_MONGODB_URL"] == "mongodb://127.0.0.1:10227"
    assert env["OPSPILOT_MONGODB_DATABASE"] == "shopdb"
    assert env["OPSPILOT_MONGODB_USERNAME"] == MONGO_USERNAME
    assert env["OPSPILOT_MONGODB_PASSWORD"] == MONGO_PASSWORD
    assert env["OPSPILOT_MONGODB_AUTH_SOURCE"] == "admin"
    assert env["OPSPILOT_MONGODB_TLS"] == "disable"
    assert "OPSPILOT_MONGODBX" not in env
    assert not (tmp_path / "pwned").exists()


# == client options of the real connector (directConnection, retries, TLS) ==========


class _ClientRecorder:
    """Stands in for ``pymongo.MongoClient``: keeps the constructor call; any
    use answers as an unreachable server."""

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).calls.append((args, kwargs))

    def _down(self, *args: Any, **kwargs: Any):
        from pymongo.errors import ServerSelectionTimeoutError

        raise ServerSelectionTimeoutError("no servers (recorder)")

    def __getattr__(self, name: str):
        if name in ("close", "_close"):
            return lambda *a, **k: None
        self._down()

    def __getitem__(self, name: str):
        self._down()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None


def _client_options(monkeypatch, env: dict[str, str]) -> dict[str, Any]:
    """The options the default connector gives ``MongoClient``, from its
    keyword arguments and from its URI query, keys lower-cased.

    # ASSUMPTION: the default connector builds ``pymongo.MongoClient`` (by
    # attribute of ``pymongo``, ``pymongo.mongo_client`` or an imported name).
    """
    pymongo = pytest.importorskip("pymongo")
    _ClientRecorder.calls = []
    targets = [pymongo, sys.modules.get("pymongo.mongo_client")]
    from opspilot.tools import mongodb_query

    targets.append(mongodb_query)
    for module in targets:
        if module is not None and hasattr(module, "MongoClient"):
            monkeypatch.setattr(module, "MongoClient", _ClientRecorder)
    executor, _ = worker(env)
    executor.execute(call(FIND, MINIMAL[FIND]))
    assert len(_ClientRecorder.calls) == 1, _ClientRecorder.calls
    args, kwargs = _ClientRecorder.calls[0]
    options: dict[str, Any] = {k.lower(): v for k, v in kwargs.items()}
    hosts = [a for a in args if isinstance(a, str)]
    if isinstance(kwargs.get("host"), str):
        hosts.append(kwargs["host"])
    for host in hosts:
        if host.startswith("mongodb"):
            parts = urlsplit(host)
            for key, value in parse_qsl(parts.query):
                options.setdefault(key.lower(), value)
            options["_netloc"] = parts.netloc
            options["_scheme"] = parts.scheme
        else:
            options["_netloc"] = host
    if "port" in options:
        options["_netloc"] = f"{options.get('_netloc', '')}:{options['port']}"
    return options


def _true(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "1")


def _false(value: Any) -> bool:
    return value is False or str(value).lower() in ("false", "0")


def test_client_connects_directly_without_retries(monkeypatch):
    options = _client_options(monkeypatch, mongo_env())
    assert options.get("_scheme", "mongodb") == "mongodb"
    assert "127.0.0.1" in options["_netloc"] and "17017" in options["_netloc"]
    assert _true(options["directconnection"])
    assert _false(options["retryreads"])
    assert _false(options["retrywrites"])
    assert options["appname"] == "opspilot-mongodb-tool"
    preference = options.get("readpreference", options.get("read_preference"))
    mode = getattr(preference, "mongos_mode", preference)
    assert str(mode).lower().replace("_", "") == "secondarypreferred", preference
    for name in ("serverselectiontimeoutms", "connecttimeoutms", "sockettimeoutms"):
        assert 0 < float(options[name]) <= 30_000, (name, options[name])
    assert options["authsource"] == "admin"
    assert options.get("username") == MONGO_USERNAME or MONGO_USERNAME in options.get(
        "_netloc", ""
    )
    assert not _true(options.get("tls", False))
    assert not _true(options.get("ssl", False))


def test_auth_source_comes_from_configuration(monkeypatch):
    options = _client_options(
        monkeypatch, mongo_env(OPSPILOT_MONGODB_AUTH_SOURCE=MONGO_DB)
    )
    assert options["authsource"] == MONGO_DB


def test_tls_defaults_to_full_verification(monkeypatch):
    options = _client_options(monkeypatch, mongo_env(OPSPILOT_MONGODB_TLS=""))
    assert _true(options.get("tls", options.get("ssl")))
    for name in (
        "tlsallowinvalidcertificates",
        "tlsallowinvalidhostnames",
        "tlsinsecure",
    ):
        assert not _true(options.get(name, False)), name


def test_tls_require_encrypts_without_verifying(monkeypatch):
    options = _client_options(monkeypatch, mongo_env(OPSPILOT_MONGODB_TLS="require"))
    assert _true(options.get("tls", options.get("ssl")))
    assert _true(options.get("tlsallowinvalidcertificates")) or _true(
        options.get("tlsinsecure")
    )


def test_tls_uses_the_configured_ca(monkeypatch, tmp_path):
    ca = _mint_ca(tmp_path)
    options = _client_options(
        monkeypatch,
        mongo_env(OPSPILOT_MONGODB_TLS="verify-full", OPSPILOT_MONGODB_CA_FILE=ca),
    )
    assert _true(options.get("tls", options.get("ssl")))
    assert options["tlscafile"] == ca


# == C6-8: OpsPilot's own databases ===================================================


def _with_dsn(monkeypatch, env: dict[str, str], **dsns: str) -> dict[str, str]:
    # ASSUMPTION (as C6a): DSNs may be read from the mapping or os.environ.
    for name, value in dsns.items():
        monkeypatch.setenv(name, value)
    return {**env, **dsns}


@pytest.mark.parametrize(
    ("url", "dsns"),
    [
        (
            "mongodb://127.0.0.1:27017",
            {"OPSPILOT_WORKER_DSN": "postgresql://o:x@localhost:27017/opspilot"},
        ),
        (
            "mongodb://localhost",
            {"OPSPILOT_DSN": "host=127.0.0.1 port=27017 dbname=opspilot user=o"},
        ),
        (
            "mongodb://127.0.0.2:5432",
            {"OPSPILOT_WEB_DSN": "postgresql://o@localhost/opspilot"},
        ),
        (
            "mongodb://[::1]:5433",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:5433/opspilot"},
        ),
        (
            "mongodb://localhost:5432",
            {"OPSPILOT_WORKER_DSN": "dbname=opspilot user=o"},
        ),
        (
            "mongodb://127.0.0.1:5432",
            {
                "OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:6543/opspilot",
                "OPSPILOT_KNOWLEDGE_ADMIN_DSN": "postgresql://o@127.0.0.1/opspilot",
            },
        ),
    ],
)
def test_worker_refuses_a_mongodb_url_on_an_opspilot_database(monkeypatch, url, dsns):
    driver = FakeMongo()
    install(monkeypatch, driver)
    env = _with_dsn(monkeypatch, mongo_env(url), **dsns)
    with pytest.raises(SystemExit, match="MONGODB_TARGETS_OPSPILOT_DB"):
        worker(env)
    assert driver.configs == []


@pytest.mark.parametrize(
    ("url", "dsns"),
    [
        (
            "mongodb://127.0.0.1:27017",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1/x"},
        ),
        (
            "mongodb://127.0.0.1",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:5432/x"},
        ),
        (
            "mongodb://127.0.0.1:5432",
            {
                "OPSPILOT_DSN_BACKUP": "postgresql://o@127.0.0.1:5432/x",
                "OPSPILOT_REPLICA_DATABASE_URL": "postgresql://o@127.0.0.1:5432/x",
            },
        ),
    ],
)
def test_worker_starts_when_no_opspilot_dsn_shares_the_address(monkeypatch, url, dsns):
    driver = FakeMongo()
    install(monkeypatch, driver)
    executor, _ = worker(_with_dsn(monkeypatch, mongo_env(url), **dsns))
    assert executor is not None


# == ⑪ lab gate ==========================================================================


def test_lab_gate_requires_a_lab_mongodb_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-c6c",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa570",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab in (
        "mongodb://127.0.0.1:10227",
        "mongodb://localhost",
        "mongodb://localhost:10228/",
        "mongodb://mongo.c6t.svc:27017",
    ):
        assert check_lab_target({**env, "OPSPILOT_MONGODB_URL": lab}) == (), lab
    for remote in (
        "mongodb://db.example.invalid:27017",
        "mongodb://10.0.0.5:27017",
        "mongodb://127.attacker.example",
        "mongodb+srv://cluster0.example.invalid",
    ):
        failures = check_lab_target({**env, "OPSPILOT_MONGODB_URL": remote})
        assert "MONGODB_NOT_LAB" in failures, remote
    others = check_lab_target(
        {**env, "OPSPILOT_ES_URL": "https://logs.example.invalid"}
    )
    assert "MONGODB_NOT_LAB" not in others


# == trace and views never carry the credential =========================================


def test_credentials_never_appear_in_views_notes_or_trace(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from opspilot.tools.sql_query import SqlDriverError
    from tests.test_tracing import lab_env

    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=K8S_API,
            OPSPILOT_MONGODB_URL=MONGO_URL,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
        ),
        exporter=exporter,
    )
    try:
        outcomes = []
        tools = make_tools(monkeypatch)
        with tracer.run(incident_id="c6c-incident", run_id=RUN_ID, versions={}):
            outcomes.append(tools.find('{"status": "paid"}'))
            outcomes.append(tools.find('{"$where": "1"}'))
            tools.driver.errors["find"] = SqlDriverError("invalid", "2", "bad query")
            outcomes.append(tools.find("{}"))
            tools.driver.errors.clear()
            tools.driver.connect_error = SqlDriverError(
                "forbidden", "18", "auth failed"
            )
            outcomes.append(tools.find("{}"))
            tools.driver.connect_error = None
            tools.driver.status = BAD_STATUSES["role-root"]
            outcomes.append(tools.find("{}"))
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    assert any(FIND in span.name for span in spans)
    exported = json.dumps([dict(span.attributes or {}) for span in spans], default=str)
    rendered = json.dumps([o.model_view for o in outcomes], default=str)
    for secret in (MONGO_PASSWORD, K8S_TOKEN, PROM_PASSWORD):
        assert secret not in exported
        assert secret not in rendered
