"""#583: the read-only fake Kubernetes API server over one ITBench-Lite snapshot.

Synthetic snapshots only (hand-written rows in the export's column layout).
The integration tests run the real ``kubernetes_jq_query``/``kubernetes_count``,
``kubernetes_tabular_query`` and ``fetch_pod_logs`` through the normal
``otel-demo-ext`` executor against the server on 127.0.0.1 over HTTPS, with a
certificate made by ``openssl`` in ``tmp_path``. Every server is closed by its
fixture.
"""

from __future__ import annotations

import csv
import json
import shutil
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from scripts.itbench_lite import fake_k8s
from scripts.itbench_lite.shift import NS
from scripts.itbench_lite.snapshot import load_snapshot
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, FakeStore, _lease
from tests.test_m1_otel_demo_contract import WINDOW as AUTHORIZED_WINDOW

TOKEN = "aaaaaaaa357"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
COLUMNS = ["Timestamp", "TimestampTime", "Body", "ResourceAttributes", "LogAttributes"]

# Snapshot times sit around the authorized window of the shared fixture; the
# server's virtual now is the executor's trusted clock.
W0 = AUTHORIZED_WINDOW.start
W1 = AUTHORIZED_WINDOW.end
FIRST = W0 - timedelta(minutes=10)  # first full collection
SECOND = W0 + timedelta(minutes=1)  # second full collection
LATER = NOW + timedelta(minutes=5)  # after the virtual now: never served


def _ns(moment: datetime) -> int:
    return int(moment.timestamp()) * NS + moment.microsecond * 1000


def _ch(moment: datetime, nanos: int = 0) -> str:
    """A ClickHouse timestamp cell with nanoseconds."""
    base = moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return f"{base}.{moment.microsecond * 1000 + nanos:09d}"


def _rfc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pod(name, phase="Running", *, ns="otel-demo", app="checkout", containers=None):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app": app, "tier": "backend"},
            "creationTimestamp": _rfc(FIRST - timedelta(hours=1)),
            "managedFields": [{"manager": "synthetic"}],
        },
        "spec": {
            "nodeName": "node-a",
            "containers": [{"name": c} for c in (containers or [app])],
        },
        "status": {"phase": phase},
    }


def _obj(plural: str, body: dict, at: datetime) -> dict[str, str]:
    return {
        "Timestamp": _ch(at, 123),
        "TimestampTime": _ch(at)[:19],
        "Body": json.dumps(body),
        "ResourceAttributes": "{}",
        "LogAttributes": repr({"k8s.resource.name": plural}),
    }


def _event(uid: str, reason: str, at: datetime, *, api="v1", pod="checkout-1"):
    if api == "v1":
        obj = {
            "apiVersion": "v1",
            "kind": "Event",
            "metadata": {"name": f"{pod}.{uid}", "namespace": "otel-demo", "uid": uid},
            "involvedObject": {"kind": "Pod", "name": pod, "namespace": "otel-demo"},
            "reason": reason,
            "type": "Warning",
            "eventTime": None,
            "firstTimestamp": _rfc(at),
            "lastTimestamp": _rfc(at),
        }
    else:
        obj = {
            "apiVersion": "events.k8s.io/v1",
            "kind": "Event",
            "metadata": {"name": f"{pod}.{uid}", "namespace": "otel-demo", "uid": uid},
            "regarding": {"kind": "Pod", "name": pod},
            "reason": reason,
            "note": "synthetic",
        }
    return obj


def _watch(kind: str, obj: dict, at: datetime) -> dict[str, str]:
    return {
        "Timestamp": _ch(at, 7),
        "TimestampTime": _ch(at)[:19],
        "Body": json.dumps({"type": kind, "object": obj}),
        "ResourceAttributes": "{}",
        "LogAttributes": repr({"k8s.resource.name": "events"}),
    }


