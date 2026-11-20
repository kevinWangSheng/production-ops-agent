"""Real-backend integration tests for tool expansion C5b (#569), R8-25 and
r10 C5-16 ... C5-20 (VictoriaLogs part): the five ``victorialogs_*`` tools
against a real VictoriaLogs v1.x behind the nginx read-only gateway of C5-U1
(``scripts/loki_readonly_gateway.py --backend victorialogs``), through the
public tool path. No model is called.

Runs only in the CI job ``log-backends`` (``OPSPILOT_LOG_BACKENDS=1``), which
starts the containers; skipped everywhere else.

The fixtures, not the product, write: synthetic entries relative to now go
straight to VictoriaLogs' own port through ``/insert/jsonline`` (the
engineer side, C5-18); the tool only sees the gateway and its read-only
account. The gateway logs method, path and status of every request, which is
how a refusal is shown never to have been sent.

The subquery probe (C5-7) asks VictoriaLogs directly whether a subquery
honours ``start``/``end``; its answer is recorded (printed, and written to
``C5B_PROBE_OUT`` when set) as evidence for a later decision, while the tool
keeps refusing the construct either way.
"""

from __future__ import annotations

import itertools
import json
import os
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV
from tests.integration.test_tool_expansion_c5_log_backends import (
    _basic,
    _executor,
    _http,
    _rows,
    _text,
    _view,
)
from tests.test_m1_otel_demo_contract import TARGET_ID

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_LOG_BACKENDS") != "1",
    reason="real VictoriaLogs runs only in the CI log-backends job",
)

DIRECT = os.environ.get("C5B_VLOGS_URL", "http://127.0.0.1:53113")
GATEWAY = os.environ.get("C5B_GATEWAY_URL", "http://127.0.0.1:53103")
GATEWAY_DENY = os.environ.get("C5B_GATEWAY_DENY_URL", "http://127.0.0.1:53104")
GATEWAY_LOG = Path(
    os.environ.get("C5B_GATEWAY_ACCESS_LOG", "/tmp/c5b-gateway-logs/access.log")
)
PROBE_OUT = os.environ.get("C5B_PROBE_OUT")
# Low-entropy CI-only account of the gateway (C5-18).
READER = "reader"
READER_PASSWORD = os.environ.get("C5_GATEWAY_PASSWORD", "changeme569")
OTHER_ACCOUNT = "7"
CANARY = "aaaaaaaa569"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
# The extension profile's longest window is 24 h (OBSERVATION_SECONDS).
FRAME = Window(NOW - timedelta(hours=24), NOW)

QUERY = "victorialogs_query"
STREAMS = "victorialogs_streams"
FIELD_NAMES = "victorialogs_field_names"
FIELD_VALUES = "victorialogs_field_values"
HITS = "victorialogs_hits"

APP = {"namespace": "c5i", "app": "c5i-app", "pod": "checkout-7b9899-x2km9"}
ENTRIES = {
    "a1": (NOW - timedelta(minutes=50), "error", "payment declined"),
    "a2": (NOW - timedelta(minutes=30), "warn", "payment slow"),
    "a3": (NOW - timedelta(minutes=10), "info", "cart updated"),
    "a-long": (NOW - timedelta(minutes=20), "info", "L" * 5000),
    "a-secret": (NOW - timedelta(minutes=15), "info", f"login password={CANARY} ok"),
    "a-old": (NOW - timedelta(hours=2, minutes=30), "error", "old outage"),
    "a-30h": (NOW - timedelta(hours=30), "error", "day old outage"),
    "a-future": (NOW + timedelta(minutes=3), "error", "future event"),
}
IN_DEFAULT_WINDOW = {"a1", "a2", "a3", "a-long", "a-secret"}
BULK = 600
BIG = 500
BIG_CHARS = 4500


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _tenant(account: str | None) -> dict[str, str]:
    return {} if account is None else {"AccountID": account, "ProjectID": "0"}


def _insert(rows: list[dict[str, Any]], *, account: str | None = None) -> None:
    query = urllib.parse.urlencode(
        {
            "_stream_fields": "namespace,app",
            "_msg_field": "_msg",
            "_time_field": "_time",
        }
    )
    body = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    status, answer = _http(
        "POST",
        f"{DIRECT}/insert/jsonline?{query}",
        body,
        headers={"Content-Type": "application/stream+json", **_tenant(account)},
    )
    assert status in (200, 204), (status, answer)


