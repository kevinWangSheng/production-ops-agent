"""Implementer real-cluster tests for the Helm release history tools (B4,
#563, R8-25), beside the independent file ``test_tool_expansion_b4_helm.py``.

Runs only in the CI job ``k8s-backends`` (``OPSPILOT_K8S_BACKENDS_LIVE=1``),
which starts the kind cluster ``b4`` with the audit log, applies the base
identity and the Helm overlay, runs ``tests/fixtures/b4_helm/seed.sh`` and
exports ``B4_*``. No model.

What this file adds: the apiserver's own continue tokens pass the gateway
(``helm_history`` of ``b4-b/cart`` takes two pages and the audit log shows
exactly the request URIs ``helm_page_query`` builds), and the A1 probe's
``--expect-helm-overlay`` mode records the residual risk as statuses only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.kubernetes_core import helm_page_query
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_K8S_BACKENDS_LIVE") != "1",
    reason="the kind cluster b4 runs only in the CI k8s-backends job",
)

REPO = Path(__file__).resolve().parents[2]
SA_USER = "system:serviceaccount:opspilot-readonly:opspilot-investigator"


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    assert value, f"{name} is set by the k8s-backends job"
    return value


def _token() -> str:
    return Path(_required("B4_K8S_TOKEN_FILE")).read_text().strip()


def _env() -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_HELM_RELEASES": "1",
        "OPSPILOT_K8S_API_URL": _required("B4_K8S_API_URL"),
        "OPSPILOT_K8S_TOKEN": _token(),
        "OPSPILOT_K8S_CA_FILE": _required("B4_K8S_CA_FILE"),
        "OPSPILOT_K8S_NAMESPACE": "b4-b",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
    }


def _call(tool: str, params: Mapping[str, object]):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = FakeClock(start=now)
    profile = select_profile(_env())
    lease = _lease()
    deadline = now + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    executor = factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="What did the last Helm release of cart do?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=4,
        ),
    )
    return executor.execute(
        ToolRequest(
            step_id="b4-impl",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params),
            window=Window(now - timedelta(hours=6), now).as_json(),
        )
    )


def _audit() -> list[dict[str, Any]]:
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
        if (event.get("user") or {}).get("username") == SA_USER:
            events.append(event)
    return events


def test_real_continue_tokens_pass_the_gateway_and_only_built_pages_are_sent():
    before = {str(e.get("auditID")) for e in _audit()}
    outcome = _call("helm_history", {"release_name": "cart", "namespace": "b4-b"})
    assert outcome.status == "ok", outcome.model_view
    assert [r["revision"] for r in outcome.model_view["content"]] == list(range(4, 14))
    deadline = time.monotonic() + 15
    while True:
        new = [
            e
            for e in _audit()
            if str(e.get("auditID")) not in before
            and e.get("stage") == "ResponseComplete"
        ]
        if len(new) >= 2 or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    uris = [str(e.get("requestURI")) for e in new]
    # Ten records in pages of five: two requests, the second with the token
    # the apiserver itself returned.
    assert len(uris) == 2, uris
    label = "owner=helm,name=cart"
    assert uris[0] == f"/api/v1/namespaces/b4-b/secrets?{helm_page_query(label, None)}"
    token = parse_qs(urlsplit(uris[1]).query)["continue"][0]
    assert uris[1] == (
        f"/api/v1/namespaces/b4-b/secrets?{helm_page_query(label, token)}"
    )
    assert {e.get("verb") for e in new} == {"list"}
    assert {(e.get("responseStatus") or {}).get("code") for e in new} == {200}


def test_the_probe_records_the_overlay_as_statuses_only(tmp_path):
    env_file = tmp_path / "k8s-investigator.env"
    env_file.write_text(
        "OPSPILOT_K8S_API_URL={}\nOPSPILOT_K8S_TOKEN={}\nOPSPILOT_K8S_CA_FILE={}\n".format(
            _required("B4_K8S_API_URL"), _token(), _required("B4_K8S_CA_FILE")
        )
    )
    env_file.chmod(0o600)
    out = tmp_path / "probe.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "k8s_authz_probe.py"),
            "--env-file",
            str(env_file),
            "--namespace",
            "b4-a",
            "--expect-helm-overlay",
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        cwd=REPO,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    text = out.read_text()
    report = json.loads(text)
    by_name = {row["name"]: row for row in report["requests"]}
    for name in (
        "list secrets, all namespaces",
        "list kube-system secrets",
        "list app secrets",
    ):
        assert (by_name[name]["status"], by_name[name]["matches"]) == (200, True)
    for name in ("get one kube-system secret", "watch secrets"):
        assert (by_name[name]["status"], by_name[name]["matches"]) == (403, True)
    # Statuses only: no Secret content reached the report.
    for canary in ("CANARY-B4-OPAQUE", "CANARY-B4-VALUES", "sh.helm.release.v1"):
        assert canary not in text
