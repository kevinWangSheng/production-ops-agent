"""Real-cluster integration tests for tool expansion B4 (#563): Helm release
history against the kind cluster ``b4`` of the CI job ``k8s-backends``
(R8-25), through the public tool path and the real ``KubernetesClient``,
with only the read-only identity's tokens. No model is called.

Written by the independent test author from the B4 task record
(docs/tasks/2026-11-20-tool-expansion-b4.md, "CI job ``k8s-backends``" and
assertions I-1 ... I-12), without reading the implementation. Runs only when
``OPSPILOT_K8S_BACKENDS_LIVE=1``; skipped everywhere else.

What the job provides (fixed by the interface):

* the kind cluster ``b4`` (apiserver ``https://127.0.0.1:36443``) with an
  audit policy that logs ``secrets`` at ``Metadata`` level to
  ``B4_AUDIT_LOG`` on node ``B4_KIND_NODE``; ``kubectl`` on PATH with the
  admin kubeconfig (engineer side only); ``docker`` to read the audit log;
* ``B4_K8S_API_URL``, ``B4_K8S_CA_FILE``; ``B4_K8S_TOKEN_FILE`` (read-only
  identity plus the Helm overlay), ``B4_K8S_BASE_TOKEN_FILE`` (base role
  only); ``B4_CANARIES`` (one canary per line);
* the seed of ``tests/fixtures/b4_helm/seed.sh``: ``b4-a/shop`` (Helm 3,
  revisions 1-4: superseded, superseded, failed, deployed = rollback to 1),
  ``b4-b/cart`` (Helm 4, revisions 4-13 kept), ``b4-junk`` (``applied`` valid;
  two malformed; one gzip bomb), and the Opaque Secret ``b4-a/plain-canary``.

The I-12 checks deliberately read Secrets with the token directly, outside
OpsPilot, to prove the residual risk; they assert and never print a body.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_K8S_BACKENDS_LIVE") != "1",
    reason="the kind cluster b4 with Helm records runs only in CI k8s-backends",
)

REPO = Path(__file__).resolve().parents[2]
SWITCH = "OPSPILOT_HELM_RELEASES"
LIST = "helm_list"
HISTORY = "helm_history"
STATUS = "helm_status"
HELM_TOOLS = (LIST, HISTORY, STATUS)
REFUSED = "RESOURCE_NOT_ALLOWED"
SA_USER = "system:serviceaccount:opspilot-readonly:opspilot-investigator"
FORBIDDEN_NOTE = (
    "This cluster's read-only identity has no permission to read Helm release records."
)
_FIRST_NOTE = re.compile(
    r"Helm release records as stored now \(queried at (\S+)\); revisions "
    r"outside the authorized window are listed too\."
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
    }


@pytest.fixture(scope="module")
def canaries() -> list[str]:
    path = Path(_required("B4_CANARIES"))
    if not path.is_absolute():
        path = REPO / path
    values = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    assert len(values) >= 10, values
    return values


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("kubectl")
    assert binary, "kubectl is a precondition of the k8s-backends job"
    return subprocess.run(
        [binary, *args], capture_output=True, text=True, check=check, timeout=120
    )


# -- the public tool path --------------------------------------------------------------


def _env(cluster: Mapping[str, str], token: str | None = None) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        SWITCH: "1",
        "OPSPILOT_K8S_API_URL": cluster["url"],
        "OPSPILOT_K8S_TOKEN": token or cluster["token"],
        "OPSPILOT_K8S_CA_FILE": cluster["ca"],
        "OPSPILOT_K8S_NAMESPACE": "b4-a",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": "ci-only-aaaaaaaa563",
    }


class Worker:
    """One freshly built worker executor; the frame ends now."""

    def __init__(self, cluster: Mapping[str, str], token: str | None = None) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.now = now
        clock = FakeClock(start=now)
        profile = select_profile(_env(cluster, token))
        assert isinstance(profile, ToolProfile)
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        assert set(HELM_TOOLS) <= names
        lease = _lease()
        deadline = now + timedelta(minutes=10)
        self.store = FakeStore(lease, deadline=deadline)
        self.sink = RecordingSink()
        factory = profile.executor_factory(self.store, self.sink, clock)
        self.executor = factory(
            lease,
            profile.face(clock).input_for(
                run_id=str(lease.run_id),
                question="When was the last release in b4-a?",
                target_id=TARGET_ID,
                deadline=deadline,
                model_requests=8,
            ),
        )
        self.frame = Window(now - timedelta(hours=6), now)
        self._index = 0

    def call(self, tool: str, params: Mapping[str, object] | None = None):
        request = ToolRequest(
            step_id="b4-live",
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
    assert outcome.status == "ok", _view(outcome)
    return list(_view(outcome)["content"])


def _notes(outcome) -> list[str]:
    return [str(n) for n in (_view(outcome).get("notes") or [])]


def _named(name: str, namespace: str) -> dict[str, str]:
    return {"release_name": name, "namespace": namespace}


# -- I-1 ... I-6: what the tools read --------------------------------------------------


def test_i1_shop_history_matches_the_helm3_seed(cluster):
    rows = _rows(Worker(cluster).call(HISTORY, _named("shop", "b4-a")))
    assert [r["revision"] for r in rows] == [1, 2, 3, 4]
    assert [r["status"] for r in rows] == [
        "superseded",
        "superseded",
        "failed",
        "deployed",
    ]
    assert {r["name"] for r in rows} == {"shop"}
    assert {r["namespace"] for r in rows} == {"b4-a"}
    assert {r["chart"] for r in rows} == {"b4-fixture"}
    by = {r["revision"]: r for r in rows}
    assert (by[1]["chart_version"], by[1]["app_version"]) == ("0.1.0", "1.0.0")
    assert (by[2]["chart_version"], by[2]["app_version"]) == ("0.2.0", "1.1.0")
    assert (by[4]["chart_version"], by[4]["app_version"]) == ("0.1.0", "1.0.0")
    assert by[1]["description"] == "Install complete"
    assert by[2]["description"] == "Upgrade complete"
    assert "failed" in by[3]["description"].lower(), by[3]["description"]
    assert by[4]["description"] == "Rollback to 1"
    assert [by[n]["rollback_revision"] for n in (1, 2, 3, 4)] == [None, None, None, 1]
    stamps = []
    for row in rows:
        assert _RFC3339_Z.fullmatch(row["first_deployed"] or ""), row
        assert _RFC3339_Z.fullmatch(row["last_deployed"] or ""), row
        assert row["deleted"] is None, row
        stamps.append(row["last_deployed"])
    assert stamps == sorted(stamps)  # monotonic, never decreasing


def test_i2_status_is_the_latest_revision(cluster):
    worker = Worker(cluster)
    (shop,) = _rows(worker.call(STATUS, _named("shop", "b4-a")))
    assert (shop["revision"], shop["status"], shop["rollback_revision"]) == (
        4,
        "deployed",
        1,
    )
    (cart,) = _rows(worker.call(STATUS, _named("cart", "b4-b")))
    assert (cart["revision"], cart["status"]) == (13, "deployed")


def test_i3_helm4_history_keeps_only_the_last_ten(cluster):
    rows = _rows(Worker(cluster).call(HISTORY, _named("cart", "b4-b")))
    assert [r["revision"] for r in rows] == list(range(4, 14))
    assert rows[-1]["status"] == "deployed"
    assert {r["status"] for r in rows[:-1]} == {"superseded"}
    for row in rows:
        assert _RFC3339_Z.fullmatch(row["last_deployed"] or ""), row


def test_i4_list_gives_each_release_once_at_its_latest_revision(cluster):
    worker = Worker(cluster)
    outcome = worker.call(LIST)
    rows = _rows(outcome)
    found = {(r["namespace"], r["name"]): r["revision"] for r in rows}
    assert found[("b4-a", "shop")] == 4
    assert found[("b4-b", "cart")] == 13
    assert found[("b4-junk", "applied")] == 1
    assert len(found) == len(rows)  # one row per release
    keys = [(r["namespace"], r["name"]) for r in rows]
    assert keys == sorted(keys)
    assert _view(outcome)["scope"] == {"namespace": None, "all_namespaces": True}
    only = _rows(worker.call(LIST, {"namespace": "b4-a"}))
    assert [(r["name"], r["revision"]) for r in only] == [("shop", 4)]


def test_i5_junk_entries_are_dropped_and_counted(cluster):
    outcome = Worker(cluster).call(LIST, {"namespace": "b4-junk"})
    assert [r["name"] for r in _rows(outcome)] == ["applied"]
    assert _view(outcome)["dropped"] == {"malformed": 2, "too_large": 1}
    assert _notes(outcome)


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (HISTORY, _named("nope", "b4-a")),
        (STATUS, _named("nope", "b4-a")),
        (HISTORY, _named("shop", "b4-none")),
        (STATUS, _named("shop", "b4-none")),
        (LIST, {"namespace": "b4-none"}),
    ],
)
def test_i6_a_missing_release_or_namespace_is_no_data(cluster, tool, params):
    outcome = Worker(cluster).call(tool, params)
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    if tool != LIST:
        notes = " ".join(_notes(outcome))
        assert params["release_name"] in notes and params["namespace"] in notes, notes


# -- I-7: without the Helm overlay -------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "params"),
    [(LIST, {}), (HISTORY, _named("shop", "b4-a")), (STATUS, _named("shop", "b4-a"))],
)
def test_i7_the_base_identity_is_source_forbidden(cluster, tool, params):
    outcome = Worker(cluster, token=cluster["base_token"]).call(tool, params)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
        outcome
    )
    assert _view(outcome).get("source_status") in (None, "403"), _view(outcome)
    assert FORBIDDEN_NOTE in _text(outcome)


# -- I-8, I-9: nothing withheld leaves; current-state times ------------------------------


def _forms(value: str) -> list[str]:
    encoded = base64.b64encode(value.encode()).decode()
    return [value, encoded, encoded.rstrip("=")]


CALLS = [
    (LIST, {}),
    (LIST, {"namespace": "b4-a"}),
    (LIST, {"namespace": "b4-b"}),
    (LIST, {"namespace": "b4-junk"}),
    (HISTORY, _named("shop", "b4-a")),
    (HISTORY, _named("cart", "b4-b")),
    (STATUS, _named("shop", "b4-a")),
    (STATUS, _named("cart", "b4-b")),
    (STATUS, _named("applied", "b4-junk")),
]


def test_i8_no_canary_reaches_any_output(cluster, canaries, caplog):
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
    try:
        with tracer.run(incident_id="b4-incident", run_id="b4-live", versions={}):
            outcomes = [worker.call(tool, params) for tool, params in CALLS]
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    for outcome in outcomes:
        assert outcome.status == "ok", _view(outcome)
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
            o.evidence.raw.decode("utf-8", "replace") for o in outcomes
        ),
        "evidence-view": json.dumps(
            [dict(o.evidence.view) for o in outcomes], default=str
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
            if any(form in text for form in _forms(value))
        }
    )
    assert leaks == []


@pytest.mark.parametrize(("tool", "params"), CALLS[:1] + CALLS[4:5] + CALLS[6:7])
def test_i9_source_times_are_empty_with_the_fixed_notes(cluster, tool, params):
    outcome = Worker(cluster).call(tool, params)
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert view["source_start_at"] is None
    assert view["source_end_at"] is None
    assert view["data_as_of"] is None
    assert outcome.evidence.source_start_at is None
    assert outcome.evidence.source_end_at is None
    assert outcome.evidence.data_as_of is None
    notes = _notes(outcome)
    assert notes and _FIRST_NOTE.fullmatch(notes[0]), notes
    cannot = str(view.get("cannot_prove") or "").lower()
    for word in ("window", "kubectl", "pod", "deployed"):
        assert word in cannot, (word, cannot)


# -- I-10: the gateway table through the real client, checked in the audit log ----------


def _audit_events() -> list[dict[str, Any]]:
    docker = shutil.which("docker")
    assert docker, "docker is a precondition of the k8s-backends job"
    text = subprocess.run(
        [docker, "exec", _required("B4_KIND_NODE"), "cat", _required("B4_AUDIT_LOG")],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        # The policy logs only ``secrets`` (Metadata level); filtering on the
        # user alone also keeps any Secret-ish request the apiserver could not
        # map to an ``objectRef``.
        if (event.get("user") or {}).get("username") != SA_USER:
            continue
        events.append(event)
    return events


def _secret_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e for e in events if (e.get("objectRef") or {}).get("resource") == "secrets"
    ]


def _ids(events: list[dict[str, Any]]) -> set[str]:
    return {str(e.get("auditID")) for e in events}


def _real_client(cluster: Mapping[str, str], *, helm_releases: bool):
    from opspilot.tools.kubernetes_core import CREDENTIAL_REF, KubernetesClient

    return KubernetesClient(
        credentials={CREDENTIAL_REF: cluster["token"]},
        ca_file=cluster["ca"],
        helm_releases=helm_releases,
    )


def _bypass_table(cluster: Mapping[str, str]) -> dict[str, tuple[str, str, bool]]:
    """The contract test's bypass table, re-pointed at the real apiserver."""
    from tests.test_tool_expansion_b4_contract import BYPASSES

    netloc = urlsplit(cluster["url"]).netloc
    return {
        name: (url.replace("127.0.0.1:6443", netloc), accept, helm_page)
        for name, (url, accept, helm_page) in BYPASSES.items()
    }