def _log(pod: str, text: str, at: datetime, *, deployment="checkout", nanos=0):
    attrs = {
        "k8s.pod.name": pod,
        "k8s.namespace.name": "otel-demo",
        "k8s.deployment.name": deployment,
        "service.name": deployment,
    }
    return {
        "Timestamp": _ch(at, nanos),
        "TimestampTime": _ch(at)[:19],
        "Body": text,
        "ResourceAttributes": repr(attrs),
        "LogAttributes": "{}",
    }


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _objects() -> list[dict[str, str]]:
    rows = [
        _obj("pods", _pod("checkout-1", "Pending"), FIRST),
        _obj("pods", _pod("checkout-1", "Running"), SECOND),
        _obj("pods", _pod("cart-1", "Failed", app="cart"), SECOND),
        _obj(
            "pods",
            _pod("multi-1", app="multi", containers=["sidecar", "multi"]),
            SECOND,
        ),
        _obj("pods", _pod("dns-1", ns="kube-system", app="dns"), SECOND),
        _obj("pods", _pod("checkout-1", "CrashLoopBackOff"), LATER),
        _obj(
            "nodes",
            {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-a"}},
            SECOND,
        ),
        _obj(
            "deployments",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "checkout", "namespace": "otel-demo"},
                "spec": {"replicas": 2},
                "status": {"readyReplicas": 1},
            },
            SECOND,
        ),
        _obj(
            "configmaps",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "flags", "namespace": "otel-demo"},
                "data": {"rollout": _rfc(FIRST)},
            },
            SECOND,
        ),
    ]
    # A fresh collection of pods arriving later never replaces the current one.
    return rows


def _events() -> list[dict[str, str]]:
    early, mid = W0 + timedelta(seconds=30), W0 + timedelta(minutes=2)
    return [
        _watch("ADDED", _event("u1", "BackOff", early), early),
        _watch("ADDED", _event("u1", "BackOff", early, api="events.k8s.io/v1"), early),
        _watch("ADDED", _event("u2", "Killing", early), early),
        _watch("MODIFIED", _event("u1", "BackOffAgain", mid), mid),
        _watch("DELETED", _event("u2", "Killing", early), mid),
        _watch("ADDED", _event("u3", "Future", LATER), LATER),
    ]


def _logs() -> list[dict[str, str]]:
    base = W0 + timedelta(minutes=1)
    rows = [
        _log(
            "checkout-1",
            # A trailing line break ends the record; it adds no empty line.
            f"order {i} placed" + ("\n" if i == 5 else ""),
            base + timedelta(seconds=i),
            nanos=i,
        )
        for i in range(6)
    ]
    rows.append(_log("checkout-1", "first\nsecond", base + timedelta(seconds=10)))
    rows.append(_log("checkout-1", "too late", LATER))
    rows.append(
        _log(
            "multi-1",
            "multi says hi",
            base,
            deployment="multi",
        )
    )
    return rows


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    root = tmp_path / "Scenario-0"
    root.mkdir()
    _write(root / "k8s_objects_raw.tsv", _objects())
    _write(root / "k8s_events_raw.tsv", _events())
    _write(root / "otel_logs_raw.tsv", _logs())
    return root


@pytest.fixture
def cluster(snapshot_dir: Path) -> fake_k8s.FakeCluster:
    return fake_k8s.FakeCluster.from_snapshot(
        load_snapshot(snapshot_dir),
        offset_seconds=0,
        token=TOKEN,
        clock=lambda: _ns(NOW),
    )


def _get(cluster, path: str, **query) -> fake_k8s.Reply:
    target = path + ("?" + urlencode(query) if query else "")
    return cluster.respond("GET", target, AUTH)


def _json(reply: fake_k8s.Reply) -> dict:
    assert reply.content_type == "application/json"
    return json.loads(reply.body)


def _names(reply: fake_k8s.Reply) -> list[str]:
    assert reply.status == 200, reply.body
    return [item["metadata"]["name"] for item in _json(reply)["items"]]


# -- objects -------------------------------------------------------------------


def test_list_is_the_latest_full_collection_not_after_the_virtual_now(cluster):
    reply = _get(cluster, "/api/v1/namespaces/otel-demo/pods")
    body = _json(reply)
    assert body["kind"] == "PodList" and body["apiVersion"] == "v1"
    assert [(i["metadata"]["name"], i["status"]["phase"]) for i in body["items"]] == [
        ("cart-1", "Failed"),
        ("checkout-1", "Running"),
        ("multi-1", "Running"),
    ]
    assert "continue" not in body["metadata"]
    assert _names(_get(cluster, "/api/v1/pods")) == [
        "dns-1",
        "cart-1",
        "checkout-1",
        "multi-1",
    ]


