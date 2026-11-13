"""A1 (#553) implementer tests: the lab's read-only cluster identity, its
token files, the worker's container wiring and the entrypoint export. No
cluster, kubectl or docker runs: every command goes to a fake."""

from __future__ import annotations

import argparse
import base64
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import kind_lab

ROOT = Path(kind_lab.ROOT)
MANIFEST = ROOT / "scripts/kind_lab/opspilot-readonly-rbac.yaml"
COMPOSE_DIR = ROOT / "deploy/compose"
ENTRYPOINT = ROOT / "deploy/docker-entrypoint.sh"
TOKEN = "aaaaaaaa357"
NEW_TOKEN = "bbbbbbbb357"
CA_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
EXPIRES = "2026-11-14T08:00:00Z"

#: decision table r3, A1-2 (every verb get, list)
A1_2_RULES = {
    "": {
        "configmaps",
        "daemonsets",
        "deployments",
        "endpoints",
        "events",
        "namespaces",
        "nodes",
        "persistentvolumes",
        "persistentvolumeclaims",
        "pods",
        "pods/log",
        "pods/status",
        "replicasets",
        "replicationcontrollers",
        "resourcequotas",
        "services",
        "serviceaccounts",
    },
    "apps": {
        "daemonsets",
        "deployments",
        "deployments/scale",
        "replicasets",
        "replicasets/scale",
        "statefulsets",
    },
    "extensions": {
        "daemonsets",
        "deployments",
        "deployments/scale",
        "ingresses",
        "replicasets",
        "replicasets/scale",
        "replicationcontrollers/scale",
    },
    "batch": {"cronjobs", "jobs"},
    "autoscaling": {"horizontalpodautoscalers"},
    "events.k8s.io": {"events"},
    "storage.k8s.io": {"storageclasses"},
    "metrics.k8s.io": {"pods", "nodes"},
    "networking.k8s.io": {"ingresses", "networkpolicies"},
    "policy": {"poddisruptionbudgets", "podsecuritypolicies"},
    "rbac.authorization.k8s.io": {
        "clusterroles",
        "clusterrolebindings",
        "roles",
        "rolebindings",
    },
    "apiregistration.k8s.io": {"apiservices"},
    "apiextensions.k8s.io": {"customresourcedefinitions"},
    "admissionregistration.k8s.io": {
        "mutatingwebhookconfigurations",
        "validatingwebhookconfigurations",
    },
}


def _docs() -> dict[str, dict]:
    return {d["kind"]: d for d in yaml.safe_load_all(MANIFEST.read_text())}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


# --- the RBAC manifest


def test_manifest_names_match_the_decision_table() -> None:
    docs = _docs()
    assert set(docs) == {
        "Namespace",
        "ServiceAccount",
        "ClusterRole",
        "ClusterRoleBinding",
    }
    assert docs["Namespace"]["metadata"]["name"] == "opspilot-readonly"
    sa = docs["ServiceAccount"]
    assert sa["metadata"] == {
        "name": "opspilot-investigator",
        "namespace": "opspilot-readonly",
        "labels": {"app.kubernetes.io/part-of": "opspilot-kind-lab"},
    }
    assert sa["automountServiceAccountToken"] is False
    assert docs["ClusterRole"]["metadata"]["name"] == "opspilot-investigator-readonly"
    binding = docs["ClusterRoleBinding"]
    assert binding["metadata"]["name"] == "opspilot-investigator-readonly"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "opspilot-investigator-readonly",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "opspilot-investigator",
            "namespace": "opspilot-readonly",
        }
    ]
    assert kind_lab.K8S_RBAC_MANIFEST == MANIFEST


def test_rules_are_exactly_the_a1_2_list_with_get_and_list_only() -> None:
    rules = _docs()["ClusterRole"]["rules"]
    granted: dict[str, set[str]] = {}
    for rule in rules:
        assert set(rule) == {"apiGroups", "resources", "verbs"}
        assert rule["verbs"] == ["get", "list"]
        for group in rule["apiGroups"]:
            granted.setdefault(group, set()).update(rule["resources"])
    assert granted == A1_2_RULES


