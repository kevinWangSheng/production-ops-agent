"""Independent contract tests for issue #553 / tool expansion A1.

接缝假设（仅限本测试，不是新增产品接口）：

* lab lifecycle keeps the existing ``scripts.kind_lab.main`` CLI and the
  existing ``capture``/``run`` subprocess seams; ``k8s-token`` is the new
  subcommand and writes the three A1 credential files below ``LAB_DIR``.
* the address helper is exposed as ``kind_lab.k8s_api_url(container=...)``;
  the token command reads the lab kubeconfig through a subprocess seam and
  uses ``k8s-investigator.env`` as the observable worker credential record.
* the authz probe follows the existing F7 probe shape: ``probe()`` returns a
  report, ``request(...)`` is the transport seam, and ``main`` accepts an
  env-file plus output path.  These choices keep this test offline and let a
  fake transport prove the 200/403 matrix without starting Kubernetes.

The assumptions are deliberately kept here so an implementation can replace
the seams without weakening the public A1 assertions.
"""

from __future__ import annotations

import ast
import base64
import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import k8s_authz_probe as probe
from scripts import kind_lab

ROOT = Path(kind_lab.ROOT)
RBAC = ROOT / "scripts/kind_lab/opspilot-readonly-rbac.yaml"
ENTRYPOINT = ROOT / "deploy/docker-entrypoint.sh"
COMPOSE = ROOT / "deploy/compose/compose.yaml"
COMPOSE_LAB = ROOT / "deploy/compose/compose.lab.yaml"


EXPECTED_RULES = {
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


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _env(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }


def _kubeconfig(server: str, ca: bytes = b"synthetic-ca-bytes") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "kind-opspilot-m1",
                "cluster": {
                    "server": server,
                    "certificate-authority-data": base64.b64encode(ca).decode(),
                },
            }
        ],
        "contexts": [
            {
                "name": "kind-opspilot-m1",
                "context": {"cluster": "kind-opspilot-m1", "user": "admin"},
            }
        ],
        "current-context": "kind-opspilot-m1",
    }


def test_readonly_rbac_manifest_is_the_narrow_cluster_contract() -> None:
    assert RBAC.is_file()
    documents = [doc for doc in yaml.safe_load_all(RBAC.read_text()) if doc]
    by_kind = {doc["kind"]: doc for doc in documents}
    assert {
        doc["metadata"]["name"] for doc in documents if doc["kind"] == "Namespace"
    } == {"opspilot-readonly"}
    assert by_kind["ServiceAccount"]["metadata"] == {
        "name": "opspilot-investigator",
        "namespace": "opspilot-readonly",
    }
    assert by_kind["ClusterRole"]["metadata"]["name"] == (
        "opspilot-investigator-readonly"
    )
    binding = by_kind["ClusterRoleBinding"]
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

    actual: dict[str, set[str]] = {}
    verbs: set[str] = set()
    for rule in by_kind["ClusterRole"]["rules"]:
        groups = rule.get("apiGroups", [])
        assert groups, "every A1 rule must name its API group, including core as ''"
        assert set(rule["verbs"]) <= {"get", "list"}
        verbs.update(rule["verbs"])
        assert "*" not in rule.get("resources", [])
        for group in groups:
            actual.setdefault(group, set()).update(rule.get("resources", []))
    assert verbs == {"get", "list"}
    assert actual == EXPECTED_RULES
    assert not {
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "nodes/proxy",
        "serviceaccounts/token",
    } & set().union(*actual.values())
    assert not any(
        resource == "impersonate"
        or resource.endswith(("/exec", "/attach", "/portforward", "/proxy", "/token"))
        for resource in set().union(*actual.values())
    )
    assert not {
        "argoproj.io",
        "fluxcd.io",
        "istio.io",
        "chaos-mesh.org",
        "monitoring.coreos.com",
        "openshift.com",
    } & set(actual)