def _direct(
    path: str, params: Mapping[str, str], *, account: str | None = None
) -> tuple[int, Any]:
    return _http(
        "GET",
        f"{DIRECT}{path}?{urllib.parse.urlencode(dict(params))}",
        headers=_tenant(account),
    )


def _count(query: str, *, account: str | None = None) -> int:
    status, body = _direct(
        "/select/logsql/query",
        {
            "query": f"{query} | stats count() as n",
            "start": _z(NOW - timedelta(hours=40)),
            "end": _z(NOW + timedelta(minutes=10)),
        },
        account=account,
    )
    assert status == 200, (status, body)
    if isinstance(body, dict):
        return int(body["n"])
    if body is None:
        return 0
    lines = [json.loads(line) for line in str(body).splitlines() if line.strip()]
    return int(lines[0]["n"]) if lines else 0


def _big(n: int) -> str:
    words, seed = [], n * 7919
    while sum(len(word) + 1 for word in words) < BIG_CHARS:
        seed = (seed * 1103515245 + 12345) % (1 << 31)
        words.append(f"{seed:08x}")
    return " ".join(words)[:BIG_CHARS]


@pytest.fixture(scope="module")
def seeded() -> Iterator[None]:
    deadline = time.monotonic() + 120
    while True:
        try:
            if _http("GET", f"{DIRECT}/health")[0] == 200:
                break
        except OSError:
            pass
        assert time.monotonic() < deadline, "VictoriaLogs never became healthy"
        time.sleep(1)
    _insert(
        [
            {"_time": _z(when), "_msg": msg, "level": level, "key": key, **APP}
            for key, (when, level, msg) in ENTRIES.items()
        ]
    )
    _insert(
        [
            {
                "_time": _z(NOW - timedelta(minutes=12)),
                "_msg": "rare field line",
                "namespace": "c5i",
                "app": "c5i-rare",
                "opspilot_c5_shard": "s1",
            }
        ]
    )
    bulk_start = NOW - timedelta(minutes=59)
    _insert(
        [
            {
                "_time": _z(bulk_start + timedelta(seconds=5 * n)),
                "_msg": f"bulk line {n:03d}",
                "namespace": "c5i",
                "app": "c5i-bulk",
            }
            for n in range(BULK)
        ]
    )
    big_start = NOW - timedelta(minutes=58)
    _insert(
        [
            {
                "_time": _z(big_start + timedelta(seconds=6 * n)),
                "_msg": _big(n),
                "namespace": "c5i",
                "app": "c5i-big",
            }
            for n in range(BIG)
        ]
    )
    # The subquery probe (C5-7): trace T2 is inside the window without the
    # marker and 30 h before with it.
    _insert(
        [
            {
                "_time": _z(NOW - timedelta(minutes=8)),
                "_msg": "probe inside",
                "namespace": "c5i",
                "app": "c5i-probe",
                "trace": "T2",
            },
            {
                "_time": _z(NOW - timedelta(hours=30)),
                "_msg": "probe outside",
                "namespace": "c5i",
                "app": "c5i-probe",
                "trace": "T2",
                "marker": "old",
            },
        ]
    )
    _insert(
        [
            {
                "_time": _z(NOW - timedelta(minutes=25)),
                "_msg": "account seven only",
                **APP,
            }
        ],
        account=OTHER_ACCOUNT,
    )
    # Make every batch searchable (force_flush is POST-only), then wait for
    # each one -- the last insert included -- before any test reads.
    _http("POST", f"{DIRECT}/internal/force_flush")
    expected = [
        ('app:"c5i-app"', len(ENTRIES), None),
        ('app:"c5i-rare"', 1, None),
        ('app:"c5i-bulk"', BULK, None),
        ('app:"c5i-big"', BIG, None),
        ('app:"c5i-probe"', 2, None),
        ('app:"c5i-app"', 1, OTHER_ACCOUNT),
    ]
    deadline = time.monotonic() + 60
    while any(_count(q, account=a) < n for q, n, a in expected):
        assert time.monotonic() < deadline, "VictoriaLogs never served the seed"
        time.sleep(1)
    yield


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c5b-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _env(url: str, k8s_ca: str, **extra: str) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd569",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_VICTORIALOGS_URL": url,
        "OPSPILOT_VICTORIALOGS_USERNAME": READER,
        "OPSPILOT_VICTORIALOGS_PASSWORD": READER_PASSWORD,
        **extra,
    }


