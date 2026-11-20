"""Real-server integration tests for tool expansion C6c (#570): the six
MongoDB tools against a real MongoDB 7.0.17, through the public tool path.
No model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), after
the four C6a/C6b files and before the C6c implementer's file; skipped
elsewhere.

Written by the independent test author from the C6 task record
(「C6c 公开接口」「CI（写死，测试作者与实现者共用）」, assertions ①-⑪),
without reading the implementation. The fixtures, not the product, write:
the administrator (``root``) creates the database ``c6c_tests``, the
synthetic documents, the C6-21 read-only account ``c6c_tests_ro`` and the
deliberately over-privileged ``c6c_tests_*`` accounts, all in ``admin``.
The tool gets the read-only account except where an assertion is about the
administrator (⑥, ⑨) or an over-privileged account (⑨).

MongoDB has no read-only session: the identity layer (⑦), the account check
(⑨), the content check (⑤) and the fixed command face (⑥) carry the
read-only guarantee. Whether a command reached the server is read from the
profiler (``{"profile": 2}`` on ``c6c_tests`` and, when the server allows
it, ``admin``): ``system.profile`` entries with the tool's ``appName`` or a
marker placed in the filter/pipeline. Each "never arrived" assertion has a
positive control that the profiler did see a legal command.

CI environment (fixed by the record): ``C6_MONGO_PORT`` (53206),
``C6_MONGO_ADMIN_USER`` (``root``) with ``C6_ADMIN_PASSWORD``
(``changeme570``, CI-only low entropy), ``C6_MONGO_CONTAINER``
(``c6-mongo``), ``C6_DB_HOST`` (127.0.0.1), ``C6_UNREACHABLE_PORT`` (53209).
The tool reaches ``mongodb://<host>:<port>`` with ``AUTH_SOURCE=admin`` and
``TLS=disable``; the engineer side uses pymongo as ``root``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.profiles import PROFILE_ENV
from tests.integration.test_tool_expansion_c6a_sql import (
    _INDEX,
    FRAME,
    _assert,
    _eventually,
    _executor,
    _marker,
    _notes,
    _rows,
    _view,
)
from tests.test_m1_otel_demo_contract import TARGET_ID

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

FIND = "mongodb_find"
AGGREGATE = "mongodb_aggregate"
LIST_COLLECTIONS = "mongodb_list_collections"
SCHEMA = "mongodb_collection_schema"
SERVER_STATUS = "mongodb_server_status"
CURRENT_OP = "mongodb_current_op"

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("C6_MONGO_PORT", "53206"))
UNREACHABLE_PORT = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))
ADMIN = os.environ.get("C6_MONGO_ADMIN_USER", "root")
ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "changeme570")
CONTAINER = os.environ.get("C6_MONGO_CONTAINER", "c6-mongo")

DB = "c6c_tests"
PRIVATE_DB = "c6c_tests_private"
RO = "c6c_tests_ro"
RW = "c6c_tests_rw"
CUSTOM = "c6c_tests_custom"
CUSTOM_ROLE = "c6c_tests_custom_role"
RO_PASSWORD = "ro-" + "pw-570"  # low entropy, CI only
OTHER_PASSWORD = "bad-" + "pw-570"
CANARY = "cccccccc570"
APP_NAME = "opspilot-mongodb-tool"
ENGINEER_APP = "c6c-engineer"
BULK_DOCS = 600
LONG_CHARS = 5000
BIG_DOCS, BIG_CHARS = 500, 4500  # about 2.25 MB before the 2000-char cut
WORDY_DOCS = 500
COLLECTIONS = (
    "c6t_orders",
    "c6t_customers",
    "c6t_long",
    "c6t_big",
    "c6t_wordy",
    "c6t_empty",
)
#: Commands the driver itself sends while connecting or closing; never a
#: tool command.
DRIVER_INTERNAL = {
    "hello",
    "isMaster",
    "ismaster",
    "saslStart",
    "saslContinue",
    "authenticate",
    "endSessions",
    "ping",
}


def _words(n: int) -> str:
    return " ".join(f"word{n}-{k} checkout latency spike" for k in range(55))


# -- engineer side (pymongo as root; never the product) ----------------------------


def _client(user: str = ADMIN, password: str = ADMIN_PASSWORD):
    from pymongo import MongoClient

    return MongoClient(
        host=HOST,
        port=PORT,
        username=user,
        password=password,
        authSource="admin",
        directConnection=True,
        appname=ENGINEER_APP,
        serverSelectionTimeoutMS=20_000,
    )


_STATE: dict[str, Any] = {}


def _admin():
    if "client" not in _STATE:
        _STATE["client"] = _client()
    return _STATE["client"]


def _drop_user(name: str) -> None:
    from pymongo.errors import OperationFailure

    try:
        _admin().admin.command("dropUser", name)
    except OperationFailure:
        pass


def _enable_profiler(database: str) -> bool:
    from pymongo.errors import OperationFailure

    db = _admin()[database]
    try:
        db.command("profile", 0)
        db["system.profile"].drop()
        db.create_collection("system.profile", capped=True, size=32 * 1024 * 1024)
        db.command("profile", 2)
    except OperationFailure as error:
        _STATE.setdefault("profiler_errors", {})[database] = str(error)
        return False
    return True


def _setup() -> None:
    client = _admin()
    client.drop_database(DB)
    client.drop_database(PRIVATE_DB)
    db = client[DB]
    base = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    db.c6t_orders.insert_many(
        [
            {
                "_id": n,
                "order_no": f"o-{n:04d}",
                "customer_id": n % 3 + 1,
                "status": "paid" if n % 2 else "open",
                "total": n * 1.5,
                "at": base + timedelta(minutes=n),
                "address": {"city": f"city-{n % 5}"},
            }
            for n in range(BULK_DOCS)
        ]
    )
    db.c6t_orders.create_index([("order_no", 1)], unique=True, name="order_no_1")
    db.c6t_orders.create_index([("customer_id", 1), ("at", -1)], name="cust_at")
    db.c6t_customers.insert_many(
        [
            {
                "_id": n,
                "name": f"customer-{n}",
                "email": f"c{n}@example.invalid",
                "password": f"pw-{CANARY}-{n}",
                "api_token": f"tok-{CANARY}-{n}",
            }
            for n in (1, 2, 3)
        ]
    )
    db.c6t_long.insert_one({"_id": 1, "body": "x" * LONG_CHARS})
    db.c6t_big.insert_many(
        [{"_id": n, "body": "b" * BIG_CHARS} for n in range(BIG_DOCS)]
    )
    db.c6t_wordy.insert_many([{"_id": n, "note": _words(n)} for n in range(WORDY_DOCS)])
    db.create_collection("c6t_empty")
    db.command(
        "create",
        "c6t_paid_view",
        viewOn="c6t_orders",
        pipeline=[{"$match": {"status": "paid"}}],
    )
    client[PRIVATE_DB].c6t_secret.insert_one({"_id": 1, "secret": "private"})

    admin = client.admin
    for name in (RO, RW, CUSTOM):
        _drop_user(name)
    from pymongo.errors import OperationFailure

    try:
        admin.command("dropRole", CUSTOM_ROLE)
    except OperationFailure:
        pass
    # C6-21, verbatim from the record.
    admin.command(
        "createUser",
        RO,
        pwd=RO_PASSWORD,
        roles=[{"role": "read", "db": DB}, {"role": "clusterMonitor", "db": "admin"}],
    )
    admin.command(
        "createUser", RW, pwd=OTHER_PASSWORD, roles=[{"role": "readWrite", "db": DB}]
    )
    admin.command(
        "createRole",
        CUSTOM_ROLE,
        privileges=[
            {"resource": {"db": DB, "collection": ""}, "actions": ["find"]},
        ],
        roles=[],
    )
    admin.command(
        "createUser",
        CUSTOM,
        pwd=OTHER_PASSWORD,
        roles=[{"role": CUSTOM_ROLE, "db": "admin"}],
    )
    _STATE["profiled"] = {name: _enable_profiler(name) for name in (DB, "admin")}


@pytest.fixture(scope="module", autouse=True)
def _database():
    pytest.importorskip("pymongo")
    _setup()
    yield
    try:
        _admin().admin.command("profile", 0)
        _admin()[DB].command("profile", 0)
    finally:
        _admin().close()
        _STATE.clear()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    import re

    for name in list(os.environ):
        if name.startswith("OPSPILOT_MONGODB") or re.fullmatch(
            r"OPSPILOT_[A-Z0-9_]*DSN", name
        ):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c6c-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _now() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=1)


def _profile(since: datetime) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name, on in _STATE["profiled"].items():
        if on:
            entries.extend(
                _admin()[name]["system.profile"].find({"ts": {"$gte": since}})
            )
    return [e for e in entries if e.get("appName") != ENGINEER_APP]


def _saw(marker: str, since: datetime) -> bool:
    from bson import json_util

    return any(marker in json_util.dumps(e) for e in _profile(since))


def _after(marker: str, since: datetime) -> datetime:
    """Just after the last profiled entry carrying ``marker``: a window that
    starts here excludes the positive control's own commands."""
    from bson import json_util

    stamps = [e["ts"] for e in _profile(since) if marker in json_util.dumps(e)]
    assert stamps, marker
    return max(stamps) + timedelta(milliseconds=1)