class _KubectlFixture:
    def __init__(self, kubeconfig: dict[str, Any], tokens: list[str]) -> None:
        self.kubeconfig = kubeconfig
        self.tokens = iter(tokens)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if "create" in cmd and "token" in cmd:
            return subprocess.CompletedProcess(cmd, 0, next(self.tokens) + "\n", "")
        if "config" in cmd and "view" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps(self.kubeconfig) + "\n", ""
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_k8s_token_writes_private_env_ca_and_metadata_and_replaces_old_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(kind_lab, "LAB_DIR", tmp_path)
    monkeypatch.setattr(kind_lab, "KUBECONFIG", tmp_path / "kubeconfig")
    fixture = _KubectlFixture(
        _kubeconfig("https://127.0.0.1:16443"), ["aaaaaaaa357", "bbbbbbbb357"]
    )
    (tmp_path / "kubeconfig").write_text(json.dumps(fixture.kubeconfig))
    monkeypatch.setattr(kind_lab, "capture", fixture)
    monkeypatch.setattr(kind_lab, "run", fixture)

    assert kind_lab.main(["k8s-token"]) == 0
    first_output = capsys.readouterr()
    assert "aaaaaaaa357" not in first_output.out + first_output.err
    env_path = tmp_path / "k8s-investigator.env"
    ca_path = tmp_path / "k8s-ca.crt"
    meta_path = tmp_path / "k8s-token.json"
    assert _mode(env_path) == _mode(ca_path) == _mode(meta_path) == 0o600
    first_env = _env(env_path)
    assert first_env["OPSPILOT_K8S_API_URL"] == "https://127.0.0.1:16443"
    assert first_env["OPSPILOT_K8S_TOKEN"] == "aaaaaaaa357"
    assert first_env["OPSPILOT_K8S_CA_FILE"] == str(ca_path)
    assert ca_path.read_bytes() == b"synthetic-ca-bytes"
    assert "aaaaaaaa357" not in meta_path.read_text()
    assert set(json.loads(meta_path.read_text())) >= {"expires_at"}

    fixture.kubeconfig = _kubeconfig("https://127.0.0.1:26443")
    (tmp_path / "kubeconfig").write_text(json.dumps(fixture.kubeconfig))
    assert kind_lab.main(["k8s-token"]) == 0
    second_output = capsys.readouterr()
    assert "bbbbbbbb357" not in second_output.out + second_output.err
    second_env = _env(env_path)
    assert second_env["OPSPILOT_K8S_TOKEN"] == "bbbbbbbb357"
    assert second_env["OPSPILOT_K8S_API_URL"] == "https://127.0.0.1:26443"
    all_written = "\n".join(
        p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file()
    )
    assert "aaaaaaaa357" not in all_written
    assert "16443" not in all_written
    token_calls = [c for c in fixture.calls if "create" in c and "token" in c]
    assert len(token_calls) == 2
    assert all("24h" in c for c in token_calls)


def test_k8s_address_helper_has_host_and_container_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kind_lab, "KUBECONFIG", tmp_path / "kubeconfig")
    (tmp_path / "kubeconfig").write_text(
        json.dumps(_kubeconfig("https://127.0.0.1:17555"))
    )
    assert kind_lab.k8s_api_url(container=False) == "https://127.0.0.1:17555"
    assert kind_lab.k8s_api_url(container=True) == (
        f"https://{kind_lab.NODE_CONTAINER}:6443"
    )


def test_k8s_token_subcommand_does_not_run_up_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kind_lab, "LAB_DIR", tmp_path)
    monkeypatch.setattr(kind_lab, "KUBECONFIG", tmp_path / "kubeconfig")
    fixture = _KubectlFixture(_kubeconfig("https://127.0.0.1:16443"), ["cccccccc357"])
    monkeypatch.setattr(kind_lab, "capture", fixture)
    monkeypatch.setattr(kind_lab, "run", fixture)
    assert kind_lab.main(["k8s-token"]) == 0
    assert not any(
        c and c[0] in {"kind", "helm", "docker", "colima"} for c in fixture.calls
    )
    assert not any("apply" in c or "upgrade" in c for c in fixture.calls)


