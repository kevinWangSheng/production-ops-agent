"""Independent contract tests for the read-only Redis/Valkey tools (#773):
``redis_info``, ``redis_config_get``, ``redis_scan_keys``, ``redis_key_info``,
``redis_get_value`` and ``redis_slowlog``.

Written by the test author from the #773 contract (R1-R18 and the
acceptance list 1-9), without reading the implementation. Red until the
module ``opspilot/tools/redis_inspect.py`` lands.

Every call goes through the shipped worker (``select_profile`` ->
``executor_factory``) to ``FakeRedis`` (``tests/redis_support.py``), a real
RESP2 server on 127.0.0.1 that records each command each connection sends.
Real Valkey is in ``tests/integration/test_redis_inspect_valkey.py``; the
safety guards in ``tests/safety/test_safety_redis_inspect.py``.

Surfaces were pinned on origin/main before any #773 code
(``tests/fixtures/redis_773/pins.json``). Assumptions beyond the contract
are marked ``# ASSUMPTION:``. All synthetic credentials are low-entropy
placeholders.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.c6c_mongo_support import (
    EXT_PROFILE,
    forbidden,
    notes,
    ok,
    require_jq,
    rows,
    view,
    worker,
)
from tests.m1_tool_support import FakeClock
from tests.redis_support import (
    ALLOWED,
    CANARY,
    CONFIG_TOOL,
    GET_VALUE_TOOL,
    GETUSER_MARKER,
    HANDSHAKE,
    INFO_TOOL,
    KEY_INFO_TOOL,
    LAB_GRANT,
    MINIMAL,
    PARAMS,
    PASSWORD_HASH,
    REDIS_PASSWORD,
    REDIS_TOOLS,
    REDIS_USERNAME,
    SCAN_TOOL,
    SLOWLOG_TOOL,
    FakeRedis,
    free_port,
    getuser_reply,
    make_tools,
    redis_env,
    shape,
)
from tests.safety.gateway_support import everything_seen

ROOT = Path(__file__).resolve().parents[1]
PIN_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RUN_ID = "r773-run"
MIB = 1024 * 1024
PINS_DOC = json.loads(
    (ROOT / "tests" / "fixtures" / "redis_773" / "pins.json").read_text()
)
PINS: dict[str, list[str]] = PINS_DOC["pins"]
SURFACE_KEYS = sorted(k for k in PINS if not k.startswith("profile|"))

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
MONGO_URL = "mongodb://127.0.0.1:17017"
#: Nothing listens here; startup never connects.
WEB_REDIS_URL = "redis://127.0.0.1:16379"
REDIS_SETTINGS = {
    "OPSPILOT_REDIS_DATABASE": "2",
    "OPSPILOT_REDIS_USERNAME": REDIS_USERNAME,
    "OPSPILOT_REDIS_PASSWORD": REDIS_PASSWORD,
    "OPSPILOT_REDIS_TLS": "require",
    "OPSPILOT_REDIS_CA_FILE": "/nonexistent/redis-ca.pem",
}
#: R12 row units. ASSUMPTION: key_info counts ``keys``; get_value counts
#: ``fields`` (hash) / ``elements`` (list, set, zset) / ``entries`` (stream).
ROW_UNIT = {
    INFO_TOOL: "sections",
    CONFIG_TOOL: "parameters",
    SCAN_TOOL: "keys",
    KEY_INFO_TOOL: "keys",
    SLOWLOG_TOOL: "entries",
}


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith(("OPSPILOT_REDIS", "OPSPILOT_MONGODB", "OPSPILOT_SQL")) or (
            re.fullmatch(r"OPSPILOT_[A-Z0-9_]*DSN", name)
        ):
            monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6C_TEST_K8S_CA_FILE", str(ca_file))


def _env_for(key: str, *, redis: bool = False) -> dict[str, str]:
    flags_text, sql, kafka, mongo = key.split("|")
    env = {PROFILE_ENV: EXT_PROFILE}
    for c, name in zip(flags_text[:3], BACKEND_URLS):
        if c == "1":
            env[name] = BACKEND_URLS[name]
    for c, name in zip(flags_text[3:], SWITCHES):
        if c == "1":
            env[name] = "1"
    if SQL_URLS[sql]:
        env["OPSPILOT_SQL_URL"] = SQL_URLS[sql]  # type: ignore[assignment]
    if kafka == "1":
        env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] = "localhost:9092"
    if mongo == "1":
        env["OPSPILOT_MONGODB_URL"] = MONGO_URL
    if redis:
        env["OPSPILOT_REDIS_URL"] = WEB_REDIS_URL
    return env


def _revision_kwargs(key: str) -> dict[str, Any]:
    flags_text, sql, kafka, mongo = key.split("|")
    kwargs: dict[str, Any] = {n: c == "1" for n, c in zip(FLAG_NAMES, flags_text)}
    kwargs["sql"] = None if sql == "none" else sql
    kwargs["kafka"] = kafka == "1"
    kwargs["mongodb"] = mongo == "1"
    return kwargs


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=PIN_NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _available(env: Mapping[str, str]) -> Mapping[str, Any]:
    return _face(env).evidence_context(RUN_ID)["data_sources"]["available"]


def _surface(profile: ToolProfile) -> str:
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


def _text(outcome) -> str:
    return json.dumps(view(outcome), ensure_ascii=False, default=str)


def _web(**extra: str) -> dict[str, str]:
    return {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_REDIS_URL": WEB_REDIS_URL, **extra}


def _status(outcome, status: str, reason: str | None, first_note: str | None = None):
    assert (outcome.status, outcome.reason) == (status, reason), view(outcome)
    if first_note is not None:
        assert (notes(outcome) or [""])[0].startswith(first_note), view(outcome)


# == 1. surfaces: existing bytes unchanged; six tools last with the address =========


@pytest.mark.parametrize("key", SURFACE_KEYS)
def test_without_the_url_every_existing_surface_is_byte_identical(key):
    from opspilot.tools import otel_demo_ext

    revision, surface = PINS[key]
    env = _env_for(key)
    assert _revision(env) == revision
    assert _surface(select_profile(env)) == surface
    # The web reads only the address: every other setting changes nothing.
    assert _surface(select_profile({**env, **REDIS_SETTINGS})) == surface
    kwargs = _revision_kwargs(key)
    assert otel_demo_ext.tool_schema_revision(**kwargs, redis=False) == revision


@pytest.mark.parametrize("profile", ["otel-demo", "fixture"])
def test_other_profiles_ignore_every_redis_setting(profile):
    revision, surface = PINS[f"profile|{profile}"]
    env = {PROFILE_ENV: profile, "OPSPILOT_REDIS_URL": WEB_REDIS_URL, **REDIS_SETTINGS}
    assert _revision(env) == revision
    assert _surface(select_profile(env)) == surface


@pytest.mark.parametrize("key", SURFACE_KEYS)
def test_the_url_appends_the_six_tools_last_and_one_source(key):
    from opspilot.tools import otel_demo_ext

    old = _tool_names(_env_for(key))
    new = _tool_names(_env_for(key, redis=True))
    assert new[: len(old)] == old
    assert sorted(new[len(old) :]) == sorted(REDIS_TOOLS)
    old_sources = dict(_available(_env_for(key)))
    new_sources = dict(_available(_env_for(key, redis=True)))
    # ASSUMPTION: the data source is named ``redis`` (the contract says "one
    # data source").
    assert new_sources.pop("redis") == sorted(REDIS_TOOLS)
    assert new_sources == old_sources
    revision = _revision(_env_for(key, redis=True))
    assert revision.startswith("otel-demo-ext-")
    assert revision == otel_demo_ext.tool_schema_revision(
        **_revision_kwargs(key), redis=True
    )
    assert revision != PINS[key][0]


def test_every_redis_surface_has_its_own_revision_and_constants():
    from opspilot.tools import otel_demo_ext as ext

    known = {pin[0] for pin in PINS.values()}
    seen = {_revision(_env_for(key, redis=True)) for key in SURFACE_KEYS}
    assert len(seen) == len(SURFACE_KEYS) and not seen & known
    assert (
        ext.MONGODB_TOOL_SCHEMA_REVISION
        == PINS_DOC["constants"]["MONGODB_TOOL_SCHEMA_REVISION"]
    )
    assert ext.REDIS_TOOL_SCHEMA_REVISION == ext.tool_schema_revision(redis=True)
    assert _revision(_web()) == ext.REDIS_TOOL_SCHEMA_REVISION
    names = list(ext.REDIS_TOOL_NAMES)
    assert len(names) == len(set(names))
    assert set(names) == set(ext.TOOL_NAMES) | set(REDIS_TOOLS)


def test_web_and_worker_agree_and_the_prompt_is_unchanged():
    require_jq()
    web, full = _web(), redis_env(WEB_REDIS_URL, OPSPILOT_REDIS_DATABASE="2")
    assert select_profile(web).versions() == select_profile(full).versions()
    assert _surface(select_profile(web)) == _surface(select_profile(full))
    old = select_profile({PROFILE_ENV: EXT_PROFILE}).versions()
    new = select_profile(web).versions()
    assert new["prompt_revision"] == old["prompt_revision"]
    assert new["context_policy_revision"] == old["context_policy_revision"]
    assert new["tool_schema_revision"] != old["tool_schema_revision"]


# == 2. schemas and registrations ====================================================


def test_schemas_take_exactly_the_documented_parameters():
    schemas = {s["function"]["name"]: s["function"] for s in _face(_web()).tool_schemas}
    for tool in REDIS_TOOLS:
        parameters = schemas[tool]["parameters"]
        properties = set(parameters.get("properties") or {})
        assert properties == PARAMS[tool], (tool, properties)
        assert set(parameters.get("required") or []) <= properties
        if "key" in properties:
            assert "key" in parameters["required"], tool
        if "limit" in properties:
            assert parameters["properties"]["limit"]["type"] == "integer"
    flat = json.dumps([schemas[t]["parameters"] for t in REDIS_TOOLS]).lower()
    for word in (
        "command",
        "argv",
        "args",
        "db",
        "database",
        "url",
        "host",
        "start",
        "end",
    ):
        assert f'"{word}"' not in flat, word
    for tool in REDIS_TOOLS:
        lower = schemas[tool]["description"].lower()
        assert "redis or valkey" in lower, tool
        for marker in ("allow_write", "enable write", "write mode", "read_only: false"):
            assert marker not in lower, (tool, marker)


def test_registrations_are_bounded_read_only_otel_demo_tools():
    from opspilot.tools import otel_demo, otel_demo_ext, redis_inspect

    # ASSUMPTION: the private registry seam takes ``redis=`` like
    # ``tool_schema_revision`` does (as ``mongodb=`` before it).
    regs = {r.name: r for r in otel_demo_ext._registrations(redis=True)}
    for name in REDIS_TOOLS:
        reg = regs[name]
        assert reg.read_only is True and reg.verb == "get", name
        assert reg.source == otel_demo.SOURCE == "otel-demo", name
        assert reg.result_path == ("results",), name
        assert reg.may_contain_secrets is True, name
        assert reg.max_result_bytes == 2 * MIB, name
        assert reg.max_view_tokens == 25_000, name
        assert 0 < reg.request_timeout_seconds <= 30, name
    assert not set(REDIS_TOOLS) & {r.name for r in otel_demo_ext._registrations()}
    if hasattr(redis_inspect, "CREDENTIAL_REF"):
        assert redis_inspect.CREDENTIAL_REF == "redis-ro"


# == 3. the command face: handshake -> account check -> the tool's own commands ======

EXPECTED = {
    INFO_TOOL: {("INFO", None)},
    CONFIG_TOOL: {("CONFIG", "GET")},
    SCAN_TOOL: {("SCAN", None)},
    KEY_INFO_TOOL: {
        ("TYPE", None),
        ("PTTL", None),
        ("OBJECT", "ENCODING"),
        ("MEMORY", "USAGE"),
        ("STRLEN", None),
    },  # fmt: skip
    GET_VALUE_TOOL: {("GETRANGE", None)},
    SLOWLOG_TOOL: {("SLOWLOG", "GET")},
}
#: R2 lists only the type-specific read for get_value; learning the type
#: first needs TYPE (in the allowlist through key_info).
MAY_ALSO = {GET_VALUE_TOOL: {("TYPE", None)}}


def _order(commands: list[list[str]]) -> tuple[list[list[str]], list[list[str]]]:
    """(handshake prefix, the rest of the commands)."""
    split = next(
        (i for i, c in enumerate(commands) if shape(c) not in HANDSHAKE), len(commands)
    )
    return commands[:split], commands[split:]


@pytest.mark.parametrize("tool", REDIS_TOOLS)
def test_each_call_is_handshake_then_account_check_then_only_its_commands(
    monkeypatch, tool
):
    tools = make_tools(monkeypatch)
    ok(tools.run(tool))
    [conn] = tools.server.connections
    head, rest = _order(conn.commands)
    assert head[0] == ["AUTH", REDIS_USERNAME, REDIS_PASSWORD], head
    shapes = [shape(c) for c in head]
    assert [c[2:] for c in head if shape(c) == ("CLIENT", "SETNAME")] == [
        ["opspilot-redis-tool"]
    ]
    assert ("SELECT", None) not in shapes  # database 0
    who, get = shapes.index(("ACL", "WHOAMI")), shapes.index(("ACL", "GETUSER"))
    assert who < get and head[get][2:] == [REDIS_USERNAME]
    assert not [c for c in rest if shape(c) in HANDSHAKE], rest
    sent = {shape(c) for c in rest}
    assert sent, "the tool sent no command of its own"
    assert sent <= EXPECTED[tool] | MAY_ALSO.get(tool, set()), sent
    assert EXPECTED[tool] <= sent, sent
    assert {shape(c) for c in conn.commands} <= ALLOWED
    assert tools.server.all_closed()


def test_select_is_sent_only_for_a_non_zero_database(monkeypatch):
    tools = make_tools(monkeypatch, OPSPILOT_REDIS_DATABASE="3")
    ok(tools.run(INFO_TOOL))
    head, _ = _order(tools.server.commands)
    assert [c for c in head if shape(c) == ("SELECT", None)] == [["SELECT", "3"]]


@pytest.mark.parametrize("tool", REDIS_TOOLS)
def test_each_call_opens_its_own_connection_and_closes_it(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    ok(tools.run(tool))
    ok(tools.run(tool))
    assert len(tools.server.connections) == 2
    assert tools.server.all_closed()
    # The account check runs on every call.
    for conn in tools.server.connections:
        assert ("ACL", "GETUSER") in {shape(c) for c in conn.commands}


INJECTED = {
    "command": "FLUSHALL",
    "argv": ["CONFIG", "SET", "save", ""],
    "db": 5,
    "database": 6,
    "count": 999_999,
    "cursor": "77",
    "start": 4242,
    "end": 4343,
    "url": "redis://10.0.0.9:6379",
}


@pytest.mark.parametrize("tool", REDIS_TOOLS)
def test_parameters_outside_the_schema_never_reach_a_command(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {**MINIMAL[tool], **INJECTED})
    if outcome.status in ("error", "denied"):
        assert tools.server.tool_commands() == [], tools.server.commands
        return
    for argv in tools.server.commands:
        assert shape(argv) in ALLOWED, argv
        flat = " ".join(argv)
        for value in ("FLUSHALL", "999999", "4242", "4343", "10.0.0.9", " SET "):
            assert value not in flat, (argv, value)
        assert shape(argv) != ("SELECT", None)


@pytest.mark.parametrize(
    "key", ["", "k" * 1025, "orders:1\r\nFLUSHALL", "a\x00b", "tab\tkey", "\x7f"]
)
@pytest.mark.parametrize("tool", [KEY_INFO_TOOL, GET_VALUE_TOOL])
def test_bad_keys_are_refused_before_any_tool_command(monkeypatch, tool, key):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {"key": key})
    _status(outcome, "error", "INVALID_PARAMS", "INVALID_IDENTIFIER")
    assert tools.server.tool_commands() == []


@pytest.mark.parametrize("pattern", ["", "p" * 1025, "orders:*\nKEYS"])
def test_bad_patterns_are_refused_before_any_tool_command(monkeypatch, pattern):
    tools = make_tools(monkeypatch)
    outcome = tools.run(SCAN_TOOL, {"pattern": pattern})
    _status(outcome, "error", "INVALID_PARAMS", "INVALID_IDENTIFIER")
    assert tools.server.tool_commands() == []


def test_the_longest_printable_key_is_accepted(monkeypatch):
    tools = make_tools(monkeypatch)
    key = "k" * 1024
    tools.server.data[key] = ("string", b"v")
    ok(tools.run(KEY_INFO_TOOL, {"key": key}))


@pytest.mark.parametrize(
    ("tool", "limit"),
    [(SCAN_TOOL, 0), (SCAN_TOOL, 501), (GET_VALUE_TOOL, -1), (GET_VALUE_TOOL, "10"),
     (GET_VALUE_TOOL, True), (SLOWLOG_TOOL, 0), (SLOWLOG_TOOL, 129)],
)  # fmt: skip
def test_limits_outside_their_range_are_refused(monkeypatch, tool, limit):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {**MINIMAL[tool], "limit": limit})
    _status(outcome, "error", "INVALID_PARAMS")
    assert tools.server.tool_commands() == []


def test_scan_type_must_be_a_redis_type(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(SCAN_TOOL, {"pattern": "*", "type": "string FLUSHALL"})
    _status(outcome, "error", "INVALID_PARAMS")
    assert tools.server.tool_commands() == []


# == 4. command shapes ================================================================


def test_scan_uses_match_count_1000_and_type(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(SCAN_TOOL, {"pattern": "orders:*", "type": "string"}))
    scans = [c for c in tools.server.commands if shape(c) == ("SCAN", None)]
    assert scans and scans[0][1] == "0"
    for argv in scans:
        assert [a.upper() for a in argv[2:]] == ["MATCH", "ORDERS:*", "COUNT", "1000", "TYPE", "STRING"], argv  # fmt: skip


def test_scan_finds_the_keys_and_reports_complete(monkeypatch):
    tools = make_tools(monkeypatch)
    for n in range(25):
        tools.server.data[f"orders:{n}"] = ("string", b"x")
    outcome = tools.run(SCAN_TOOL, {"pattern": "orders:*"})
    ok(outcome)
    text = _text(outcome)
    for n in range(25):
        assert f"orders:{n}" in text, n
    assert "cart:9" not in text
    assert view(outcome)["incomplete"] is False
    assert view(outcome)["backend_keys_returned"] == 25


def test_scan_stops_after_50_iterations_and_marks_incomplete(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.scan_forever = True
    outcome = tools.run(SCAN_TOOL, {"pattern": "*"})
    ok(outcome)  # the first page carries one key
    assert view(outcome)["incomplete"] is True
    assert tools.server.names().count("SCAN") == 50


@pytest.mark.parametrize("limit", [1, 7, 100])
def test_scan_reaching_the_limit_is_incomplete_and_exactly_limit_is_complete(
    monkeypatch, limit
):
    tools = make_tools(monkeypatch)
    tools.server.data = {f"k:{n:04d}": ("string", b"x") for n in range(limit + 5)}
    over = tools.run(SCAN_TOOL, {"pattern": "k:*", "limit": limit})
    ok(over)
    assert view(over)["incomplete"] is True
    assert len(rows(over)) <= limit
    tools.server.data = {f"k:{n:04d}": ("string", b"x") for n in range(limit)}
    exact = tools.run(SCAN_TOOL, {"pattern": "k:*", "limit": limit})
    ok(exact)
    assert view(exact)["incomplete"] is False
    assert view(exact)["backend_keys_returned"] == limit


def test_strings_are_read_with_getrange_0_8191_only(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(GET_VALUE_TOOL, {"key": "orders:1"})
    ok(outcome)
    assert "paid" in _text(outcome)
    reads = [c for c in tools.server.commands if shape(c) == ("GETRANGE", None)]
    assert reads == [["GETRANGE", "orders:1", "0", "8191"]]


def _collection(kind: str, n: int) -> Any:
    if kind == "list":
        return [f"item-{i}" for i in range(n)]
    if kind == "set":
        return [f"member-{i}" for i in range(n)]
    if kind == "hash":
        return {f"field-{i}": f"value-{i}" for i in range(n)}
    if kind == "zset":
        return [(f"member-{i}", i) for i in range(n)]
    return [(f"{1763812800000 + i}-0", {"event": f"e-{i}"}) for i in range(n)]


READ_SHAPE = {
    "list": lambda argv, limit: (
        argv[0].upper() == "LRANGE" and argv[2:] == ["0", str(limit)]
    ),
    "zset": lambda argv, limit: (
        argv[0].upper() == "ZRANGE"
        and argv[2:4] == ["0", str(limit)]
        and argv[4].upper() == "WITHSCORES"
    ),
    "stream": lambda argv, limit: (
        argv[0].upper() == "XREVRANGE"
        and argv[2:4] == ["+", "-"]
        and argv[4].upper() == "COUNT"
        and argv[5] == str(limit + 1)
    ),
    "hash": lambda argv, limit: argv[0].upper() == "HSCAN",
    "set": lambda argv, limit: argv[0].upper() == "SSCAN",
}


@pytest.mark.parametrize("kind", ["list", "set", "hash", "zset", "stream"])
def test_collections_are_read_with_limit_plus_one(monkeypatch, kind):
    limit = 20
    tools = make_tools(monkeypatch)
    tools.server.data["big"] = (kind, _collection(kind, limit + 30))
    tools.server.data["exact"] = (kind, _collection(kind, limit))
    over = tools.run(GET_VALUE_TOOL, {"key": "big", "limit": limit})
    ok(over)
    assert view(over)["incomplete"] is True
    reads = [c for c in tools.server.tool_commands() if shape(c) != ("TYPE", None)]
    assert reads and all(READ_SHAPE[kind](c, limit) for c in reads), reads
    exact = tools.run(GET_VALUE_TOOL, {"key": "exact", "limit": limit})
    ok(exact)
    assert view(exact)["incomplete"] is False
    unit = {"hash": "fields", "stream": "entries"}.get(kind, "elements")
    assert view(exact)[f"backend_{unit}_returned"] == limit
    names = set(tools.server.names())
    assert not names & {"GET", "HGETALL", "SMEMBERS", "KEYS", "SORT", "GETEX", "GETDEL"}


def test_get_value_default_limit_is_100(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.data["big"] = ("list", _collection("list", 300))
    outcome = tools.run(GET_VALUE_TOOL, {"key": "big"})
    ok(outcome)
    assert view(outcome)["backend_elements_returned"] == 100
    assert ["LRANGE", "big", "0", "100"] in tools.server.commands


def test_slowlog_default_and_maximum(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(SLOWLOG_TOOL, {}))
    ok(tools.run(SLOWLOG_TOOL, {"limit": 128}))
    counts = [
        int(c[2]) for c in tools.server.commands if shape(c) == ("SLOWLOG", "GET")
    ]
    assert counts[0] in (50, 51) and counts[1] in (128, 129), counts


def test_slowlog_times_are_iso_utc_and_views_are_current_state(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(SLOWLOG_TOOL, {})
    ok(outcome)
    # 1763812800 = 2025-11-22T12:00:00Z
    assert re.search(r"2025-11-22T12:00:00(\.0+)?(Z|\+00:00)", _text(outcome))
    v = view(outcome)
    assert v.get("source_start_at") is None and v.get("source_end_at") is None
    assert "read at query time" in " ".join(notes(outcome)).lower()
    assert v["source"] == "otel-demo"


@pytest.mark.parametrize("tool", sorted(ROW_UNIT))
def test_views_name_their_row_unit(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool)
    ok(outcome)
    assert f"backend_{ROW_UNIT[tool]}_returned" in view(outcome), view(outcome)


def test_the_selector_carries_the_endpoint_and_no_credential(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(INFO_TOOL))
    [request] = tools.requests
    selector = dict(request.selector)
    assert f"127.0.0.1:{tools.server.port}" in selector["redis_endpoint"]
    flat = json.dumps(selector)
    assert REDIS_PASSWORD not in flat and REDIS_USERNAME not in flat


def test_info_returns_only_the_chosen_sections(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.run(INFO_TOOL, {"sections": "memory,clients"})
    ok(outcome)
    text = _text(outcome)
    assert "used_memory" in text and "connected_clients" in text
    assert "evicted_keys" not in text
    for argv in tools.server.commands:
        if shape(argv) == ("INFO", None):
            assert {a.lower() for a in argv[1:]} <= {"memory", "clients", "server"}


# == 5. the account check (R4) ==========================================================

BAD_ACCOUNTS = {
    "+@all": getuser_reply(LAB_GRANT + " +@all"),
    "+@write": getuser_reply(LAB_GRANT + " +@write"),
    "+@keyspace": getuser_reply(LAB_GRANT + " +@keyspace"),
    "+@connection": getuser_reply(LAB_GRANT + " +@connection"),
    "allcommands": getuser_reply(flags=["on", "allcommands"]),
    "selector": getuser_reply(
        selectors=[["commands", "-@all +set", "keys", "~*", "channels", ""]]
    ),
    "+config|set": getuser_reply(LAB_GRANT + " +config|set"),
    "+acl|setuser": getuser_reply(LAB_GRANT + " +acl|setuser"),
    "+slowlog|reset": getuser_reply(LAB_GRANT + " +slowlog|reset"),
    "+flushall": getuser_reply(LAB_GRANT + " +flushall"),
    "unparsable-string": "not a getuser reply",
    "unparsable-no-commands": ["flags", ["on"], "keys", "~*"],
    "unparsable-odd": ["flags", ["on"], "commands"],
}
#: The rule a refusal names, where the case has one.
NAMED = {
    "+@all": "+@all",
    "+@write": "+@write",
    "+@keyspace": "+@keyspace",
    "+@connection": "+@connection",
    "allcommands": "allcommands",
    "+config|set": "+config|set",
    "+acl|setuser": "+acl|setuser",
    "+slowlog|reset": "+slowlog|reset",
}


def _refused_before_tool(tools, outcome, code: str) -> None:
    forbidden(outcome, code)
    assert tools.server.tool_commands() == [], tools.server.commands
    text = _text(outcome) + repr(outcome)
    for leaked in (PASSWORD_HASH, GETUSER_MARKER, "+client|setname +config|get"):
        assert leaked not in text, leaked


@pytest.mark.parametrize("tool", REDIS_TOOLS)
def test_the_lab_grant_passes_the_account_check(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    ok(tools.run(tool))


@pytest.mark.parametrize("case", sorted(BAD_ACCOUNTS))
def test_other_accounts_are_refused_before_any_tool_command(monkeypatch, case):
    tools = make_tools(monkeypatch)
    tools.server.getuser = BAD_ACCOUNTS[case]
    outcome = tools.run(GET_VALUE_TOOL)
    _refused_before_tool(tools, outcome, "REDIS_ACCOUNT_NOT_READ_ONLY")
    if case in NAMED:
        assert NAMED[case] in " ".join(notes(outcome)), notes(outcome)


def test_a_server_without_acl_is_unsupported(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.errors["ACL"] = "ERR unknown command 'ACL', with args beginning with:"
    outcome = tools.run(SCAN_TOOL)
    _refused_before_tool(tools, outcome, "REDIS_ACCOUNT_UNSUPPORTED")


def test_the_account_is_checked_on_every_call(monkeypatch):
    tools = make_tools(monkeypatch)
    ok(tools.run(INFO_TOOL))
    tools.server.getuser = BAD_ACCOUNTS["+@write"]
    before = len(tools.server.tool_commands())
    outcome = tools.run(INFO_TOOL)
    forbidden(outcome, "REDIS_ACCOUNT_NOT_READ_ONLY")
    assert len(tools.server.tool_commands()) == before


def test_account_problem_is_a_module_level_check():
    from opspilot.tools.redis_inspect import account_problem

    # ASSUMPTION: the reply is passed decoded (str), as the RESP2 array.
    assert account_problem(getuser_reply()) is None
    for case in ("+@all", "allcommands", "selector", "unparsable-string"):
        problem = account_problem(BAD_ACCOUNTS[case])
        assert isinstance(problem, str) and problem, case


# == 6. redaction (R9) ==================================================================

DENYLISTED = {
    "requirepass": "",
    "masterauth": CANARY + "a",
    "masteruser": CANARY + "b",
    "tls-key-file-pass": CANARY + "c",
    "tls-client-key-file-pass": CANARY + "d",
    "x-secret-setting": CANARY + "e",
    "x-auth-thing": CANARY + "f",
}


def _config_values(outcome) -> dict[str, Any]:
    """Parameter -> shown value, from either ``{name: value}`` rows or
    ``{name|parameter: n, value: v}`` rows."""
    found: dict[str, Any] = {}
    for row in rows(outcome):
        row = {k: v for k, v in row.items() if k != "_truncated_fields"}
        name = row.get("name", row.get("parameter"))
        if isinstance(name, str) and "value" in row:
            found[name] = row["value"]
        else:
            found.update(row)
    return found


def test_config_get_always_hides_denylisted_names_even_when_empty(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.config.update(DENYLISTED)
    outcome = tools.run(CONFIG_TOOL, {"pattern": "*"})
    ok(outcome)
    values = _config_values(outcome)
    for name in DENYLISTED:
        assert values[name] == "[REDACTED_CREDENTIAL]", (name, values.get(name))
    assert values["maxmemory-policy"] == "noeviction"
    seen = everything_seen(outcome, tools.sinks[-1])
    assert CANARY not in seen


def test_credential_named_hash_fields_and_string_keys_are_redacted(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.data["user:1"] = (
        "hash",
        {"name": "alice", "password": CANARY + "1", "api_token": CANARY + "2"},
    )
    tools.server.data["svc:password"] = ("string", (CANARY + "3").encode())
    hashed = tools.run(GET_VALUE_TOOL, {"key": "user:1"})
    ok(hashed)
    assert "alice" in _text(hashed)
    string = tools.run(GET_VALUE_TOOL, {"key": "svc:password"})
    ok(string)
    for outcome, sink in ((hashed, tools.sinks[0]), (string, tools.sinks[1])):
        assert CANARY not in everything_seen(outcome, sink)


def test_slowlog_arguments_after_credential_commands_are_redacted(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.slowlog = [
        [5, 1763812800, 900, ["AUTH", "admin", CANARY + "1"], "10.0.0.7:1", ""],
        [4, 1763812800, 900, ["HELLO", "3", "AUTH", "u", CANARY + "2"], "10.0.0.7:2", ""],
        [3, 1763812800, 900, ["MIGRATE", "h", "6379", "", "0", "5000", "AUTH2", "u", CANARY + "3",
                              "KEYS", "k"], "10.0.0.7:3", ""],
        [2, 1763812800, 900, ["ACL", "SETUSER", "u", ">" + CANARY + "4"], "10.0.0.7:4", ""],
        [1, 1763812800, 900, ["GET", "orders:42"], "10.0.0.7:5", "checkout"],
    ]  # fmt: skip
    outcome = tools.run(SLOWLOG_TOOL, {})
    ok(outcome)
    assert "orders:42" in _text(outcome)
    assert CANARY not in everything_seen(outcome, tools.sinks[-1])


# == 7. caps (R8) =======================================================================


def test_long_strings_are_cut_at_2000_and_listed(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.data["long"] = ("string", b"L" * 5000)
    outcome = tools.run(GET_VALUE_TOOL, {"key": "long"})
    ok(outcome)
    text = _text(outcome)
    assert "L" * 2000 in text and "L" * 2001 not in text
    assert "_truncated_fields" in text


def test_non_utf8_values_are_shown_as_hex(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.data["bin"] = ("string", b"\xff\xfeok")
    outcome = tools.run(GET_VALUE_TOOL, {"key": "bin"})
    ok(outcome)
    assert re.search(r"0x(fffe6f6b|FFFE6F6B)", _text(outcome)), _text(outcome)


def test_config_output_is_capped_at_500_rows(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.config = {f"p-{n:04d}": "1" for n in range(600)}
    outcome = tools.run(CONFIG_TOOL, {"pattern": "*"})
    ok(outcome)
    assert len(_config_values(outcome)) <= 500
    assert view(outcome)["incomplete"] is True


def test_a_raw_result_over_2_mib_is_refused_whole(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.data["huge"] = ("list", ["z" * 5000 for _ in range(600)])
    outcome = tools.run(GET_VALUE_TOOL, {"key": "huge", "limit": 500})
    _status(outcome, "error", "RESULT_TOO_LARGE")
    assert view(outcome)["content"] is None


def test_a_view_over_25k_tokens_is_refused_whole(monkeypatch):
    words = " ".join(f"word{n} checkout latency spike" for n in range(60))
    assert len(words) < 2000
    tools = make_tools(monkeypatch)
    tools.server.data["wordy"] = ("list", [words for _ in range(500)])
    outcome = tools.run(GET_VALUE_TOOL, {"key": "wordy", "limit": 500})
    _status(outcome, "error", "RESULT_TOO_LARGE")
    assert view(outcome)["content"] is None


# == 8. error mapping (R14) and time (R7) ===============================================

SERVER_ERRORS = [
    ("NOPERM User r773 has no permissions to run the 'scan' command", "SCAN",
     "error", "SOURCE_FORBIDDEN", "REDIS_FORBIDDEN"),
    ("LOADING Redis is loading the dataset in memory", "SCAN",
     "error", "SOURCE_UNAVAILABLE", None),
    ("BUSY Redis is busy running a script.", "SCAN",
     "error", "SOURCE_UNAVAILABLE", None),
    ("MOVED 3999 127.0.0.1:6381", "SCAN",
     "error", "INVALID_PARAMS", "REDIS_CLUSTER_UNSUPPORTED"),
    ("ASK 3999 127.0.0.1:6381", "SCAN",
     "error", "INVALID_PARAMS", "REDIS_CLUSTER_UNSUPPORTED"),
    ("ERR something unexpected", "SCAN", "error", "SOURCE_ERROR", None),
    ("WRONGPASS invalid username-password pair", "AUTH",
     "error", "SOURCE_FORBIDDEN", "REDIS_AUTH_FAILED"),
]  # fmt: skip


@pytest.mark.parametrize(("text", "on", "status", "reason", "code"), SERVER_ERRORS)
def test_server_errors_are_classified_and_never_echoed(
    monkeypatch, text, on, status, reason, code
):
    tools = make_tools(monkeypatch)
    tools.server.errors[on] = f"{text} {CANARY}"
    outcome = tools.run(SCAN_TOOL)
    _status(outcome, status, reason, code)
    assert CANARY not in _text(outcome)
    assert len(tools.server.connections) == 1  # no retry
    assert tools.server.names().count(on) == 1


def test_noauth_is_auth_required(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.error_after_auth = f"NOAUTH Authentication required. {CANARY}"
    outcome = tools.run(INFO_TOOL)
    _status(outcome, "error", "SOURCE_FORBIDDEN", "REDIS_AUTH_REQUIRED")
    assert CANARY not in _text(outcome)
    assert tools.server.tool_commands() == []


def test_a_wrong_password_is_auth_failed_and_sends_nothing_else(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.password = "something-" + "else773"
    outcome = tools.run(INFO_TOOL)
    _status(outcome, "error", "SOURCE_FORBIDDEN", "REDIS_AUTH_FAILED")
    assert tools.server.tool_commands() == []
    assert REDIS_PASSWORD not in _text(outcome)


def test_a_refused_port_is_source_unavailable(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.env["OPSPILOT_REDIS_URL"] = f"redis://127.0.0.1:{free_port()}"
    outcome = tools.run(INFO_TOOL)
    _status(outcome, "error", "SOURCE_UNAVAILABLE")


def test_a_connection_reset_before_the_deadline_is_source_unavailable(monkeypatch):
    tools = make_tools(monkeypatch)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def reset():
        conn, _ = listener.accept()
        conn.close()

    threading.Thread(target=reset, daemon=True).start()
    tools.env["OPSPILOT_REDIS_URL"] = f"redis://127.0.0.1:{port}"
    outcome = tools.run(INFO_TOOL)
    _status(outcome, "error", "SOURCE_UNAVAILABLE")
    listener.close()


@pytest.mark.parametrize("tool", [KEY_INFO_TOOL, GET_VALUE_TOOL])
def test_a_missing_key_is_no_data_with_key_not_found(monkeypatch, tool):
    tools = make_tools(monkeypatch)
    outcome = tools.run(tool, {"key": "no-such-key"})
    assert outcome.status == "no_data", view(outcome)
    assert any(n.startswith("KEY_NOT_FOUND") for n in notes(outcome)), notes(outcome)


def test_the_watchdog_closes_the_socket_at_the_deadline(monkeypatch):
    tools = make_tools(monkeypatch, request_timeout_seconds=3.0)
    tools.server.stall_on = {"SCAN"}
    started = time.monotonic()
    outcome = tools.run(SCAN_TOOL)
    elapsed = time.monotonic() - started
    _status(outcome, "timeout", "TOOL_TIMEOUT")
    assert elapsed < 8, elapsed
    assert tools.server.all_closed(seconds=5)


def _connect_timeouts(monkeypatch) -> list[float | None]:
    """The socket timeout in force at each ``connect``.

    # ASSUMPTION: the standard-library client connects through
    # ``socket.socket.connect`` (directly or via ``socket.create_connection``)
    # with the connect timeout set on the socket.
    """
    import socket

    seen: list[float | None] = []
    original = socket.socket.connect

    def recording(self, address):
        seen.append(self.gettimeout())
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", recording)
    return seen


@pytest.mark.parametrize(("budget", "low", "high"), [(30.0, 1.0, 10.0), (5.0, 1.0, 3.0), (2.5, 1.0, 1.0)])  # fmt: skip
def test_the_connect_timeout_is_bounded(monkeypatch, budget, low, high):
    tools = make_tools(monkeypatch, request_timeout_seconds=budget)
    seen = _connect_timeouts(monkeypatch)
    ok(tools.run(INFO_TOOL))
    assert seen, "no connect observed"
    timeout = seen[-1]
    assert timeout is not None and low - 0.05 <= timeout <= high + 0.05, timeout


# == 9. configuration (R6, R10) =========================================================

BAD_CONFIG = [
    ({"OPSPILOT_REDIS_USERNAME": ""}, "REDIS_CONFIG_MISSING"),
    ({"OPSPILOT_REDIS_PASSWORD": ""}, "REDIS_CONFIG_MISSING"),
    ({"OPSPILOT_REDIS_URL": "redis://u:p@127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://u@127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://:p@127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://127.0.0.1:6379/0"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://127.0.0.1:6379?db=1"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://127.0.0.1:6379,127.0.0.2:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "rediss://127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "valkey://127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "unix:///tmp/redis.sock"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "http://127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "127.0.0.1:6379"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "redis://127.0.0.1:notaport"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_DATABASE": "one"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_DATABASE": "1.5"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_TLS": "prefer"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_TLS": "true"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_TLS": "disable", "OPSPILOT_REDIS_CA_FILE": "<ca>"}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_TLS": "verify-full", "OPSPILOT_REDIS_CA_FILE": "/nonexistent/ca.pem"},
     "REDIS_CONFIG_INVALID"),
    # Settings without the address are never silently ignored.
    ({"OPSPILOT_REDIS_URL": ""}, "REDIS_CONFIG_INVALID"),
    ({"OPSPILOT_REDIS_URL": "", "OPSPILOT_REDIS_USERNAME": "", "OPSPILOT_REDIS_PASSWORD": ""},
     "REDIS_CONFIG_INVALID"),
]  # fmt: skip


def _with_ca(overrides: dict[str, str], tmp_path) -> dict[str, str]:
    if overrides.get("OPSPILOT_REDIS_CA_FILE") == "<ca>":
        ca = tmp_path / "redis-ca.pem"
        ca.write_text("synthetic ca placeholder\n")
        return {**overrides, "OPSPILOT_REDIS_CA_FILE": str(ca)}
    return overrides


@pytest.mark.parametrize(("overrides", "code"), BAD_CONFIG)
def test_worker_refuses_to_start_with_bad_redis_configuration(
    tmp_path, overrides, code
):
    require_jq()
    env = redis_env(WEB_REDIS_URL, **_with_ca(overrides, tmp_path))
    with pytest.raises(SystemExit, match=code):
        worker(env)


@pytest.mark.parametrize(
    "url",
    ["redis://127.0.0.1", "redis://localhost:6380", "redis://valkey.lab.svc:6379"],
)
def test_worker_accepts_plain_single_host_urls(url):
    require_jq()
    executor, _ = worker(redis_env(url))
    assert executor is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"OPSPILOT_REDIS_DATABASE": "0"},
        {"OPSPILOT_REDIS_DATABASE": "15"},
        {"OPSPILOT_REDIS_TLS": ""},
        {"OPSPILOT_REDIS_TLS": "verify-full"},
        {"OPSPILOT_REDIS_TLS": "require", "OPSPILOT_REDIS_CA_FILE": "<ca>"},
    ],
)
def test_worker_starts_with_valid_redis_configuration(tmp_path, overrides):
    require_jq()
    server = FakeRedis()
    url = {"OPSPILOT_REDIS_URL": server.url}
    executor, _ = worker(
        redis_env(server.url, **{**url, **_with_ca(overrides, tmp_path)})
    )
    assert executor is not None
    assert server.connections == []  # no connection at startup
    server.close()


def test_startup_codes_end_the_process():
    from opspilot.tools import profiles

    assert {
        "REDIS_CONFIG_MISSING",
        "REDIS_CONFIG_INVALID",
        "REDIS_TARGETS_OPSPILOT_DB",
    } <= set(profiles._STARTUP_CODES)


@pytest.mark.parametrize(
    ("url", "dsns", "refused"),
    [
        ("redis://127.0.0.1:5432", {"OPSPILOT_WORKER_DSN": "postgresql://o@localhost:5432/opspilot"}, True),
        ("redis://localhost:5433", {"OPSPILOT_DSN": "host=127.0.0.1 port=5433 dbname=opspilot user=o"}, True),
        ("redis://127.0.0.1:5432", {"OPSPILOT_WEB_DSN": "postgresql://o@127.0.0.1/opspilot"}, True),
        ("redis://127.0.0.1", {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1/opspilot"}, False),
        ("redis://127.0.0.1:6379", {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:5432/x"}, False),
    ],
)  # fmt: skip
def test_a_url_on_an_opspilot_database_is_refused(monkeypatch, url, dsns, refused):
    require_jq()
    for name, value in dsns.items():
        monkeypatch.setenv(name, value)  # ASSUMPTION (as C6): read from either
    env = {**redis_env(url), **dsns}
    if refused:
        with pytest.raises(SystemExit, match="REDIS_TARGETS_OPSPILOT_DB"):
            worker(env)
    else:
        executor, _ = worker(env)
        assert executor is not None


def test_lab_gate_requires_a_lab_redis_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "LANGSMITH_PROJECT": "opspilot-lab-r773",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa773",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab in (
        "redis://127.0.0.1:6379",
        "redis://localhost",
        "redis://valkey.lab.svc:6379",
    ):
        assert check_lab_target({**env, "OPSPILOT_REDIS_URL": lab}) == (), lab
    for remote in ("redis://cache.example.invalid:6379", "redis://10.0.0.5:6379",
                   "redis://127.attacker.example"):  # fmt: skip
        assert "REDIS_NOT_LAB" in check_lab_target(
            {**env, "OPSPILOT_REDIS_URL": remote}
        )


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


def test_the_web_process_reads_only_the_redis_address():
    env = _TrackingEnv({**_web(), **REDIS_SETTINGS})
    profile = select_profile(env)
    face = profile.face(FakeClock(start=PIN_NOW))
    face.evidence_context(RUN_ID)
    profile.versions()
    assert set(REDIS_TOOLS) <= {s["function"]["name"] for s in face.tool_schemas}
    assert not env.read & set(REDIS_SETTINGS), env.read
    assert not env.iterated


def test_the_entrypoint_imports_redis_settings_and_rejects_shell_text(tmp_path):
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_REDIS_URL=redis://127.0.0.1:10379\n"
        "OPSPILOT_REDIS_DATABASE=2\n"
        f"OPSPILOT_REDIS_USERNAME={REDIS_USERNAME}\n"
        f"OPSPILOT_REDIS_PASSWORD='{REDIS_PASSWORD}'\n"
        "OPSPILOT_REDIS_TLS=disable\n"
        "OPSPILOT_REDISX=not-a-setting\n"
        "OPSPILOT_REDIS_$(touch pwned)=x\n"
        "OPSPILOT_REDIS_`touch pwned2`=x\n"
    )
    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "docker-entrypoint.sh"), "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(worker_env)},
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env["OPSPILOT_REDIS_URL"] == "redis://127.0.0.1:10379"
    assert env["OPSPILOT_REDIS_DATABASE"] == "2"
    assert env["OPSPILOT_REDIS_USERNAME"] == REDIS_USERNAME
    assert env["OPSPILOT_REDIS_PASSWORD"] == REDIS_PASSWORD
    assert env["OPSPILOT_REDIS_TLS"] == "disable"
    assert "OPSPILOT_REDISX" not in env
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


def test_credentials_come_only_from_the_redis_settings(monkeypatch):
    tools = make_tools(
        monkeypatch,
        OPSPILOT_SQL_URL="postgresql://127.0.0.1:15432/app",
        OPSPILOT_SQL_USERNAME="c6tsqluser",
        OPSPILOT_SQL_PASSWORD="c6t-sql-" + "bbbb773",
    )
    ok(tools.run(INFO_TOOL))
    [auth] = [c for c in tools.server.commands if shape(c) == ("AUTH", None)]
    assert auth == ["AUTH", REDIS_USERNAME, REDIS_PASSWORD]


def test_an_unconfigured_worker_has_no_redis_tool(monkeypatch):
    require_jq()
    server = FakeRedis()
    executor, _ = worker(redis_env(None))
    from tests.c6c_mongo_support import call

    for tool in REDIS_TOOLS:
        outcome = executor.execute(call(tool, MINIMAL[tool]))
        assert outcome.status in ("error", "denied"), (tool, view(outcome))
    assert server.connections == []
    server.close()