def _tool_commands(since: datetime) -> list[str]:
    names = []
    for entry in _profile(since):
        if entry.get("appName") != APP_NAME:
            continue
        command = entry.get("command") or {}
        if command:
            names.append(next(iter(command)))
    return names


def _counts() -> dict[str, int]:
    db = _admin()[DB]
    return {name: db[name].count_documents({}) for name in COLLECTIONS}


def _collection_names() -> list[str]:
    return sorted(
        n for n in _admin()[DB].list_collection_names() if not n.startswith("system.")
    )


def _running(marker: str) -> int:
    from bson import json_util

    ops = _admin().admin.aggregate(
        [{"$currentOp": {"allUsers": True, "idleConnections": False}}]
    )
    return sum(
        1
        for op in ops
        if marker in json_util.dumps(op.get("command") or {})
        and op.get("appName") != ENGINEER_APP
    )


# -- the product path ---------------------------------------------------------------


def _env(
    k8s_ca: str,
    *,
    user: str = RO,
    password: str = RO_PASSWORD,
    database: str = DB,
    port: int = PORT,
    **extra: str,
) -> dict[str, str]:
    values = {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd570",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_MONGODB_URL": f"mongodb://{HOST}:{port}",
        "OPSPILOT_MONGODB_DATABASE": database,
        "OPSPILOT_MONGODB_USERNAME": user,
        "OPSPILOT_MONGODB_PASSWORD": password,
        "OPSPILOT_MONGODB_AUTH_SOURCE": "admin",
        "OPSPILOT_MONGODB_TLS": "disable",
    }
    values.update(extra)
    return {k: v for k, v in values.items() if v != ""}


