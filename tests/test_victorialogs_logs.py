"""Implementer unit tests for the VictoriaLogs tools (C5b, #569).

The independent contract tests are the acceptance suite; these pin the
implementer's own reversible choices (C5 record, C5b interface) through the
public profile path against a local ``http.server`` fake. No container, no
model. Synthetic credentials are low-entropy placeholders.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools import otel_demo_ext as ext
from opspilot.tools import victorialogs_logs as vl
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import ToolContractError
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease
from tests.test_tool_expansion_c5a_contract import FRAME, _base_env

TOKEN = "vlogs-ro-aaaaaaaa569"


class FakeVictoriaLogs:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answers: dict[str, tuple[int, bytes]] = {}

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return None

            def _serve(self) -> None:
                parts = urlsplit(self.path)
                fake.requests.append(
                    {
                        "method": self.command,
                        "path": parts.path,
                        "query": parse_qsl(parts.query, keep_blank_values=True),
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                status, body = fake.answers.get(parts.path, (404, b"not found"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Iterator[FakeVictoriaLogs]:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    server = FakeVictoriaLogs()
    server.start()
    yield server
    server.stop()


def _env(url: str, **extra: str) -> dict[str, str]:
    return {
        **_base_env(),
        vl.URL_ENV: url,
        vl.BEARER_TOKEN_ENV: TOKEN,
        **extra,
    }


def _execute(env: dict[str, str], tool: str, params: dict[str, object]):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    lease = _lease()
    deadline = NOW + timedelta(seconds=600)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    executor = factory(lease, model_input)
    return executor.execute(
        ToolRequest(
            step_id="c5b-step",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=params,
            window=FRAME.as_json(),
        )
    )


def _z(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# -- LogsQL scan -------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "error",
        '{namespace="app-1"} level:error',
        '"join" OR `union` "stream_context options(x)"',
        '_msg:"a | b" in(a, b)',
        "level:in(error, warn)",
        '~"5\\\\d\\\\d" | stats count() | sort by (count) desc',
        "_time:5m error | rename x as y | delete z",
        "foo#bar",
        'error AND "a b" OR "x y"',
        '_msg:"x | y" | stats count()',
    ],
)
def test_allowed_logsql(query):
    assert vl.logsql_problem(query) is None


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("error | join by (x) (foo)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("error | UNION (foo)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "options(ignore_global_time_filter=true) error",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("error | stream_context before 5", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("x:in(error | fields x)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("_stream_id:in(_time:1d error)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("x:contains_any (a | b)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("x:in((a:b)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("'single' quoted", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        # A # is not a comment to the scan: what follows is still checked.
        ("error # | join (x)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ('"unterminated', "INVALID_QUERY"),
        ("`unterminated", "INVALID_QUERY"),
        # A quoted token right after | (PR #731 review): refused, whatever
        # VictoriaLogs would make of it.
        ('error | "join" x', "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "error |`options`(ignore_global_time_filter=true)",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("error | 'x'", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ('error |   "stats" count()', "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("a\tb", "INVALID_QUERY"),
        ("x" * 4001, "INVALID_QUERY"),
        ("   ", "INVALID_QUERY"),
    ],
)
def test_refused_logsql(query, code):
    problem = vl.logsql_problem(query)
    assert problem is not None and problem[0] == code


# -- configuration and profile -------------------------------------------------------


def test_revisions_without_victorialogs_are_unchanged():
    revision = ext.tool_schema_revision
    assert revision() == "otel-demo-ext-e00b0e7d95c9"
    assert revision(elasticsearch=True) == "otel-demo-ext-bd3bc6f91e49"
    assert revision(loki=True) == "otel-demo-ext-855bfed29802"
    assert revision(elasticsearch=True, loki=True) == "otel-demo-ext-b2f074df8640"
    assert ext.VICTORIALOGS_TOOL_SCHEMA_REVISION == revision(victorialogs=True)
    with_vl = {
        revision(elasticsearch=e, loki=lk, victorialogs=True)
        for e in (False, True)
        for lk in (False, True)
    }
    assert len(with_vl) == 4 and revision() not in with_vl


def test_face_lists_five_tools_after_loki_only_when_configured():
    clock = FakeClock(start=NOW)
    names = [
        s["function"]["name"]
        for s in ext.otel_demo_ext_face(
            clock, loki=True, victorialogs=True
        ).tool_schemas
    ]
    assert names[-6:] == [
        "grafana_loki_query",
        vl.QUERY_TOOL,
        vl.STREAMS_TOOL,
        vl.FIELD_NAMES_TOOL,
        vl.FIELD_VALUES_TOOL,
        vl.HITS_TOOL,
    ]
    plain = ext.otel_demo_ext_face(clock).tool_schemas
    assert not {s["function"]["name"] for s in plain} & vl.TOOL_NAMES


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({vl.URL_ENV: "http://127.0.0.1:9428"}, "VICTORIALOGS_CONFIG_MISSING"),
        ({vl.BEARER_TOKEN_ENV: TOKEN}, "VICTORIALOGS_CONFIG_INVALID"),
        (
            {
                vl.URL_ENV: "http://127.0.0.1:9428",
                vl.BEARER_TOKEN_ENV: TOKEN,
                vl.USERNAME_ENV: "u",
                vl.PASSWORD_ENV: "p",
            },
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {vl.URL_ENV: "http://127.0.0.1:9428?x=1", vl.BEARER_TOKEN_ENV: TOKEN},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {
                vl.URL_ENV: "http://127.0.0.1:9428",
                vl.BEARER_TOKEN_ENV: TOKEN,
                vl.ACCOUNT_ID_ENV: "4294967296",
            },
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {vl.URL_ENV: "http://127.0.0.1:9428", vl.USERNAME_ENV: "u"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
    ],
)
def test_configuration_codes(env, code):
    with pytest.raises(ToolContractError, match=code):
        vl.VictoriaLogsConfig.from_env(env)


def test_lab_gate():
    assert "VICTORIALOGS_NOT_LAB" in check_lab_target(
        {vl.URL_ENV: "http://logs.example.com:9428"}
    )
    assert "VICTORIALOGS_NOT_LAB" not in check_lab_target(
        {vl.URL_ENV: "http://127.0.0.1:9428"}
    )


# -- requests and views ---------------------------------------------------------------


def _jsonl(*rows: dict[str, Any]) -> bytes:
    return b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"


def test_query_sends_fixed_get_and_orders_newest_first(fake):
    t1 = _z(NOW - timedelta(minutes=40))
    t2 = _z(NOW - timedelta(minutes=5))
    fake.answers[vl.PATHS[vl.QUERY_TOOL]] = (
        200,
        _jsonl(
            {"_time": t1, "_msg": "old", "_stream": '{app="a"}'},
            {"_time": t2, "_msg": "new", "_stream": '{app="a"}'},
            {"_time": t1, "_msg": "L" * 2500},
        ),
    )
    outcome = _execute(
        _env(fake.url, **{vl.ACCOUNT_ID_ENV: "3"}),
        vl.QUERY_TOOL,
        {"query": "error", "limit": 2},
    )
    [request] = fake.requests
    assert request["method"] == "GET"
    assert request["path"] == "/select/logsql/query"
    assert dict(request["query"]) == {
        "query": "error",
        "start": _z(NOW - timedelta(hours=1)),
        "end": _z(NOW),
        "limit": "3",
    }
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["accountid"] == "3"
    assert "projectid" not in request["headers"]
    view = outcome.model_view
    assert outcome.status == "ok", view
    assert [row["_msg"][:3] for row in view["content"]] == ["new", "old"]
    assert view["incomplete_reason"].startswith("limit:")
    assert TOKEN not in json.dumps(view)


def test_metadata_tools_default_query_and_limit_plus_one(fake):
    fake.answers[vl.PATHS[vl.FIELD_VALUES_TOOL]] = (
        200,
        json.dumps(
            {"values": [{"value": "b", "hits": 1}, {"value": "a", "hits": 9}]}
        ).encode(),
    )
    outcome = _execute(
        _env(fake.url), vl.FIELD_VALUES_TOOL, {"field": "namespace", "limit": 5}
    )
    [request] = fake.requests
    assert [k for k, _ in request["query"]] == [
        "query",
        "field",
        "start",
        "end",
        "limit",
    ]
    assert dict(request["query"])["query"] == "*"
    assert dict(request["query"])["limit"] == "6"
    assert outcome.status == "ok", outcome.model_view
    assert outcome.model_view["content"] == [
        {"value": "a", "hits": 9},
        {"value": "b", "hits": 1},
    ]
    assert outcome.model_view["incomplete_reason"] is None


def test_hits_step_and_field_and_zero_series_dropped(fake):
    fake.answers[vl.PATHS[vl.HITS_TOOL]] = (
        200,
        json.dumps(
            {
                "hits": [
                    {
                        "fields": {"level": "error"},
                        "timestamps": [_z(NOW)],
                        "values": [4],
                        "total": 4,
                    },
                    {
                        "fields": {"level": "info"},
                        "timestamps": [],
                        "values": [],
                        "total": 0,
                    },
                ]
            }
        ).encode(),
    )
    outcome = _execute(
        _env(fake.url), vl.HITS_TOOL, {"query": "*", "step": "1m", "field": "level"}
    )
    [request] = fake.requests
    assert [k for k, _ in request["query"]] == [
        "query",
        "start",
        "end",
        "step",
        "field",
        "extra_filters",
    ]
    sent = dict(request["query"])
    # The window again as a filter: VictoriaLogs widens start/end to the step.
    assert sent["extra_filters"] == f"_time:[{sent['start']}, {sent['end']})"
    assert outcome.status == "ok", outcome.model_view
    assert [row["fields"] for row in outcome.model_view["content"]] == [
        {"level": "error"}
    ]


@pytest.mark.parametrize(
    ("tool", "params", "code"),
    [
        (vl.HITS_TOOL, {"query": "*", "step": "1s"}, "INVALID_STEP"),
        (vl.HITS_TOOL, {"query": "*", "step": "5d"}, "INVALID_STEP"),
        (vl.FIELD_VALUES_TOOL, {"field": "bad field"}, "INVALID_FIELD"),
        (vl.STREAMS_TOOL, {"limit": 501}, "INVALID_LIMIT"),
        (vl.QUERY_TOOL, {"query": ""}, "INVALID_QUERY"),
        (vl.QUERY_TOOL, {"query": "x | join (y)"}, "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            vl.QUERY_TOOL,
            {"query": "x", "start": _z(NOW - timedelta(days=2)), "end": _z(NOW)},
            "QUERY_OUT_OF_WINDOW",
        ),
    ],
)
def test_refusals_are_never_sent(fake, tool, params, code):
    outcome = _execute(_env(fake.url), tool, params)
    assert fake.requests == []
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.model_view["notes"][0].startswith(code)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(400, "INVALID_PARAMS"), (401, "SOURCE_FORBIDDEN"), (403, "SOURCE_FORBIDDEN")],
)
def test_error_classes(fake, status, reason):
    fake.answers[vl.PATHS[vl.QUERY_TOOL]] = (status, b"cannot parse query: oops")
    outcome = _execute(_env(fake.url), vl.QUERY_TOOL, {"query": "x"})
    assert (outcome.status, outcome.reason) == ("error", reason)


def test_empty_answer_is_no_data(fake):
    fake.answers[vl.PATHS[vl.STREAMS_TOOL]] = (200, b'{"values":[]}')
    outcome = _execute(_env(fake.url), vl.STREAMS_TOOL, {})
    assert outcome.status == "no_data", outcome.model_view
