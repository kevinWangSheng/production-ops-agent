"""Real-cluster integration tests for tool expansion B3 (#562), R8-25 and
B3-13 ... B3-15: ``kubectl_top_pods`` and ``kubectl_top_nodes`` against a
single-node kind cluster with metrics-server, through the public tool path,
with only the read-only identity's token. No model is called.

Runs only in the CI job ``k8s-live-metrics`` (``OPSPILOT_K8S_LIVE=1``),
which creates the cluster; skipped everywhere else. The tests compare what
the tool read with what the cluster reports (``kubectl get --raw``); they
never assert anything about a fault.

What the job is assumed to provide (not pinned by the interface; each can be
overridden by the environment variable named):

* ``kubectl`` on PATH, its current context the kind cluster's admin
  (engineer side, used for fixtures, ``get --raw`` and the destructive
  steps only).
* API address and CA: ``B3_K8S_API_URL`` / ``B3_K8S_CA_FILE``, else taken
  from ``kubectl config view --raw --minify``.
* Read-only token: ``B3_READONLY_TOKEN``, else
  ``kubectl create token opspilot-investigator -n opspilot-readonly`` (the
  identity of ``scripts/kind_lab/opspilot-readonly-rbac.yaml``).
* Synthetic load labelled ``b3-role``: in ``B3_NS_A`` (default ``ns-a``)
  ``b3-role=cpu`` (CPU limit 200m, busy), ``b3-role=memory`` (holds about
  64 MiB) and ``b3-role=multi`` (two containers); in ``B3_NS_B`` (default
  ``ns-b``) ``b3-role=idle``.
* metrics-server is the Deployment ``metrics-server`` in ``kube-system``,
  served as ``APIService v1beta1.metrics.k8s.io``.

The last test scales metrics-server to zero and then deletes the
APIService; it must stay last in this file (pytest runs in file order).
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_K8S_LIVE") != "1",
    reason="the kind cluster with metrics-server runs only in CI k8s-live-metrics",
)

PODS = "kubectl_top_pods"
NODES = "kubectl_top_nodes"
NS_A = os.environ.get("B3_NS_A", "ns-a")
NS_B = os.environ.get("B3_NS_B", "ns-b")
READONLY_SA = ("opspilot-readonly", "opspilot-investigator")
NO_METRICS_SA = ("default", "b3-no-metrics")
METRICS_DEPLOYMENT = ("kube-system", "metrics-server")
APISERVICE = "v1beta1.metrics.k8s.io"
WRONG_TOKEN = "b3-not-a-valid-token-aaaaaaaa"
MIB = 1024 * 1024

ALLOWED_PATHS = re.compile(
    r"/apis/metrics\.k8s\.io/v1beta1/(pods|nodes|namespaces/[a-z0-9-]+/pods)"
    r"|/api/v1/nodes"
)
_FIRST_NOTE = re.compile(
    r"Sampled at (\S+)–(\S+): current usage, (\d+) s (after|before) the end of "
    r"the authorized window \((.+)\)\."
)


# -- engineer side --------------------------------------------------------------------


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("kubectl")
    assert binary, "kubectl is a precondition of the k8s-live-metrics job"
    return subprocess.run(
        [binary, *args], capture_output=True, text=True, check=check, timeout=120
    )


def raw_get(path: str) -> dict[str, Any]:
    return json.loads(kubectl("get", "--raw", path).stdout)


def _token(namespace: str, account: str) -> str:
    return kubectl(
        "create", "token", account, "-n", namespace, "--duration=1h"
    ).stdout.strip()


@pytest.fixture(scope="module")
def cluster(tmp_path_factory) -> dict[str, str]:
    url = os.environ.get("B3_K8S_API_URL")
    ca_file = os.environ.get("B3_K8S_CA_FILE")
    if not url or not ca_file:
        config = json.loads(
            kubectl("config", "view", "--raw", "--minify", "-o", "json").stdout
        )
        entry = config["clusters"][0]["cluster"]
        url = url or entry["server"]
        if not ca_file:
            path = tmp_path_factory.mktemp("b3-ca") / "cluster-ca.pem"
            path.write_bytes(base64.b64decode(entry["certificate-authority-data"]))
            ca_file = str(path)
    token = os.environ.get("B3_READONLY_TOKEN") or _token(*READONLY_SA)
    return {"url": url.rstrip("/"), "ca": ca_file, "token": token}


def _env(cluster: Mapping[str, str], token: str | None = None) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_LIVE_METRICS": "1",
        "OPSPILOT_K8S_API_URL": cluster["url"],
        "OPSPILOT_K8S_TOKEN": token or cluster["token"],
        "OPSPILOT_K8S_CA_FILE": cluster["ca"],
        "OPSPILOT_K8S_NAMESPACE": NS_A,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": "ci-only-aaaaaaaa562",
    }


def _run(
    cluster: Mapping[str, str],
    tool: str,
    params: dict[str, object] | None = None,
    *,
    token: str | None = None,
):
    """One call through a freshly built worker; the frame ends now."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = FakeClock(start=now)
    profile = select_profile(_env(cluster, token))
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    deadline = now + timedelta(minutes=10)
    store = FakeStore(lease, deadline=deadline)
    factory = profile.executor_factory(store, RecordingSink(), clock)
    executor = factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="How busy is ns-a?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=8,
        ),
    )
    frame = Window(now - timedelta(hours=6), now)
    return executor.execute(
        ToolRequest(
            step_id="b3-live",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params or {}),
            window=frame.as_json(),
        )
    )


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _rows(outcome) -> list[Mapping[str, Any]]:
    assert outcome.status == "ok", _view(outcome)
    return list(_view(outcome)["content"])