class Tool:
    def __init__(self, env: dict[str, str]):
        self.env = env

    def run(self, tool: str, params: dict[str, object]):
        return _executor(self.env).execute(
            ToolRequest(
                step_id="c6c-live",
                tool_index=next(_INDEX),
                tool_name=tool,
                target_ref=TARGET_ID,
                params=params,
                window=FRAME.as_json(),
            )
        )

    def find(self, collection: str, filter: object = None, **params: object):
        text = filter if isinstance(filter, str) else json.dumps(filter or {})
        return self.run(FIND, {"collection": collection, "filter": text, **params})

    def aggregate(self, collection: str, pipeline: object, **params: object):
        text = pipeline if isinstance(pipeline, str) else json.dumps(pipeline)
        return self.run(
            AGGREGATE, {"collection": collection, "pipeline": text, **params}
        )


@pytest.fixture
def ro(k8s_ca) -> Tool:
    return Tool(_env(k8s_ca))


def _admin_tool(k8s_ca: str) -> Tool:
    return Tool(_env(k8s_ca, user=ADMIN, password=ADMIN_PASSWORD))


# == ① real data from all six tools ===================================================


def test_find_returns_real_documents(ro):
    outcome = ro.find("c6t_customers", {}, sort='{"_id": 1}')
    _assert(outcome, "ok", None)
    rows = _rows(outcome)
    assert [r["name"] for r in rows] == ["customer-1", "customer-2", "customer-3"]
    assert _view(outcome)["incomplete"] is False
    assert _view(outcome)["source_start_at"] is None