def _legal(cluster: Mapping[str, str]) -> dict[str, str]:
    from tests.test_tool_expansion_b4_contract import LEGAL

    netloc = urlsplit(cluster["url"]).netloc
    return {name: url.replace("127.0.0.1:6443", netloc) for name, url in LEGAL.items()}


def _wait_for(predicate, *, seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def test_i10_no_refused_request_reaches_the_apiserver(cluster):
    from opspilot.tools.kubernetes_core import KubernetesRequestRefused

    before = _ids(_audit_events())
    on = _real_client(cluster, helm_releases=True)
    off = _real_client(cluster, helm_releases=False)
    table = _bypass_table(cluster)
    # The interface's own list (I-10), all present in the shared table.
    for required in (
        "no-fieldSelector",
        "fs-twice-valid-first",
        "fs-twice-valid-last",
        "fs-not-equal",
        "fs-double-equal",
        "fs-extra-condition",
        "key-FieldSelector",
        "key-field%53elector",
        "path-%73ecrets",
        "path-Secrets",
        "named-secret",
        "named-secret-subresource",
        "watch-true",
        "resourceVersion-0",
        "semicolon-extra",
        "as-Table",
        "accept-table",
        "not-helm-page-named",
    ):
        assert required in table, required
    not_refused = []
    for name, (url, accept, helm_page) in table.items():
        try:
            on.get(url, deadline=time.monotonic() + 10, max_bytes=1024 * 1024,
                   accept=accept, helm_page=helm_page)  # fmt: skip
        except KubernetesRequestRefused as error:
            if REFUSED not in (getattr(error, "code", None) or str(error)):
                not_refused.append(name)
        else:
            not_refused.append(name)
    for name, url in _legal(cluster).items():
        # With the switch off even the legal shapes are refused.
        try:
            off.get(
                url,
                deadline=time.monotonic() + 10,
                max_bytes=1024 * 1024,
                helm_page=True,
            )
        except KubernetesRequestRefused:
            continue
        not_refused.append(f"switch-off-{name}")
    assert not_refused == []
    time.sleep(2)
    after = _audit_events()
    assert [
        e for e in _secret_events(after) if str(e.get("auditID")) not in before
    ] == []
    assert _ids(after) == before
    # Positive control: a legal page does show up in the audit log, so the
    # silence above is evidence and not a deaf log.
    legal = _legal(cluster)["namespace-list"]
    fetched = on.get(legal, deadline=time.monotonic() + 10, max_bytes=16 * 1024 * 1024,
                     helm_page=True)  # fmt: skip
    assert fetched.status == 200
    legal_uri = legal[len(cluster["url"]) :]
    assert _wait_for(lambda: any(
        e.get("requestURI") == legal_uri for e in _audit_events() if str(e.get("auditID")) not in before
    )), "the legal control request never reached the audit log"  # fmt: skip
    new = [e for e in _audit_events() if str(e.get("auditID")) not in before]
    assert {e.get("requestURI") for e in new} == {legal_uri}


# -- I-11: ordinary paths that happen to be called secrets --------------------------------


def test_i11_names_and_namespaces_called_secrets_still_work(cluster):
    kubectl(
        "create",
        "configmap",
        "secrets",
        "-n",
        "b4-a",
        "--from-literal=k=v",
        check=False,
    )
    kubectl("get", "configmap", "secrets", "-n", "b4-a")
    kubectl("create", "namespace", "secrets", check=False)
    kubectl("get", "namespace", "secrets")
    client = _real_client(cluster, helm_releases=True)
    fetched = client.get(
        f"{cluster['url']}/api/v1/namespaces/b4-a/configmaps/secrets",
        deadline=time.monotonic() + 10,
        max_bytes=1024 * 1024,
    )
    assert fetched.status == 200
    worker = Worker(cluster)
    outcome = worker.call(
        "kubernetes_jq_query",
        {
            "kind": "configmaps",
            "namespace": "b4-a",
            "jq_expr": '.items[] | select(.metadata.name == "secrets") | .metadata.name',
        },
    )
    assert _rows(outcome) == ["secrets"]

    def listed() -> bool:
        result = worker.call(
            "kubernetes_jq_query",
            {
                "kind": "configmaps",
                "namespace": "secrets",
                "jq_expr": ".items[] | .metadata.name",
            },
        )
        return result.status == "ok" and "kube-root-ca.crt" in _rows(result)

    assert _wait_for(listed)


# -- I-12: the residual risk is real (direct reads with the same token) -------------------


def _direct(cluster: Mapping[str, str], path: str) -> tuple[int, bytes]:
    context = ssl.create_default_context(cafile=cluster["ca"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    request = urllib.request.Request(
        cluster["url"] + path,
        headers={
            "Authorization": f"Bearer {cluster['token']}",
            "Accept": "application/json",
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        with error:
            error.read()
        return int(error.code), b""


def test_i12_the_same_token_reads_every_secret_directly(cluster):
    status, body = _direct(cluster, "/api/v1/secrets")
    assert status == 200
    items = json.loads(body.decode())["items"]
    del body
    found = False
    for item in items:
        metadata = item.get("metadata") or {}
        if (metadata.get("namespace"), metadata.get("name")) != (
            "b4-a",
            "plain-canary",
        ):
            continue
        for value in (item.get("data") or {}).values():
            if b"CANARY-B4-OPAQUE" in base64.b64decode(value):
                found = True
    del items
    assert found is True, "the Opaque canary was not readable through a plain list"
    status, _ = _direct(cluster, "/api/v1/namespaces/b4-a/secrets/plain-canary")
    assert status == 403
    status, _ = _direct(cluster, "/api/v1/secrets?watch=true&timeoutSeconds=1")
    assert status == 403


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch) -> Iterator[None]:
    # The cluster transport goes direct: a refusing proxy proves it.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