def _roles() -> dict[str, tuple[str, str]]:
    listing = json.loads(
        kubectl("get", "pods", "-A", "-l", "b3-role", "-o", "json").stdout
    )
    return {
        item["metadata"]["labels"]["b3-role"]: (
            item["metadata"]["namespace"],
            item["metadata"]["name"],
        )
        for item in listing["items"]
    }


@pytest.fixture(scope="module")
def roles(cluster) -> dict[str, tuple[str, str]]:
    found = _roles()
    assert {"cpu", "memory", "multi", "idle"} <= set(found), found
    deadline = time.monotonic() + 240
    while True:  # metrics-server needs a scrape or two after the Pods start
        outcome = _run(cluster, PODS, {"namespace": NS_A})
        if outcome.status == "ok":
            names = {r["pod"] for r in _rows(outcome)}
            if {found[k][1] for k in ("cpu", "memory", "multi")} <= names:
                return found
        assert time.monotonic() < deadline, _view(outcome)
        time.sleep(5)


# -- reference arithmetic (kubectl's: exact value, rounded up) ---------------------------

_QUANTITY = re.compile(
    r"([+-]?(?:\d+\.?\d*|\.\d+))(?:[eE]([+-]?\d+)|(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E))?"
)
_SUFFIX = {
    None: Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}


def exact(quantity: str) -> Decimal:
    match = _QUANTITY.fullmatch(quantity)
    assert match, quantity
    number = Decimal(match[1])
    if match[2] is not None:
        return number * (Decimal(10) ** int(match[2]))
    return number * _SUFFIX[match[3]]


def ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding="ROUND_CEILING"))


def second_z(text: str) -> str:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def expected_pod(item: Mapping[str, Any]) -> dict[str, Any]:
    containers = sorted(item["containers"], key=lambda c: c["name"])
    cpu = [exact(c["usage"]["cpu"]) * 1000 for c in containers]
    memory = [exact(c["usage"]["memory"]) for c in containers]
    return {
        "namespace": item["metadata"]["namespace"],
        "pod": item["metadata"]["name"],
        "cpu_millicores": ceil(sum(cpu, Decimal(0))),
        "memory_bytes": ceil(sum(memory, Decimal(0))),
        "timestamp": second_z(item["timestamp"]),
        "window": item["window"],
        "containers": [
            {"name": c["name"], "cpu_millicores": ceil(m), "memory_bytes": ceil(b)}
            for c, m, b in zip(containers, cpu, memory)
        ],
    }


def _compare(
    read: Callable[[], list[Mapping[str, Any]]],
    raw: Callable[[], list[Mapping[str, Any]]],
    key: Callable[[Mapping[str, Any]], str],
    check: Callable[[Mapping[str, Any], Mapping[str, Any]], None],
) -> int:
    """Rows read at (nearly) the same moment, compared where the sampling
    instant agrees; retried when a scrape landed between the two reads."""
    for _ in range(6):
        rows = read()
        reference = {key(item): item for item in raw()}
        same = [
            r
            for r in rows
            if key(r) in reference
            and second_z(reference[key(r)]["timestamp"]) == r["timestamp"]
        ]
        if same:
            for row in same:
                check(row, reference[key(row)])
            return len(same)
        time.sleep(3)
    pytest.fail("no row shared its sampling instant with kubectl get --raw")


# -- both tools read the real cluster ---------------------------------------------------------


