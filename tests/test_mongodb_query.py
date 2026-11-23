"""Implementer unit tests for the MongoDB tools (C6c, #570). The independent
contract tests are the acceptance suite; these pin the implementer's own
reversible choices through the public profile path with a recording fake
connector. No database, no model. Credentials are low-entropy placeholders.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools import mongodb_query as mq
from opspilot.tools import otel_demo_ext as ext
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import ToolContractError
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease
from tests.test_tool_expansion_c5a_contract import FRAME, _base_env

PASSWORD = "readonly" + "570c"
URL = "mongodb://127.0.0.1:57017"
NESTED_SECRET = "plain-" + "nested-" + "570c"

READ_ONLY_STATUS = {
    "authInfo": {
        "authenticatedUsers": [{"user": "ro", "db": "admin"}],
        "authenticatedUserRoles": [
            {"role": "read", "db": "shop"},
            {"role": "clusterMonitor", "db": "admin"},
        ],
        "authenticatedUserPrivileges": [
            {
                "resource": {"db": "shop", "collection": ""},
                "actions": ["find", "listCollections"],
            },
            {"resource": {"cluster": True}, "actions": ["serverStatus", "inprog"]},
        ],
    },
    "ok": 1,
}


class Script:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.status: Mapping[str, Any] = READ_ONLY_STATUS
        self.docs: list[dict[str, Any]] = [{"_id": 1, "status": "paid"}]
        self.fail: Exception | None = None

    def connector(self, config: mq.MongoConfig, timeout: float) -> Any:
        script = self
        assert config.password == PASSWORD

        class Conn:
            def command(
                self, database: str, spec: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                script.sent.append((database, dict(spec)))
                first = next(iter(spec))
                if first == "connectionStatus":
                    return script.status
                if script.fail is not None:
                    raise script.fail
                if first in ("find", "aggregate"):
                    limit = spec.get("limit") or spec["pipeline"][-1]["$limit"]
                    return {
                        "cursor": {"id": 0, "firstBatch": script.docs[:limit]},
                        "ok": 1,
                    }
                if first == "listCollections":
                    return {
                        "cursor": {
                            "id": 0,
                            "firstBatch": [
                                {"name": "b", "type": "collection"},
                                {"name": "a", "type": "view"},
                            ],
                        },
                        "ok": 1,
                    }
                if first == "listIndexes":
                    return {
                        "cursor": {
                            "id": 0,
                            "firstBatch": [{"name": "_id_", "key": {"_id": 1}}],
                        },
                        "ok": 1,
                    }
                if first == "count":
                    return {"n": 42, "ok": 1}
                if first == "serverStatus":
                    return {
                        "host": "h",
                        "version": "7.0.17",
                        "uptime": 5,
                        "connections": {"current": 3},
                        "wiredTiger": {"x": 1},
                        "ok": 1,
                    }
                return {"ok": 1}

            def cancel(self) -> None:
                script.sent.append(("", {"<cancel>": 1}))

            def close(self) -> None:
                script.sent.append(("", {"<close>": 1}))

        return Conn()


@pytest.fixture
def fake(monkeypatch, tmp_path) -> Script:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))
    script = Script()
    monkeypatch.setattr(mq, "default_connector", script.connector)
    return script


def _env(**extra: str) -> dict[str, str]:
    return {
        **_base_env(),
        mq.URL_ENV: URL,
        mq.DATABASE_ENV: "shop",
        mq.USERNAME_ENV: "ro",
        mq.PASSWORD_ENV: PASSWORD,
        mq.TLS_ENV: "disable",
        **extra,
    }


def _execute(tool: str, params: dict[str, object], env: dict[str, str] | None = None):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env or _env())
    lease = _lease()
    deadline = NOW + timedelta(seconds=600)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout slow?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input).execute(
        ToolRequest(
            step_id="c6c-step",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=params,
            window=FRAME.as_json(),
        )
    )


def _view(outcome) -> dict[str, Any]:
    assert outcome.model_view is not None
    return dict(outcome.model_view)


def _commands(script: Script) -> list[str]:
    return [next(iter(spec)) for _, spec in script.sent]


# -- content check -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"$where": "this.a > 1"}',
        '{"a": {"$elemMatch": {"$where": "1"}}}',
        '{"$expr": {"$function": {"body": "x", "args": [], "lang": "js"}}}',
        '{"a": {"$code": "function() {}"}}',
        '{"$and": [{"b": 1}, {"$where": "1"}]}',
    ],
)
def test_filter_refuses_javascript(text: str) -> None:
    problem = mq.query_problem(text)
    assert problem is not None and problem.startswith("QUERY_CONSTRUCT_NOT_ALLOWED:"), (
        problem
    )


@pytest.mark.parametrize(
    "text",
    [
        '[{"$out": "x"}]',
        '[{"$merge": {"into": "x"}}]',
        '[{"$facet": {"a": [{"$out": "x"}]}}]',
        '[{"$lookup": {"from": "b", "pipeline": [{"$merge": "x"}], "as": "c"}}]',
        '[{"$unionWith": {"coll": "b", "pipeline": [{"$out": "x"}]}}]',
        '[{"$lookup": {"from": {"db": "other", "coll": "b"}, "localField": "a", "foreignField": "b", "as": "c"}}]',
        '[{"$unionWith": {"coll": "b", "db": "other"}}]',
        '[{"$documents": [{"a": 1}]}]',
        '[{"$currentOp": {}}]',
        '[{"$collStats": {"count": {}}}]',
        '[{"$indexStats": {}}]',
        '[{"$group": {"_id": null, "x": {"$accumulator": {}}}}]',
        '[{"$match": {}, "$limit": 1}]',
    ],
)
def test_pipeline_refusals(text: str) -> None:
    problem = mq.pipeline_problem(text)
    assert problem is not None and problem.split(":")[0] in (
        "QUERY_CONSTRUCT_NOT_ALLOWED",
        "INVALID_QUERY",
    ), problem


@pytest.mark.parametrize(
    "text",
    [
        '[{"$match": {"status": "error"}}, {"$group": {"_id": "$svc", "n": {"$sum": 1}}}]',
        '[{"$lookup": {"from": "b", "localField": "a", "foreignField": "b", "as": "c"}}]',
        '[{"$unionWith": "b"}, {"$sort": {"a": -1}}]',
        '[{"$facet": {"x": [{"$count": "n"}], "y": [{"$limit": 1}]}}]',
        '[{"$match": {"ts": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}}]',
    ],
)
def test_pipeline_allows_reads(text: str) -> None:
    assert mq.pipeline_problem(text) is None


def test_json_errors() -> None:
    assert (mq.query_problem("{not json") or "").startswith("INVALID_JSON")
    assert (mq.query_problem("[1]") or "").startswith("INVALID_QUERY")
    assert (mq.pipeline_problem('{"a": 1}') or "").startswith("INVALID_QUERY")
    deep = "[" * 40 + "]" * 40
    assert (mq.pipeline_problem(deep) or "").startswith("INVALID_QUERY")


# -- account check -------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        {"authInfo": {"authenticatedUsers": [], "authenticatedUserRoles": []}},
        {
            "authInfo": {
                "authenticatedUsers": [{"user": "root"}],
                "authenticatedUserRoles": [{"role": "root", "db": "admin"}],
            }
        },
        {
            "authInfo": {
                "authenticatedUsers": [{"user": "u"}],
                "authenticatedUserRoles": [{"role": "readWrite", "db": "shop"}],
            }
        },
        {
            "authInfo": {
                "authenticatedUsers": [{"user": "u"}],
                "authenticatedUserRoles": [{"role": "myRole", "db": "shop"}],
            }
        },
        {
            "authInfo": {
                "authenticatedUsers": [{"user": "u"}],
                "authenticatedUserRoles": [{"role": "read", "db": "shop"}],
                "authenticatedUserPrivileges": [
                    {"resource": {}, "actions": ["find", "insert"]}
                ],
            }
        },
    ],
)
def test_account_check_refuses(status) -> None:
    problem = mq.account_problem(status)
    assert problem is not None and problem.startswith("MONGODB_ACCOUNT_NOT_READ_ONLY")


def test_account_check_allows_read_roles() -> None:
    assert mq.account_problem(READ_ONLY_STATUS) is None


def _reshaped(**auth_info: Any) -> dict[str, Any]:
    """READ_ONLY_STATUS with some authInfo fields replaced; ``None`` drops one."""
    info = dict(READ_ONLY_STATUS["authInfo"])
    for key, value in auth_info.items():
        if value is None:
            info.pop(key, None)
        else:
            info[key] = value
    return {"authInfo": info, "ok": 1.0}


@pytest.mark.parametrize(
    "status",
    [
        _reshaped(authenticatedUserRoles=None),
        _reshaped(authenticatedUserRoles=[]),
        _reshaped(authenticatedUserRoles={"role": "read", "db": "shop"}),
        _reshaped(authenticatedUserRoles=["read"]),
        _reshaped(authenticatedUserRoles=[{"db": "shop"}]),
        _reshaped(authenticatedUserPrivileges=None),
        _reshaped(authenticatedUserPrivileges={"actions": ["find"]}),
        _reshaped(authenticatedUserPrivileges=["find"]),
        _reshaped(authenticatedUserPrivileges=[{"resource": {}, "actions": "insert"}]),
        _reshaped(authenticatedUserPrivileges=[{"resource": {}}]),
        _reshaped(authenticatedUserPrivileges=[{"resource": {}, "actions": [["x"]]}]),
        _reshaped(authenticatedUsers={"user": "ro", "db": "admin"}),
        _reshaped(authenticatedUsers="ro"),
    ],
    ids=[
        "roles-missing",
        "roles-empty",
        "roles-dict",
        "role-not-object",
        "role-without-name",
        "privileges-missing",
        "privileges-dict",
        "privilege-not-object",
        "actions-not-list",
        "actions-missing",
        "action-not-string",
        "users-dict",
        "users-string",
    ],
)
def test_account_check_fails_closed_on_unexpected_shape(status) -> None:
    # #755: a connectionStatus reply without the MongoDB 7 shape is refused,
    # never read as "no bad role or action found"
    problem = mq.account_problem(status)
    assert problem is not None and problem.startswith("MONGODB_ACCOUNT_NOT_READ_ONLY")


# -- sequences --------------------------------------------------------------------------


def test_find_sequence_and_projection(fake) -> None:
    fake.docs = [
        {
            "_id": 1,
            "note": "x" * 5000,
            "at": datetime(2026, 11, 20, tzinfo=timezone.utc),
            "nested": {"password": NESTED_SECRET},
        },
        {"_id": 2},
        {"_id": 3},
    ]
    outcome = _execute(
        mq.FIND_TOOL,
        {"collection": "orders", "filter": "{}", "sort": '{"_id": 1}', "limit": 2},
    )
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert _commands(fake) == ["connectionStatus", "find", "<close>"]
    _, spec = fake.sent[1]
    assert (
        spec["limit"] == 3
        and spec["singleBatch"] is True
        and spec["comment"].startswith("opspilot-")
    )
    assert 25_000 <= spec["maxTimeMS"] <= 28_000
    rows = view["content"]
    assert len(rows) == 2 and view["incomplete_reason"]
    assert len(rows[0]["note"]) == mq.MAX_VALUE_CHARS and rows[0][
        "_truncated_fields"
    ] == ["note"]
    assert rows[0]["at"].startswith("2026-11-20")
    assert NESTED_SECRET not in json.dumps(view)


def test_aggregate_appends_limit(fake) -> None:
    outcome = _execute(
        mq.AGGREGATE_TOOL, {"collection": "orders", "pipeline": '[{"$match": {}}]'}
    )
    assert outcome.status == "ok", _view(outcome)
    _, spec = fake.sent[1]
    assert spec["pipeline"][-1] == {"$limit": mq.DEFAULT_LIMIT + 1}


def test_refused_before_connecting(fake) -> None:
    for tool, params in (
        (mq.FIND_TOOL, {"collection": "orders", "filter": '{"$where": "1"}'}),
        (mq.AGGREGATE_TOOL, {"collection": "orders", "pipeline": '[{"$out": "x"}]'}),
        (mq.FIND_TOOL, {"collection": "$bad", "filter": "{}"}),
        (mq.FIND_TOOL, {"collection": "orders", "filter": "{}", "limit": 501}),
        (mq.STATUS_TOOL, {"sections": "secrets"}),
    ):
        outcome = _execute(tool, params)
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
    assert fake.sent == []


def test_account_refusal_sends_no_tool_command(fake) -> None:
    fake.status = {
        "authInfo": {
            "authenticatedUsers": [{"user": "root"}],
            "authenticatedUserRoles": [{"role": "root", "db": "admin"}],
        }
    }
    outcome = _execute(mq.FIND_TOOL, {"collection": "orders", "filter": "{}"})
    assert outcome.reason == "SOURCE_FORBIDDEN"
    assert "MONGODB_ACCOUNT_NOT_READ_ONLY" in json.dumps(_view(outcome))
    assert _commands(fake) == ["connectionStatus", "<close>"]


def test_catalog_tools(fake) -> None:
    listed = _view(_execute(mq.LIST_TOOL, {}))
    assert [r["name"] for r in listed["content"]] == ["a", "b"]
    fake.sent.clear()
    fake.docs = [{"_id": 1, "a": {"b": 2}}, {"_id": 2, "a": "x"}]
    schema = _view(_execute(mq.SCHEMA_TOOL, {"collection": "orders"}))
    assert {"field": "a", "types": ["object", "string"]} in schema["content"]
    assert schema["estimated_document_count"] == 42
    assert _commands(fake) == [
        "connectionStatus",
        "find",
        "listIndexes",
        "count",
        "<close>",
    ]
    status = _view(_execute(mq.STATUS_TOOL, {}))
    assert "wiredTiger" not in status["content"][0]
    ops = _execute(mq.CURRENT_OP_TOOL, {"min_duration_ms": 100})
    assert ops.status in ("ok", "no_data"), _view(ops)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (2, "INVALID_PARAMS"),
        (13, "SOURCE_FORBIDDEN"),
        (18, "SOURCE_FORBIDDEN"),
        (50, "TOOL_TIMEOUT"),
        (8000, "SOURCE_ERROR"),
    ],
)
def test_operation_failures(fake, code, reason) -> None:
    from pymongo.errors import OperationFailure

    fake.fail = OperationFailure("bad thing", code=code)
    outcome = _execute(mq.FIND_TOOL, {"collection": "orders", "filter": "{}"})
    assert outcome.reason == reason, _view(outcome)


def test_unreachable(fake) -> None:
    from pymongo.errors import ServerSelectionTimeoutError

    fake.fail = ServerSelectionTimeoutError("no server")
    outcome = _execute(mq.FIND_TOOL, {"collection": "orders", "filter": "{}"})
    assert outcome.reason == "SOURCE_UNAVAILABLE", _view(outcome)


# -- configuration and profile -------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({mq.URL_ENV: URL}, "MONGODB_CONFIG_MISSING"),
        ({mq.DATABASE_ENV: "shop"}, "MONGODB_CONFIG_INVALID"),
        (
            {
                mq.URL_ENV: "mongodb+srv://c.example/x",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://u:p@h",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://h1,h2",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://h/shop",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://h/?authSource=admin",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://h",
                mq.DATABASE_ENV: "a.b",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://h",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
                mq.TLS_ENV: "prefer",
            },
            "MONGODB_CONFIG_INVALID",
        ),
        (
            {
                mq.URL_ENV: "mongodb://127.0.0.1:5432",
                mq.DATABASE_ENV: "s",
                mq.USERNAME_ENV: "u",
                mq.PASSWORD_ENV: "p",
                "OPSPILOT_WORKER_DSN": "host=localhost port=5432",
            },
            "MONGODB_TARGETS_OPSPILOT_DB",
        ),
    ],
)
def test_config_errors(env, code) -> None:
    with pytest.raises(ToolContractError, match=code):
        mq.MongoConfig.from_env(env)


def test_profile_and_lab_gate(fake) -> None:
    assert ext.tool_schema_revision() == "otel-demo-ext-d44b47f8618a"
    assert ext.MONGODB_TOOL_SCHEMA_REVISION != ext.TOOL_SCHEMA_REVISION
    profile = select_profile(_env())
    assert (
        profile.versions()["tool_schema_revision"] == ext.MONGODB_TOOL_SCHEMA_REVISION
    )
    names = [
        s["function"]["name"] for s in profile.face(FakeClock(start=NOW)).tool_schemas
    ]
    assert names[-6:] == list(mq.TOOL_NAMES)
    assert "MONGODB_NOT_LAB" in check_lab_target(
        {mq.URL_ENV: "mongodb://db.example.com"}
    )
    assert "MONGODB_NOT_LAB" not in check_lab_target({mq.URL_ENV: URL})


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1, "a": 2}',
        '{"a": {"$where": "1"}, "a": 1}',
        '{"$WHERE": "1"}',
        '{"a": {"$Code": "x"}}',
    ],
)
def test_rulings_refuse(text: str) -> None:
    problem = mq.query_problem(text)
    assert problem is not None and problem.split(":")[0] in (
        "INVALID_JSON",
        "QUERY_CONSTRUCT_NOT_ALLOWED",
    ), problem


def test_rulings_pipeline_case_and_depth() -> None:
    assert (mq.pipeline_problem('[{"$OUT": "x"}]') or "").startswith(
        "QUERY_CONSTRUCT_NOT_ALLOWED"
    )
    twenty = '{"a": ' * 20 + "1" + "}" * 20
    assert mq.query_problem(twenty) is None
    forty = '{"a": ' * 40 + "1" + "}" * 40
    assert (mq.query_problem(forty) or "").startswith("INVALID_QUERY")
