"""Implementer real-cluster tests for the ArgoCD tools (C4, #568, R8-25),
beside the independent file ``test_tool_expansion_c4_argocd.py``.

Runs only in the CI job ``k8s-backends`` (``OPSPILOT_K8S_BACKENDS_LIVE=1``)
after ``tests/fixtures/c4_argocd/seed.sh``; the CRD-removed test runs only in
the later step that deletes ``applications.argoproj.io``
(``C4_ARGOCD_CRD_REMOVED=1``). No model.

What this file adds: the apiserver really kept every canary field (positive
control) while no tool output carries one; the pages of the cluster-wide
list; and the two kinds of 404.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

LIVE = os.environ.get("OPSPILOT_K8S_BACKENDS_LIVE") == "1"
CRD_REMOVED = os.environ.get("C4_ARGOCD_CRD_REMOVED") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="the kind cluster runs only in the CI k8s-backends job"
)
REPO = Path(__file__).resolve().parents[2]
TOOLS = (
    "argocd_app_list",
    "argocd_app_get",
    "argocd_app_history",
    "argocd_app_resources",
)


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    assert value, f"{name} is set by the k8s-backends job"
    return value


def _env() -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_ARGOCD": "1",
        "OPSPILOT_K8S_API_URL": _required("B4_K8S_API_URL"),
        "OPSPILOT_K8S_TOKEN": Path(_required("B4_K8S_TOKEN_FILE")).read_text().strip(),
        "OPSPILOT_K8S_CA_FILE": _required("B4_K8S_CA_FILE"),
        "OPSPILOT_K8S_NAMESPACE": "argocd",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
    }


def _call(tool: str, params: Mapping[str, object]):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = FakeClock(start=now)
    profile = select_profile(_env())
    lease = _lease()
    deadline = now + timedelta(minutes=10)
    sink = RecordingSink()
    factory = profile.executor_factory(FakeStore(lease, deadline=deadline), sink, clock)
    executor = factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="What is the sync state of the canary application?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=4,
        ),
    )
    outcome = executor.execute(
        ToolRequest(
            step_id="c4-impl",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params),
            window=Window(now - timedelta(hours=6), now).as_json(),
        )
    )
    return outcome, sink


def _canaries() -> list[str]:
    path = REPO / _required("C4_CANARIES")
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def _admin_json(*args: str) -> str:
    return subprocess.run(
        ["kubectl", *args, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


@pytest.mark.skipif(CRD_REMOVED, reason="needs the Application CRD")
def test_canaries_are_stored_by_the_apiserver_but_never_returned():
    stored = _admin_json("-n", "argocd", "get", "application", "c4-canary")
    canaries = _canaries()
    # Positive control: pruning did not drop a field we test for.
    missing = [c for c in canaries if c not in stored]
    assert not missing, missing
    for tool in TOOLS:
        params = {} if tool == "argocd_app_list" else {"app_name": "c4-canary"}
        outcome, sink = _call(tool, params)
        assert outcome.status in ("ok", "no_data"), (tool, outcome.model_view)
        shown = json.dumps(outcome.model_view, default=str)
        raw = b"".join((r.raw or b"") for r in sink.records)
        for canary in canaries:
            assert canary not in shown, (tool, canary)
            assert canary.encode() not in raw, (tool, canary)
    outcome, _ = _call("argocd_app_get", {"app_name": "c4-canary"})
    (row,) = outcome.model_view["content"]
    assert row["sources"][0]["repo_url"] == "https://git.example.test/c4/canary.git"


@pytest.mark.skipif(CRD_REMOVED, reason="needs the Application CRD")
def test_cluster_list_and_history_order():
    outcome, _ = _call("argocd_app_list", {})
    assert outcome.status == "ok", outcome.model_view
    names = [(r["namespace"], r["name"]) for r in outcome.model_view["content"]]
    assert names == sorted(names)
    assert ("c4-other", "c4-elsewhere") in names
    outcome, _ = _call("argocd_app_history", {"app_name": "c4-history"})
    assert outcome.status == "ok", outcome.model_view
    assert [r["id"] for r in outcome.model_view["content"]] == list(range(1, 13))
    assert outcome.model_view.get("source_start_at") is None


@pytest.mark.skipif(CRD_REMOVED, reason="needs the Application CRD")
def test_a_missing_application_is_no_data():
    outcome, _ = _call("argocd_app_get", {"app_name": "c4-no-such-app"})
    assert outcome.status == "no_data", outcome.model_view
    assert "APP_NOT_FOUND" in json.dumps(outcome.model_view.get("notes"))


@pytest.mark.skipif(not CRD_REMOVED, reason="runs after the CRD is deleted")
def test_without_the_crd_both_paths_are_source_error():
    for tool, params in (
        ("argocd_app_list", {}),
        ("argocd_app_get", {"app_name": "c4-healthy"}),
    ):
        outcome, _ = _call(tool, params)
        assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), (
            tool,
            outcome.model_view,
        )
        assert "ARGOCD_CRD_MISSING" in json.dumps(outcome.model_view, default=str)