def test_the_virtual_now_picks_the_collection(snapshot_dir):
    def at(moment):
        return fake_k8s.FakeCluster.from_snapshot(
            load_snapshot(snapshot_dir),
            offset_seconds=0,
            token=TOKEN,
            clock=lambda: _ns(moment),
        )

    early = _get(at(FIRST + timedelta(seconds=5)), "/api/v1/pods")
    assert [i["status"]["phase"] for i in _json(early)["items"]] == ["Pending"]
    late = _json(_get(at(LATER + timedelta(seconds=5)), "/api/v1/pods"))
    assert [i["status"]["phase"] for i in late["items"]] == ["CrashLoopBackOff"]
    # Before the first full collection there is no answer, not an empty list.
    before = _get(at(FIRST - timedelta(minutes=1)), "/api/v1/pods")
    assert before.status == 503


def test_offset_moves_collection_times_and_body_times(snapshot_dir):
    offset = 3600
    moved = fake_k8s.FakeCluster.from_snapshot(
        load_snapshot(snapshot_dir),
        offset_seconds=offset,
        token=TOKEN,
        clock=lambda: _ns(NOW + timedelta(seconds=offset)),
    )
    body = _json(_get(moved, "/api/v1/namespaces/otel-demo/configmaps"))
    expected = _rfc(FIRST + timedelta(seconds=offset))
    assert body["items"][0]["data"]["rollout"] == expected
    pods = _json(_get(moved, "/api/v1/namespaces/otel-demo/pods"))
    assert {i["status"]["phase"] for i in pods["items"]} == {"Running", "Failed"}


def test_pagination_with_limit_and_continue_covers_the_list_once(cluster):
    first = _json(_get(cluster, "/api/v1/pods", limit="3"))
    assert len(first["items"]) == 3
    token = first["metadata"]["continue"]
    assert first["metadata"]["remainingItemCount"] == 1
    second = _json(_get(cluster, "/api/v1/pods", limit="3", **{"continue": token}))
    assert "continue" not in second["metadata"]
    names = [i["metadata"]["name"] for i in first["items"] + second["items"]]
    assert names == ["dns-1", "cart-1", "checkout-1", "multi-1"]
    bad = _get(cluster, "/api/v1/pods", limit="3", **{"continue": "%%%"})
    assert bad.status == 400


def test_label_selectors(cluster):
    path = "/api/v1/pods"
    assert _names(_get(cluster, path, labelSelector="app=checkout")) == ["checkout-1"]
    assert _names(_get(cluster, path, labelSelector="app==cart")) == ["cart-1"]
    assert _names(_get(cluster, path, labelSelector="app in (cart, dns)")) == [
        "dns-1",
        "cart-1",
    ]
    assert _names(
        _get(cluster, path, labelSelector="app notin (cart,dns),tier=backend")
    ) == ["checkout-1", "multi-1"]
    assert _names(_get(cluster, path, labelSelector="app!=checkout,tier")) == [
        "dns-1",
        "cart-1",
        "multi-1",
    ]
    assert _names(_get(cluster, path, labelSelector="!tier")) == []
    assert _get(cluster, path, labelSelector="app in (cart").status == 400


def test_field_selectors(cluster):
    path = "/api/v1/pods"
    assert _names(_get(cluster, path, fieldSelector="status.phase=Failed")) == [
        "cart-1"
    ]
    assert _names(
        _get(
            cluster,
            path,
            fieldSelector="metadata.namespace==otel-demo,status.phase!=Failed",
        )
    ) == ["checkout-1", "multi-1"]
    assert _names(_get(cluster, path, fieldSelector="metadata.name=dns-1")) == ["dns-1"]
    assert _names(_get(cluster, path, fieldSelector="spec.nodeName=node-a")) != []
    unsupported = _get(cluster, path, fieldSelector="spec.containers=x")
    assert unsupported.status == 400
    assert "field label not supported" in _json(unsupported)["message"]