def test_pods_without_namespace_cover_the_whole_cluster(cluster, roles):
    rows = _rows(_run(cluster, PODS))
    namespaces = {r["namespace"] for r in rows}
    assert {"kube-system", NS_A, NS_B} <= namespaces
    assert roles["idle"][1] in {r["pod"] for r in rows if r["namespace"] == NS_B}
    assert rows == sorted(rows, key=lambda r: (r["namespace"], r["pod"]))


def test_namespace_reads_only_that_namespace(cluster, roles):
    rows = _rows(_run(cluster, PODS, {"namespace": NS_A}))
    assert {r["namespace"] for r in rows} == {NS_A}
    names = {r["pod"] for r in rows}
    assert {roles[k][1] for k in ("cpu", "memory", "multi")} <= names


def test_usage_magnitudes_and_the_pod_total_rule(cluster, roles):
    by = {r["pod"]: r for r in _rows(_run(cluster, PODS, {"namespace": NS_A}))}
    cpu = by[roles["cpu"][1]]
    assert 120 <= cpu["cpu_millicores"] <= 260, cpu
    memory = by[roles["memory"][1]]
    assert memory["memory_bytes"] >= 64 * MIB, memory
    multi = by[roles["multi"][1]]
    assert len(multi["containers"]) == 2, multi
    assert [c["name"] for c in multi["containers"]] == sorted(
        c["name"] for c in multi["containers"]
    )
    summed = sum(c["cpu_millicores"] for c in multi["containers"])
    assert 0 <= summed - multi["cpu_millicores"] < len(multi["containers"])
    assert multi["memory_bytes"] == sum(c["memory_bytes"] for c in multi["containers"])
    for row in by.values():
        assert type(row["cpu_millicores"]) is int and row["cpu_millicores"] >= 0
        assert type(row["memory_bytes"]) is int and row["memory_bytes"] >= 0


def test_pod_rows_match_kubectl_get_raw(cluster, roles):
    path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{NS_A}/pods"

    def check(row, item):
        assert row == expected_pod(item)

    matched = _compare(
        lambda: _rows(_run(cluster, PODS, {"namespace": NS_A})),
        lambda: raw_get(path)["items"],
        lambda r: r["pod"] if "pod" in r else r["metadata"]["name"],
        check,
    )
    assert matched >= 1


def test_node_rows_match_kubectl_get_raw(cluster, roles):
    allocatable = {
        item["metadata"]["name"]: item["status"]["allocatable"]
        for item in raw_get("/api/v1/nodes")["items"]
    }

    def percent(usage: Decimal, total: str) -> set[int]:
        # Whether the rounded-up or the exact usage is divided is not
        # pinned; both floor to the same value except at a boundary.
        whole = exact(total)
        return {
            math.floor(usage * 100 / whole),
            math.floor(Decimal(ceil(usage)) * 100 / whole),
        }

    def check(row, item):
        cpu = exact(item["usage"]["cpu"]) * 1000
        memory = exact(item["usage"]["memory"])
        assert row["cpu_millicores"] == ceil(cpu)
        assert row["memory_bytes"] == ceil(memory)
        assert row["window"] == item["window"]
        alloc = allocatable[row["node"]]
        assert row["cpu_percent"] in percent(cpu / 1000, alloc["cpu"]), row
        assert row["memory_percent"] in percent(memory, alloc["memory"]), row

    matched = _compare(
        lambda: _rows(_run(cluster, NODES)),
        lambda: raw_get("/apis/metrics.k8s.io/v1beta1/nodes")["items"],
        lambda r: r["node"] if "node" in r else r["metadata"]["name"],
        check,
    )
    assert matched >= 1


def test_node_rows_have_values(cluster, roles):
    rows = _rows(_run(cluster, NODES))
    assert rows
    for row in rows:
        assert row["cpu_millicores"] > 0 and row["memory_bytes"] > 0, row
        for name in ("cpu_percent", "memory_percent"):
            assert type(row[name]) is int and row[name] >= 0, row


@pytest.mark.parametrize("tool", [PODS, NODES])
def test_current_state_time_semantics(cluster, roles, tool):
    outcome = _run(cluster, tool)
    view = _view(outcome)
    rows = _rows(outcome)
    assert view["source_start_at"] is None and view["source_end_at"] is None
    assert view["data_as_of"] is None
    stamps = sorted(r["timestamp"] for r in rows)
    assert view["sampled_at"] == {"earliest": stamps[0], "latest": stamps[-1]}
    match = _FIRST_NOTE.fullmatch(str(view["notes"][0]))
    assert match, view["notes"]
    assert (match[1], match[2]) == (stamps[0], stamps[-1])
    assert view.get("cannot_prove")


# -- two layers of read-only ----------------------------------------------------------------


