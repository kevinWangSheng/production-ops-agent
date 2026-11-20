"""Real-backend integration tests for tool expansion C5b (#569), R8-25 and
r10 C5-16 ... C5-20 (VictoriaLogs part): the five ``victorialogs_*`` tools
against a real ``victoriametrics/victoria-logs`` v1.x behind the nginx
read-only gateway of C5-U1 (``scripts/loki_readonly_gateway.py --backend
victorialogs``), through the public tool path. No model is called.

Runs only in the CI job ``log-backends`` (``OPSPILOT_LOG_BACKENDS=1``), which
starts the containers; skipped everywhere else (C5-16).

The fixtures, not the product, write: synthetic lines relative to now go
straight to VictoriaLogs' own port through ``/insert/jsonline`` with the
257 eval's ``_stream_fields=service,namespace``, ``_msg_field=message`` and
``_time_field=time``; the tool only ever sees the gateway and its read-only
account. The gateway logs method, path and status of every request, which
is how a refusal is shown never to have been sent.

ASSUMPTIONS about the CI job (the interface names no ports or variables;
the defaults below follow the Loki job's 531xx scheme):

* ``C5B_VLOGS_URL`` -- VictoriaLogs' own port (default 127.0.0.1:53128).
* ``C5B_GATEWAY_VLOGS_URL`` -- the gateway server for VictoriaLogs
  (default 127.0.0.1:53103).
* ``C5_GATEWAY_DENY_URL`` -- the existing deny-all gateway server (53102).
* ``C5B_GATEWAY_ACCESS_LOG`` -- the access log of the VictoriaLogs gateway,
  falling back to ``C5_GATEWAY_ACCESS_LOG`` (one nginx may serve both).
* ``C5_GATEWAY_PASSWORD`` -- the reader account, as for Loki.
* Tenant B is ``AccountID: 42`` / ``ProjectID: 7``; the default data is the
  default tenant (0:0, no headers).
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_LOG_BACKENDS") != "1",
    reason="real VictoriaLogs runs only in the CI log-backends job",
)

QUERY = "victorialogs_query"
STREAMS = "victorialogs_streams"
FIELD_NAMES = "victorialogs_field_names"
FIELD_VALUES = "victorialogs_field_values"
HITS = "victorialogs_hits"
PATHS = {
    QUERY: "/select/logsql/query",
    STREAMS: "/select/logsql/streams",
    FIELD_NAMES: "/select/logsql/field_names",
    FIELD_VALUES: "/select/logsql/field_values",
    HITS: "/select/logsql/hits",
}

VLOGS_DIRECT = os.environ.get("C5B_VLOGS_URL", "http://127.0.0.1:53128")
GATEWAY_VLOGS = os.environ.get("C5B_GATEWAY_VLOGS_URL", "http://127.0.0.1:53103")
GATEWAY_DENY = os.environ.get("C5_GATEWAY_DENY_URL", "http://127.0.0.1:53102")
GATEWAY_LOG = Path(
    os.environ.get("C5B_GATEWAY_ACCESS_LOG")
    or os.environ.get("C5_GATEWAY_ACCESS_LOG", "/tmp/c5-gateway-logs/access.log")
)
READER = "reader"
READER_PASSWORD = os.environ.get("C5_GATEWAY_PASSWORD", "changeme569")
TENANT_B = {"AccountID": "42", "ProjectID": "7"}
CANARY = "aaaaaaaa569"
INSERT = "/insert/jsonline?_stream_fields=service,namespace&_msg_field=message&_time_field=time"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRAME = Window(NOW - timedelta(hours=6), NOW)

LINES_APP = {
    "a1": (NOW - timedelta(minutes=50), "error", "level=error payment declined"),
    "a2": (NOW - timedelta(minutes=30), "warn", "level=warn payment slow"),
    "a3": (NOW - timedelta(minutes=10), "info", "level=info cart updated"),
    "a-long": (NOW - timedelta(minutes=20), "info", "L" * 5000),
    "a-secret": (
        NOW - timedelta(minutes=15),
        "info",
        f"user login password={CANARY} ok",
    ),
    "a-old": (NOW - timedelta(hours=2, minutes=30), "error", "level=error old outage"),
    "a-far": (NOW - timedelta(hours=30), "error", "level=error far outage"),
    "a-future": (NOW + timedelta(minutes=3), "error", "level=error future event"),
}
IN_DEFAULT_WINDOW = {"a1", "a2", "a3", "a-long", "a-secret"}
BULK_LINES = 600
BIG_LINES = 500
BIG_LINE_CHARS = 4500
# An explicit 30 min window not aligned to the 5 min step: a burst one
# minute before its start (inside the first aligned bucket) and three lines
# inside it.
BURST_WINDOW = Window(
    NOW - timedelta(minutes=100, seconds=7), NOW - timedelta(minutes=70)
)
BURST_BEFORE = 7
BURST_INSIDE = 3


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _vl_time(moment: datetime, extra_ns: int = 0) -> str:
    whole = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{whole}.{moment.microsecond * 1000 + extra_ns:09d}Z"


def _instant(value: str) -> datetime:
    head, _, tail = value.rstrip("Z").partition(".")
    moment = datetime.fromisoformat(head).replace(tzinfo=timezone.utc)
    return moment + timedelta(microseconds=int((tail or "0")[:6].ljust(6, "0")))


# -- raw HTTP for fixtures (engineer side, never the product) ----------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {})
    )
    try:
        with _OPENER.open(request, timeout=60) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _insert(
    rows: list[dict[str, str]], tenant: Mapping[str, str] | None = None
) -> None:
    for start in range(0, len(rows), 200):
        body = b"".join(
            json.dumps(row).encode() + b"\n" for row in rows[start : start + 200]
        )
        status, text = _http(
            "POST",
            VLOGS_DIRECT + INSERT,
            body,
            headers={"Content-Type": "application/stream+json", **(tenant or {})},
        )
        assert status in (200, 204), (status, text[:300])


def _flush() -> None:
    status, text = _http("GET", f"{VLOGS_DIRECT}/internal/force_flush")
    assert status == 200, (status, text[:300])


def _direct(
    query: str,
    *,
    tenant: Mapping[str, str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    params = {
        "query": query,
        "start": _z(start or NOW - timedelta(hours=40)),
        "end": _z(end or NOW + timedelta(minutes=10)),
    }
    if limit is not None:
        params["limit"] = str(limit)
    status, text = _http(
        "GET",
        f"{VLOGS_DIRECT}/select/logsql/query?{urllib.parse.urlencode(params)}",
        headers=dict(tenant or {}),
    )
    if status != 200:
        return status, []
    return status, [json.loads(line) for line in text.splitlines() if line.strip()]


def _count(query: str, tenant: Mapping[str, str] | None = None) -> int:
    status, rows = _direct(query, tenant=tenant)
    assert status == 200, (status, query)
    return len(rows)


def _big_line(n: int) -> str:
    words = []
    seed = n * 7919
    while sum(len(word) + 1 for word in words) < BIG_LINE_CHARS:
        seed = (seed * 1103515245 + 12345) % (1 << 31)
        words.append(f"{seed:08x}")
    return " ".join(words)[:BIG_LINE_CHARS]


def _row(service: str, when: datetime, message: str, **fields: str) -> dict[str, str]:
    return {
        "time": _vl_time(when),
        "service": service,
        "namespace": "c5",
        "message": message,
        **fields,
    }


@pytest.fixture(scope="module", autouse=True)
def seeded() -> None:
    deadline = time.monotonic() + 120
    while True:
        try:
            if _http("GET", f"{VLOGS_DIRECT}/health")[0] == 200:
                break
        except OSError:
            pass
        assert time.monotonic() < deadline, f"VictoriaLogs at {VLOGS_DIRECT} not ready"
        time.sleep(2)
    rows = [
        _row("c5-app", when, line, level=level, pod="checkout-7b9899-x2km9")
        for when, level, line in LINES_APP.values()
    ]
    rows.append(
        _row(
            "c5-rare",
            NOW - timedelta(minutes=12),
            "rare field line",
            opspilot_c5_shard="s1",
        )
    )
    bulk_start = NOW - timedelta(minutes=59)
    rows += [
        _row(
            "c5-bulk",
            bulk_start + timedelta(seconds=5 * n),
            f"bulk line {n:03d}",
            level="error" if n % 3 == 0 else "info",
        )
        for n in range(BULK_LINES)
    ]
    big_start = NOW - timedelta(minutes=58)
    rows += [
        _row("c5-big", big_start + timedelta(seconds=6 * n), _big_line(n))
        for n in range(BIG_LINES)
    ]
    rows += [
        _row(
            "c5-burst",
            BURST_WINDOW.start - timedelta(minutes=1, seconds=-n),
            f"burst before {n}",
        )
        for n in range(BURST_BEFORE)
    ]
    rows += [
        _row(
            "c5-burst",
            BURST_WINDOW.start + timedelta(minutes=2, seconds=n),
            f"burst inside {n}",
        )
        for n in range(BURST_INSIDE)
    ]
    # Subquery leak probe (C5-7): the subquery's only match is outside every
    # window; the outer match is inside the default hour.
    rows.append(
        _row("c5-sub-old", NOW - timedelta(hours=30), "probe old", marker="m-probe")
    )
    rows.append(
        _row("c5-sub-new", NOW - timedelta(minutes=20), "probe new", marker="m-probe")
    )
    _insert(rows)
    _insert(
        [
            _row(
                "c5-app",
                NOW - timedelta(minutes=25),
                "tenant b only line",
                level="info",
            )
        ],
        tenant=TENANT_B,
    )
    _flush()
    deadline = time.monotonic() + 60
    while (
        _count('{service="c5-bulk"}') < BULK_LINES
        or _count('{service="c5-app"}', TENANT_B) < 1
    ):
        assert time.monotonic() < deadline, "VictoriaLogs never served the seed"
        _flush()
        time.sleep(1)


# -- the product path ----------------------------------------------------------


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c5b-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _env(url: str, k8s_ca: str, **vlogs: str) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd569",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_VICTORIALOGS_URL": url,
        **vlogs,
    }


def _reader(**extra: str) -> dict[str, str]:
    return {
        "OPSPILOT_VICTORIALOGS_USERNAME": READER,
        "OPSPILOT_VICTORIALOGS_PASSWORD": READER_PASSWORD,
        **extra,
    }


def _executor(env: Mapping[str, str]):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    return factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="Why is checkout degraded?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=12,
        ),
    )


@pytest.fixture
def tools(k8s_ca):
    return _executor(_env(GATEWAY_VLOGS, k8s_ca, **_reader()))


_INDEX = itertools.count()


def _call(params: dict[str, object], tool: str = QUERY) -> ToolRequest:
    return ToolRequest(
        step_id="c5b-live",
        tool_index=next(_INDEX),
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


def _rows(outcome) -> list[dict[str, Any]]:
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _msgs(outcome) -> list[str]:
    return [row["_msg"] for row in _rows(outcome)]


def _ids(outcome) -> set[str]:
    by_line = {line[:1000]: key for key, (_, _, line) in LINES_APP.items()}
    found = set()
    for msg in _msgs(outcome):
        key = by_line.get(msg[:1000])
        if key is None and msg.startswith("user login password="):
            key = "a-secret"  # redacted in the view
        assert key is not None, msg[:200]
        found.add(key)
    return found


def _gateway_lines() -> list[list[str]]:
    """``$time_iso8601 $server_port $remote_user $request_method $uri $status``."""
    if not GATEWAY_LOG.exists():
        return []
    return [line.split() for line in GATEWAY_LOG.read_text().splitlines()]


def _wait_for_log(before: int, expected: int) -> list[list[str]]:
    # nginx writes the line when the request is finalized, just after the
    # answer: give it a moment.
    deadline = time.monotonic() + 5
    while len(_gateway_lines()) < before + expected and time.monotonic() < deadline:
        time.sleep(0.2)
    return _gateway_lines()[before:]


# -- real data, windows, ordering, limits -----------------------------------------


def test_query_returns_real_rows_in_the_default_window_newest_first(tools):
    outcome = tools.execute(_call({"query": '{service="c5-app"}'}))
    assert outcome.status == "ok", _view(outcome)
    assert _ids(outcome) == IN_DEFAULT_WINDOW
    view = _view(outcome)
    assert view["incomplete"] is False
    assert view["window"] == {
        "start": (NOW - timedelta(hours=1)).isoformat(),
        "end": NOW.isoformat(),
    }
    times = [_instant(row["_time"]) for row in _rows(outcome)]
    assert times == sorted(times, reverse=True)
    assert datetime.fromisoformat(view["source_start_at"]) == LINES_APP["a1"][0]
    assert datetime.fromisoformat(view["source_end_at"]) == LINES_APP["a3"][0]
    row = _rows(outcome)[0]
    assert {"_time", "_stream", "_msg", "level", "pod"} <= set(row)
    assert row["_stream"] == '{namespace="c5",service="c5-app"}'


def test_query_explicit_window(tools):
    outcome = tools.execute(
        _call(
            {
                "query": '{service="c5-app"}',
                "start": _z(NOW - timedelta(hours=4)),
                "end": _z(NOW),
            }
        )
    )
    assert outcome.status == "ok", _view(outcome)
    assert _ids(outcome) == IN_DEFAULT_WINDOW | {"a-old"}


def test_rows_outside_the_query_window_never_return(tools):
    # Control: VictoriaLogs itself holds all three rows (engineer port).
    assert _count('{service="c5-app"} "future event"') == 1
    assert _count('{service="c5-app"} "old outage"') == 1
    assert _count('{service="c5-app"} "far outage"') == 1
    for phrase in ("future event", "old outage", "far outage"):
        outcome = tools.execute(_call({"query": f'{{service="c5-app"}} "{phrase}"'}))
        assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), phrase
    # The widest window the frame allows: only the old row (the far row is
    # before the frame, the future row after its end).
    widest = tools.execute(
        _call(
            {
                "query": '{service="c5-app"} outage',
                "start": _z(FRAME.start),
                "end": _z(FRAME.end),
            }
        )
    )
    assert widest.status == "ok", _view(widest)
    assert _msgs(widest) == [LINES_APP["a-old"][2]]
    # _time in the query only narrows the window (C5-7).
    narrowed = tools.execute(_call({"query": '{service="c5-app"} _time:2d outage'}))
    assert (narrowed.status, narrowed.reason) == ("no_data", "NO_DATA")


def test_limit_marks_incomplete_and_keeps_the_newest(tools):
    outcome = tools.execute(_call({"query": '{service="c5-bulk"}'}))
    assert outcome.status == "ok", _view(outcome)
    msgs = _msgs(outcome)
    assert len(msgs) == 100
    assert msgs[0] == f"bulk line {BULK_LINES - 1:03d}"
    assert msgs[-1] == f"bulk line {BULK_LINES - 100:03d}"
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in str(view["incomplete_reason"]).lower()
    # Exactly as many rows as the limit is complete: the extra row decides.
    rare = tools.execute(_call({"query": '{service="c5-rare"}', "limit": 1}))
    assert rare.status == "ok", _view(rare)
    assert _view(rare)["incomplete"] is False


def test_victorialogs_order_and_limit_choice_are_recorded():
    """Recorded observation (C5-10 未确认): which rows VictoriaLogs keeps for
    ``limit`` and in which order it returns them. The tool's own guarantee
    is the test above."""
    status, rows = _direct(
        '{service="c5-bulk"}', start=NOW - timedelta(hours=1), end=NOW, limit=10
    )
    assert status == 200
    msgs = [row["_msg"] for row in rows]
    newest = {f"bulk line {n:03d}" for n in range(BULK_LINES - 10, BULK_LINES)}
    order = (
        "descending"
        if msgs == sorted(msgs, reverse=True)
        else "ascending"
        if msgs == sorted(msgs)
        else "unordered"
    )
    warnings.warn(
        f"C5b observation: VictoriaLogs limit=10 returned the newest 10: "
        f"{set(msgs) == newest}; order {order}: {msgs}",
        stacklevel=1,
    )
    assert len(msgs) == 10


def test_oversized_answers_are_refused_whole(tools):
    raw = tools.execute(_call({"query": '{service="c5-big"}', "limit": BIG_LINES}))
    assert (raw.status, raw.reason) == ("error", "RESULT_TOO_LARGE"), _view(raw)
    assert _view(raw)["content"] is None
    view = tools.execute(_call({"query": '{service="c5-big"}', "limit": 100}))
    assert (view.status, view.reason) == ("error", "RESULT_TOO_LARGE"), _view(view)
    assert _view(view)["content"] is None
    small = tools.execute(_call({"query": '{service="c5-big"}', "limit": 5}))
    assert small.status == "ok", _view(small)
    assert all(len(msg) == 2000 for msg in _msgs(small))


def test_long_values_are_cut_and_credentials_in_logs_are_redacted(tools):
    outcome = tools.execute(_call({"query": '{service="c5-app"}'}))
    assert outcome.status == "ok"
    text = _text(outcome)
    assert "L" * 2000 in text and "L" * 2001 not in text
    long_row = next(row for row in _rows(outcome) if row["_msg"].startswith("LLL"))
    assert long_row["_truncated_fields"] == ["_msg"]
    assert CANARY not in text and "REDACTED" in text


# -- the metadata tools -----------------------------------------------------------


def test_streams_are_real_and_sorted_by_hits(tools):
    outcome = tools.execute(_call({"query": '{namespace="c5"}'}, STREAMS))
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert all(set(row) == {"stream", "hits"} for row in rows), rows
    by_stream = {row["stream"]: row["hits"] for row in rows}
    assert by_stream['{namespace="c5",service="c5-bulk"}'] == BULK_LINES
    assert by_stream['{namespace="c5",service="c5-app"}'] == len(IN_DEFAULT_WINDOW)
    assert by_stream['{namespace="c5",service="c5-rare"}'] == 1
    assert '{namespace="c5",service="c5-sub-old"}' not in by_stream  # 30 h ago
    assert rows == sorted(rows, key=lambda row: (-row["hits"], row["stream"]))
    view = _view(outcome)
    assert datetime.fromisoformat(view["source_start_at"]) == NOW - timedelta(hours=1)
    assert datetime.fromisoformat(view["source_end_at"]) == NOW
    default = tools.execute(_call({}, STREAMS))
    assert default.status == "ok", _view(default)


def test_field_names_find_a_rare_field(tools):
    outcome = tools.execute(_call({"query": '{service="c5-rare"}'}, FIELD_NAMES))
    assert outcome.status == "ok", _view(outcome)
    fields = {row["field"]: row["hits"] for row in _rows(outcome)}
    assert fields.get("opspilot_c5_shard") == 1
    assert "_msg" in fields
    app = tools.execute(_call({"query": '{service="c5-app"}'}, FIELD_NAMES))
    assert {"level", "pod", "_msg"} <= {row["field"] for row in _rows(app)}


def test_field_values_are_real_counts(tools):
    outcome = tools.execute(
        _call({"field": "level", "query": '{service="c5-app"}'}, FIELD_VALUES)
    )
    assert outcome.status == "ok", _view(outcome)
    assert _rows(outcome) == [
        {"value": "info", "hits": 3},
        {"value": "error", "hits": 1},
        {"value": "warn", "hits": 1},
    ]
    assert _view(outcome)["field"] == "level"
    limited = tools.execute(
        _call(
            {"field": "level", "query": '{service="c5-app"}', "limit": 1}, FIELD_VALUES
        )
    )
    assert limited.status == "ok", _view(limited)
    assert len(_rows(limited)) == 1 and _view(limited)["incomplete"] is True


def test_hits_count_only_the_window_with_step_and_grouping(tools):
    outcome = tools.execute(_call({"query": '{service="c5-bulk"}'}, HITS))
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert sum(row["total"] for row in rows) == BULK_LINES
    for row in rows:
        assert set(row) >= {"fields", "timestamps", "values", "total"}
        assert sum(int(v) for v in row["values"]) == row["total"]
    grouped = tools.execute(
        _call({"query": '{service="c5-bulk"}', "field": "level", "step": "10m"}, HITS)
    )
    assert grouped.status == "ok", _view(grouped)
    totals = {row["fields"]["level"]: row["total"] for row in _rows(grouped)}
    assert totals == {"error": BULK_LINES // 3, "info": BULK_LINES - BULK_LINES // 3}
    assert all(row["total"] > 0 for row in _rows(grouped))


def test_hits_buckets_are_step_aligned_but_count_only_inside(tools):
    outcome = tools.execute(
        _call(
            {
                "query": '{service="c5-burst"}',
                "start": _z(BURST_WINDOW.start),
                "end": _z(BURST_WINDOW.end),
                "step": "5m",
            },
            HITS,
        )
    )
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert sum(row["total"] for row in rows) == BURST_INSIDE
    view = _view(outcome)
    notes = " ".join(str(note) for note in view.get("notes") or []).lower()
    assert "step" in notes or "bucket" in notes
    first = min(_instant(stamp) for row in rows for stamp in row["timestamps"])
    warnings.warn(
        f"C5b observation: first hits bucket {first.isoformat()} for window start "
        f"{BURST_WINDOW.start.isoformat()} (step 5m)",
        stacklevel=1,
    )


# -- refusals before sending, errors from real responses ------------------------


def test_refusals_are_never_sent(tools):
    before = len(_gateway_lines())
    refused = [
        ({"query": '{service="c5-app"} | join by (pod) (level:error)'}, QUERY),
        ({"query": "options(ignore_global_time_filter=true) *"}, QUERY),
        ({"query": "* | union (service:c5-sub-old)"}, QUERY),
        ({"query": '{service="c5-app"} | stream_context before 5'}, QUERY),
        ({"query": 'marker:in({service="c5-sub-old"} | fields marker)'}, QUERY),
        ({"query": "'error'"}, QUERY),
        ({"query": "error # join"}, QUERY),
        ({"query": '"unclosed'}, QUERY),
        (
            {
                "query": '{service="c5-app"}',
                "start": _z(FRAME.start - timedelta(hours=1)),
                "end": _z(NOW),
            },
            QUERY,
        ),
        ({"query": '{service="c5-app"}', "start": _z(NOW - timedelta(hours=1))}, QUERY),
        ({"query": '{service="c5-app"}', "limit": 501}, QUERY),
        ({"query": "*", "step": "1s"}, HITS),
        ({"query": "*", "step": "5d"}, HITS),
        ({"query": "*", "field": "level|x"}, HITS),
        ({"field": "le vel"}, FIELD_VALUES),
        ({"query": "* | join (x)"}, STREAMS),
        ({"query": "x:contains_any(* | fields y)"}, FIELD_NAMES),
    ]
    for params, tool in refused:
        outcome = tools.execute(_call(params, tool))
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), params
        assert _view(outcome)["source_contact"] == "none", params
    time.sleep(1)
    assert len(_gateway_lines()) == before
    sent = tools.execute(_call({"query": '{service="c5-app"}'}))
    assert sent.status == "ok"
    assert len(_wait_for_log(before, 1)) == 1


def test_tools_send_only_the_five_gets(tools):
    before = len(_gateway_lines())
    calls = [
        ({"query": '{service="c5-app"}'}, QUERY),
        ({"query": '{namespace="c5"}'}, STREAMS),
        ({"query": '{service="c5-app"}'}, FIELD_NAMES),
        ({"field": "level", "query": '{service="c5-app"}'}, FIELD_VALUES),
        ({"query": '{service="c5-app"}', "field": "level"}, HITS),
    ]
    for params, tool in calls:
        outcome = tools.execute(_call(params, tool))
        assert outcome.status == "ok", (tool, _view(outcome))
    lines = _wait_for_log(before, len(calls))
    assert len(lines) == len(calls), lines
    assert [(line[3], line[4], line[5]) for line in lines] == [
        ("GET", PATHS[tool], "200") for _, tool in calls
    ]


def test_error_classes_come_from_real_responses(tools, k8s_ca):
    # The scenario really is a 400 (guards the setup itself).
    query = urllib.parse.urlencode({"query": '{service="c5-app"'})
    assert _http("GET", f"{VLOGS_DIRECT}/select/logsql/query?{query}")[0] == 400
    bad = tools.execute(_call({"query": '{service="c5-app"'}))
    assert (bad.status, bad.reason) == ("error", "INVALID_PARAMS"), _view(bad)
    assert _view(bad)["source_error"]["http_status"] == 400

    nothing = tools.execute(_call({"query": '{service="c5-nothing-here"}'}))
    assert (nothing.status, nothing.reason) == ("no_data", "NO_DATA")

    wrong = _executor(
        _env(
            GATEWAY_VLOGS,
            k8s_ca,
            OPSPILOT_VICTORIALOGS_USERNAME=READER,
            OPSPILOT_VICTORIALOGS_PASSWORD="wrong569",
        )
    )
    outcome = wrong.execute(_call({"query": "*"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert _view(outcome)["source_error"]["http_status"] == 401

    denied = _executor(_env(GATEWAY_DENY, k8s_ca, **_reader()))
    outcome = denied.execute(_call({}, STREAMS))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert _view(outcome)["source_error"]["http_status"] == 403

    unreachable = _executor(_env("http://127.0.0.1:9", k8s_ca, **_reader()))
    outcome = unreachable.execute(_call({"query": "*"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")


def test_tenants_come_from_configuration_only(tools, k8s_ca):
    default = _msgs(tools.execute(_call({"query": '{service="c5-app"}'})))
    assert "tenant b only line" not in default
    tenant_b = _executor(
        _env(
            GATEWAY_VLOGS,
            k8s_ca,
            **_reader(
                OPSPILOT_VICTORIALOGS_ACCOUNT_ID=TENANT_B["AccountID"],
                OPSPILOT_VICTORIALOGS_PROJECT_ID=TENANT_B["ProjectID"],
            ),
        )
    )
    outcome = tenant_b.execute(_call({"query": '{service="c5-app"}'}))
    assert outcome.status == "ok", _view(outcome)
    assert _msgs(outcome) == ["tenant b only line"]
    streams = tenant_b.execute(_call({"query": '{namespace="c5"}'}, STREAMS))
    assert [row["stream"] for row in _rows(streams)] == [
        '{namespace="c5",service="c5-app"}'
    ]


# -- read-only at both layers ---------------------------------------------------


def test_read_only_account_cannot_write_through_the_gateway():
    before = _count('{service="c5-app"}')
    before_all = _count("*")
    reader = {"Authorization": _basic(READER, READER_PASSWORD)}
    line = json.dumps(
        {
            "time": _vl_time(NOW),
            "service": "c5-app",
            "namespace": "c5",
            "message": "injected",
        }
    ).encode()
    push = json.dumps(
        {
            "streams": [
                {
                    "stream": {"service": "c5-app"},
                    "values": [[str(int(NOW.timestamp() * 1e9)), "injected"]],
                }
            ]
        }
    ).encode()
    attempts = [
        ("POST", INSERT, line),
        ("PUT", INSERT, line),
        ("POST", "/insert/loki/api/v1/push", push),
        ("POST", "/insert/elasticsearch/_bulk", line),
        ("GET", "/internal/force_flush", None),
        ("POST", "/internal/force_flush", None),
        ("GET", "/internal/force_merge", None),
        ("POST", "/internal/force_merge", None),
        ("POST", "/select/logsql/query", b"query=*"),
        ("PUT", "/select/logsql/query", b"query=*"),
        ("DELETE", "/select/logsql/query", None),
        ("POST", "/select/logsql/hits", b"query=*"),
        ("GET", "/select/logsql/tail?query=*", None),
        ("GET", "/select/logsql/stats_query?query=*", None),
        ("GET", "/metrics", None),
    ]
    for method, path, body in attempts:
        headers = dict(reader)
        if body is not None:
            headers["Content-Type"] = "application/stream+json"
        status, answer = _http(method, GATEWAY_VLOGS + path, body, headers=headers)
        assert status == 403, (method, path, status, answer[:200])
    _flush()
    time.sleep(1)
    assert _count('{service="c5-app"}') == before
    assert _count("*") == before_all
    # The engineer port still accepts writes: the refusal is the gateway's.
    _insert([_row("c5-probe", NOW - timedelta(seconds=1), "x")])
    _flush()
    deadline = time.monotonic() + 10
    while _count('{service="c5-probe"}') < 1:
        assert time.monotonic() < deadline
        time.sleep(0.5)


def test_subquery_leak_probe_is_recorded_and_the_tool_still_refuses(tools):
    """Recorded evidence for C5-7: does a subquery inside in(...) honour the
    request's start/end? Its only match is 30 h old; if VictoriaLogs ignored
    the window for it, the 20 min old outer row would come back."""
    hour_start = NOW - timedelta(hours=1)
    status, rows = _direct(
        'marker:in({service="c5-sub-old"} | fields marker)', start=hour_start, end=NOW
    )
    leaked = [row.get("service") for row in rows]
    status_opt, rows_opt = _direct(
        'options(ignore_global_time_filter=true) {service="c5-sub-old"}',
        start=hour_start,
        end=NOW,
    )
    warnings.warn(
        f"C5b observation: in()-subquery direct status {status}, rows {leaked} "
        f"(non-empty = the subquery ignored start/end); "
        f"options(ignore_global_time_filter=true) status {status_opt}, "
        f"rows {len(rows_opt)}",
        stacklevel=1,
    )
    assert status in (200, 400)
    outcome = tools.execute(
        _call({"query": 'marker:in({service="c5-sub-old"} | fields marker)'})
    )
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert str(_view(outcome)["notes"][0]).startswith("QUERY_CONSTRUCT_NOT_ALLOWED")