@pytest.fixture
def tools(seeded, k8s_ca):
    return _executor(_env(GATEWAY, k8s_ca))


_INDEX = itertools.count()


def _call(tool: str, params: dict[str, object]) -> ToolRequest:
    return ToolRequest(
        step_id="c5b-live",
        tool_index=next(_INDEX),
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


def _keys(outcome) -> list[str]:
    return [str(row.get("key")) for row in _rows(outcome)]


def _gateway_requests() -> int:
    if not GATEWAY_LOG.exists():
        return 0
    return len(GATEWAY_LOG.read_text().splitlines())


# -- real data, windows, order and limits ---------------------------------------


def test_query_returns_default_window_newest_first(tools):
    outcome = tools.execute(_call(QUERY, {"query": 'app:"c5i-app"'}))
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert set(_keys(outcome)) == IN_DEFAULT_WINDOW
    times = [row["_time"] for row in rows]
    assert times == sorted(times, reverse=True)
    assert all({"_time", "_msg", "_stream"} <= set(row) for row in rows)
    assert _view(outcome)["incomplete"] is False


def test_explicit_window_reaches_older_entries_but_never_the_future(tools):
    outcome = tools.execute(
        _call(
            QUERY,
            {
                "query": '{namespace="c5i",app="c5i-app"} level:error',
                "start": _z(NOW - timedelta(hours=23)),
                "end": _z(NOW),
            },
        )
    )
    assert outcome.status == "ok", _view(outcome)
    # a-30h lies outside the 24 h frame, a-future after its end.
    assert set(_keys(outcome)) == {"a1", "a-old"}


def test_time_filter_in_the_query_only_narrows(tools):
    outcome = tools.execute(
        _call(
            QUERY,
            {
                "query": 'app:"c5i-app" _time:3h',
                "start": _z(NOW - timedelta(minutes=40)),
                "end": _z(NOW),
            },
        )
    )
    assert outcome.status == "ok", _view(outcome)
    assert set(_keys(outcome)) == {"a2", "a3", "a-long", "a-secret"}


def test_limit_marks_incomplete_and_keeps_the_newest(tools):
    # VictoriaLogs' own order and limit choice, recorded for C5-10.
    status, body = _direct(
        "/select/logsql/query",
        {
            "query": 'app:"c5i-bulk"',
            "start": _z(NOW - timedelta(hours=1)),
            "end": _z(NOW),
            "limit": "5",
        },
    )
    assert status == 200
    raw = [json.loads(line)["_msg"] for line in str(body).splitlines() if line]
    print(f"victorialogs limit=5 raw order: {raw}")
    outcome = tools.execute(_call(QUERY, {"query": 'app:"c5i-bulk"'}))
    assert outcome.status == "ok", _view(outcome)
    msgs = [row["_msg"] for row in _rows(outcome)]
    assert len(msgs) == 100
    assert msgs[0] == f"bulk line {BULK - 1:03d}"
    assert msgs[-1] == f"bulk line {BULK - 100:03d}"
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in str(view["incomplete_reason"])


def test_oversized_answers_are_refused_whole(tools):
    raw = tools.execute(_call(QUERY, {"query": 'app:"c5i-big"', "limit": BIG}))
    assert (raw.status, raw.reason) == ("error", "RESULT_TOO_LARGE")
    view = tools.execute(_call(QUERY, {"query": 'app:"c5i-big"', "limit": 100}))
    assert (view.status, view.reason) == ("error", "RESULT_TOO_LARGE")
    small = tools.execute(_call(QUERY, {"query": 'app:"c5i-big"', "limit": 5}))
    assert small.status == "ok", _view(small)
    assert all(len(row["_msg"]) == 2000 for row in _rows(small))


def test_long_values_are_cut_and_credentials_redacted(tools):
    outcome = tools.execute(_call(QUERY, {"query": 'app:"c5i-app"'}))
    assert outcome.status == "ok"
    text = _text(outcome)
    assert "L" * 2000 in text and "L" * 2001 not in text
    long_row = next(row for row in _rows(outcome) if row["key"] == "a-long")
    assert long_row["_truncated_fields"] == ["_msg"]
    assert CANARY not in text and "REDACTED" in text


# -- metadata tools ----------------------------------------------------------------


def test_streams_field_names_and_values_are_real(tools):
    streams = tools.execute(_call(STREAMS, {"query": 'namespace:"c5i"'}))
    assert streams.status == "ok", _view(streams)
    names = [row["stream"] for row in _rows(streams)]
    assert any("c5i-app" in name for name in names)
    assert any("c5i-rare" in name for name in names)
    hits = [row["hits"] for row in _rows(streams)]
    assert hits == sorted(hits, reverse=True)

    fields = tools.execute(_call(FIELD_NAMES, {}))
    assert fields.status == "ok", _view(fields)
    assert "opspilot_c5_shard" in {row["field"] for row in _rows(fields)}

    values = tools.execute(
        _call(FIELD_VALUES, {"field": "level", "query": 'app:"c5i-app"'})
    )
    assert values.status == "ok", _view(values)
    # Inside the default hour only: a-old/a-30h (error) are outside it.
    got = {row["value"]: row["hits"] for row in _rows(values)}
    assert got == {"info": 3, "warn": 1, "error": 1}

    cut = tools.execute(_call(FIELD_VALUES, {"field": "level", "limit": 1}))
    assert cut.status == "ok"
    assert len(_rows(cut)) == 1 and _view(cut)["incomplete"] is True


def test_hits_counts_only_the_window(tools):
    outcome = tools.execute(
        _call(HITS, {"query": 'app:"c5i-app"', "step": "10m", "field": "level"})
    )
    assert outcome.status == "ok", _view(outcome)
    totals = {row["fields"].get("level"): row["total"] for row in _rows(outcome)}
    assert totals == {"info": 3, "warn": 1, "error": 1}
    refused = tools.execute(_call(HITS, {"query": "*", "step": "1s"}))
    assert (refused.status, refused.reason) == ("error", "INVALID_PARAMS")


# -- refusals before sending, errors from real responses ------------------------


def test_refusals_are_never_sent(tools):
    before = _gateway_requests()
    refused = [
        (QUERY, {"query": 'app:"c5i-app" | join by (key) (app:"c5i-probe")'}),
        (QUERY, {"query": "trace:in(marker:old | fields trace)"}),
        (QUERY, {"query": "options(ignore_global_time_filter=true) error"}),
        (QUERY, {"query": 'app:"c5i-app" | stream_context before 3'}),
        (QUERY, {"query": 'app:"c5i-app" | union (app:"c5i-probe")'}),
        (QUERY, {"query": "'quoted' error"}),
        (
            QUERY,
            {
                "query": "*",
                "start": _z(FRAME.start - timedelta(hours=1)),
                "end": _z(NOW),
            },
        ),
        (QUERY, {"query": "*", "limit": 501}),
        (HITS, {"query": "*", "step": "1s"}),
        (FIELD_VALUES, {"field": "bad field"}),
    ]
    for tool, params in refused:
        outcome = tools.execute(_call(tool, params))
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), params
        assert _view(outcome)["source_contact"] == "none", params
    assert _gateway_requests() == before
    sent = tools.execute(_call(QUERY, {"query": 'app:"c5i-app"'}))
    assert sent.status == "ok"
    deadline = time.monotonic() + 5
    while _gateway_requests() == before and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _gateway_requests() == before + 1


