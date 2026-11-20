"""Implementer real-database tests for tool expansion C6c (#570): the six
``mongodb_*`` tools against a real MongoDB 7.0.17 through the public tool
path. No model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), which
starts ``c6-mongo`` and hands over the root account; skipped everywhere
else. The fixtures, not the product, write: root builds the ``c6c_impl``
database, synthetic collections and the accounts (C6-21 grants, created in
``admin``); the tool uses the read-only account except where a test
configures another one on purpose. The test database runs the profiler
(level 2), which is how a command is shown to have reached the server or
not.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from opspilot.tools import mongodb_query as mq
from opspilot.tools.executor import TransportRequest, TransportTimeout
from opspilot.tools.registry import ToolContractError
from tests.test_mongodb_query import _execute, _view
from tests.test_tool_expansion_c5a_contract import FRAME, _base_env

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("C6_MONGO_PORT", "53206"))
ADMIN = os.environ.get("C6_MONGO_ADMIN_USER", "root")
ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "")
UNREACHABLE = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))

DB = "c6c_impl"
OTHER_DB = "c6c_impl_other"
RO = "c6c_impl_ro"
PASSWORD = "ro" + "only" + "570c"
SECRET = "plain-" + "secret-" + "value-570c"
LONG = "L" * 5000


def _client(user: str = ADMIN, password: str = ADMIN_PASSWORD) -> Any:
    from pymongo import MongoClient

    return MongoClient(
        host=HOST,
        port=PORT,
        username=user,
        password=password,
        authSource="admin",
        directConnection=True,
        serverSelectionTimeoutMS=10_000,
    )


@pytest.fixture(scope="module", autouse=True)
def seeded() -> Iterator[None]:
    client = _client()
    admin = client["admin"]
    for user in (RO, "c6c_impl_rw", "c6c_impl_custom"):
        try:
            admin.command("dropUser", user)
        except Exception:
            pass
    try:
        admin.command("dropRole", "c6c_impl_role")
    except Exception:
        pass
    client.drop_database(DB)
    client.drop_database(OTHER_DB)
    db = client[DB]
    db.orders.insert_many(
        [
            {
                "_id": 1,
                "status": "paid",
                "note": LONG,
                "password": SECRET,
                "api_token": SECRET,
            },
            {
                "_id": 2,
                "status": "new",
                "note": "short",
                "password": SECRET,
                "api_token": SECRET,
            },
        ]
    )
    db.orders.create_index("status")
    db.big.insert_many([{"_id": i, "v": i % 7} for i in range(1, 601)])
    db.create_collection(
        "v_orders", viewOn="orders", pipeline=[{"$project": {"status": 1}}]
    )
    client[OTHER_DB].secrets.insert_one({"_id": 1, "x": 1})
    admin.command(
        "createUser",
        RO,
        pwd=PASSWORD,
        roles=[{"role": "read", "db": DB}, {"role": "clusterMonitor", "db": "admin"}],
    )
    admin.command(
        "createUser",
        "c6c_impl_rw",
        pwd=PASSWORD,
        roles=[{"role": "readWrite", "db": DB}],
    )
    admin.command(
        "createRole",
        "c6c_impl_role",
        privileges=[{"resource": {"db": DB, "collection": ""}, "actions": ["find"]}],
        roles=[],
    )
    admin.command(
        "createUser",
        "c6c_impl_custom",
        pwd=PASSWORD,
        roles=[{"role": "c6c_impl_role", "db": "admin"}],
    )
    db.command("profile", 2)
    client.close()
    yield


@pytest.fixture(autouse=True)
def ca_file(tmp_path, monkeypatch) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))


def _env(
    *, user: str = RO, password: str = PASSWORD, port: int = PORT
) -> dict[str, str]:
    return {
        **_base_env(),
        mq.URL_ENV: f"mongodb://{HOST}:{port}",
        mq.DATABASE_ENV: DB,
        mq.USERNAME_ENV: user,
        mq.PASSWORD_ENV: password,
        mq.AUTH_SOURCE_ENV: "admin",
        mq.TLS_ENV: "disable",
    }


def _call(tool: str, params: dict[str, object], **env: Any):
    return _execute(tool, params, _env(**env))


def _ok(outcome) -> dict[str, Any]:
    view = _view(outcome)
    assert outcome.status == "ok", view
    return view


def _marker() -> str:
    return "c6cimpl" + uuid.uuid4().hex[:12]


def _profiled(marker: str) -> list[dict[str, Any]]:
    client = _client()
    try:
        return [
            entry
            for entry in client[DB]["system.profile"].find({})
            if marker in json.dumps(entry.get("command", {}), default=str)
        ]
    finally:
        client.close()


def _count(collection: str = "orders") -> int:
    client = _client()
    try:
        return int(client[DB][collection].count_documents({}))
    finally:
        client.close()


# -- ① every tool -------------------------------------------------------------------


def test_six_tools_return_real_data() -> None:
    rows = _ok(
        _call(
            mq.FIND_TOOL, {"collection": "orders", "filter": "{}", "sort": '{"_id": 1}'}
        )
    )["content"]
    assert [r["status"] for r in rows] == ["paid", "new"]
    agg = _ok(
        _call(
            mq.AGGREGATE_TOOL,
            {
                "collection": "big",
                "pipeline": '[{"$group": {"_id": "$v", "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}]',
            },
        )
    )["content"]
    assert len(agg) == 7
    names = {r["name"] for r in _ok(_call(mq.LIST_TOOL, {}))["content"]}
    assert {"orders", "big", "v_orders"} <= names
    schema = _ok(_call(mq.SCHEMA_TOOL, {"collection": "orders"}))
    assert {r["field"] for r in schema["content"]} >= {"_id", "status", "note"}
    assert any(index["name"] == "status_1" for index in schema["indexes"])
    assert schema["estimated_document_count"] == 2
    status = _ok(_call(mq.STATUS_TOOL, {"sections": "connections,opcounters"}))[
        "content"
    ][0]
    assert status["version"].startswith("7.0") and "connections" in status
    ops = _call(mq.CURRENT_OP_TOOL, {"active_only": False})
    assert ops.status in ("ok", "no_data"), _view(ops)


# -- ③ ④ limits, cuts, redaction ---------------------------------------------------


def test_limit_long_values_and_redaction() -> None:
    view = _ok(_call(mq.FIND_TOOL, {"collection": "big", "filter": "{}", "limit": 100}))
    assert len(view["content"]) == 100 and view["incomplete_reason"]
    view = _ok(_call(mq.FIND_TOOL, {"collection": "orders", "filter": '{"_id": 1}'}))
    assert len(view["content"][0]["note"]) == mq.MAX_VALUE_CHARS
    assert SECRET not in json.dumps(view)


# -- ⑤ content check on a real server ---------------------------------------------


def test_refused_pipelines_send_nothing() -> None:
    marker = _marker()
    before = _count()
    for tool, params in (
        (
            mq.AGGREGATE_TOOL,
            {
                "collection": "orders",
                "pipeline": f'[{{"$match": {{"m": "{marker}"}}}}, {{"$out": "copy"}}]',
            },
        ),
        (
            mq.AGGREGATE_TOOL,
            {
                "collection": "orders",
                "pipeline": f'[{{"$facet": {{"a": [{{"$merge": "copy"}}]}}}}, {{"$match": {{"m": "{marker}"}}}}]',
            },
        ),
        (
            mq.FIND_TOOL,
            {
                "collection": "orders",
                "filter": f'{{"$where": "true", "m": "{marker}"}}',
            },
        ),
        (
            mq.AGGREGATE_TOOL,
            {
                "collection": "orders",
                "pipeline": f'[{{"$lookup": {{"from": {{"db": "{OTHER_DB}", "coll": "secrets"}}, "localField": "_id", "foreignField": "_id", "as": "x"}}}}, {{"$match": {{"m": "{marker}"}}}}]',
            },
        ),
        (
            mq.AGGREGATE_TOOL,
            {
                "collection": "orders",
                "pipeline": f'[{{"$collStats": {{"count": {{}}}}}}, {{"$match": {{"m": "{marker}"}}}}]',
            },
        ),
    ):
        outcome = _call(tool, params)
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
    assert _count() == before
    client = _client()
    try:
        assert "copy" not in client[DB].list_collection_names()
    finally:
        client.close()
    assert _profiled(marker) == []


# -- ⑥ the command surface -----------------------------------------------------------


def test_only_the_fixed_commands_reach_the_server(monkeypatch) -> None:
    monkeypatch.setattr(mq, "account_problem", lambda status: None)
    marker = _marker()
    outcome = _call(
        mq.AGGREGATE_TOOL,
        {"collection": "orders", "pipeline": f'[{{"$match": {{"m": "{marker}"}}}}]'},
        user=ADMIN,
        password=ADMIN_PASSWORD,
    )
    assert outcome.status == "no_data", _view(outcome)
    entries = _profiled(marker)
    assert [e["op"] for e in entries] == ["command"], entries
    assert "aggregate" in entries[0]["command"]


# -- ⑦ the read-only account alone ---------------------------------------------------


def test_read_only_account_cannot_write_directly() -> None:
    from pymongo.errors import OperationFailure

    client = _client(RO, PASSWORD)
    db = client[DB]
    before = _count()
    try:
        for action in (
            lambda: db.orders.insert_one({"_id": 99}),
            lambda: db.orders.update_one({"_id": 1}, {"$set": {"status": "x"}}),
            lambda: db.orders.delete_one({"_id": 1}),
            lambda: db.orders.drop(),
            lambda: list(db.orders.aggregate([{"$out": "copy2"}])),
        ):
            with pytest.raises(OperationFailure) as caught:
                action()
            assert caught.value.code == 13
    finally:
        client.close()
    assert _count() == before


# -- ⑧ timeout -----------------------------------------------------------------------


def _request(params: dict[str, object]) -> TransportRequest:
    return TransportRequest(
        operation_id="c6cimpl",
        source="otel-demo",
        verb="query",
        endpoint="",
        selector={},
        params=params,
        window=FRAME,
        timeout_seconds=5.0,
        max_result_bytes=mq.READ_LIMIT_BYTES,
        credential_ref=mq.CREDENTIAL_REF,
        tool=mq.AGGREGATE_TOOL,
    )


def test_heavy_pipeline_times_out_and_is_gone() -> None:
    config = mq.MongoConfig(
        url=f"mongodb://{HOST}:{PORT}",
        database=DB,
        username=RO,
        password=PASSWORD,
        tls="disable",
    )
    transport = mq.MongoTransport(config, request_timeout_seconds=5.0)
    pipeline = (
        '[{"$project": {"x": {"$range": [0, 200000]}}}, {"$unwind": "$x"}, '
        '{"$group": {"_id": {"$mod": ["$x", 7]}, "n": {"$sum": 1}}}]'
    )
    started = time.monotonic()
    try:
        response = transport.fetch(
            _request({"collection": "big", "pipeline": pipeline})
        )
    except TransportTimeout:
        pass
    else:
        pytest.fail(f"no timeout: {response.source_status} {response.view_fields}")
    assert time.monotonic() - started < 12
    client = _client()
    try:
        end = time.monotonic() + 10
        running = 1
        while time.monotonic() < end:
            ops = list(
                client.admin.aggregate(
                    [
                        {"$currentOp": {"allUsers": True}},
                        {"$match": {"appName": "opspilot-mongodb-tool"}},
                    ]
                )
            )
            running = len([op for op in ops if op.get("op") == "command"])
            if running == 0:
                break
            time.sleep(0.5)
        assert running == 0
    finally:
        client.close()


# -- ⑨ account check -------------------------------------------------------------------


@pytest.mark.parametrize("user", [ADMIN, "c6c_impl_rw", "c6c_impl_custom"])
def test_over_privileged_accounts_refused(user: str) -> None:
    marker = _marker()
    password = ADMIN_PASSWORD if user == ADMIN else PASSWORD
    outcome = _call(
        mq.FIND_TOOL,
        {"collection": "orders", "filter": f'{{"m": "{marker}"}}'},
        user=user,
        password=password,
    )
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "MONGODB_ACCOUNT_NOT_READ_ONLY" in json.dumps(_view(outcome))
    assert _profiled(marker) == []
    marker = _marker()
    _call(mq.FIND_TOOL, {"collection": "orders", "filter": f'{{"m": "{marker}"}}'})
    assert _profiled(marker), "the read-only account's find was not profiled"


# -- ⑩ OpsPilot's own database --------------------------------------------------------


def test_opspilot_dsn_on_the_same_address() -> None:
    env = _env()
    env["OPSPILOT_WORKER_DSN"] = f"postgresql://w:x@localhost:{PORT}/opspilot"
    with pytest.raises(ToolContractError, match="MONGODB_TARGETS_OPSPILOT_DB"):
        mq.MongoConfig.from_env(env)


# -- ⑪ error classes --------------------------------------------------------------------


def test_error_classes() -> None:
    bad = _call(
        mq.FIND_TOOL, {"collection": "orders", "filter": '{"a": {"$nosuch": 1}}'}
    )
    assert bad.reason == "INVALID_PARAMS", _view(bad)
    wrong = _call(
        mq.FIND_TOOL,
        {"collection": "orders", "filter": "{}"},
        password="wrong" + "570c",
    )
    assert wrong.reason == "SOURCE_FORBIDDEN", _view(wrong)
    down = _call(
        mq.FIND_TOOL, {"collection": "orders", "filter": "{}"}, port=UNREACHABLE
    )
    assert down.reason == "SOURCE_UNAVAILABLE", _view(down)
    empty = _call(mq.FIND_TOOL, {"collection": "orders", "filter": '{"_id": 0}'})
    assert empty.status == "no_data", _view(empty)