def test_control_secrets_replaces_worker_k8s_lines_and_restarts_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_dir = tmp_path / "lab"
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(kind_lab, "LAB_DIR", lab_dir)
    monkeypatch.setattr(kind_lab, "AUTH_FILE", lab_dir / "auth.json")
    monkeypatch.setattr(kind_lab, "WEBHOOK_TOKEN_FILE", lab_dir / "webhook-token")
    (lab_dir / "k8s-investigator.env").parent.mkdir(parents=True)
    (lab_dir / "k8s-investigator.env").write_text(
        "OPSPILOT_K8S_API_URL=https://127.0.0.1:26443\n"
        "OPSPILOT_K8S_TOKEN=bbbbbbbb357\n"
        f"OPSPILOT_K8S_CA_FILE={lab_dir / 'k8s-ca.crt'}\n"
    )
    secrets_dir.mkdir()
    (secrets_dir / "worker_env").write_text(
        "DEEPSEEK_API_KEY=modelkey357\n"
        "OPSPILOT_K8S_API_URL=https://127.0.0.1:16443\n"
        "OPSPILOT_K8S_TOKEN=oldtoken357\n"
        f"OPSPILOT_K8S_CA_FILE={lab_dir / 'k8s-ca.crt'}\n"
    )
    webhook_token = lab_dir / "webhook-token"
    webhook_token.write_text("webhook-token\n")
    webhook_token.chmod(0o600)
    monkeypatch.setattr(
        kind_lab,
        "prometheus_accounts",
        lambda *_: {role: f"pw-{role}" for role in kind_lab.PROMETHEUS_ACCOUNTS},
    )
    restarted: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        restarted.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_capture(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        output = (
            "worker-container-id\n"
            if cmd and cmd[0] == "docker" and "ps" in cmd
            else ""
        )
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(kind_lab, "run", fake_run)
    monkeypatch.setattr(kind_lab, "capture", fake_capture)
    assert kind_lab.main(["control-secrets", "--secrets-dir", str(secrets_dir)]) == 0
    worker = _env(secrets_dir / "worker_env")
    container_url = f"https://{kind_lab.NODE_CONTAINER}:6443"
    assert worker["OPSPILOT_K8S_API_URL"] == container_url
    assert worker["OPSPILOT_K8S_TOKEN"] == "bbbbbbbb357"
    assert "https://127.0.0.1:16443" not in (secrets_dir / "worker_env").read_text()
    assert "oldtoken357" not in (secrets_dir / "worker_env").read_text()
    assert any(
        "worker" in cmd
        and ("restart" in cmd or ("up" in cmd and "--force-recreate" in cmd))
        and not {"web", "observer"} & set(cmd)
        for cmd in restarted
    )


def test_entrypoint_exports_the_k8s_allowlist_from_worker_env(tmp_path: Path) -> None:
    env_file = tmp_path / "worker_env"
    env_file.write_text(
        "\n".join(
            (
                "OPSPILOT_K8S_API_URL=https://127.0.0.1:16443",
                "OPSPILOT_K8S_TOKEN=aaaaaaaa357",
                f"OPSPILOT_K8S_CA_FILE={tmp_path / 'k8s-ca.crt'}",
                "OPSPILOT_K8S_NAMESPACE=opspilot-readonly",
            )
        )
        + "\n"
    )
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    exported = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line.startswith("OPSPILOT_K8S_")
    )
    assert exported == _env(env_file)


def test_lab_compose_mounts_only_the_worker_ca_secret() -> None:
    base_text = COMPOSE.read_text()
    base = yaml.safe_load(base_text)
    lab = yaml.safe_load(COMPOSE_LAB.read_text())
    services = lab["services"]
    ca_names = {
        name
        for name in lab.get("secrets", {})
        if "k8s" in name.lower() and "ca" in name.lower()
    }
    assert len(ca_names) == 1
    (ca_name,) = ca_names
    assert ca_name in services["worker"].get("secrets", [])
    assert ca_name not in services["web"].get("secrets", [])
    assert ca_name not in services["observer"].get("secrets", [])
    assert ca_name not in base.get("secrets", {})
    assert ca_name not in base_text


def test_authz_probe_is_stdlib_only_and_disables_proxy_and_redirects() -> None:
    path = ROOT / "scripts/k8s_authz_probe.py"
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    stdlib = set(sys.stdlib_module_names)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported <= stdlib
    assert "ProxyHandler({})" in source
    assert "HTTPRedirectHandler" in source
    assert "redirect_request" in source


