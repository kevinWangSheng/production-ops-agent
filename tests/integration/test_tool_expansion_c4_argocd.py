"""Real-cluster integration tests for tool expansion C4 (#568): the four
ArgoCD read-only tools against the kind cluster ``b4`` of the CI job
``k8s-backends`` (R8-25), through the public tool path and the real
``KubernetesClient``, with only the read-only identity's tokens. No model is
called.

Written by the independent test author from the C4 task record
(docs/tasks/2026-11-20-tool-expansion-c4.md, "CI" and assertions A-1 ... A-9),
without reading the implementation. Runs only when
``OPSPILOT_K8S_BACKENDS_LIVE=1``; skipped everywhere else.

What the job provides (fixed by the interface; shared with B4):

* the kind cluster ``b4`` with the three ArgoCD v3.6.1 CRDs applied
  server-side and no ArgoCD controller (status is written with the object);
  ``kubectl`` on PATH with the admin kubeconfig (engineer side only);
* ``B4_K8S_API_URL``, ``B4_K8S_CA_FILE``; ``B4_K8S_TOKEN_FILE`` (read-only
  identity plus the ArgoCD overlay ``opspilot-investigator-argocd``),
  ``B4_K8S_BASE_TOKEN_FILE`` (an identity without the overlay);
  ``C4_CANARIES`` (one canary per line), ``C4_ARGOCD_NAMESPACE`` (``argocd``);
* the seed of ``tests/fixtures/c4_argocd/seed.sh``: in ``argocd``
  ``c4-healthy``, ``c4-degraded``, ``c4-history``, ``c4-canary``, the
  ApplicationSet ``c4-set`` and the AppProject ``c4-proj``; in ``c4-other``
  ``c4-elsewhere``.

Assumptions where the interface is silent (also in the reply to the lead):

* The CI step runs this file twice: once with the CRD installed, then after
  ``kubectl delete crd applications.argoproj.io`` with
  ``C4_ARGOCD_CRD_REMOVED=1``. Each test is gated on exactly one of the two
  states, so both runs can invoke the whole file.
* Expected values of A-2/A-3 are read back from the apiserver with the admin
  kubeconfig rather than copied from ``apps.yaml``, so the seed's exact
  messages and revisions are not pinned here.
* The seed's times are fixed past instants; "outside the authorized window" is
  asserted as "at least one history entry before the frame start".

Canary and seed bodies are never printed: failures list canary names only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_K8S_BACKENDS_LIVE") != "1",
    reason="the kind cluster b4 with ArgoCD CRDs runs only in CI k8s-backends",
)

CRD_REMOVED = os.environ.get("C4_ARGOCD_CRD_REMOVED") == "1"
with_crd = pytest.mark.skipif(CRD_REMOVED, reason="needs the Application CRD")
without_crd = pytest.mark.skipif(
    not CRD_REMOVED, reason="runs after the Application CRD is deleted"
)

REPO = Path(__file__).resolve().parents[2]
SWITCH = "OPSPILOT_ARGOCD"
LIST = "argocd_app_list"
GET = "argocd_app_get"
HISTORY = "argocd_app_history"
RESOURCES = "argocd_app_resources"
ARGO_TOOLS = (LIST, GET, HISTORY, RESOURCES)
GROUP = "/apis/argoproj.io/v1alpha1"
SEEDED = {
    ("argocd", "c4-canary"),
    ("argocd", "c4-degraded"),
    ("argocd", "c4-healthy"),
    ("argocd", "c4-history"),
    ("c4-other", "c4-elsewhere"),
}
INTERFACE_CANARIES = {
    "CANARY-C4-VALUES", "CANARY-C4-VALUESOBJ", "CANARY-C4-PARAM",
    "CANARY-C4-FILEPARAM", "CANARY-C4-PLUGINENV", "CANARY-C4-PLUGINPARAM",
    "CANARY-C4-KPATCH", "CANARY-C4-JSONNET", "CANARY-C4-ANNOTATION",
    "CANARY-C4-LABEL", "CANARY-C4-OPERATION", "CANARY-C4-HISTSRC",
    "CANARY-C4-OPSTATE", "CANARY-C4-COMPARED", "CANARY-C4-REPOUSER",
    "CANARY-C4-REPOQUERY",
}  # fmt: skip
FORBIDDEN_NOTE = (
    "This cluster's read-only identity has no permission to read ArgoCD Applications."
)
CRD_MISSING_NOTE = (
    "ARGOCD_CRD_MISSING: this cluster has no ArgoCD Application CRD "
    "(applications.argoproj.io)."
)
_FIRST_NOTE = re.compile(
    r"ArgoCD Application state as stored now \(queried at (\S+)\); history "
    r"entries outside the authorized window are listed too\."
)
_RFC3339_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _required(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is set by the k8s-backends job"
    return value


def _token(name: str) -> str:
    return Path(_required(name)).read_text().strip()


@pytest.fixture(scope="module")
def cluster() -> dict[str, str]:
    return {
        "url": _required("B4_K8S_API_URL").rstrip("/"),
        "ca": _required("B4_K8S_CA_FILE"),
        "token": _token("B4_K8S_TOKEN_FILE"),
        "base_token": _token("B4_K8S_BASE_TOKEN_FILE"),
        "namespace": _required("C4_ARGOCD_NAMESPACE"),
    }


@pytest.fixture(scope="module")
def canaries() -> list[str]:
    path = Path(_required("C4_CANARIES"))
    if not path.is_absolute():
        path = REPO / path
    values = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    assert INTERFACE_CANARIES <= set(values), sorted(INTERFACE_CANARIES - set(values))
    return values


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("kubectl")
    assert binary, "kubectl is a precondition of the k8s-backends job"
    return subprocess.run(
        [binary, *args], capture_output=True, text=True, check=check, timeout=120
    )


def admin_app(name: str, namespace: str = "argocd") -> dict[str, Any]:
    """The object as the apiserver stores it, read with the admin kubeconfig."""
    result = kubectl(
        "get", "applications.argoproj.io", name, "-n", namespace, "-o", "json",
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, f"admin read of {namespace}/{name} failed"
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch) -> Iterator[None]:
    # The cluster transport goes direct: a refusing proxy proves it.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield


# -- the public tool path --------------------------------------------------------------


def _env(
    cluster: Mapping[str, str], token: str | None = None, *, switch: str | None = "1"
) -> dict[str, str]:
    env = {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": cluster["url"],
        "OPSPILOT_K8S_TOKEN": token or cluster["token"],
        "OPSPILOT_K8S_CA_FILE": cluster["ca"],
        "OPSPILOT_K8S_NAMESPACE": "argocd",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": "ci-only-aaaaaaaa568",
    }
    if switch is not None:
        env[SWITCH] = switch
    return env


class Worker:
    """One freshly built worker executor; the frame ends now."""

    def __init__(
        self,
        cluster: Mapping[str, str],
        token: str | None = None,
        *,
        switch: str | None = "1",
    ) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.now = now
        clock = FakeClock(start=now)
        profile = select_profile(_env(cluster, token, switch=switch))
        assert isinstance(profile, ToolProfile)
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        if switch == "1":
            assert set(ARGO_TOOLS) <= names
        else:
            assert not set(ARGO_TOOLS) & names
        lease = _lease()
        deadline = now + timedelta(minutes=10)
        self.store = FakeStore(lease, deadline=deadline)
        self.sink = RecordingSink()
        factory = profile.executor_factory(self.store, self.sink, clock)
        self.executor = factory(
            lease,
            profile.face(clock).input_for(
                run_id=str(lease.run_id),
                question="What is the sync status of c4-healthy?",
                target_id=TARGET_ID,
                deadline=deadline,
                model_requests=8,
            ),
        )
        self.frame = Window(now - timedelta(hours=6), now)
        self._index = 0

    def call(self, tool: str, params: Mapping[str, object] | None = None):
        request = ToolRequest(
            step_id="c4-live",
            tool_index=self._index,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params or {}),
            window=self.frame.as_json(),
        )
        self._index += 1
        return self.executor.execute(request)


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


def _rows(outcome) -> list[Mapping[str, Any]]:
    assert outcome.status == "ok", (outcome.status, outcome.reason)
    return list(_view(outcome)["content"])


def _notes(outcome) -> list[str]:
    return [str(n) for n in (_view(outcome).get("notes") or [])]


def _named(name: str, namespace: str | None = None) -> dict[str, str]:
    params = {"app_name": name}
    if namespace is not None:
        params["namespace"] = namespace
    return params


def _utc(value: str | None) -> str | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_repo(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _spec_sources(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    spec = item["spec"]
    return list(spec["sources"]) if spec.get("sources") else [spec["source"]]


# -- A-1: every tool returns real rows ------------------------------------------------------


@with_crd
def test_a1_list_spans_the_cluster_sorted_and_scopes_to_a_namespace(cluster):
    worker = Worker(cluster)
    rows = _rows(worker.call(LIST))
    keys = [(r["namespace"], r["name"]) for r in rows]
    assert keys == sorted(keys)
    assert SEEDED <= set(keys), sorted(SEEDED - set(keys))
    rows = _rows(worker.call(LIST, {"namespace": "c4-other"}))
    assert [(r["namespace"], r["name"]) for r in rows] == [("c4-other", "c4-elsewhere")]


@with_crd
def test_a1_healthy_app_through_all_four_tools(cluster):
    stored = admin_app("c4-healthy")
    worker = Worker(cluster)
    [row] = _rows(worker.call(GET, _named("c4-healthy")))
    assert (row["name"], row["namespace"]) == ("c4-healthy", "argocd")
    assert row["project"] == stored["spec"]["project"]
    assert row["sync"]["status"] == stored["status"]["sync"]["status"] == "Synced"
    assert row["health"]["status"] == stored["status"]["health"]["status"] == "Healthy"
    assert [s["repo_url"] for s in row["sources"]] == [
        _clean_repo(s["repoURL"]) for s in _spec_sources(stored)
    ]
    assert len(row["sources"]) == 1  # single spec.source
    history = _rows(worker.call(HISTORY, _named("c4-healthy", "argocd")))
    assert [h["id"] for h in history] == sorted(
        h["id"] for h in stored["status"]["history"]
    )
    assert len(history) == 2
    resources = _rows(worker.call(RESOURCES, _named("c4-healthy")))
    assert len(resources) == 3
    assert {r["status"] for r in resources} == {"Synced"}
    assert {(r.get("health") or {}).get("status") for r in resources} == {"Healthy"}
    keys = [(r["group"], r["kind"], r["namespace"], r["name"]) for r in resources]
    assert keys == sorted(keys)


@with_crd
def test_a1_an_app_in_another_namespace_is_reached_by_its_namespace(cluster):
    [row] = _rows(Worker(cluster).call(GET, _named("c4-elsewhere", "c4-other")))
    assert (row["namespace"], row["name"]) == ("c4-other", "c4-elsewhere")


# -- A-2: the degraded app matches what the apiserver stores --------------------------------


@with_crd
def test_a2_degraded_app_status_conditions_and_operation_match_the_seed(cluster):
    stored = admin_app("c4-degraded")
    status = stored["status"]
    worker = Worker(cluster)
    [row] = _rows(worker.call(GET, _named("c4-degraded")))
    assert row["sync"]["status"] == status["sync"]["status"] == "OutOfSync"
    assert row["health"]["status"] == status["health"]["status"] == "Degraded"
    assert [c["type"] for c in row["conditions"]] == [
        c["type"] for c in status["conditions"]
    ]
    assert "SyncError" in [c["type"] for c in row["conditions"]]
    for got, want in zip(row["conditions"], status["conditions"], strict=True):
        assert got["message"] == want["message"][:2000]
        assert got["last_transition_time"] == _utc(want.get("lastTransitionTime"))
    op, stored_op = row["operation_state"], status["operationState"]
    assert op["phase"] == stored_op["phase"] == "Failed"
    assert op["message"] == stored_op["message"][:2000]
    assert op["started_at"] == _utc(stored_op.get("startedAt"))
    assert op["finished_at"] == _utc(stored_op.get("finishedAt"))
    stored_results = stored_op["syncResult"]["resources"]
    results = op["sync_result"]["resources"]
    assert [(r["kind"], r["name"], r["status"]) for r in results] == [
        (r["kind"], r["name"], r["status"]) for r in stored_results
    ]
    assert any(r["kind"] == "Deployment" for r in results)
    resources = _rows(worker.call(RESOURCES, _named("c4-degraded")))
    out_of_sync = [r for r in resources if r["status"] == "OutOfSync"]
    assert [r["kind"] for r in out_of_sync] == ["Deployment"]
    stored_out = [r for r in status["resources"] if r["status"] == "OutOfSync"]
    assert [r["name"] for r in out_of_sync] == [r["name"] for r in stored_out]


# -- A-3: twelve history rows, not cut to the frame, current-state time ----------------------


@with_crd
def test_a3_history_has_all_twelve_entries_in_id_order_with_current_state_time(
    cluster,
):
    stored = admin_app("c4-history")
    assert len(stored["spec"].get("sources") or []) >= 2  # the multi-source app
    by_id = {h["id"]: h for h in stored["status"]["history"]}
    worker = Worker(cluster)
    outcome = worker.call(HISTORY, _named("c4-history"))
    rows = _rows(outcome)
    assert [r["id"] for r in rows] == list(range(1, 13))
    for row in rows:
        assert row["deployed_at"] == _utc(by_id[row["id"]].get("deployedAt"))
        assert _RFC3339_Z.fullmatch(row["deployed_at"]), row["id"]
    frame_start = worker.frame.start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert any(r["deployed_at"] < frame_start for r in rows)  # listed, not cut
    view = _view(outcome)
    assert view["source_start_at"] is None
    assert view["source_end_at"] is None
    assert view["data_as_of"] is None
    assert outcome.evidence.source_start_at is None
    assert outcome.evidence.source_end_at is None
    assert outcome.evidence.data_as_of is None
    notes = _notes(outcome)
    assert notes and _FIRST_NOTE.fullmatch(notes[0]), notes[:1]
    cannot = str(view.get("cannot_prove") or "").lower()
    for word in ("reconcil", "outofsync", "window"):
        assert word in cannot, word
    [app_row] = _rows(worker.call(GET, _named("c4-history")))
    assert len(app_row["sources"]) == len(stored["spec"]["sources"])


# -- A-4: canaries are really stored, and never leave a tool -------------------------------


def _calls(namespace: str) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = [
        (LIST, {}),
        (LIST, {"namespace": namespace}),
        (LIST, {"namespace": "c4-other"}),
    ]
    for ns, name in sorted(SEEDED):
        for tool in (GET, HISTORY, RESOURCES):
            calls.append((tool, _named(name, ns)))
    return calls


@with_crd
def test_a4_admin_reads_back_every_canary_so_the_apiserver_kept_them(canaries):
    # Positive control: without it a missing canary would pass the leak test.
    stored = json.dumps(admin_app("c4-canary"))
    missing = [c for c in canaries if c not in stored]
    del stored
    assert missing == []


@with_crd
def test_a4_no_canary_reaches_any_output(cluster, canaries, caplog):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from opspilot import tracing
    from tests.test_tracing import lab_env

    caplog.set_level(logging.DEBUG)
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE="otel-demo-ext",
            OPSPILOT_K8S_API_URL=cluster["url"],
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
            **{SWITCH: "1"},
        ),
        exporter=exporter,
    )
    worker = Worker(cluster)
    calls = _calls(cluster["namespace"])
    try:
        with tracer.run(incident_id="c4-incident", run_id="c4-live", versions={}):
            outcomes = [worker.call(tool, params) for tool, params in calls]
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    statuses = [(t, p.get("app_name"), o.status) for (t, p), o in zip(calls, outcomes)]
    assert all(s in ("ok", "no_data") for *_, s in statuses), statuses
    assert spans, "the lab tracer exported no span"
    exported = json.dumps(
        [
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [dict(e.attributes or {}) for e in s.events],
            }
            for s in spans
        ],
        default=str,
    )
    outputs = {
        "views": json.dumps([_view(o) for o in outcomes], default=str),
        "evidence-raw": "\n".join(
            o.evidence.raw.decode("utf-8", "replace")
            for o in outcomes
            if o.evidence is not None
        ),
        "evidence-view": json.dumps(
            [dict(o.evidence.view) for o in outcomes if o.evidence is not None],
            default=str,
        ),
        "sink": repr(worker.sink.records),
        "ledger": repr(worker.store.charges),
        "spans": exported,
        "logs": caplog.text + repr([r.args for r in caplog.records]),
    }
    leaks = sorted(
        {
            (where, value)
            for where, text in outputs.items()
            for value in canaries
            if value in text
        }
    )
    assert leaks == []
    for where, text in outputs.items():
        assert "last-applied-configuration" not in text, where
        assert "managedFields" not in text, where


@with_crd
def test_a4_repo_url_is_the_stored_one_without_userinfo_or_query(cluster):
    stored = admin_app("c4-canary")
    expected = [_clean_repo(s["repoURL"]) for s in _spec_sources(stored)]
    assert all("@" not in u and "?" not in u for u in expected)
    [row] = _rows(Worker(cluster).call(GET, _named("c4-canary")))
    assert [s["repo_url"] for s in row["sources"]] == expected


# -- A-5, A-6: not found and forbidden ---------------------------------------------------


@with_crd
@pytest.mark.parametrize("tool", (GET, HISTORY, RESOURCES))
def test_a5_a_missing_app_is_no_data_app_not_found(cluster, tool):
    outcome = Worker(cluster).call(tool, _named("c4-missing"))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    assert any("APP_NOT_FOUND: argocd/c4-missing" in n for n in _notes(outcome))
    assert "ARGOCD_CRD_MISSING" not in _text(outcome)


@with_crd
def test_a5_listing_a_namespace_that_does_not_exist_is_no_data(cluster):
    outcome = Worker(cluster).call(LIST, {"namespace": "c4-nonexistent"})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


@with_crd
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (LIST, {}),
        (LIST, {"namespace": "argocd"}),
        (GET, _named("c4-healthy")),
        (HISTORY, _named("c4-healthy")),
        (RESOURCES, _named("c4-healthy")),
    ],
)
def test_a6_the_identity_without_the_overlay_is_source_forbidden(cluster, tool, params):
    outcome = Worker(cluster, cluster["base_token"]).call(tool, params)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
        outcome
    )
    assert FORBIDDEN_NOTE in _text(outcome)


# -- A-7: the identity layer, checked with the token directly -------------------------------


def _direct(
    cluster: Mapping[str, str],
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str = "application/json",
) -> int:
    context = ssl.create_default_context(cafile=cluster["ca"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    headers = {
        "Authorization": f"Bearer {cluster['token']}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        cluster["url"] + path, data=body, headers=headers, method=method
    )
    try:
        with opener.open(request, timeout=30) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as error:
        with error:
            error.read()
        return int(error.code)


@with_crd
def test_a7_the_overlay_reads_applications_and_nothing_else_in_argoproj(cluster):
    # The objects exist (admin), so a 403 is the identity, not a missing object.
    kubectl(
        "get", "applicationsets.argoproj.io", "c4-set", "-n", "argocd", "-o", "name"
    )
    kubectl("get", "appprojects.argoproj.io", "c4-proj", "-n", "argocd", "-o", "name")
    assert _direct(cluster, f"{GROUP}/namespaces/argocd/applications") == 200
    assert _direct(cluster, f"{GROUP}/namespaces/argocd/applications/c4-healthy") == 200
    assert _direct(cluster, f"{GROUP}/applications") == 200
    for path in (
        f"{GROUP}/applicationsets",
        f"{GROUP}/namespaces/argocd/applicationsets",
        f"{GROUP}/namespaces/argocd/applicationsets/c4-set",
        f"{GROUP}/appprojects",
        f"{GROUP}/namespaces/argocd/appprojects",
        f"{GROUP}/namespaces/argocd/appprojects/c4-proj",
        f"{GROUP}/namespaces/argocd/applications?watch=true&timeoutSeconds=1",
    ):
        assert _direct(cluster, path) == 403, path


@with_crd
def test_a7_the_overlay_cannot_write_applications(cluster):
    before = admin_app("c4-healthy")["metadata"]["resourceVersion"]
    patch = json.dumps({"metadata": {"labels": {"c4-write": "attempt"}}}).encode()
    assert (
        _direct(
            cluster,
            f"{GROUP}/namespaces/argocd/applications/c4-healthy",
            method="PATCH",
            body=patch,
            content_type="application/merge-patch+json",
        )
        == 403
    )
    assert (
        _direct(cluster, f"{GROUP}/namespaces/argocd/applications/c4-healthy",
                method="DELETE")
        == 403
    )  # fmt: skip
    new_app = json.dumps(
        {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "metadata": {"name": "c4-write-attempt", "namespace": "argocd"},
            "spec": {
                "project": "default",
                "destination": {"server": "https://kubernetes.default.svc"},
                "source": {"repoURL": "https://git.example.com/x.git"},
            },
        }
    ).encode()
    assert (
        _direct(cluster, f"{GROUP}/namespaces/argocd/applications", method="POST",
                body=new_app)
        == 403
    )  # fmt: skip
    after = admin_app("c4-healthy")
    assert after["metadata"]["resourceVersion"] == before
    assert "c4-write" not in (after["metadata"].get("labels") or {})
    missing = kubectl(
        "get", "applications.argoproj.io", "c4-write-attempt", "-n", "argocd",
        check=False,
    )  # fmt: skip
    assert missing.returncode != 0


# -- A-8: switch off ----------------------------------------------------------------------


@with_crd
@pytest.mark.parametrize("off", [None, "", "0"])
def test_a8_with_the_switch_off_the_tools_are_absent_and_refused(cluster, off):
    worker = Worker(cluster, switch=off)
    for tool in ARGO_TOOLS:
        params = {} if tool == LIST else _named("c4-healthy")
        outcome = worker.call(tool, params)
        assert outcome.status in ("error", "denied"), (tool, outcome.status)


@with_crd
def test_a8_the_otel_demo_profile_ignores_the_switch():
    def surface(env):
        profile = select_profile(env)
        face = profile.face(FakeClock(start=datetime.now(timezone.utc)))
        return canonical(
            {
                "tools": [dict(s) for s in face.tool_schemas],
                "versions": profile.versions(),
            }
        )

    base = {PROFILE_ENV: "otel-demo"}
    assert surface({**base, SWITCH: "1"}) == surface(base)


# -- A-9: after the CRD is deleted ---------------------------------------------------------


@without_crd
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (LIST, {}),
        (LIST, {"namespace": "argocd"}),
        (GET, _named("c4-healthy")),
        (HISTORY, _named("c4-healthy")),
        (RESOURCES, _named("c4-healthy")),
    ],
)
def test_a9_without_the_crd_the_tools_say_so_and_are_not_no_data(cluster, tool, params):
    outcome = Worker(cluster).call(tool, params)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), _view(outcome)
    assert CRD_MISSING_NOTE in _text(outcome)
    assert "APP_NOT_FOUND" not in _text(outcome)