def test_aggregate_groups_real_documents(ro):
    outcome = ro.aggregate(
        "c6t_orders",
        [
            {"$match": {"status": "paid"}},
            {"$group": {"_id": "$customer_id", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ],
    )
    _assert(outcome, "ok", None)
    rows = _rows(outcome)
    assert [r["_id"] for r in rows] == [1, 2, 3]
    assert sum(r["n"] for r in rows) == BULK_DOCS // 2


def test_list_collections_includes_collections_and_the_view(ro):
    outcome = ro.run(LIST_COLLECTIONS, {})
    _assert(outcome, "ok", None)
    rows = _rows(outcome)
    names = [r["name"] for r in rows]
    assert names == sorted(names)
    by_name = {r["name"]: r["type"] for r in rows}
    for name in COLLECTIONS:
        assert by_name.get(name) == "collection", (name, by_name)
    assert by_name.get("c6t_paid_view") == "view"


def test_collection_schema_reports_fields_indexes_and_estimate(ro):
    outcome = ro.run(SCHEMA, {"collection": "c6t_orders"})
    _assert(outcome, "ok", None)
    fields = {r["field"] for r in _rows(outcome)}
    assert {
        "_id",
        "order_no",
        "customer_id",
        "status",
        "total",
        "at",
        "address.city",
    } <= fields
    view = _view(outcome)
    assert view["estimated_document_count"] == BULK_DOCS
    assert view["sample_size"] == 10
    indexes = {i["name"]: i for i in view["indexes"]}
    assert {"_id_", "order_no_1", "cust_at"} <= set(indexes)
    assert indexes["order_no_1"]["unique"] is True
    assert dict(indexes["cust_at"]["keys"]) == {"customer_id": 1, "at": -1}


def test_server_status_returns_the_chosen_sections(ro):
    outcome = ro.run(SERVER_STATUS, {"sections": "connections,opcounters"})
    _assert(outcome, "ok", None)
    [row] = _rows(outcome)
    assert str(row["version"]).startswith("7.0")
    assert row["uptime_seconds"] >= 0
    assert row["connections"]["current"] >= 1
    assert "opcounters" in row and "mem" not in row
    default = ro.run(SERVER_STATUS, {})
    _assert(default, "ok", None)
    [row] = _rows(default)
    for section in (
        "connections",
        "opcounters",
        "mem",
        "locks",
        "globalLock",
        "network",
    ):
        assert section in row, section
    assert "wiredTiger" not in row


def test_current_op_lists_real_operations(ro):
    outcome = ro.run(CURRENT_OP, {"active_only": False, "min_duration_ms": 0})
    _assert(outcome, "ok", None)
    rows = _rows(outcome)
    assert rows and all("opid" in r for r in rows)


# == ③ limits, cuts and whole refusals ================================================


def test_limit_truncates_and_marks_incomplete(ro):
    outcome = ro.find("c6t_orders", {}, projection='{"order_no": 1}', limit=500)
    _assert(outcome, "ok", None)
    assert len(_rows(outcome)) == 500
    assert _view(outcome)["incomplete"] is True
    outcome = ro.aggregate("c6t_orders", [{"$sort": {"_id": 1}}], limit=7)
    _assert(outcome, "ok", None)
    assert [r["_id"] for r in _rows(outcome)] == list(range(7))
    assert _view(outcome)["incomplete"] is True


def test_long_values_are_cut(ro):
    outcome = ro.find("c6t_long", {})
    _assert(outcome, "ok", None)
    [row] = _rows(outcome)
    assert len(row["body"]) <= 2000 + 64
    assert row["_truncated_fields"] == ["body"]


def test_raw_over_2_mib_and_oversized_views_are_refused_whole(ro):
    big = ro.find("c6t_big", {}, limit=500)
    _assert(big, "error", "RESULT_TOO_LARGE")
    assert _view(big)["content"] is None
    wordy = ro.find("c6t_wordy", {}, limit=500)
    _assert(wordy, "error", "RESULT_TOO_LARGE")
    assert _view(wordy)["content"] is None


# == ④ redaction ======================================================================


def test_password_and_token_fields_are_redacted(ro, record_property):
    outcome = ro.find("c6t_customers", {})
    _assert(outcome, "ok", None)
    text = json.dumps(_view(outcome))
    assert CANARY not in text
    assert "REDACTED" in text
    # Residual risk (PRODUCT-CONSTRAINTS:40): an alias is not recognised by
    # name. Recorded as evidence, not asserted.
    alias = ro.aggregate("c6t_customers", [{"$project": {"x": "$password"}}])
    record_property(
        "c6c_alias_redacted", CANARY not in json.dumps(_view(alias), default=str)
    )


# == ⑤ the content check on a real server: refused, never arrives, nothing changes ===


def _refused_constructs(m: str) -> dict[str, list[Any]]:
    match = {"$match": {"marker": m}}
    return {
        "out": [match, {"$out": "c6t_stolen"}],
        "merge": [match, {"$merge": {"into": "c6t_stolen"}}],
        "facet-out": [match, {"$facet": {"a": [{"$out": "c6t_stolen"}]}}],
        "facet-merge": [match, {"$facet": {"a": [{"$merge": "c6t_stolen"}]}}],
        "lookup-pipeline-out": [
            match,
            {"$lookup": {"from": "c6t_orders", "pipeline": [{"$out": "c6t_stolen"}], "as": "j"}},
        ],
        "lookup-pipeline-merge": [
            match,
            {"$lookup": {"from": "c6t_orders", "pipeline": [{"$merge": "c6t_stolen"}], "as": "j"}},
        ],
        "unionwith-pipeline-out": [
            match,
            {"$unionWith": {"coll": "c6t_orders", "pipeline": [{"$out": "c6t_stolen"}]}},
        ],
        "unionwith-pipeline-merge": [
            match,
            {"$unionWith": {"coll": "c6t_orders", "pipeline": [{"$merge": "c6t_stolen"}]}},
        ],
        "where": [{"$match": {"marker": m, "$where": "true"}}],
        "function": [
            match,
            {
                "$addFields": {
                    "x": {"$function": {"body": "function() { return 1; }", "args": [], "lang": "js"}}
                }
            },
        ],
        "accumulator": [
            match,
            {
                "$group": {
                    "_id": None,
                    "x": {
                        "$accumulator": {
                            "init": "function() { return 0; }",
                            "accumulate": "function(s) { return s; }",
                            "accumulateArgs": [],
                            "merge": "function(a, b) { return a; }",
                            "lang": "js",
                        }
                    },
                }
            },
        ],
        "cross-db-lookup": [
            match,
            {
                "$lookup": {
                    "from": {"db": PRIVATE_DB, "coll": "c6t_secret"},
                    "localField": "a",
                    "foreignField": "b",
                    "as": "j",
                }
            },
        ],
        "documents": [{"$documents": [{"marker": m}]}],
        "coll-stats": [{"$collStats": {"count": {}}}, match],
    }  # fmt: skip


def test_refused_constructs_never_arrive_and_change_nothing(ro, record_property):
    before_counts, before_names = _counts(), _collection_names()
    control = _marker("c5-control")
    since = _now()
    _assert(ro.find("c6t_orders", {"marker": control}), "no_data", "NO_DATA")
    assert _eventually(lambda: _saw(control, since), 10), (
        "the profiler does not record what the tool sends",
        _STATE.get("profiler_errors"),
    )
    hidden = _marker("c5-refused")
    since = _now()
    cases = _refused_constructs(hidden)
    for name, pipeline in cases.items():
        outcome = ro.aggregate("c6t_orders", pipeline)
        _assert(outcome, "error", "INVALID_PARAMS", "QUERY_CONSTRUCT_NOT_ALLOWED")
    for name, text in {
        "find-where": {"marker": hidden, "$where": "true"},
        "find-function": {
            "marker": hidden,
            "$expr": {"$function": {"body": "function() { return true; }", "args": [], "lang": "js"}},
        },
    }.items():  # fmt: skip
        outcome = ro.find("c6t_orders", text)
        _assert(outcome, "error", "INVALID_PARAMS", "QUERY_CONSTRUCT_NOT_ALLOWED")
    time.sleep(1.0)
    assert not _saw(hidden, since)
    assert _counts() == before_counts
    assert _collection_names() == before_names
    assert "c6t_stolen" not in _admin()[DB].list_collection_names()
    record_property("c6c_refused_constructs", ",".join(sorted(cases)))


# == ⑥ command face: with the account check skipped, root sends only its command =====


def test_command_face_sends_only_the_check_and_the_tool_command(
    k8s_ca, monkeypatch, record_property
):
    from opspilot.tools import mongodb_query

    # ASSUMPTION: the transport looks ``account_problem`` up on the module at
    # call time (the record names it as the seam integration tests patch).
    monkeypatch.setattr(mongodb_query, "account_problem", lambda status: None)
    marker = _marker("c6-face")
    since = _now()
    outcome = _admin_tool(k8s_ca).aggregate(
        "c6t_orders", [{"$match": {"marker": {"$ne": marker}}}, {"$count": "n"}]
    )
    _assert(outcome, "ok", None)
    assert _eventually(lambda: _saw(marker, since), 10), _STATE.get("profiler_errors")
    time.sleep(0.5)
    names = _tool_commands(since)
    record_property("c6c_profiled_tool_commands", ",".join(names))
    tool_names = [n for n in names if n not in DRIVER_INTERNAL]
    assert "aggregate" in tool_names, names
    assert set(tool_names) <= {"connectionStatus", "aggregate"}, names
    assert tool_names.count("aggregate") == 1, names
    # MongoDB 7.0.17 does not profile connectionStatus on admin; the check
    # command itself is pinned by the contract tests from the sent specs.


# == ⑦ identity: the read-only account cannot write when connected directly =========


def test_read_only_account_cannot_write_when_connected_directly(record_property):
    from pymongo.errors import OperationFailure

    before_counts, before_names = _counts(), _collection_names()
    client = _client(RO, RO_PASSWORD)
    db = client[DB]
    attempts = {
        "insertOne": lambda: db.c6t_orders.insert_one({"_id": "c6c-write"}),
        "updateOne": lambda: db.c6t_orders.update_one({"_id": 1}, {"$set": {"total": -1}}),
        "deleteOne": lambda: db.c6t_orders.delete_one({"_id": 1}),
        "drop": lambda: db.c6t_orders.drop(),
        "aggregate-out": lambda: list(
            db.c6t_orders.aggregate([{"$match": {}}, {"$out": "c6t_stolen"}])
        ),
        "createCollection": lambda: db.create_collection("c6t_new"),
        "createIndex": lambda: db.c6t_orders.create_index("total"),
    }  # fmt: skip
    seen = []
    try:
        for name, attempt in attempts.items():
            with pytest.raises(OperationFailure) as caught:
                attempt()
            assert caught.value.code == 13, (name, caught.value.code, caught.value)
            seen.append(f"{name}:{caught.value.code}")
    finally:
        client.close()
    record_property("c6c_direct_write_errors", ",".join(seen))
    assert _counts() == before_counts
    assert _collection_names() == before_names


# == ⑧ timeout: maxTimeMS ends the operation on the server ============================


def test_a_heavy_pipeline_times_out_and_is_gone_from_the_server(k8s_ca, monkeypatch):
    from tests.c6c_mongo_support import set_request_timeout

    set_request_timeout(monkeypatch, 5.0)
    marker = _marker("c8-timeout")
    heavy = [
        {"$match": {"marker": {"$ne": marker}}},
        {"$project": {"r": {"$range": [0, 100_000]}}},
        {"$unwind": "$r"},
        {"$group": {"_id": {"$mod": ["$r", 7]}, "n": {"$sum": {"$sqrt": "$r"}}}},
    ]
    started = time.monotonic()
    outcome = Tool(_env(k8s_ca)).aggregate("c6t_orders", heavy)
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), _view(
        outcome
    )
    assert elapsed < 15, elapsed
    end = time.monotonic() + 10
    while _running(marker) and time.monotonic() < end:
        time.sleep(0.5)
    assert _running(marker) == 0


# == ⑨ C6-U1: root, readWrite and custom-role accounts are refused ====================


@pytest.mark.parametrize(
    ("user", "password"),
    [(ADMIN, ADMIN_PASSWORD), (RW, OTHER_PASSWORD), (CUSTOM, OTHER_PASSWORD)],
    ids=["root", "readWrite", "custom-role"],
)
def test_not_read_only_accounts_are_refused_and_the_command_never_arrives(
    k8s_ca, user, password
):
    control = _marker("c9-control")
    since = _now()
    _assert(
        Tool(_env(k8s_ca)).find("c6t_orders", {"marker": control}), "no_data", "NO_DATA"
    )
    assert _eventually(lambda: _saw(control, since), 10), _STATE.get("profiler_errors")
    hidden = _marker(f"c9-{user}")
    # The window starts after the read-only control's find, so only this
    # account's commands can appear in it.
    since = _after(control, since)
    tool = Tool(_env(k8s_ca, user=user, password=password))
    for name, params in {
        FIND: {"collection": "c6t_orders", "filter": json.dumps({"marker": hidden})},
        AGGREGATE: {
            "collection": "c6t_orders",
            "pipeline": json.dumps([{"$match": {"marker": hidden}}]),
        },
        LIST_COLLECTIONS: {},
        SCHEMA: {"collection": "c6t_orders"},
        SERVER_STATUS: {},
        CURRENT_OP: {},
    }.items():
        outcome = tool.run(name, params)
        _assert(outcome, "error", "SOURCE_FORBIDDEN", "MONGODB_ACCOUNT_NOT_READ_ONLY")
    time.sleep(1.0)
    assert not _saw(hidden, since)
    names = [n for n in _tool_commands(since) if n not in DRIVER_INTERNAL]
    assert set(names) <= {"connectionStatus"}, names


def test_the_c6_21_account_passes_the_check(ro):
    # Positive control for ⑨: the record's grant list is accepted as is.
    _assert(ro.find("c6t_orders", {"_id": 1}), "ok", None)
    _assert(ro.run(SERVER_STATUS, {}), "ok", None)


# == ⑩ C6-8: OpsPilot's own databases =================================================


def test_a_worker_dsn_on_the_same_address_stops_the_worker(k8s_ca, monkeypatch):
    dsn = f"postgresql://opspilot:x@localhost:{PORT}/opspilot"
    monkeypatch.setenv("OPSPILOT_WORKER_DSN", dsn)
    env = {**_env(k8s_ca), "OPSPILOT_WORKER_DSN": dsn}
    with pytest.raises(SystemExit, match="MONGODB_TARGETS_OPSPILOT_DB"):
        _executor(env)


# == ⑪ error classification from real responses =======================================


def test_real_error_responses_are_classified(ro, k8s_ca):
    unknown = ro.find("c6t_orders", {"total": {"$nosuchop": 1}})
    _assert(unknown, "error", "INVALID_PARAMS")
    assert "MONGODB_INVALID" in " ".join(_notes(unknown))
    wrong = Tool(_env(k8s_ca, password="wrong-" + "pw-570")).find("c6t_orders", {})
    _assert(wrong, "error", "SOURCE_FORBIDDEN")
    assert "wrong-pw-570" not in json.dumps(_view(wrong), default=str)
    denied = Tool(_env(k8s_ca, database=PRIVATE_DB)).find("c6t_secret", {})
    _assert(denied, "error", "SOURCE_FORBIDDEN")
    assert "private" not in json.dumps(_rows_or_none(denied), default=str)
    gone = Tool(_env(k8s_ca, port=UNREACHABLE_PORT)).find("c6t_orders", {})
    _assert(gone, "error", "SOURCE_UNAVAILABLE")
    empty = ro.find("c6t_empty", {})
    _assert(empty, "no_data", "NO_DATA")
    none = ro.find("c6t_orders", {"status": "no-such-status"})
    _assert(none, "no_data", "NO_DATA")


def _rows_or_none(outcome) -> Any:
    return _view(outcome).get("content")


def test_views_never_carry_the_credentials(k8s_ca):
    outcomes = [
        Tool(_env(k8s_ca)).find("c6t_orders", {"_id": 1}),
        Tool(_env(k8s_ca)).find("c6t_orders", "{bad json"),
        _admin_tool(k8s_ca).find("c6t_orders", {"_id": 1}),
        Tool(_env(k8s_ca, password="wrong-" + "pw-570")).find("c6t_orders", {}),
    ]
    rendered = json.dumps([o.model_view for o in outcomes], default=str)
    assert RO_PASSWORD not in rendered
    assert ADMIN_PASSWORD not in rendered
    assert "wrong-pw-570" not in rendered