def _probe_path(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    strings = [value for value in args if isinstance(value, str)] + [
        value for value in kwargs.values() if isinstance(value, str)
    ]
    for value in strings:
        if value.startswith(("http://", "https://", "/")):
            parsed = urlsplit(value)
            return unquote(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    return ""


def _probe_method(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    for value in args:
        if isinstance(value, str) and value.upper() in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            return value.upper()
    for key in ("method", "verb"):
        value = kwargs.get(key)
        if isinstance(value, str):
            return value.upper()
    return "GET"


def _probe_matrix(
    monkeypatch: pytest.MonkeyPatch, mismatch: bool = False
) -> dict[str, Any]:
    seen: list[tuple[str, str]] = []
    known_secrets = ("lab-known-secret-357",)

    def fake_request(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        method = _probe_method(args, kwargs)
        path = _probe_path(args, kwargs)
        seen.append((method, path))
        headers: dict[str, str] = {}
        for value in (*args, *kwargs.values()):
            if isinstance(value, dict):
                headers.update(
                    {str(key).lower(): str(item) for key, item in value.items()}
                )
        write = method in {"POST", "PUT", "PATCH", "DELETE"}
        query = path.partition("?")[2].split("&")
        watches = {
            item.lower() for item in query if item.lower() in {"watch=true", "watch=1"}
        }
        impersonating = any(
            key in {"impersonate-user", "impersonate-group"} for key in headers
        )
        custom_groups = (
            "source.toolkit.fluxcd.io",
            "networking.istio.io",
            "argoproj.io",
            "chaos-mesh.org",
            "monitoring.coreos.com",
            "openshift.io",
        )
        custom_resource = any(f"/apis/{group}/" in path for group in custom_groups)
        if write:
            assert "dryRun=All" in unquote(path)
        denied = (
            any(
                marker in path
                for marker in (
                    "/secrets",
                    "/exec",
                    "/attach",
                    "/portforward",
                    "/proxy",
                    "/serviceaccounts/token",
                )
            )
            or bool(watches)
            or impersonating
            or custom_resource
        )
        status = 403 if write or denied else 200
        if mismatch and denied:
            status = 200
        if "/configmaps" in path and method == "GET" and not denied:
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMapList",
                "items": [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "flagd-config"},
                        "data": {"paymentFailure": "off"},
                    }
                ],
            }
            return status, json.dumps(body).encode()
        return status, b"{}"

    monkeypatch.setattr(
        probe, "authorization", lambda *args, **kwargs: "Bearer aaaaaaaaa357"
    )
    monkeypatch.setattr(probe, "request", fake_request)
    report = probe.probe(known_secrets=known_secrets)
    assert seen, "probe must exercise the fake transport"
    assert any(method in {"POST", "PUT", "PATCH", "DELETE"} for method, _ in seen)
    assert report.get("known_secret_hits") == 0
    return report


def test_authz_probe_accepts_expected_200_and_403_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _probe_matrix(monkeypatch)
    assert report["ok"] is True


def test_authz_probe_fails_as_a_whole_when_a_denial_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _probe_matrix(monkeypatch, mismatch=True)
    assert report["ok"] is False


@pytest.mark.parametrize("payload", ["token", "jwt"])
def test_authz_probe_refuses_to_write_token_or_jwt_shaped_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    token = "aaaaaaaa357"
    jwt = ".".join(("a" * 8, "b" * 8, "c" * 8))
    env_file = tmp_path / "k8s-investigator.env"
    ca_file = tmp_path / "k8s-ca.crt"
    ca_file.write_bytes(b"synthetic-ca")
    env_file.write_text(
        "\n".join(
            (
                "OPSPILOT_K8S_API_URL=https://127.0.0.1:16443",
                f"OPSPILOT_K8S_TOKEN={token}",
                f"OPSPILOT_K8S_CA_FILE={ca_file}",
            )
        )
        + "\n"
    )
    monkeypatch.setattr(
        probe,
        "probe",
        lambda **_: {"ok": True, "evidence": token if payload == "token" else jwt},
    )
    output = tmp_path / "probe.json"
    assert probe.main(["--env-file", str(env_file), "--output", str(output)]) != 0
    assert not output.exists()
