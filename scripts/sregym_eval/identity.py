"""OpsPilot's read-only identity in the SREGym cluster (#667 S-4, S-5, S-14).

The identity is the kind lab's (``scripts/kind_lab/opspilot-readonly-rbac.yaml``,
get/list only, no secrets). It must exist before SREGym's first ``main.py``
captures its cluster baseline: after every problem SREGym deletes namespaces,
ClusterRoles and ClusterRoleBindings that are not in the baseline
(``cluster_state.py``). Every trial gets a fresh token and a ``can-i`` probe
first; anything off stops the round.

All kubectl calls go through ``host.kubectl(argv, env, timeout)`` with the
engineer's SREGym kubeconfig, set explicitly (S-14).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml  # type: ignore[import-untyped]

from scripts.itbench_lite.services import base_env
from scripts.sregym_eval.boundary import (
    CAN_I_CHECKS,
    CanICheck,
    can_i_argv,
    check_rbac_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_NAMESPACE = "opspilot-readonly"
SERVICE_ACCOUNT = "opspilot-investigator"
RBAC_MANIFEST = ROOT / "scripts" / "kind_lab" / "opspilot-readonly-rbac.yaml"
TOKEN_DURATION = "24h"
EXPECTED_CONTEXT = "kind-sregym"
DEFAULT_BASELINE = Path("~/cache_dir/cluster_baseline_state.json").expanduser()
KUBECTL_TIMEOUT_S = 60.0


class IdentityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Done(Protocol):
    returncode: int
    stdout: str


class KubectlHost(Protocol):
    def kubectl(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> _Done: ...


@dataclass(frozen=True)
class K8sTarget:
    api_url: str
    ca_file: str
    token: str = field(repr=False)

    def as_json(self) -> dict[str, str]:
        return {"api_url": self.api_url, "ca_file": self.ca_file, "token": self.token}


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def write_target(path: Path, target: K8sTarget) -> None:
    private_write(path, json.dumps(target.as_json()) + "\n")


def read_target(path: Path) -> K8sTarget:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        target = K8sTarget(str(raw["api_url"]), str(raw["ca_file"]), str(raw["token"]))
    except (OSError, ValueError, KeyError, TypeError):
        raise IdentityError("K8S_TARGET_INVALID") from None
    if not (target.api_url and target.ca_file and target.token):
        raise IdentityError("K8S_TARGET_INVALID")
    return target


def kubectl_env(kubeconfig: Path) -> dict[str, str]:
    return base_env() | {"KUBECONFIG": str(kubeconfig)}


def _run(host: KubectlHost, kubeconfig: Path, *args: str) -> _Done:
    return host.kubectl(["kubectl", *args], kubectl_env(kubeconfig), KUBECTL_TIMEOUT_S)


def _loopback_server(server: str) -> bool:
    parts = urllib.parse.urlsplit(server)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "127.0.0.1"
        and port is not None
        and parts.path in ("", "/")
        and not parts.query
        and not parts.username
    )


def _cluster(host: KubectlHost, kubeconfig: Path) -> tuple[str, bytes]:
    done = _run(host, kubeconfig, "config", "view", "--raw", "--minify", "-o", "json")
    if done.returncode != 0:
        raise IdentityError("KUBECONFIG_UNREADABLE")
    try:
        (cluster,) = json.loads(done.stdout)["clusters"]
        server = str(cluster["cluster"]["server"])
        ca = base64.b64decode(
            cluster["cluster"]["certificate-authority-data"], validate=True
        )
    except (KeyError, TypeError, ValueError):
        raise IdentityError("KUBECONFIG_UNREADABLE") from None
    if not ca:
        raise IdentityError("KUBECONFIG_UNREADABLE")
    return server, ca


def check_context(host: KubectlHost, kubeconfig: Path) -> None:
    done = _run(host, kubeconfig, "config", "current-context")
    if done.returncode != 0 or done.stdout.strip() != EXPECTED_CONTEXT:
        raise IdentityError("WRONG_CLUSTER")
    server, _ = _cluster(host, kubeconfig)
    if not _loopback_server(server):
        raise IdentityError("KUBECONFIG_SERVER_UNEXPECTED")


def check_baseline(path: Path = DEFAULT_BASELINE) -> None:
    """A baseline captured before the identity existed would make SREGym
    delete it after the first problem."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError:
        raise IdentityError("BASELINE_UNREADABLE") from None
    if IDENTITY_NAMESPACE not in text:
        raise IdentityError("BASELINE_WITHOUT_IDENTITY")


def apply_identity(
    host: KubectlHost, kubeconfig: Path, manifest: Path = RBAC_MANIFEST
) -> None:
    if check_rbac_manifest(manifest.read_text(encoding="utf-8")):
        raise IdentityError("RBAC_MANIFEST_VIOLATION")
    if _run(host, kubeconfig, "apply", "-f", str(manifest)).returncode != 0:
        raise IdentityError("IDENTITY_APPLY_FAILED")


def readonly_kubeconfig(server: str, ca: bytes, token: str) -> str:
    doc: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "opspilot-readonly",
        "clusters": [
            {
                "name": "sregym",
                "cluster": {
                    "server": server,
                    "certificate-authority-data": base64.b64encode(ca).decode("ascii"),
                },
            }
        ],
        "contexts": [
            {
                "name": "opspilot-readonly",
                "context": {"cluster": "sregym", "user": SERVICE_ACCOUNT},
            }
        ],
        "users": [{"name": SERVICE_ACCOUNT, "user": {"token": token}}],
    }
    return str(yaml.safe_dump(doc, sort_keys=False))


def issue_token(host: KubectlHost, kubeconfig: Path, out_dir: Path) -> K8sTarget:
    """A fresh token; writes ``readonly-kubeconfig.yaml`` and ``k8s-ca.crt``
    (0600) under ``out_dir``. The token is never printed."""
    server, ca = _cluster(host, kubeconfig)
    if not _loopback_server(server):
        raise IdentityError("KUBECONFIG_SERVER_UNEXPECTED")
    done = _run(
        host, kubeconfig,
        "-n", IDENTITY_NAMESPACE, "create", "token", SERVICE_ACCOUNT,
        "--duration", TOKEN_DURATION,
    )  # fmt: skip
    token = done.stdout.strip() if done.returncode == 0 else ""
    if not token or any(c.isspace() for c in token):
        raise IdentityError("TOKEN_ISSUE_FAILED")
    ca_file = out_dir / "k8s-ca.crt"
    private_write(ca_file, ca.decode("ascii", "replace"))
    private_write(
        out_dir / "readonly-kubeconfig.yaml", readonly_kubeconfig(server, ca, token)
    )
    return K8sTarget(api_url=server, ca_file=str(ca_file), token=token)


def probe_can_i(host: KubectlHost, kubeconfig: Path) -> dict[str, str]:
    """Each of ``CAN_I_CHECKS``; ``kubectl auth can-i`` prints yes/no (exit
    0/1). Anything else is kept as-is and fails the verdict."""
    answers: dict[str, str] = {}
    for check in CAN_I_CHECKS:
        done = host.kubectl(
            can_i_argv(check), kubectl_env(kubeconfig), KUBECTL_TIMEOUT_S
        )
        lines = (done.stdout or "").strip().splitlines()
        answers[check.label] = lines[0].strip() if lines else ""
    return answers


def answers_by_check(answers: Mapping[str, str]) -> dict[CanICheck, str]:
    by_label = {c.label: c for c in CAN_I_CHECKS}
    return {by_label[k]: v for k, v in answers.items() if k in by_label}