def test_single_object_get_and_scope_rules(cluster):
    pod = _json(_get(cluster, "/api/v1/namespaces/otel-demo/pods/checkout-1"))
    assert pod["status"]["phase"] == "Running"
    assert _get(cluster, "/api/v1/namespaces/otel-demo/pods/nope").status == 404
    assert _names(_get(cluster, "/api/v1/nodes")) == ["node-a"]
    assert _get(cluster, "/api/v1/namespaces/otel-demo/nodes").status == 404
    deployments = _json(_get(cluster, "/apis/apps/v1/namespaces/otel-demo/deployments"))
    assert deployments["kind"] == "DeploymentList"
    assert deployments["apiVersion"] == "apps/v1"


def test_secrets_are_403_unseen_classes_503_and_seen_but_absent_empty(cluster):
    for path in (
        "/api/v1/secrets",
        "/api/v1/namespaces/otel-demo/secrets",
        "/api/v1/namespaces/otel-demo/secrets/db",
    ):
        reply = _get(cluster, path)
        assert reply.status == 403
        assert _json(reply)["reason"] == "Forbidden"
    for path in (
        "/apis/batch/v1/cronjobs",  # in the A2 table, never in a snapshot
        "/api/v1/replicationcontrollers",
        "/apis/chaos-mesh.org/v1alpha1/podchaos",
        "/apis/apps/v2/deployments",  # a version never recorded
    ):
        assert _get(cluster, path).status == 503, path
    empty = _json(_get(cluster, "/apis/networking.k8s.io/v1/networkpolicies"))
    assert empty["kind"] == "NetworkPolicyList" and empty["items"] == []
    assert _get(cluster, "/healthz").status == 404


def test_auth_and_read_only(cluster):
    assert cluster.respond("GET", "/api/v1/pods", {}).status == 401
    wrong = {"Authorization": "Bearer bbbbbbbb357"}
    assert cluster.respond("GET", "/api/v1/pods", wrong).status == 401
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert cluster.respond(method, "/api/v1/pods", AUTH).status == 405


# -- events --------------------------------------------------------------------


def test_events_are_the_core_watch_stream_replayed_to_the_virtual_now(cluster):
    body = _json(_get(cluster, "/api/v1/namespaces/otel-demo/events"))
    assert body["kind"] == "EventList"
    assert [(e["metadata"]["uid"], e["reason"]) for e in body["items"]] == [
        ("u1", "BackOffAgain")
    ]
    assert body["items"][0]["apiVersion"] == "v1"
    by_field = _get(
        cluster,
        "/api/v1/events",
        fieldSelector="involvedObject.kind=Pod,involvedObject.name=checkout-1,type=Warning",
    )
    assert _names(by_field) == ["checkout-1.u1"]
    # The events.k8s.io group is not served (A2 reads core v1 only).
    assert _get(cluster, "/apis/events.k8s.io/v1/events").status == 503


# -- logs ----------------------------------------------------------------------

LOG = "/api/v1/namespaces/otel-demo/pods/checkout-1/log"


def _lines(reply: fake_k8s.Reply) -> list[str]:
    assert reply.status == 200, reply.body
    assert reply.content_type == "text/plain"
    return reply.body.decode().splitlines()


def test_logs_render_records_up_to_now_with_timestamps(cluster):
    plain = _lines(_get(cluster, LOG))
    assert plain == [f"order {i} placed" for i in range(6)] + ["first", "second"]
    stamped = _lines(_get(cluster, LOG, timestamps="true", container="checkout"))
    base = W0 + timedelta(minutes=1)
    assert stamped[0] == f"{_rfc(base)} order 0 placed"  # zero fraction dropped
    assert stamped[1].startswith(
        _rfc(base + timedelta(seconds=1))[:-1] + ".000000001Z "
    )
    # A record whose body has a newline is two lines with the same timestamp.
    assert stamped[6].split(" ")[0] == stamped[7].split(" ")[0]
    assert stamped[6].endswith(" first") and stamped[7].endswith(" second")