def test_no_rule_reaches_secrets_wildcards_or_escalation() -> None:
    rules = _docs()["ClusterRole"]["rules"]
    words = {
        value
        for rule in rules
        for key in ("apiGroups", "resources", "verbs")
        for value in rule[key]
    }
    assert "*" not in words
    for rule in rules:
        assert "resourceNames" not in rule and "nonResourceURLs" not in rule
        for resource in rule["resources"]:
            assert "secret" not in resource
            assert "*" not in resource
            for banned in ("exec", "attach", "portforward", "proxy", "token"):
                assert banned not in resource
    assert words.isdisjoint(
        {"watch", "create", "update", "patch", "delete", "deletecollection"}
        | {"impersonate", "escalate", "bind"}
    )


# --- API address, CA and token


def test_both_api_addresses_come_from_one_function() -> None:
    assert (
        kind_lab.k8s_api_url("host", "https://127.0.0.1:41234")
        == "https://127.0.0.1:41234"
    )
    assert (
        kind_lab.k8s_api_url("container") == f"https://{kind_lab.NODE_CONTAINER}:6443"
    )
    for server in (
        "http://127.0.0.1:41234",
        "https://10.0.0.1:6443",
        "https://localhost:6443",
        "https://127.0.0.1",
        "https://127.0.0.1:41234/prefix",
    ):
        with pytest.raises(SystemExit):
            kind_lab.k8s_api_url("host", server)
    with pytest.raises(ValueError):
        kind_lab.k8s_api_url("worker")


