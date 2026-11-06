"""#488: ``up`` on an existing cluster with a new Prometheus auth file must
leave the OTel collector pushing with the new password.

The collector reads ``PROMETHEUS_COLLECTOR_PASSWORD`` from the Secret
``prometheus-web-auth`` as an env var, fixed at pod start, so ``up`` stamps
the Secret's hash on the collector's pod template: a changed Secret rolls the
pod, an unchanged one patches the same value (no roll).
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import kind_lab

HASH = kind_lab.CONFIG_HASH_ANNOTATION


def _secret(password: str) -> dict:
    encoded = base64.b64encode(password.encode()).decode()
    return {
        "metadata": {"name": kind_lab.COLLECTOR_AUTH_SECRET, "resourceVersion": "1"},
        "data": {"PROMETHEUS_COLLECTOR_PASSWORD": encoded},
    }


def _patch_of(cmd: list[str]) -> dict:
    return json.loads(cmd[cmd.index("-p") + 1])


def _roll(
    monkeypatch: pytest.MonkeyPatch, got: subprocess.CompletedProcess, patch_rc=0
) -> tuple[int, list[list[str]], list[list[str]]]:
    reads: list[list[str]] = []
    patches: list[list[str]] = []

    def fake_capture(cmd, **kw):
        reads.append(list(cmd))
        return got

    def fake_run(cmd, **kw):
        patches.append(list(cmd))
        return subprocess.CompletedProcess(cmd, patch_rc, "", "")

    monkeypatch.setattr(kind_lab, "capture", fake_capture)
    monkeypatch.setattr(kind_lab, "run", fake_run)
    return kind_lab.roll_collector_on_auth_change(), reads, patches


def test_roll_reads_the_push_secret_and_patches_the_collector_hash_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _secret("pw-1")
    got = subprocess.CompletedProcess([], 0, json.dumps(secret), "")
    code, reads, patches = _roll(monkeypatch, got)
    assert code == 0
    (read,) = reads
    assert read[-5:] == ["get", "secret", "prometheus-web-auth", "-o", "json"]
    assert read[read.index("-n") + 1] == kind_lab.NAMESPACE
    (patch,) = patches
    assert patch[patch.index("patch") + 1 : patch.index("patch") + 3] == [
        "deployment",
        "otel-collector",
    ]
    assert _patch_of(patch) == {
        "spec": {
            "template": {
                "metadata": {"annotations": {HASH: kind_lab.config_sha256(secret)}}
            }
        }
    }
    # the password never reaches argv
    assert not any("pw-1" in part for part in patch)
    assert not any(base64.b64encode(b"pw-1").decode() in part for part in patch)


def test_roll_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    code, _, patches = _roll(
        monkeypatch, subprocess.CompletedProcess([], 1, "", "not found")
    )
    assert code == 1 and patches == []
    code, _, patches = _roll(
        monkeypatch, subprocess.CompletedProcess([], 0, "not json", "")
    )
    assert code == 1 and patches == []
    ok = subprocess.CompletedProcess([], 0, json.dumps(_secret("x")), "")
    code, _, _ = _roll(monkeypatch, ok, patch_rc=5)
    assert code == 5


# --- the issue's scenario through ``cmd_up`` on an existing cluster


class _Lab:
    """An existing cluster: the push Secret ``up`` applies, and every
    ``kubectl``/``helm`` call ``up`` makes, in order."""

    def __init__(self) -> None:
        self.secret: dict | None = None
        self.calls: list[list[str]] = []

    def apply_auth(self, accounts: dict[str, str]) -> int:
        self.secret = _secret(accounts["collector"])
        return 0

    def proc(self, cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if cmd[:3] == ["kind", "get", "clusters"]:
            return subprocess.CompletedProcess(cmd, 0, "opspilot-m1\n", "")
        if "get" in cmd and kind_lab.COLLECTOR_AUTH_SECRET in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.secret), "")
        if "get" in cmd and "configmap" in cmd:
            return subprocess.CompletedProcess(cmd, 0, '{"data": {}}', "")
        # stop the walk at the kube-state-metrics release, after the rolls
        if "upgrade" in cmd and kind_lab.KSM_RELEASE in cmd:
            return subprocess.CompletedProcess(cmd, 9, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def collector_patches(self) -> list[dict]:
        return [
            _patch_of(c)
            for c in self.calls
            if "patch" in c and kind_lab.COLLECTOR_DEPLOYMENT in c
        ]


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Lab:
    fake = _Lab()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("OPSPILOT_LAB_RUNTIME", raising=False)
    monkeypatch.setattr(kind_lab, "LAB_DIR", tmp_path)
    monkeypatch.setattr(kind_lab, "KUBECONFIG", tmp_path / "kubeconfig")
    auth_file = tmp_path / "prometheus-auth.json"
    monkeypatch.setattr(kind_lab, "AUTH_FILE", auth_file)
    # the function's default path is bound at import time
    real_accounts = kind_lab.prometheus_accounts
    monkeypatch.setattr(
        kind_lab, "prometheus_accounts", lambda path=auth_file: real_accounts(path)
    )
    monkeypatch.setattr(kind_lab, "capture", fake.proc)
    monkeypatch.setattr(kind_lab, "run", fake.proc)
    monkeypatch.setattr(kind_lab, "apply_prometheus_auth_secrets", fake.apply_auth)
    monkeypatch.setattr(kind_lab, "role_env_file", lambda *a, **k: tmp_path / "x")
    monkeypatch.setattr(kind_lab, "webhook_token", lambda *a, **k: "t")
    monkeypatch.setattr(kind_lab, "apply_webhook_secret", lambda token: 0)
    monkeypatch.setattr(kind_lab, "workbench_event_env", lambda *a, **k: "env")
    monkeypatch.setattr(kind_lab, "apply_readonly_proxy", lambda accounts: 0)
    monkeypatch.setattr(kind_lab, "lab_endpoints", lambda: None)
    return fake


def _up() -> int:
    return kind_lab.cmd_up(argparse.Namespace(skip_memory_check=True, wait=1))


def test_up_with_a_new_auth_file_rolls_the_collector_once_per_change(
    lab: _Lab,
) -> None:
    assert _up() == 9
    first = lab.collector_patches()
    assert len(first) == 1
    # the roll comes after the demo chart (which owns the Deployment) and
    # stamps the hash of the Secret ``up`` just applied
    demo = next(
        i for i, c in enumerate(lab.calls) if "upgrade" in c and kind_lab.RELEASE in c
    )
    roll = next(
        i
        for i, c in enumerate(lab.calls)
        if "patch" in c and kind_lab.COLLECTOR_DEPLOYMENT in c
    )
    assert demo < roll
    assert first[0]["spec"]["template"]["metadata"]["annotations"][
        HASH
    ] == kind_lab.config_sha256(lab.secret or {})

    # same auth file again: the same hash, which Kubernetes treats as no change
    assert _up() == 9
    assert lab.collector_patches()[1] == first[0]

    # another checkout's ``up`` (no auth file there) generates new passwords:
    # the collector's pod template changes, so the pod restarts with them
    kind_lab.AUTH_FILE.unlink()
    assert _up() == 9
    third = lab.collector_patches()[2]
    assert third != first[0]
    assert third["spec"]["template"]["metadata"]["annotations"][
        HASH
    ] == kind_lab.config_sha256(lab.secret or {})