def test_logs_since_time_since_seconds_tail_lines_and_limit_bytes(cluster):
    base = W0 + timedelta(minutes=1)
    since = _lines(_get(cluster, LOG, sinceTime=_rfc(base + timedelta(seconds=4))))
    assert since == ["order 4 placed", "order 5 placed", "first", "second"]
    assert _lines(_get(cluster, LOG, tailLines="2")) == ["first", "second"]
    assert _lines(_get(cluster, LOG, tailLines="0")) == []
    window = int((NOW - base).total_seconds()) - 5
    assert _lines(_get(cluster, LOG, sinceSeconds=str(window)))[0] == "order 5 placed"
    cut = _get(cluster, LOG, limitBytes="10")
    assert cut.body == b"order 0 pl"
    both = _get(cluster, LOG, sinceTime=_rfc(base), sinceSeconds="10")
    assert both.status == 400
    assert _get(cluster, LOG, sinceTime="yesterday").status == 400
    for impossible in (
        "2026-99-99T99:99:99Z",
        "2026-02-30T00:00:00Z",
        "2026-01-01T00:00:00+99:00",
    ):
        reply = _get(cluster, LOG, sinceTime=impossible)
        assert reply.status == 400, impossible
        assert "sinceTime" in _json(reply)["message"]
    assert _get(cluster, LOG, tailLines="-1").status == 400


def test_logs_previous_is_400_and_unknown_pod_is_404(cluster):
    previous = _get(cluster, LOG, container="checkout", previous="true")
    assert previous.status == 400
    assert "previous terminated container" in _json(previous)["message"]
    gone = _get(cluster, "/api/v1/namespaces/otel-demo/pods/nope/log")
    assert gone.status == 404


def test_multi_container_logs_sit_on_the_deployment_named_container(cluster):
    path = "/api/v1/namespaces/otel-demo/pods/multi-1/log"
    assert _get(cluster, path).status == 400  # several containers, none named
    assert _lines(_get(cluster, path, container="multi")) == ["multi says hi"]
    assert _lines(_get(cluster, path, container="sidecar")) == []
    assert _get(cluster, path, container="nope").status == 400
    # A Pod without log records answers an empty stream.
    assert _lines(_get(cluster, "/api/v1/namespaces/otel-demo/pods/cart-1/log")) == []


# -- loading -------------------------------------------------------------------


def test_loader_refuses_a_directory_with_an_answer_file(snapshot_dir):
    from scripts.itbench_lite.snapshot import AnswerFilePresent

    (snapshot_dir / "ground_truth.yaml").write_text("")  # name only; never read
    with pytest.raises(AnswerFilePresent):
        fake_k8s.main(
            [
                str(snapshot_dir),
                "--offset-seconds",
                "0",
                "--token-file",
                "/nonexistent",
                "--cert-dir",
                "/nonexistent",
            ]
        )


def test_bind_address_must_be_loopback(cluster, tmp_path):
    with pytest.raises(ValueError):
        fake_k8s.serve(
            cluster,
            cert_file="x",
            key_file="y",
            host="0.0.0.0",  # noqa: S104
        )
    with pytest.raises(ValueError):
        fake_k8s.serve(cluster, cert_file="x", key_file="y", host="localhost")


# -- the server over HTTPS ---------------------------------------------------------


def _need_openssl() -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required to make the test certificate")


@pytest.fixture
def server(cluster, tmp_path) -> Iterator[fake_k8s.RunningServer]:
    _need_openssl()
    cert, key = fake_k8s.generate_self_signed_cert(tmp_path / "tls")
    running = fake_k8s.serve(cluster, cert_file=cert, key_file=key)
    try:
        yield running
    finally:
        running.close()


def test_https_on_loopback_with_the_generated_ca(server):
    assert server.url.startswith("https://127.0.0.1:")
    context = ssl.create_default_context(cafile=str(server.ca_file))
    request = urllib.request.Request(
        server.url + "/api/v1/namespaces/otel-demo/pods", headers=AUTH
    )
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        assert response.status == 200
        assert len(json.loads(response.read())["items"]) == 3
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(
            urllib.request.Request(server.url + "/api/v1/secrets", headers=AUTH),
            context=context,
            timeout=10,
        )
    assert error.value.code == 403
    error.value.close()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(
            server.url + "/api/v1/pods",
            context=ssl.create_default_context(),
            timeout=10,
        )