class _Kubectl:
    def __init__(self, *, token: str = TOKEN, port: int = 41234) -> None:
        self.token = token
        self.port = port
        self.calls: list[list[str]] = []
        self.fail_token = False

    def proc(self, cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if "config" in cmd and "view" in cmd:
            view = {
                "clusters": [
                    {
                        "name": kind_lab.KUBE_CONTEXT,
                        "cluster": {
                            "server": f"https://127.0.0.1:{self.port}",
                            "certificate-authority-data": base64.b64encode(
                                CA_PEM.encode()
                            ).decode(),
                        },
                    }
                ]
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(view), "")
        if "create" in cmd and "token" in cmd:
            if self.fail_token:
                return subprocess.CompletedProcess(cmd, 1, "", "forbidden\n")
            request = {
                "kind": "TokenRequest",
                "status": {"token": self.token, "expirationTimestamp": EXPIRES},
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(request), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def kube(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Kubectl:
    fake = _Kubectl()
    monkeypatch.setattr(kind_lab, "LAB_DIR", tmp_path / "lab")
    monkeypatch.setattr(kind_lab, "KUBECONFIG", tmp_path / "lab/kubeconfig")
    monkeypatch.setattr(kind_lab, "capture", fake.proc)
    monkeypatch.setattr(kind_lab, "run", fake.proc)
    return fake


def test_issuing_writes_private_host_files_and_never_prints_the_token(
    kube: _Kubectl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert kind_lab.issue_k8s_token() == 0
    files = kind_lab.k8s_lab_files()
    for path in files.values():
        assert path.parent == kind_lab.LAB_DIR
        assert _mode(path) == 0o600
    assert [p.name for p in files.values()] == [
        "k8s-ca.crt",
        "k8s-investigator.env",
        "k8s-token.json",
    ]
    assert files["ca"].read_text() == CA_PEM
    assert _env(files["env"]) == {
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:41234",
        "OPSPILOT_K8S_TOKEN": TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(files["ca"]),
    }
    record_text = files["record"].read_text()
    assert TOKEN not in record_text
    record = json.loads(record_text)
    assert record["expires_at"] == EXPIRES
    assert record["requested_duration"] == "24h"
    assert record["api_url"] == {
        "host": "https://127.0.0.1:41234",
        "container": f"https://{kind_lab.NODE_CONTAINER}:6443",
    }
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    (create,) = [c for c in kube.calls if "create" in c and "token" in c]
    assert create[:5] == [
        "kubectl",
        "--kubeconfig",
        str(kind_lab.KUBECONFIG),
        "--context",
        kind_lab.KUBE_CONTEXT,
    ]
    assert create[5:] == [
        "-n",
        "opspilot-readonly",
        "create",
        "token",
        "opspilot-investigator",
        "--duration=24h",
        "-o",
        "json",
    ]


def test_reissuing_rereads_the_port_and_replaces_the_token(kube: _Kubectl) -> None:
    assert kind_lab.issue_k8s_token() == 0
    kube.token, kube.port = NEW_TOKEN, 45678
    assert kind_lab.issue_k8s_token() == 0
    env = _env(kind_lab.k8s_lab_files()["env"])
    assert env["OPSPILOT_K8S_TOKEN"] == NEW_TOKEN
    assert env["OPSPILOT_K8S_API_URL"] == "https://127.0.0.1:45678"


def test_a_refused_token_request_writes_nothing(
    kube: _Kubectl, capsys: pytest.CaptureFixture[str]
) -> None:
    kube.fail_token = True
    assert kind_lab.issue_k8s_token() != 0
    assert not any(p.exists() for p in kind_lab.k8s_lab_files().values())


def test_k8s_token_command_only_reissues(
    kube: _Kubectl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert kind_lab.main(["k8s-token"]) == 0
    assert kind_lab.k8s_lab_files()["env"].exists()
    # no apply, no helm, no kind: only the kubeconfig read and the token
    assert [c[5:7] for c in kube.calls] == [
        ["config", "view"],
        ["-n", "opspilot-readonly"],
    ]


def test_up_applies_the_identity_and_issues_a_token(
    kube: _Kubectl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("OPSPILOT_LAB_RUNTIME", raising=False)
    real = kube.proc

    def proc(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        if cmd[:3] == ["kind", "get", "clusters"]:
            kube.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, f"{kind_lab.CLUSTER}\n", "")
        return real(cmd, **kw)

    monkeypatch.setattr(kind_lab, "capture", proc)
    monkeypatch.setattr(kind_lab, "run", proc)
    for name in (
        "apply_prometheus_auth_secrets",
        "apply_readonly_proxy",
    ):
        monkeypatch.setattr(kind_lab, name, lambda *a, **k: 0)
    for name in (
        "roll_prometheus_on_config_change",
        "roll_collector_on_auth_change",
        "apply_webhook_secret",
    ):
        monkeypatch.setattr(kind_lab, name, lambda *a, **k: 0)
    monkeypatch.setattr(kind_lab, "prometheus_accounts", lambda *a, **k: {})
    monkeypatch.setattr(kind_lab, "ROLE_ENV_VARS", {})
    monkeypatch.setattr(kind_lab, "webhook_token", lambda *a, **k: "t")
    monkeypatch.setattr(kind_lab, "workbench_event_env", lambda *a, **k: "env")
    monkeypatch.setattr(kind_lab, "lab_endpoints", lambda: None)
    monkeypatch.setattr(kind_lab, "health", lambda quiet=False: {"ok": True})
    assert kind_lab.cmd_up(argparse.Namespace(skip_memory_check=True, wait=1)) == 0
    apply = kube.calls.index(
        kind_lab.kubectl("apply", "-f", str(kind_lab.K8S_RBAC_MANIFEST))
    )
    token = next(i for i, c in enumerate(kube.calls) if "create" in c and "token" in c)
    assert apply < token
    assert kind_lab.k8s_lab_files()["env"].exists()


# --- control-secrets: the worker container's wiring (A1-7)


class _Docker:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.worker: list[str] = []
        self.mounted = False

    def proc(self, cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        self.calls.append((list(cmd), dict(kw.get("env") or {})))
        if cmd[:2] == ["docker", "ps"]:
            service = cmd[-1].rsplit("=", 1)[1]
            ids = self.worker if service == "worker" else []
            return subprocess.CompletedProcess(cmd, 0, "\n".join(ids), "")
        if cmd[:2] == ["docker", "inspect"] and "{{json .Mounts}}" in cmd:
            mounts = [{"Destination": "/run/secrets/k8s_ca"}] if self.mounted else []
            return subprocess.CompletedProcess(cmd, 0, json.dumps(mounts), "")
        if cmd[:2] == ["docker", "inspect"]:
            networks = {"kind": {"Aliases": ["opspilot-worker"]}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(networks), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def compose_ups(self) -> list[tuple[list[str], dict]]:
        return [(c, e) for c, e in self.calls if c[:2] == ["docker", "compose"]]


@pytest.fixture
def control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_Docker, Path, Path]:
    lab_dir = tmp_path / "lab"
    monkeypatch.setattr(kind_lab, "LAB_DIR", lab_dir)
    monkeypatch.setattr(kind_lab, "WEBHOOK_TOKEN_FILE", lab_dir / "token")
    monkeypatch.setattr(kind_lab, "AUTH_FILE", lab_dir / "auth.json")
    monkeypatch.delenv("OPSPILOT_SECRETS_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("OPSPILOT_LAB_RUNTIME", raising=False)
    fake = _Docker()
    monkeypatch.setattr(kind_lab, "capture", fake.proc)
    monkeypatch.setattr(kind_lab, "run", fake.proc)
    secrets = tmp_path / "secrets"
    _issue(lab_dir, TOKEN, 41234)
    return fake, lab_dir, secrets


def _issue(lab_dir: Path, token: str, port: int) -> None:
    lab_dir.mkdir(parents=True, exist_ok=True)
    (lab_dir / "k8s-ca.crt").write_text(CA_PEM)
    (lab_dir / "k8s-investigator.env").write_text(
        f"OPSPILOT_K8S_API_URL=https://127.0.0.1:{port}\n"
        f"OPSPILOT_K8S_TOKEN={token}\n"
        f"OPSPILOT_K8S_CA_FILE={lab_dir / 'k8s-ca.crt'}\n"
    )


def _control(secrets: Path, project: str | None = None) -> int:
    return kind_lab.cmd_control_secrets(
        argparse.Namespace(secrets_dir=str(secrets), project=project)
    )


def _report(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    lines = capsys.readouterr().out.splitlines()
    return next(json.loads(x) for x in lines if x.startswith('{"secrets_dir"'))["files"]


def test_worker_env_gets_the_container_form(
    control: tuple[_Docker, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _, secrets = control
    assert _control(secrets) == 0
    env = _env(secrets / "worker_env")
    assert env["OPSPILOT_K8S_API_URL"] == f"https://{kind_lab.NODE_CONTAINER}:6443"
    assert env["OPSPILOT_K8S_TOKEN"] == TOKEN
    assert env["OPSPILOT_K8S_CA_FILE"] == "/run/secrets/k8s_ca"
    assert (secrets / "k8s_ca.crt").read_text() == CA_PEM
    assert _mode(secrets / "k8s_ca.crt") == 0o600
    assert _mode(secrets / "worker_env") == 0o600
    # observer_env carries no cluster credential
    assert not [k for k in _env(secrets / "observer_env") if "K8S" in k]
    report = _report(capsys)
    assert report["worker"] == "not running"
    assert not fake.compose_ups()
    assert TOKEN not in json.dumps(report)


def test_a_reissued_token_replaces_the_old_lines_and_restarts_the_worker(
    control: tuple[_Docker, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, lab_dir, secrets = control
    assert _control(secrets) == 0
    worker_env = secrets / "worker_env"
    worker_env.write_text(worker_env.read_text() + "DEEPSEEK_API_KEY=cccccccc357\n")
    capsys.readouterr()
    _issue(lab_dir, NEW_TOKEN, 45678)
    fake.worker = ["c0ffee"]
    assert _control(secrets, project="opslab") == 0
    lines = worker_env.read_text().splitlines()
    assert [line for line in lines if line.startswith("OPSPILOT_K8S_TOKEN=")] == [
        f"OPSPILOT_K8S_TOKEN={NEW_TOKEN}"
    ]
    assert TOKEN not in worker_env.read_text()
    assert sum(line.startswith("OPSPILOT_K8S_API_URL=") for line in lines) == 1
    assert "DEEPSEEK_API_KEY=cccccccc357" in lines
    assert _report(capsys)["worker"] == "restarted"
    ((cmd, env),) = fake.compose_ups()
    assert cmd == [
        "docker",
        "compose",
        "-p",
        "opslab",
        "-f",
        str(COMPOSE_DIR / "compose.yaml"),
        "-f",
        str(COMPOSE_DIR / "compose.lab.yaml"),
        "up",
        "--detach",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "worker",
    ]
    assert env["OPSPILOT_SECRETS_DIR"] == str(secrets.absolute())
    assert env["COMPOSE_PROFILES"] == "agent"
    # then the recreated worker's kind network membership is checked again
    up = next(
        i for i, (c, _) in enumerate(fake.calls) if c[:2] == ["docker", "compose"]
    )
    assert any(
        c[:3] == ["docker", "inspect", "c0ffee"]
        and "{{json .NetworkSettings.Networks}}" in c
        for c, _ in fake.calls[up:]
    )


def test_an_unchanged_rerun_restarts_only_a_worker_without_the_ca_mount(
    control: tuple[_Docker, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _, secrets = control
    assert _control(secrets) == 0
    fake.worker = ["c0ffee"]
    fake.mounted = True
    capsys.readouterr()
    assert _control(secrets) == 0
    assert _report(capsys)["worker"] == "unchanged"
    assert not fake.compose_ups()
    # e.g. install.sh recreated it without the lab override
    fake.mounted = False
    assert _control(secrets) == 0
    assert _report(capsys)["worker"] == "restarted"
    assert len(fake.compose_ups()) == 1


def test_a_failed_restart_fails_the_command(
    control: tuple[_Docker, Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _, secrets = control
    fake.worker = ["c0ffee"]
    real = fake.proc

    def proc(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        if cmd[:2] == ["docker", "compose"]:
            fake.calls.append((list(cmd), {}))
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return real(cmd, **kw)

    monkeypatch.setattr(kind_lab, "run", proc)
    monkeypatch.setattr(kind_lab, "capture", proc)
    assert _control(secrets) == 1
    assert _report(capsys)["worker"] == "restart failed"


def test_no_token_in_the_lab_leaves_worker_env_without_k8s(
    control: tuple[_Docker, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, lab_dir, secrets = control
    (lab_dir / "k8s-investigator.env").unlink()
    assert _control(secrets) == 0
    assert not [k for k in _env(secrets / "worker_env") if k.startswith("OPSPILOT_K8S")]
    assert not (secrets / "k8s_ca.crt").exists()


# --- Compose override and image entrypoint


def test_only_the_worker_mounts_the_ca_and_only_in_the_lab_override() -> None:
    lab = yaml.safe_load((COMPOSE_DIR / "compose.lab.yaml").read_text())
    assert lab["secrets"] == {
        "k8s_ca": {
            "file": "${OPSPILOT_SECRETS_DIR:?run deploy/compose/install.sh}/k8s_ca.crt"
        }
    }
    assert lab["services"]["worker"]["secrets"] == ["k8s_ca"]
    for service in ("web", "observer"):
        assert "secrets" not in lab["services"][service]
    base = (COMPOSE_DIR / "compose.yaml").read_text()
    assert "k8s" not in base.lower()


def _entrypoint(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ENTRYPOINT), "env"],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def test_the_entrypoint_exports_opspilot_k8s_names_only(tmp_path: Path) -> None:
    env_file = tmp_path / "worker_env"
    marker = tmp_path / "marker"
    env_file.write_text(
        "OPSPILOT_K8S_API_URL=https://opspilot-m1-control-plane:6443\n"
        f"OPSPILOT_K8S_TOKEN={TOKEN}\n"
        "OPSPILOT_K8S_CA_FILE=/run/secrets/k8s_ca\n"
        "OPSPILOT_K8S_NAMESPACE='otel-demo'\n"
        f"OPSPILOT_K8S_TOKEN={NEW_TOKEN}\n"
        f"OPSPILOT_K8S_X$(touch {marker})=1\n"
        "OPSPILOT_K8S_lower=1\n"
        "OPSPILOT_K8SX=1\n"
        "DEEPSEEK_API_KEY=cccccccc357\n"
    )
    result = _entrypoint({"OPSPILOT_ENV_FILE": str(env_file)}, tmp_path)
    assert result.returncode == 0, result.stderr
    exported = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert {k: v for k, v in exported.items() if "K8S" in k} == {
        "OPSPILOT_K8S_API_URL": "https://opspilot-m1-control-plane:6443",
        "OPSPILOT_K8S_TOKEN": TOKEN,
        "OPSPILOT_K8S_CA_FILE": "/run/secrets/k8s_ca",
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
    }
    assert "DEEPSEEK_API_KEY" not in exported
    assert not marker.exists()


def test_an_inline_k8s_value_and_the_env_file_conflict(tmp_path: Path) -> None:
    env_file = tmp_path / "worker_env"
    env_file.write_text(f"OPSPILOT_K8S_TOKEN={TOKEN}\n")
    result = _entrypoint(
        {"OPSPILOT_ENV_FILE": str(env_file), "OPSPILOT_K8S_TOKEN": NEW_TOKEN}, tmp_path
    )
    assert result.returncode == 64
    assert TOKEN not in result.stderr + result.stdout