def test_error_classes_come_from_real_responses(seeded, k8s_ca):
    tools = _executor(_env(GATEWAY, k8s_ca))
    bad_query = 'app:"c5i-app" | nosuchpipe'
    status, _ = _direct(
        "/select/logsql/query",
        {"query": bad_query, "start": _z(NOW - timedelta(hours=1)), "end": _z(NOW)},
    )
    assert status == 400
    bad = tools.execute(_call(QUERY, {"query": bad_query}))
    assert (bad.status, bad.reason) == ("error", "INVALID_PARAMS")
    assert _view(bad)["source_error"]["http_status"] == 400

    nothing = tools.execute(_call(QUERY, {"query": 'app:"c5i-nothing-here"'}))
    assert (nothing.status, nothing.reason) == ("no_data", "NO_DATA")

    wrong = _executor(_env(GATEWAY, k8s_ca, OPSPILOT_VICTORIALOGS_PASSWORD="wrong569"))
    outcome = wrong.execute(_call(QUERY, {"query": "*"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert _view(outcome)["source_error"]["http_status"] == 401

    denied = _executor(_env(GATEWAY_DENY, k8s_ca))
    outcome = denied.execute(_call(STREAMS, {}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert _view(outcome)["source_error"]["http_status"] == 403

    unreachable = _executor(_env("http://127.0.0.1:9", k8s_ca))
    outcome = unreachable.execute(_call(QUERY, {"query": "*"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")


def test_tenant_comes_from_configuration_only(seeded, k8s_ca):
    default = _executor(_env(GATEWAY, k8s_ca))
    query = {"query": 'app:"c5i-app"'}
    msgs = [r["_msg"] for r in _rows(default.execute(_call(QUERY, query)))]
    assert "account seven only" not in msgs
    seven = _executor(
        _env(
            GATEWAY,
            k8s_ca,
            OPSPILOT_VICTORIALOGS_ACCOUNT_ID=OTHER_ACCOUNT,
            OPSPILOT_VICTORIALOGS_PROJECT_ID="0",
        )
    )
    outcome = seven.execute(_call(QUERY, query))
    assert outcome.status == "ok", _view(outcome)
    assert [row["_msg"] for row in _rows(outcome)] == ["account seven only"]


# -- read-only at both layers ---------------------------------------------------


def test_read_only_account_cannot_write_through_the_gateway(seeded):
    before = _count('app:"c5i-app"')
    reader = {"Authorization": _basic(READER, READER_PASSWORD)}
    line = json.dumps({"_msg": "injected via gateway", **APP}).encode()
    attempts = [
        ("POST", "/insert/jsonline?_stream_fields=namespace,app", line),
        ("POST", "/insert/loki/api/v1/push", b"{}"),
        ("POST", "/insert/elasticsearch/_bulk", line),
        ("POST", "/internal/force_flush", None),
        ("POST", "/internal/force_merge", None),
        ("GET", "/internal/force_merge", None),
        ("POST", "/select/logsql/query", b"query=*"),
        ("PUT", "/select/logsql/query", b"query=*"),
        ("GET", "/metrics", None),
    ]
    for method, path, body in attempts:
        status, answer = _http(method, GATEWAY + path, body, headers=reader)
        assert status == 403, (method, path, status, answer)
    time.sleep(2)
    assert _count('app:"c5i-app"') == before
    # The engineer port still accepts writes: the refusal is the gateway's.
    status, _ = _http(
        "POST",
        f"{DIRECT}/insert/jsonline?_stream_fields=namespace,app",
        json.dumps(
            {"_msg": "direct write", "namespace": "c5i", "app": "c5i-w"}
        ).encode(),
    )
    assert status in (200, 204)


# -- C5-7 evidence ------------------------------------------------------------------


def test_subquery_probe_is_recorded(seeded):
    """Not a pass/fail of the product: whether a subquery reads outside
    start/end. ``leaks`` true means the construct must stay refused."""
    window = {"start": _z(NOW - timedelta(hours=1)), "end": _z(NOW)}
    results: dict[str, Any] = {}
    probes = {
        "in_subquery": 'app:"c5i-probe" trace:in(marker:old | fields trace)',
        "options_ignore_global_time_filter": (
            'options(ignore_global_time_filter=true) app:"c5i-probe" marker:old'
        ),
    }
    for name, query in probes.items():
        status, body = _direct("/select/logsql/query", {"query": query, **window})
        if isinstance(body, dict):
            rows = [body]
        elif status == 200 and isinstance(body, str):
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
        else:
            rows = []  # empty answer (None) or an error status
        msgs = sorted(str(row.get("_msg")) for row in rows)
        results[name] = {
            "status": status,
            "messages": msgs,
            "leaks": "probe inside" in msgs or "probe outside" in msgs,
        }
    print("C5-7 probe:", json.dumps(results, sort_keys=True))
    if PROBE_OUT:
        Path(PROBE_OUT).write_text(json.dumps(results, indent=2, sort_keys=True))
    assert results