# -- integration: the real tools through the otel-demo-ext executor ------------------


def _require_jq() -> None:
    if shutil.which("jq") is None:
        pytest.skip("local jq is required for Kubernetes execution tests")


@pytest.fixture
def executor(server):
    _require_jq()
    env = {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": server.url,
        "OPSPILOT_K8S_TOKEN": TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(server.ca_file),
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_TOKEN": "otel-aaaaaaaa357",
    }
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    clock = FakeClock(start=NOW)
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
    )
    return profile.executor_factory(store, RecordingSink(), clock)(lease, model_input)


def _call(tool: str, params: dict, index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="lite-583",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=AUTHORIZED_WINDOW.as_json(),
    )


def _ok(outcome) -> dict:
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    assert outcome.model_view is not None
    return outcome.model_view


def test_real_jq_query_and_count_read_the_snapshot(executor):
    view = _ok(
        executor.execute(
            _call(
                "kubernetes_jq_query",
                {
                    "kind": "pods",
                    "namespace": "otel-demo",
                    "label_selector": "tier=backend",
                    "field_selector": "status.phase!=Failed",
                    "jq_expr": ".items[] | {name: .metadata.name, phase: .status.phase}",
                },
            )
        )
    )
    assert view["content"] == [
        {"name": "checkout-1", "phase": "Running"},
        {"name": "multi-1", "phase": "Running"},
    ]
    events = _ok(
        executor.execute(
            _call(
                "kubernetes_count",
                {"kind": "events", "jq_expr": ".items[] | .reason"},
                index=1,
            )
        )
    )
    # The replayed stream: u1 kept at its latest version, u2 deleted, u3 later.
    assert events["matches_found"] == 1
    assert events["content"] == ['"BackOffAgain"']
    denied = executor.execute(
        _call("kubernetes_jq_query", {"kind": "cronjobs", "jq_expr": "."}, index=2)
    )
    assert denied.status != "ok" and denied.reason == "SOURCE_UNAVAILABLE"


def test_real_tabular_query_reads_the_snapshot(executor):
    view = _ok(
        executor.execute(
            _call(
                "kubernetes_tabular_query",
                {
                    "kind": "deployments",
                    "namespace": "otel-demo",
                    "columns": "NAME:.metadata.name,WANT:.spec.replicas,READY:.status.readyReplicas",
                },
            )
        )
    )
    assert view["content"] == [["checkout", "2", "1"]]


def test_real_pod_logs_read_the_snapshot(executor):
    view = _ok(
        executor.execute(
            _call(
                "fetch_pod_logs", {"pod_name": "checkout-1", "namespace": "otel-demo"}
            )
        )
    )
    rows = view["content"]
    texts = [row["text"] for row in rows]
    assert texts == [f"order {i} placed" for i in range(6)] + ["first", "second"]
    assert all(row["container"] == "checkout" for row in rows)
    assert not any(row["previous"] for row in rows)
    # The previous stream's 400 is a note, not a failure (A3-4).
    assert "previous" in json.dumps(view).lower()


# -- fail-closed loading and version isolation (review of PR #603) -------------------


def _bad_object(**changes) -> dict[str, str]:
    row = _obj("pods", _pod("bad-1"), SECOND)
    row.update(changes)
    return row


def _body_without(key: str) -> str:
    body = _pod("bad-1")
    if key == "name":
        del body["metadata"]["name"]
    else:
        del body[key]
    return json.dumps(body)


