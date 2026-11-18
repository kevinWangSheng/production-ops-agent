"""Real-backend integration tests for tool expansion C5a (#569), R8-25 and
r10 C5-16 ... C5-20 (Loki part): ``grafana_loki_query`` against real Loki
2.9.0 and Loki 3.x (multi-tenant) behind the nginx read-only gateway of
C5-U1 (rendered by ``scripts/loki_readonly_gateway.py``), through the public
tool path. No model is called.

Runs only in the CI job ``log-backends`` (``OPSPILOT_LOG_BACKENDS=1``), which
starts the containers; skipped everywhere else (the local machine has no
memory for them, C5-16).

The fixtures, not the product, write: synthetic lines relative to now are
pushed straight to each Loki's own port (the engineer side, C5-18), and the
tool only ever sees the gateway and its read-only account. The gateway logs
method, path and status of every request it receives, which is how a
refusal is shown never to have been sent.
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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
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
    reason="real Loki runs only in the CI log-backends job",
)

TOOL = "grafana_loki_query"

LOKI3_DIRECT = os.environ.get("C5_LOKI3_URL", "http://127.0.0.1:53111")
LOKI29_DIRECT = os.environ.get("C5_LOKI29_URL", "http://127.0.0.1:53110")
GATEWAY_LOKI3 = os.environ.get("C5_GATEWAY_LOKI3_URL", "http://127.0.0.1:53100")
GATEWAY_LOKI29 = os.environ.get("C5_GATEWAY_LOKI29_URL", "http://127.0.0.1:53101")
GATEWAY_DENY = os.environ.get("C5_GATEWAY_DENY_URL", "http://127.0.0.1:53102")
GATEWAY_LOG = Path(
    os.environ.get("C5_GATEWAY_ACCESS_LOG", "/tmp/c5-gateway-logs/access.log")
)
# Low-entropy CI-only account of the gateway (C5-18).
READER = "reader"
READER_PASSWORD = os.environ.get("C5_GATEWAY_PASSWORD", "changeme569")
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
CANARY = "aaaaaaaa569"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRAME = Window(NOW - timedelta(hours=6), NOW)

APP = {"job": "c5-app", "namespace": "c5", "pod": "checkout-7b9899-x2km9"}
# A label name the model would not guess (C5-19).
RARE = {"job": "c5-rare", "namespace": "c5", "opspilot_c5_shard": "s1"}
LINES_APP = {
    "a1": (NOW - timedelta(minutes=50), "level=error payment declined"),
    "a2": (NOW - timedelta(minutes=30), "level=warn payment slow"),
    "a3": (NOW - timedelta(minutes=10), "level=info cart updated"),
    "a-long": (NOW - timedelta(minutes=20), "L" * 5000),
    "a-secret": (NOW - timedelta(minutes=15), f"user login password={CANARY} ok"),
    # Inside Loki's query_ingesters_within (3 h): nothing is flushed to the
    # store in CI, so an older line could not be read back at all.
    "a-old": (NOW - timedelta(hours=2, minutes=30), "level=error old outage"),
    "a-future": (NOW + timedelta(minutes=3), "level=error future event"),
}
IN_DEFAULT_WINDOW = {"a1", "a2", "a3", "a-long", "a-secret"}
BULK_LINES = 600
BIG_LINES = 500
BIG_LINE_CHARS = 4500
# C5-20 lookback: an explicit 30 min window, a 10 min range, a burst just
# before the window start and three lines inside it.
BURST_WINDOW = Window(NOW - timedelta(minutes=100), NOW - timedelta(minutes=70))
BURST_BEFORE = 7
BURST_INSIDE = 3


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns(moment: datetime, offset: int = 0) -> str:
    return str(int(moment.timestamp()) * 1_000_000_000 + offset)


def _instant(value: str) -> datetime:
    # Loki rows carry nanoseconds; Python keeps microseconds.
    head, _, tail = value.rstrip("Z").partition(".")
    moment = datetime.fromisoformat(head).replace(tzinfo=timezone.utc)
    return moment + timedelta(microseconds=int((tail or "0")[:6].ljust(6, "0")))


# -- raw HTTP for fixtures (engineer side, never the product) ----------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(
    method: str,
    url: str,
    body: object = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Any]:
    data = None
    sent = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        sent.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=sent)
    try:
        with _OPENER.open(request, timeout=60) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@dataclass(frozen=True)
class Loki:
    name: str
    direct: str  # the engineer's own port, for seeding and counting
    gateway: str  # what the product is configured with
    tenant: str | None  # None: auth disabled (single tenant)

    def headers(self, tenant: str | None = None) -> dict[str, str]:
        chosen = tenant or self.tenant
        return {} if chosen is None else {"X-Scope-OrgID": chosen}


LOKI3 = Loki("loki-3", LOKI3_DIRECT, GATEWAY_LOKI3, TENANT_A)
LOKI29 = Loki("loki-2.9", LOKI29_DIRECT, GATEWAY_LOKI29, None)


def _wait_ready(loki: Loki) -> None:
    deadline = time.monotonic() + 180
    while True:
        try:
            status, _ = _http("GET", f"{loki.direct}/ready")
            if status == 200:
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"{loki.name} at {loki.direct} never became ready")
        time.sleep(2)


def _push(
    loki: Loki,
    labels: Mapping[str, str],
    values: list[list[str]],
    *,
    tenant: str | None = None,
) -> None:
    """Push one stream oldest first. Loki (2.9 and 3.x) accepts an entry out
    of order only within max_chunk_age/2 (1 h) of the stream's newest entry
    so far, so an entry 3 h or 30 h old pushed after a recent one is
    rejected ("entry too far behind"); in ascending order every entry is the
    newest when it arrives and all are kept."""
    ordered = sorted(values, key=lambda value: int(value[0]))
    for start in range(0, len(ordered), 100):
        chunk = ordered[start : start + 100]
        status, body = _http(
            "POST",
            f"{loki.direct}/loki/api/v1/push",
            {"streams": [{"stream": dict(labels), "values": chunk}]},
            headers=loki.headers(tenant),
        )
        assert status == 204, (loki.name, status, body)


def _direct_lines(loki: Loki, selector: str, *, tenant: str | None = None) -> int:
    """How many lines Loki holds for ``selector`` in the frame and slightly
    beyond, read on the engineer port (not through the gateway)."""
    query = urllib.parse.urlencode(
        {
            "query": selector,
            "start": _z(NOW - timedelta(hours=40)),
            "end": _z(NOW + timedelta(minutes=10)),
            "limit": "5000",
        }
    )
    status, body = _http(
        "GET",
        f"{loki.direct}/loki/api/v1/query_range?{query}",
        headers=loki.headers(tenant),
    )
    assert status == 200, (loki.name, status, body)
    return sum(len(stream["values"]) for stream in body["data"]["result"])


def _big_line(n: int) -> str:
    # Hex words tokenize poorly, so a cut 2000-character line is many tokens.
    words = []
    seed = n * 7919
    while sum(len(word) + 1 for word in words) < BIG_LINE_CHARS:
        seed = (seed * 1103515245 + 12345) % (1 << 31)
        words.append(f"{seed:08x}")
    return " ".join(words)[:BIG_LINE_CHARS]


def _seed(loki: Loki) -> None:
    _wait_ready(loki)
    _push(
        loki,
        APP,
        [[_ns(when), line] for when, line in LINES_APP.values()],
    )
    _push(loki, RARE, [[_ns(NOW - timedelta(minutes=12)), "rare label line"]])
    bulk_start = NOW - timedelta(minutes=59)
    _push(
        loki,
        {"job": "c5-bulk", "namespace": "c5"},
        [
            [_ns(bulk_start + timedelta(seconds=5 * n)), f"bulk line {n:03d}"]
            for n in range(BULK_LINES)
        ],
    )
    big_start = NOW - timedelta(minutes=58)
    _push(
        loki,
        {"job": "c5-big", "namespace": "c5"},
        [
            [_ns(big_start + timedelta(seconds=6 * n)), _big_line(n)]
            for n in range(BIG_LINES)
        ],
    )
    before = BURST_WINDOW.start - timedelta(minutes=2)
    inside = BURST_WINDOW.start + timedelta(minutes=5)
    _push(
        loki,
        {"job": "c5-burst", "namespace": "c5"},
        [[_ns(before, n), f"burst before {n}"] for n in range(BURST_BEFORE)]
        + [[_ns(inside, n), f"burst inside {n}"] for n in range(BURST_INSIDE)],
    )
    if loki.tenant is not None:
        _push(
            loki,
            APP,
            [[_ns(NOW - timedelta(minutes=25)), "tenant b only line"]],
            tenant=TENANT_B,
        )
    # Ingested lines are queryable from the ingester at once; wait until the
    # last stream reads back complete.
    deadline = time.monotonic() + 60
    while _direct_lines(loki, '{job="c5-burst"}') < BURST_BEFORE + BURST_INSIDE:
        assert time.monotonic() < deadline, f"{loki.name} never served the seed"
        time.sleep(1)


@pytest.fixture(scope="module")
def loki3() -> Iterator[Loki]:
    _seed(LOKI3)
    yield LOKI3


@pytest.fixture(scope="module")
def loki29() -> Iterator[Loki]:
    _seed(LOKI29)
    yield LOKI29


@pytest.fixture(params=["loki-3", "loki-2.9"])
def loki(request) -> Loki:
    return request.getfixturevalue("loki3" if request.param == "loki-3" else "loki29")


# -- the product path ----------------------------------------------------------


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c5-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _env(url: str, k8s_ca: str, **loki: str) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd569",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_LOKI_URL": url,
        **loki,
    }


def _reader(**extra: str) -> dict[str, str]:
    return {
        "OPSPILOT_LOKI_USERNAME": READER,
        "OPSPILOT_LOKI_PASSWORD": READER_PASSWORD,
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


def _tools_for(loki: Loki, k8s_ca: str, tenant: str | None = None):
    extra = {}
    chosen = tenant or loki.tenant
    if chosen is not None:
        extra["OPSPILOT_LOKI_TENANT_ID"] = chosen
    return _executor(_env(loki.gateway, k8s_ca, **_reader(**extra)))


@pytest.fixture
def tools(loki, k8s_ca):
    return _tools_for(loki, k8s_ca)


_INDEX = itertools.count()


def _call(params: dict[str, object]) -> ToolRequest:
    return ToolRequest(
        step_id="c5-live",
        tool_index=next(_INDEX),
        tool_name=TOOL,
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


def _lines(outcome) -> list[str]:
    return [row["line"] for row in _rows(outcome)]


def _ids(outcome) -> set[str]:
    by_line = {line[:1000]: key for key, (_, line) in LINES_APP.items()}
    found = set()
    for line in _lines(outcome):
        key = by_line.get(line[:1000])
        if key is None and line.startswith("user login password="):
            key = "a-secret"  # redacted in the view
        assert key is not None, line[:200]
        found.add(key)
    return found


def _gateway_requests() -> int:
    if not GATEWAY_LOG.exists():
        return 0
    return len(GATEWAY_LOG.read_text().splitlines())


# -- both versions: real data, windows, lookback, limits ------------------------


def test_real_lines_in_the_default_window_newest_first(tools):
    outcome = tools.execute(_call({"query": '{job="c5-app"}'}))
    assert outcome.status == "ok", _view(outcome)
    assert _ids(outcome) == IN_DEFAULT_WINDOW
    view = _view(outcome)
    assert view["incomplete"] is False
    assert view["window"] == {
        "start": (NOW - timedelta(hours=1)).isoformat(),
        "end": NOW.isoformat(),
    }
    times = [_instant(row["timestamp"]) for row in _rows(outcome)]
    assert times == sorted(times, reverse=True)
    assert datetime.fromisoformat(view["source_start_at"]) == LINES_APP["a1"][0]
    assert datetime.fromisoformat(view["source_end_at"]) == LINES_APP["a3"][0]
    assert {row["labels"]["pod"] for row in _rows(outcome)} == {APP["pod"]}


def test_explicit_window_and_regex_pod_match(tools):
    outcome = tools.execute(
        _call(
            {
                "query": '{namespace="c5", pod=~"checkout-.*"}',
                "start": _z(NOW - timedelta(hours=4)),
                "end": _z(NOW),
            }
        )
    )
    assert outcome.status == "ok", _view(outcome)
    assert _ids(outcome) == IN_DEFAULT_WINDOW | {"a-old"}


def test_rare_label_is_visible_from_a_broad_selector(tools):
    outcome = tools.execute(_call({"query": '{namespace="c5"} |= "rare label"'}))
    assert outcome.status == "ok", _view(outcome)
    assert [row["labels"].get("opspilot_c5_shard") for row in _rows(outcome)] == ["s1"]


def test_line_filter_regex_with_brackets_is_not_a_range(tools):
    outcome = tools.execute(
        _call({"query": '{job="c5-bulk"} |~ "bulk line 0[0-4][0-9]$"', "limit": 500})
    )
    assert outcome.status == "ok", _view(outcome)
    assert len(_rows(outcome)) == 50


def test_lines_outside_the_query_window_never_return(loki, tools):
    # Control: Loki itself serves both lines (read on the engineer port).
    assert _direct_lines(loki, '{job="c5-app"} |= "future event"') == 1
    assert _direct_lines(loki, '{job="c5-app"} |= "old outage"') == 1
    future = tools.execute(_call({"query": '{job="c5-app"} |= "future event"'}))
    assert (future.status, future.reason) == ("no_data", "NO_DATA")
    old = tools.execute(_call({"query": '{job="c5-app"} |= "old outage"'}))
    assert (old.status, old.reason) == ("no_data", "NO_DATA")
    # The widest window the frame allows returns the old line, never the
    # future one.
    widest = tools.execute(
        _call(
            {
                "query": '{job="c5-app"} |= "outage"',
                "start": _z(FRAME.start),
                "end": _z(FRAME.end),
            }
        )
    )
    assert widest.status == "ok", _view(widest)
    assert _lines(widest) == [LINES_APP["a-old"][1]]


def test_metric_evaluation_starts_after_the_lookback(tools):
    outcome = tools.execute(
        _call(
            {
                "query": 'count_over_time({job="c5-burst"}[10m])',
                "start": _z(BURST_WINDOW.start),
                "end": _z(BURST_WINDOW.end),
            }
        )
    )
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    assert view["lookback_seconds"] == 600
    # The tool sends step = max(floor(1200 s / 250), 1) = 4 s and rounds the
    # evaluation start up to it, so the read starts at most one step after
    # the window start and never before it.
    read_start = datetime.fromisoformat(view["lookback_start_at"])
    assert BURST_WINDOW.start <= read_start < BURST_WINDOW.start + timedelta(seconds=4)
    assert view["window"]["start"] == view["lookback_start_at"]
    rows = _rows(outcome)
    assert len(rows) == 1
    points = rows[0]["values"]
    assert points
    first = min(float(point[0]) for point in points)
    assert first == (read_start + timedelta(minutes=10)).timestamp()
    # The burst before the window is never counted, the three inside are.
    assert {int(float(point[1])) for point in points} == {BURST_INSIDE}


def test_limit_marks_incomplete_and_keeps_the_newest(tools):
    outcome = tools.execute(_call({"query": '{job="c5-bulk"}'}))
    assert outcome.status == "ok", _view(outcome)
    lines = _lines(outcome)
    assert len(lines) == 100
    assert lines[0] == f"bulk line {BULK_LINES - 1:03d}"
    assert lines[-1] == f"bulk line {BULK_LINES - 100:03d}"
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in str(view["incomplete_reason"])


def test_oversized_answers_are_refused_whole(tools):
    raw = tools.execute(_call({"query": '{job="c5-big"}', "limit": BIG_LINES}))
    assert (raw.status, raw.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(raw)["content"] is None
    view = tools.execute(_call({"query": '{job="c5-big"}', "limit": 100}))
    assert (view.status, view.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(view)["content"] is None
    small = tools.execute(_call({"query": '{job="c5-big"}', "limit": 5}))
    assert small.status == "ok", _view(small)
    assert all(len(line) == 2000 for line in _lines(small))


def test_long_lines_are_cut_and_credentials_in_logs_are_redacted(tools):
    outcome = tools.execute(_call({"query": '{job="c5-app"}'}))
    assert outcome.status == "ok"
    text = _text(outcome)
    assert "L" * 2000 in text and "L" * 2001 not in text
    long_row = next(row for row in _rows(outcome) if row["line"].startswith("LLL"))
    assert long_row["_truncated_fields"] == ["line"]
    assert _view(outcome)["long_values_cut"] == 1
    assert CANARY not in text and "REDACTED" in text


# -- refusals before sending, errors from real responses ------------------------


def test_window_and_construct_refusals_are_never_sent(loki3, k8s_ca):
    tools = _tools_for(loki3, k8s_ca)
    before = _gateway_requests()
    refused = [
        {"query": '{job="c5-app"} offset 1h'},
        {"query": 'count_over_time({job="c5-app"}[1h] offset 5m)'},
        {"query": 'count_over_time({job="c5-app"}[2h])'},
        {"query": 'count_over_time({job="c5-app"}[5 minutes])'},
        # PR #724 review: Loki's lexer skips comments and reads '...' as a
        # char literal, so a quote inside one must not hide a range or offset.
        {"query": 'sum(count_over_time({job="c5-app"} /* " */ [1h] /* " */))'},
        {"query": 'sum(count_over_time({job="c5-app"}[1m] /* " */ offset 29m))'},
        {"query": "sum by ('\"') (count_over_time({job=`c5-app`}[1h])) #\""},
        {"query": 'sum(count_over_time({job="c5-app"}[1m])) // [30d]'},
        {
            "query": '{job="c5-app"}',
            "start": _z(FRAME.start - timedelta(hours=1)),
            "end": _z(NOW),
        },
        {"query": '{job="c5-app"}', "start": _z(NOW - timedelta(hours=1))},
        {"query": '{job="c5-app"}', "limit": 501},
    ]
    for params in refused:
        outcome = tools.execute(_call(params))
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), params
        assert _view(outcome)["source_contact"] == "none", params
    assert _gateway_requests() == before
    sent = tools.execute(_call({"query": '{job="c5-app"}'}))
    assert sent.status == "ok"
    # nginx writes the line when the request is finalized, just after the
    # answer: give it a moment, then exactly one request has arrived.
    deadline = time.monotonic() + 5
    while _gateway_requests() == before and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _gateway_requests() == before + 1


def test_error_classes_come_from_real_responses(loki3, loki29, k8s_ca):
    tools = _tools_for(loki3, k8s_ca)
    # The scenarios really produce these statuses (guards the setup itself).
    query = urllib.parse.urlencode({"query": '{job="c5-app"'})
    assert (
        _http(
            "GET",
            f"{LOKI3.direct}/loki/api/v1/query_range?{query}",
            headers=LOKI3.headers(),
        )[0]
        == 400
    )
    bad = tools.execute(_call({"query": '{job="c5-app"'}))
    assert (bad.status, bad.reason) == ("error", "INVALID_PARAMS")
    assert _view(bad)["source_error"]["http_status"] == 400
    assert "parse error" in str(_view(bad)["source_error"]["error"]).lower()

    nothing = tools.execute(_call({"query": '{job="c5-nothing-here"}'}))
    assert (nothing.status, nothing.reason) == ("no_data", "NO_DATA")

    wrong = _executor(
        _env(
            GATEWAY_LOKI3,
            k8s_ca,
            OPSPILOT_LOKI_USERNAME=READER,
            OPSPILOT_LOKI_PASSWORD="wrong569",
            OPSPILOT_LOKI_TENANT_ID=TENANT_A,
        )
    )
    outcome = wrong.execute(_call({"query": '{job="c5-app"}'}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert _view(outcome)["source_error"]["http_status"] == 401

    denied = _executor(_env(GATEWAY_DENY, k8s_ca, **_reader()))
    outcome = denied.execute(_call({"query": '{job="c5-app"}'}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert _view(outcome)["source_error"]["http_status"] == 403

    # Loki 3 has auth enabled: no tenant is Loki's own 401 ("no org id").
    no_tenant = _executor(_env(GATEWAY_LOKI3, k8s_ca, **_reader()))
    outcome = no_tenant.execute(_call({"query": '{job="c5-app"}'}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")

    unreachable = _executor(_env("http://127.0.0.1:9", k8s_ca, **_reader()))
    outcome = unreachable.execute(_call({"query": '{job="c5-app"}'}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")


def test_tenant_comes_from_configuration_only(loki3, k8s_ca):
    tenant_a = _tools_for(loki3, k8s_ca, TENANT_A)
    a_lines = _lines(tenant_a.execute(_call({"query": '{job="c5-app"}'})))
    assert "tenant b only line" not in a_lines
    tenant_b = _tools_for(loki3, k8s_ca, TENANT_B)
    outcome = tenant_b.execute(_call({"query": '{job="c5-app"}'}))
    assert outcome.status == "ok", _view(outcome)
    assert _lines(outcome) == ["tenant b only line"]


# -- read-only at both layers ---------------------------------------------------


def test_read_only_account_cannot_write_through_the_gateway(loki3, loki29):
    for loki in (loki3, loki29):
        before = _direct_lines(loki, '{job="c5-app"}')
        reader = {"Authorization": _basic(READER, READER_PASSWORD), **loki.headers()}
        push = {
            "streams": [
                {"stream": dict(APP), "values": [[_ns(NOW), "injected via gateway"]]}
            ]
        }
        delete_query = urllib.parse.urlencode(
            {"query": '{job="c5-app"}', "start": str(int(FRAME.start.timestamp()))}
        )
        attempts = [
            ("POST", "/loki/api/v1/push", push),
            ("PUT", "/loki/api/v1/push", push),
            ("POST", f"/loki/api/v1/delete?{delete_query}", None),
            ("PUT", f"/loki/api/v1/delete?{delete_query}", None),
            ("POST", "/flush", None),
            ("POST", "/loki/api/v1/query_range", b"query=%7Bjob%3D%22c5-app%22%7D"),
            ("GET", "/loki/api/v1/labels", None),
            ("GET", "/config", None),
        ]
        for method, path, body in attempts:
            status, answer = _http(method, loki.gateway + path, body, headers=reader)
            assert status == 403, (loki.name, method, path, status, answer)
        time.sleep(2)
        assert _direct_lines(loki, '{job="c5-app"}') == before, loki.name
        # The engineer port still accepts writes: the refusal is the
        # gateway's, not a broken backend.
        assert (
            _http(
                "POST",
                f"{loki.direct}/loki/api/v1/push",
                {
                    "streams": [
                        {"stream": {"job": "c5-probe"}, "values": [[_ns(NOW), "x"]]}
                    ]
                },
                headers=loki.headers(),
            )[0]
            == 204
        )