@pytest.fixture
def recorded(monkeypatch) -> Iterator[list[tuple[str, str]]]:
    """Every request a tool's opener sends: (method, URL)."""
    sent: list[tuple[str, str]] = []
    real = urllib.request.build_opener

    def build(*handlers):
        opener = real(*handlers)
        original = opener.open

        def open_(request, *args, **kwargs):
            if isinstance(request, urllib.request.Request):
                sent.append((request.get_method(), request.full_url))
            else:
                sent.append(("GET", str(request)))
            return original(request, *args, **kwargs)

        opener.open = open_  # type: ignore[method-assign]
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build)
    yield sent


def test_the_tools_send_only_gets_of_the_fixed_paths(cluster, roles, recorded):
    for tool, params in ((PODS, {}), (PODS, {"namespace": NS_A}), (NODES, {})):
        assert _run(cluster, tool, params).status == "ok"
    assert recorded
    for method, url in recorded:
        parts = urlsplit(url)
        assert method == "GET", (method, url)
        assert ALLOWED_PATHS.fullmatch(parts.path), url
        if parts.path.startswith("/apis/metrics.k8s.io/"):
            assert parts.query == "", url
        else:
            assert set(re.findall(r"([a-z]+)=", parts.query)) <= {"limit", "continue"}


def _direct(
    cluster: Mapping[str, str], method: str, path: str, body: bytes | None = None
) -> int:
    context = ssl.create_default_context(cafile=cluster["ca"])
    request = urllib.request.Request(
        cluster["url"] + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {cluster['token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=30) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def test_the_read_only_identity_cannot_write_metrics_or_read_secrets(cluster, roles):
    pod = roles["cpu"][1]
    body = json.dumps(
        {"apiVersion": "metrics.k8s.io/v1beta1", "kind": "PodMetrics",
         "metadata": {"name": "b3-forged", "namespace": NS_A}}
    ).encode()  # fmt: skip
    base = f"/apis/metrics.k8s.io/v1beta1/namespaces/{NS_A}/pods"
    assert _direct(cluster, "POST", base, body) in (403, 405)
    assert _direct(cluster, "DELETE", f"{base}/{pod}") in (403, 405)
    assert _direct(cluster, "DELETE", "/apis/metrics.k8s.io/v1beta1/nodes/x") in (
        403,
        405,
    )
    assert _direct(cluster, "GET", "/api/v1/secrets") == 403
    assert _direct(cluster, "GET", f"/api/v1/namespaces/{NS_A}/secrets") == 403
    # Control: the same token reads the metrics it is granted.
    assert _direct(cluster, "GET", base) == 200


# -- error classes from real responses ------------------------------------------------------


def test_a_wrong_token_is_source_forbidden(cluster, roles):
    for tool in (PODS, NODES):
        outcome = _run(cluster, tool, token=WRONG_TOKEN)
        assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
            outcome
        )
        assert (_view(outcome).get("source_error") or {}).get("http_status") in (
            401,
            None,
        )


def test_an_identity_without_metrics_rights_is_source_forbidden(cluster, roles):
    namespace, account = NO_METRICS_SA
    created = kubectl("create", "serviceaccount", account, "-n", namespace, check=False)
    assert created.returncode == 0 or "AlreadyExists" in created.stderr, created.stderr
    token = _token(namespace, account)
    for tool in (PODS, NODES):
        outcome = _run(cluster, tool, token=token)
        assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
            outcome
        )


def _until(cluster, reason: str, seconds: float = 120):
    deadline = time.monotonic() + seconds
    while True:
        outcome = _run(cluster, PODS, {"namespace": NS_A})
        if (outcome.status, outcome.reason) == ("error", reason):
            return outcome
        assert time.monotonic() < deadline, _view(outcome)
        time.sleep(3)


def test_zz_metrics_server_down_then_api_removed(cluster, roles):
    """Destructive, last: no replica (503), then no metrics.k8s.io (404)."""
    namespace, name = METRICS_DEPLOYMENT
    kubectl("scale", f"deployment/{name}", "-n", namespace, "--replicas=0")
    kubectl(
        "wait", "--for=delete", "pod", "-n", namespace, "-l", "k8s-app=metrics-server",
        "--timeout=120s", check=False,
    )  # fmt: skip
    _until(cluster, "SOURCE_UNAVAILABLE")
    outcome = _run(cluster, NODES)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), _view(
        outcome
    )
    kubectl("delete", "apiservice", APISERVICE)
    outcome = _until(cluster, "SOURCE_ERROR")
    text = json.dumps(_view(outcome)).lower()
    assert "metrics-server" in text or "metrics.k8s.io" in text, _view(outcome)
    outcome = _run(cluster, NODES)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), _view(outcome)