@pytest.mark.parametrize(
    ("file", "row", "reason"),
    [
        ("k8s_objects_raw.tsv", _bad_object(Body=""), "no Body"),
        ("k8s_objects_raw.tsv", _bad_object(Body="{not json"), "not JSON"),
        ("k8s_objects_raw.tsv", _bad_object(Body="[1]"), "not an object"),
        ("k8s_objects_raw.tsv", _bad_object(LogAttributes=""), "no LogAttributes"),
        ("k8s_objects_raw.tsv", _bad_object(LogAttributes="{'x': 'y'}"), "k8s.resource.name"),
        ("k8s_objects_raw.tsv", _bad_object(LogAttributes="{'k8s"), "does not parse"),
        ("k8s_objects_raw.tsv", _bad_object(Body=_body_without("apiVersion")), "apiVersion"),
        ("k8s_objects_raw.tsv", _bad_object(Body=_body_without("kind")), "kind"),
        ("k8s_objects_raw.tsv", _bad_object(Body=_body_without("name")), "metadata.name"),
        ("k8s_objects_raw.tsv", _bad_object(Timestamp="yesterday"), "Timestamp"),
        ("k8s_events_raw.tsv", {**_watch("ADDED", _event("x", "R", W0), W0), "Body": ""}, "no Body"),
        ("k8s_events_raw.tsv", _watch("BOOKMARK", _event("x", "R", W0), W0), "watch type"),
        ("k8s_events_raw.tsv", {**_watch("ADDED", {}, W0), "Body": '{"type": "ADDED"}'}, "no object"),
        ("k8s_events_raw.tsv", _watch("ADDED", {**_event("x", "R", W0), "apiVersion": "v1beta1"}, W0), "apiVersion"),
        ("k8s_events_raw.tsv", _watch("ADDED", {**_event("x", "R", W0), "metadata": {}}, W0), "neither uid nor name"),
        ("otel_logs_raw.tsv", {**_log("p", "t", W0), "ResourceAttributes": "{'k8s.namespace.name': 'otel-demo'}"}, "k8s.pod.name"),
        ("otel_logs_raw.tsv", {**_log("p", "t", W0), "ResourceAttributes": "{'k8s.pod.name': 'p'}"}, "k8s.namespace.name"),
        ("otel_logs_raw.tsv", {**_log("p", "t", W0), "ResourceAttributes": ""}, "no ResourceAttributes"),
        ("otel_logs_raw.tsv", {**_log("p", "t", W0), "Timestamp": ""}, "Timestamp"),
    ],
)  # fmt: skip
def test_a_bad_row_refuses_to_start_naming_file_and_row(
    snapshot_dir, file, row, reason
):
    path = snapshot_dir / file
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    rows.insert(1, row)  # the second data row
    _write(path, rows)
    with pytest.raises(fake_k8s.SnapshotRowError) as error:
        fake_k8s.FakeCluster.from_snapshot(
            load_snapshot(snapshot_dir), offset_seconds=0, token=TOKEN
        )
    message = str(error.value)
    assert f"{file} data row 2" in message and reason in message


def test_log_column_without_body_refuses_to_start(snapshot_dir):
    path = snapshot_dir / "otel_logs_raw.tsv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Timestamp", "TimestampTime", "ResourceAttributes"],
            delimiter="\t",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(_logs())
    with pytest.raises(fake_k8s.SnapshotRowError, match="no Body column"):
        fake_k8s.FakeCluster.from_snapshot(
            load_snapshot(snapshot_dir), offset_seconds=0, token=TOKEN
        )


def test_objects_are_indexed_by_group_version_and_plural(snapshot_dir):
    path = snapshot_dir / "k8s_objects_raw.tsv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    old = {
        "apiVersion": "apps/v1beta1",
        "kind": "Deployment",
        "metadata": {"name": "legacy", "namespace": "otel-demo"},
    }
    rows.append(_obj("deployments", old, SECOND))
    _write(path, rows)
    cluster = fake_k8s.FakeCluster.from_snapshot(
        load_snapshot(snapshot_dir),
        offset_seconds=0,
        token=TOKEN,
        clock=lambda: _ns(NOW),
    )
    assert _names(_get(cluster, "/apis/apps/v1/deployments")) == ["checkout"]
    legacy = _json(_get(cluster, "/apis/apps/v1beta1/namespaces/otel-demo/deployments"))
    assert legacy["apiVersion"] == "apps/v1beta1"
    assert [i["metadata"]["name"] for i in legacy["items"]] == ["legacy"]
    assert _get(cluster, "/apis/apps/v1beta2/deployments").status == 503
