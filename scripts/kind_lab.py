"""Engineer entry point for the M1-02 kind lab: OTel Demo 2.0.2 on Kubernetes.

Successor of ``scripts/otel_demo_lab.py`` (the M0 Compose lab, whose
directory no longer exists) for decision D2/D4 of
``docs/tasks/2026-10-03-m1-02-recovery-observation.md``: a kind cluster on its
own colima profile, the official OTel Demo Helm chart at a pinned version
trimmed to the checkout fault path (``scripts/kind_lab/values.yaml``), and
kube-state-metrics so Prometheus carries deployment and pod state. M1-04
(``docs/tasks/2026-10-10-m1-04-alert-intake.md``) adds a pinned Alertmanager
release, one checkout alert rule in the demo's Prometheus and a webhook to
the workbench on the host with a bearer token (``webhook_token``).

On a Linux host (F1 closure S4, ``lab_runtime``) the same commands use the
host's docker daemon on the default socket instead of a colima VM, read
``/proc/meminfo`` for the memory check and point the webhook and the OpsPilot
scrape targets at the control-side containers joined to the kind network
(``connect-control``); ``scripts/kind_lab/install_linux.sh``
installs the pinned tools and runs ``up``.

``up`` checks host memory, starts the colima profile, creates the kind
cluster and installs or upgrades the three Helm releases, then waits for the
backends (it takes chart-owned ConfigMap fields back, so ``up`` while a
fault is injected restores flagd); ``health`` checks what the product profile and the Observer read
(Prometheus span metrics, kube-state-metrics series, Jaeger lists
``checkout``, frontend answers); Prometheus requires basic auth since M1-02
step 4 (one account per role, see ``prometheus_accounts``) and ``env-file``
writes a role's credentials for the engineer to source; ``stop`` stops the colima VM and keeps the
cluster and releases installed (no ``kind delete``, no ``helm uninstall``);
``fault inject|restore`` patches the ``flagd-config`` ConfigMap
(``paymentFailure`` -> ``100%`` by default, or ``--flag`` one of
``FAULT_FLAGS`` to its fault variant / back to ``off``), recording original and
changed bytes with SHA-256 under an engineer-only history directory and
refusing a second injection or a restore over an unexpected edit, like the
M0 ``development_fault.py`` hook. ``up`` also applies the read-only cluster
identity (``scripts/kind_lab/opspilot-readonly-rbac.yaml``, A1 #553) and
issues it a 24h token under LAB_DIR; ``k8s-token`` re-issues only the token
and ``control-secrets`` hands it to the control-side worker.

This is an engineer script: it starts, stops and mutates the *lab*, which
PRODUCT-CONSTRAINTS reserves for engineers and isolated harnesses. Nothing
here is reachable from the product, the worker or the model; the product
profile (``opspilot.tools.otel_demo``) only ever reads Prometheus and Jaeger
over the host ports kind publishes (``scripts/kind_lab/kind-config.yaml``).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts/kind_lab"

PROFILE = "m1-kind"
CONTEXT = f"colima-{PROFILE}"
# F1 closure S7c: several independent labs on one Linux host. Instance 0 is
# the lab as it always was (names, host ports, LAB_DIR); instance n in
# 1..MAX_INSTANCE (``docker`` runtime only: one colima VM holds one lab) gets
# its own kind cluster, LAB_DIR, Prometheus accounts, control-side aliases
# and host ports shifted by n * PORT_STEP. Chosen by OPSPILOT_LAB_INSTANCE,
# a small integer, never a path (PR #111 review P1 still holds: no override
# can point one lab's files at another's).
INSTANCE_ENV = "OPSPILOT_LAB_INSTANCE"
MAX_INSTANCE = 3
PORT_STEP = 100


def lab_instance() -> int:
    configured = os.environ.get(INSTANCE_ENV, "0")
    if not configured.isdigit() or not 0 <= int(configured) <= MAX_INSTANCE:
        raise SystemExit(
            f"{INSTANCE_ENV} must be an integer 0..{MAX_INSTANCE}, not {configured!r}"
        )
    return int(configured)


INSTANCE = lab_instance()
SUFFIX = "" if INSTANCE == 0 else f"-i{INSTANCE}"
FRONTEND_PORT = 18080 + INSTANCE * PORT_STEP
PROMETHEUS_PORT = 19090 + INSTANCE * PORT_STEP
JAEGER_PORT = 16686 + INSTANCE * PORT_STEP
CLUSTER = f"opspilot-m1{SUFFIX}"
# The kind node container (``docker start/stop`` on a Linux host).
NODE_CONTAINER = f"{CLUSTER}-control-plane"
# Where the lab runs (F1 closure S4). ``colima`` (macOS default): the
# ``m1-kind`` VM, its docker context and the colima host address. ``docker``
# (Linux default): the host's own daemon on the default socket, no VM; the
# control-side containers join the kind network (``connect-control``). Overrides:
# OPSPILOT_LAB_RUNTIME, OPSPILOT_LAB_DOCKER_CONTEXT, OPSPILOT_LAB_HOST_ADDRESS,
# OPSPILOT_LAB_HOST_PORTS.
RUNTIMES = ("colima", "docker")
# host.lima.internal under colima (vz, user-mode network): reaches the host
# loopback. The committed values files carry this address.
COLIMA_HOST_ADDRESS = "192.168.5.2"
MEMINFO = Path("/proc/meminfo")
KUBE_CONTEXT = f"kind-{CLUSTER}"
NAMESPACE = "otel-demo"
RELEASE = "otel-demo"
# Pinned charts (D4). The demo chart's appVersion is 2.0.2, the version the
# M0 Compose lab froze and the product profile's service list was taken from.
DEMO_REPO = "https://open-telemetry.github.io/opentelemetry-helm-charts"
DEMO_CHART_VERSION = "0.37.8"
KSM_REPO = "https://prometheus-community.github.io/helm-charts"
KSM_CHART_VERSION = "8.6.0"
KSM_RELEASE = "kube-state-metrics"
# M1-04 (E15): its own pinned release, the version the demo chart's
# Prometheus subchart bundles (alertmanager 1.24.0, appVersion v0.28.1).
AM_CHART_VERSION = "1.24.0"
AM_RELEASE = "alertmanager"
COLIMA_CPU = "4"
COLIMA_MEMORY_GIB = "8"
COLIMA_DISK_GIB = "30"
# D4: 16 GB host shared with other agents. ``up`` refuses to start the VM when
# less than this is reclaimable (free + inactive + speculative + purgeable
# pages), unless ``--skip-memory-check`` is given.
MIN_HOST_RECLAIMABLE_GIB = 3.0

PROMETHEUS = os.environ.get(
    "OPSPILOT_OTEL_PROMETHEUS_URL", f"http://127.0.0.1:{PROMETHEUS_PORT}"
)
# Same variable and meaning as the product profile: the Jaeger UI base.
JAEGER = os.environ.get(
    "OPSPILOT_OTEL_JAEGER_URL", f"http://127.0.0.1:{JAEGER_PORT}/jaeger/ui"
).rstrip("/")
FRONTEND = f"http://127.0.0.1:{FRONTEND_PORT}/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


# The opener every engineer script uses for the lab Prometheus: no proxy and
# no redirects. urllib would otherwise follow a 30x and copy the lab
# ``Authorization`` header onto the new request, another origin included
# (issue #126); a redirect surfaces as its 30x status instead.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

FLAG = "paymentFailure"
FLAG_FILE = "demo.flagd.json"
NORMAL_VARIANT = "off"
FAULT_VARIANT = "100%"
# The only flags the fault hook may move, each to its one fault variant
# (eval PR-C, A2): the payment default above plus the two development
# scenarios' flags. No free flag or variant is accepted.
FAULT_FLAGS = {
    "paymentFailure": FAULT_VARIANT,
    "productCatalogFailure": "on",
    "cartFailure": "on",
}
# Everything this script writes on the host lives under this git-ignored,
# lab-owned directory: fault history, the lab kubeconfig and Helm's
# repository config/cache/data. Not configurable (PR #111 review P1: an
# environment override could point the history at another lab's data).
LAB_DIR = ROOT / f"tmp/m1-kind-lab{SUFFIX}"
HISTORY_ROOT = LAB_DIR / "engineer-only"
KUBECONFIG = LAB_DIR / "kubeconfig"
KUBECTL = "kubectl"
# Prometheus basic auth (M1-02 step 4, decision D3: separate read-only
# credentials for the Observer and the investigation side). The passwords
# are generated here on the first ``up`` and live only in this 600 file;
# ``up`` renders them into two Kubernetes Secrets (the web config with
# bcrypt hashes; the collector's push password as an env var) and writes one
# ``KEY=value`` env file per product role for the engineer to source. They
# never go into the repository, the chart values, logs or evidence.
AUTH_FILE = LAB_DIR / "prometheus-auth.json"
PROMETHEUS_ACCOUNTS = (
    "observer",
    "investigator",
    "collector",
    "lab",
    "proxy",
    "holmes",
)
# Accounts an older lab auth file may lack; ``prometheus_accounts`` adds them
# without touching the existing passwords (F7 PR-C added proxy, #396 holmes).
ADDED_ACCOUNTS = frozenset({"proxy", "holmes"})
WEB_CONFIG_SECRET = "prometheus-web-config"
COLLECTOR_AUTH_SECRET = "prometheus-web-auth"
# M1-04 (E13): Alertmanager's bearer token for the workbench webhook. The
# token stays in the engineer-only file and the Secret Alertmanager mounts;
# the workbench only gets its sha256 under the dedicated actor.
WEBHOOK_TOKEN_FILE = LAB_DIR / "alertmanager-webhook-token"
WEBHOOK_SECRET = "alertmanager-opspilot-webhook"
WEBHOOK_ACTOR = "alertmanager"
WORKBENCH_EVENT_ENV = LAB_DIR / "workbench-alertmanager.env"
# F11 PR-3 (U4): the experiment harness's bearer token for
# ``POST /intake/version-changes``; same file discipline, its own dedicated
# actor, and its hash in the same workbench env file as Alertmanager's.
RELEASE_TOKEN_FILE = LAB_DIR / "release-deployer-token"
RELEASE_ACTOR = "release-deployer"
# The variables each product role reads (only its own; no fallback).
ROLE_ENV_VARS = {
    "observer": (
        "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME",
        "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD",
    ),
    "investigator": (
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD",
    ),
    # engineer scripts (this one, scripts/otel_demo_observe.py, the evidence
    # collectors): not a product role, its own account all the same
    "lab": (
        "OPSPILOT_LAB_PROMETHEUS_USERNAME",
        "OPSPILOT_LAB_PROMETHEUS_PASSWORD",
    ),
    # F14 step 1 (#396, decision Q6): the pinned HolmesGPT baseline's own
    # proxy account; not a product role. The proxy lets only this account
    # POST form bodies, and only to query/query_range (``proxy_nginx_config``).
    "holmes": (
        "OPSPILOT_HOLMES_PROMETHEUS_USERNAME",
        "OPSPILOT_HOLMES_PROMETHEUS_PASSWORD",
    ),
}


def lab_env() -> dict[str, str]:
    """Environment for helm and kind: lab-local Helm homes and kubeconfig.

    ``helm repo add/update`` would otherwise edit the user's global
    ``repositories.yaml`` and chart cache, and ``kind create`` would write
    into and switch the current context of ``~/.kube/config``.
    """
    return {
        **os.environ,
        "HELM_CONFIG_HOME": str(LAB_DIR / "helm/config"),
        "HELM_CACHE_HOME": str(LAB_DIR / "helm/cache"),
        "HELM_DATA_HOME": str(LAB_DIR / "helm/data"),
        "KUBECONFIG": str(KUBECONFIG),
    }


def history_dir(experiment_id: str | None) -> Path:
    """The history directory for one experiment, confined to ``HISTORY_ROOT``.

    Refuses an id that escapes the root (``..``, absolute) and a directory
    that resolves (through a symlink) outside the root.
    """
    target = HISTORY_ROOT / experiment_id if experiment_id else HISTORY_ROOT
    root = HISTORY_ROOT.resolve()
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise SystemExit(f"fault history must stay under {HISTORY_ROOT}; refusing")
    return target


def run(
    cmd: list[str], *, timeout: float = 900, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, timeout=timeout, check=False, env=env)


def capture(
    cmd: list[str], *, timeout: float = 120, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def docker_env() -> dict[str, str]:
    """kind talks to the profile's docker daemon through the docker context.

    kind copies the host's proxy variables into the node's containerd. A
    host proxy on ``127.0.0.1`` is unreachable from inside the node (every
    image pull failed with ``proxyconnect ... connection refused`` on
    2026-10-05) while the colima VM reaches the registries directly, so the
    proxy variables are dropped for kind.
    """
    env = {
        key: value
        for key, value in lab_env().items()
        if key.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    }
    # Only the lab's own choice of daemon applies: an inherited context or
    # DOCKER_HOST (which overrides any context) would redirect it.
    env.pop("DOCKER_CONTEXT", None)
    env.pop("DOCKER_HOST", None)
    context = docker_context()
    return env if context is None else {**env, "DOCKER_CONTEXT": context}


def lab_runtime() -> str:
    """``colima`` on macOS, ``docker`` elsewhere, or OPSPILOT_LAB_RUNTIME."""
    configured = os.environ.get("OPSPILOT_LAB_RUNTIME")
    if configured:
        if configured not in RUNTIMES:
            raise SystemExit(
                f"OPSPILOT_LAB_RUNTIME must be one of {RUNTIMES}, not {configured!r}"
            )
        return configured
    return "colima" if sys.platform == "darwin" else "docker"


def docker_context() -> str | None:
    """The docker context kind and the lab's docker calls use.

    OPSPILOT_LAB_DOCKER_CONTEXT when set; else the colima profile's context,
    or ``None`` (the default socket) for the ``docker`` runtime.
    """
    configured = os.environ.get("OPSPILOT_LAB_DOCKER_CONTEXT")
    if configured:
        return configured
    return CONTEXT if lab_runtime() == "colima" else None


#: The OpsPilot endpoints the committed values address under colima: the
#: workbench webhook and the three health listeners as compose.lab.yaml
#: publishes them on the host loopback.
COLIMA_ENDPOINTS = {
    "webhook": f"{COLIMA_HOST_ADDRESS}:8080",
    "web": f"{COLIMA_HOST_ADDRESS}:19464",
    "worker": f"{COLIMA_HOST_ADDRESS}:19465",
    "observer": f"{COLIMA_HOST_ADDRESS}:19466",
}
#: Linux default: the control-side containers join the ``kind`` docker
#: network under these aliases (``connect-control``) and pods reach their
#: container ports directly; the control side keeps publishing on 127.0.0.1
#: only and the host firewall is not involved (the #393 wiring).
CONTROL_SERVICES = ("web", "worker", "observer")
CONTROL_ALIASES = {
    service: f"opspilot-{service}{SUFFIX}" for service in CONTROL_SERVICES
}
KIND_NETWORK_ENDPOINTS = {
    "webhook": f"{CONTROL_ALIASES['web']}:8080",
    "web": f"{CONTROL_ALIASES['web']}:9464",
    "worker": f"{CONTROL_ALIASES['worker']}:9464",
    "observer": f"{CONTROL_ALIASES['observer']}:9464",
}


def lab_endpoints() -> dict[str, str] | None:
    """Where pods send the webhook and scrape the OpsPilot health listeners,
    as replacements for the committed ``COLIMA_ENDPOINTS``; ``None`` keeps the
    committed values (colima).

    OPSPILOT_LAB_HOST_ADDRESS: that address with the host ports (fallback
    when the control side cannot join the kind network: it must then listen
    on that address and the firewall must admit the kind subnet), each port
    replaceable through OPSPILOT_LAB_HOST_PORTS (``_host_ports``). Otherwise
    colima: ``None``; docker: ``KIND_NETWORK_ENDPOINTS``.
    """
    configured = os.environ.get("OPSPILOT_LAB_HOST_ADDRESS")
    ports = _host_ports(os.environ.get("OPSPILOT_LAB_HOST_PORTS", ""))
    if ports and not configured:
        raise SystemExit("OPSPILOT_LAB_HOST_PORTS needs OPSPILOT_LAB_HOST_ADDRESS")
    if configured:
        address = _checked_address(configured)
        if address == COLIMA_HOST_ADDRESS and not ports:
            return None
        return {
            key: f"{address}:{ports.get(key, endpoint.rsplit(':', 1)[1])}"
            for key, endpoint in COLIMA_ENDPOINTS.items()
        }
    if lab_runtime() == "colima":
        return None
    return dict(KIND_NETWORK_ENDPOINTS)


def _host_ports(spec: str) -> dict[str, int]:
    """OPSPILOT_LAB_HOST_PORTS: ``key=port`` pairs (keys of COLIMA_ENDPOINTS)
    replacing the host ports with the host address fallback. The 72h soak
    (S-R1) needs them: its reverse tunnel listens on the kind gateway, where
    18080 is the lab frontend's port on that host."""
    ports: dict[str, int] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in COLIMA_ENDPOINTS or key in ports:
            raise SystemExit(f"lab host ports: bad entry {item!r}")
        if not value.isdigit() or not 0 < int(value) < 65536:
            raise SystemExit(f"lab host ports: bad port {item!r}")
        ports[key] = int(value)
    return ports


def _checked_address(address: str) -> str:
    parts = address.split(".")
    if len(parts) != 4 or not all(p.isdigit() and int(p) < 256 for p in parts):
        raise SystemExit(f"lab host address must be an IPv4 address: {address!r}")
    return address


def lab_values_file(name: str, endpoints: dict[str, str] | None) -> Path:
    """The values file ``helm`` gets for ``scripts/kind_lab/<name>``.

    The committed file itself when ``endpoints`` is ``None`` (macOS
    behaviour unchanged); otherwise a copy under LAB_DIR with each committed
    endpoint replaced (comments keep their wording), refusing a file whose
    values still name the colima address.
    """
    source = CONFIG / name
    if endpoints is None:
        return source
    text = source.read_text()
    for key, committed in COLIMA_ENDPOINTS.items():
        text = text.replace(committed, endpoints[key])
    if any(
        COLIMA_HOST_ADDRESS in line and not line.lstrip().startswith("#")
        for line in text.splitlines()
    ):
        raise SystemExit(f"{name}: unmapped {COLIMA_HOST_ADDRESS} endpoint; refusing")
    rendered = LAB_DIR / "rendered" / name
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_text(text)
    return rendered


def control_containers(project: str) -> dict[str, list[str]]:
    """Container ids of each control-side service of a Compose project."""
    found: dict[str, list[str]] = {}
    for service in CONTROL_SERVICES:
        proc = capture(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ],
            timeout=30,
            env=docker_env(),
        )
        found[service] = proc.stdout.split() if proc.returncode == 0 else []
    return found


def cmd_connect_control(args: argparse.Namespace) -> int:
    """Join the running web/worker/observer containers of a Compose project
    to the ``kind`` network under ``CONTROL_ALIASES`` (idempotent; a
    container already on the network without its alias is reconnected with
    it). Compose
    recreating a container drops the connection: run again after
    ``install.sh upgrade``."""
    env = docker_env()
    report: dict[str, str] = {}
    # ``services``: only these (control-secrets rejoins a recreated worker)
    services = getattr(args, "services", CONTROL_SERVICES)
    for service, ids in control_containers(args.project).items():
        if service not in services:
            continue
        if len(ids) != 1:
            report[service] = "missing" if not ids else "ambiguous"
            continue
        (container,) = ids
        inspect = capture(
            [
                "docker",
                "inspect",
                container,
                "-f",
                "{{json .NetworkSettings.Networks}}",
            ],
            timeout=30,
            env=env,
        )
        try:
            networks = json.loads(inspect.stdout or "{}")
        except ValueError:
            networks = {}
        alias = CONTROL_ALIASES[service]
        attached = networks.get("kind") if isinstance(networks, dict) else None
        if isinstance(attached, dict):
            names = (attached.get("Aliases") or []) + (attached.get("DNSNames") or [])
            if alias in names:
                report[service] = "already connected"
                continue
            # joined without the alias (e.g. the #393 manual wiring): the
            # rendered values would not resolve, so reconnect with it
            proc = run(
                ["docker", "network", "disconnect", "kind", container],
                env=env,
                timeout=60,
            )
            if proc.returncode != 0:
                report[service] = "failed"
                continue
        proc = run(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                alias,
                "kind",
                container,
            ],
            env=env,
            timeout=60,
        )
        report[service] = "connected" if proc.returncode == 0 else "failed"
    print(json.dumps({"project": args.project, "kind_network": report}))
    ok = all(v in ("connected", "already connected") for v in report.values())
    return 0 if ok else 1


#: Single-host wiring (F9 step 1, #531): lab and control side on one Linux
#: host. Inside the ``kind`` network the control containers reach the lab's
#: readonly Prometheus proxy and Jaeger on the kind node's NodePorts.
NODE_PROMETHEUS_URL = f"http://{NODE_CONTAINER}:30090"
NODE_JAEGER_URL = f"http://{NODE_CONTAINER}:30686/jaeger/ui"
#: The workbench host port: the lab frontend holds FRONTEND_PORT, and every
#: lab instance's ports are PORT_STEP apart, so +10 collides with none.
CONTROL_WEB_PORT = FRONTEND_PORT + 10
#: The otel-demo profile's one target (``opspilot.tools.otel_demo``) and the
#: registry entry the checkout alert rule's labels resolve to (as
#: ``scripts/eval_live_inject.ALERT_TARGET``).
OTEL_TARGET_ID = "m0-otel-20260909"
OTEL_TARGET = {
    "integration_id": OTEL_TARGET_ID,
    "cluster_uid": CLUSTER,
    "namespace": NAMESPACE,
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {
        "version": 1,
        "labels": {"namespace": NAMESPACE, "service": "checkout"},
    },
}


def control_secrets_dir(configured: str | None) -> Path:
    """The installer's default secrets directory (``deploy/compose/install.sh``)."""
    if configured:
        path = Path(configured)
    elif os.environ.get("OPSPILOT_SECRETS_DIR"):
        path = Path(os.environ["OPSPILOT_SECRETS_DIR"])
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / "opspilot"
    path = path.expanduser().absolute()
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise SystemExit("the secrets directory must be outside the repository")
    return path


def _active_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def _merge_env(path: Path, lines: Mapping[str, str]) -> str:
    """Append ``KEY=value`` for each key ``path`` has no active line for;
    an existing line is kept, whatever its value."""
    current = path.read_text() if path.exists() else ""
    present = _active_keys(current)
    missing = {k: v for k, v in lines.items() if k not in present}
    if not missing:
        os.chmod(path, 0o600)
        return "kept"
    if current and not current.endswith("\n"):
        current += "\n"
    _private_write(path, current + "".join(f"{k}={v}\n" for k, v in missing.items()))
    return "written" if not present & set(lines) else "completed"


def cmd_control_secrets(args: argparse.Namespace) -> int:
    """Prepare the control side's secrets directory for this lab on the same
    host, before ``install.sh install`` (#531): the lab's Alertmanager token
    as ``alertmanager_token`` (the installer generates one only when the
    file is absent), the otel-demo endpoints and the investigator / Observer
    accounts in ``worker_env`` / ``observer_env``, the lab target in
    ``target_identities.json`` and, for a first install, ``install.env``
    with a workbench port clear of the lab frontend. Never prints a value."""
    secrets_dir = control_secrets_dir(args.secrets_dir)
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    report: dict[str, str] = {}
    lab_token = webhook_token(WEBHOOK_TOKEN_FILE)
    token_file = secrets_dir / "alertmanager_token"
    if token_file.exists() and token_file.read_text().strip() != lab_token:
        print(
            f"{token_file} already holds another token (the installer generated it); "
            "the workbench accepts only that one. Give the lab the same token: "
            f"install -m 600 {token_file} {WEBHOOK_TOKEN_FILE} && "
            "python3 scripts/kind_lab.py up",
            file=sys.stderr,
        )
        return 1
    report["alertmanager_token"] = "kept" if token_file.exists() else "written"
    if not token_file.exists():
        _private_write(token_file, lab_token + "\n")
    os.chmod(token_file, 0o600)
    accounts = prometheus_accounts(AUTH_FILE)
    roles = {
        "worker_env": (
            "investigator",
            {
                "OPSPILOT_OTEL_PROMETHEUS_URL": NODE_PROMETHEUS_URL,
                "OPSPILOT_OTEL_JAEGER_URL": NODE_JAEGER_URL,
            },
        ),
        "observer_env": ("observer", {}),
    }
    for name, (role, extra) in roles.items():
        user_var, password_var = ROLE_ENV_VARS[role]
        lines = {**extra, user_var: role, password_var: accounts[role]}
        report[name] = _merge_env(secrets_dir / name, lines)
    settings = secrets_dir / "install.env"
    project = (
        getattr(args, "project", None)
        or (
            _read_env_values(settings).get("OPSPILOT_PROJECT")
            if settings.exists()
            else None
        )
        or "opspilot"
    )
    k8s = control_k8s_wiring(secrets_dir, project)
    report.update(k8s or {})
    targets_file = secrets_dir / "target_identities.json"
    targets = json.loads(targets_file.read_text()) if targets_file.exists() else {}
    if not isinstance(targets, dict):
        raise SystemExit(f"{targets_file} is not a JSON object; refusing")
    if OTEL_TARGET_ID in targets:
        report["target_identities.json"] = "kept"
    else:
        targets[OTEL_TARGET_ID] = OTEL_TARGET
        _private_write(targets_file, json.dumps(targets, indent=2) + "\n")
        report["target_identities.json"] = "written"
    settings = secrets_dir / "install.env"
    wanted = {
        "OPSPILOT_WEB_HOST_PORT": str(CONTROL_WEB_PORT),
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OBSERVER_PROMETHEUS_URL": NODE_PROMETHEUS_URL,
    }
    differing: list[str] = []
    if settings.exists():
        report["install.env"] = "kept"
        saved = dict(
            line.split("=", 1)
            for line in settings.read_text().splitlines()
            if "=" in line
        )
        differing = [f"{k}={v}" for k, v in wanted.items() if saved.get(k) != v]
    else:
        _private_write(settings, "".join(f"{k}={v}\n" for k, v in wanted.items()))
        report["install.env"] = "written"
    print(json.dumps({"secrets_dir": str(secrets_dir), "files": report}))
    if differing:
        print(
            "install.env (an earlier install) has other values; for this lab pass "
            f"{' '.join(differing)} to the next install.sh run"
        )
    print(
        f"next: add DEEPSEEK_API_KEY=<key> to {secrets_dir}/worker_env, then "
        "OPSPILOT_START_AGENT=1 deploy/compose/install.sh install && "
        "python3 scripts/kind_lab.py connect-control --project opspilot"
    )
    if k8s is None:
        print(
            "no read-only cluster token in this lab yet (run `kind_lab.py up` or "
            "`kind_lab.py k8s-token`); worker_env has no OPSPILOT_K8S_* lines"
        )
    elif report.get("worker") == "not running":
        print(
            "the cluster CA reaches the worker only through compose.lab.yaml: "
            "after install.sh install (or upgrade) run control-secrets again "
            "to recreate the worker with it"
        )
    if str(report.get("worker", "")).endswith("failed"):
        return 1
    return 0


def kubectl(*args: str) -> list[str]:
    """Always the lab kubeconfig and context; never the user's current one."""
    return [
        KUBECTL,
        "--kubeconfig",
        str(KUBECONFIG),
        "--context",
        KUBE_CONTEXT,
        *args,
    ]


def helm(*args: str) -> list[str]:
    return [
        "helm",
        "--kubeconfig",
        str(KUBECONFIG),
        "--kube-context",
        KUBE_CONTEXT,
        *args,
    ]


#: The committed kind config's cluster name and host port per NodePort
#: (instance 0).
KIND_CONFIG_NAME = "opspilot-m1"
KIND_CONFIG_PORTS = {30080: 18080, 30090: 19090, 30686: 16686}


def check_instance() -> None:
    """Refuse an instance > 0 anywhere but the docker runtime on Linux: on
    macOS every lifecycle command would act on the one shared colima VM
    (``stop`` would stop instance 0's lab). Also refuse a telemetry URL
    override that points at another instance's host port, which would
    silently read instance 0's (or another lab's) Prometheus or Jaeger."""
    if INSTANCE == 0:
        return
    if sys.platform == "darwin" or lab_runtime() != "docker":
        raise SystemExit(
            f"{INSTANCE_ENV}>0 needs the docker runtime on a Linux host; "
            "refusing before touching the lab"
        )
    for variable, port in (
        ("OPSPILOT_OTEL_PROMETHEUS_URL", PROMETHEUS_PORT),
        ("OPSPILOT_OTEL_JAEGER_URL", JAEGER_PORT),
    ):
        configured = os.environ.get(variable)
        if configured and urllib.parse.urlsplit(configured).port != port:
            raise SystemExit(
                f"{variable} must use this instance's port {port} "
                f"({INSTANCE_ENV}={INSTANCE}); refusing"
            )


def kind_config_file() -> Path:
    """The committed kind config for instance 0; otherwise a copy under
    LAB_DIR with this instance's cluster name and host ports (refused under
    colima, whose one VM is sized for one lab)."""
    source = CONFIG / "kind-config.yaml"
    if INSTANCE == 0:
        return source
    check_instance()
    text = source.read_text()
    name = f"name: {KIND_CONFIG_NAME}\n"
    if text.count(name) != 1:
        raise SystemExit("kind-config.yaml: cluster name not found; refusing")
    text = text.replace(name, f"name: {CLUSTER}\n")
    ports = {30080: FRONTEND_PORT, 30090: PROMETHEUS_PORT, 30686: JAEGER_PORT}
    for container, committed in KIND_CONFIG_PORTS.items():
        line = f"hostPort: {committed}\n"
        if text.count(line) != 1:
            raise SystemExit(f"kind-config.yaml: hostPort {committed} not found")
        text = text.replace(line, f"hostPort: {ports[container]}\n")
    rendered = LAB_DIR / "rendered" / "kind-config.yaml"
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_text(text)
    return rendered


def kind_create_cmd() -> list[str]:
    return [
        "kind",
        "create",
        "cluster",
        "--config",
        str(kind_config_file()),
        "--kubeconfig",
        str(KUBECONFIG),
        "--wait",
        "120s",
    ]


def _private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as handle:
        handle.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# -- read-only cluster identity (A1, #553; decision table r3 A1-1 .. A1-7) ----

#: The identity's manifest, namespace, ServiceAccount and ClusterRole /
#: ClusterRoleBinding name (A1-1).
K8S_RBAC_MANIFEST = CONFIG / "opspilot-readonly-rbac.yaml"
K8S_IDENTITY_NAMESPACE = "opspilot-readonly"
K8S_SERVICE_ACCOUNT = "opspilot-investigator"
K8S_CLUSTER_ROLE = "opspilot-investigator-readonly"
#: ``kubectl create token --duration`` (A1-3); the API server may shorten it,
#: so the expiry recorded is the one the TokenRequest returns.
K8S_TOKEN_DURATION = "24h"
#: Files under LAB_DIR (A1-4), resolved at call time like LAB_DIR itself.
K8S_CA_NAME = "k8s-ca.crt"
K8S_ENV_NAME = "k8s-investigator.env"
K8S_TOKEN_RECORD_NAME = "k8s-token.json"
#: The API server's port inside the kind network (A1-6).
K8S_NODE_API_PORT = 6443
#: The CA as the worker container sees it: the ``k8s_ca`` secret of
#: ``deploy/compose/compose.lab.yaml``, from ``<secrets dir>/k8s_ca.crt``.
K8S_CONTROL_CA_NAME = "k8s_ca.crt"
K8S_CONTAINER_CA_FILE = "/run/secrets/k8s_ca"
K8S_API_URL_VAR = "OPSPILOT_K8S_API_URL"
K8S_TOKEN_VAR = "OPSPILOT_K8S_TOKEN"
K8S_CA_FILE_VAR = "OPSPILOT_K8S_CA_FILE"


def k8s_lab_files() -> dict[str, Path]:
    return {
        "ca": LAB_DIR / K8S_CA_NAME,
        "env": LAB_DIR / K8S_ENV_NAME,
        "record": LAB_DIR / K8S_TOKEN_RECORD_NAME,
    }


def _kubeconfig_server(path: Path) -> str:
    """The one ``server`` of the lab kubeconfig file (kind writes only this
    cluster there); JSON or kind's YAML, read without a YAML library."""
    text = path.read_text()
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict):
        servers = [
            str((c.get("cluster") or {}).get("server", ""))
            for c in document.get("clusters") or []
            if isinstance(c, dict)
        ]
    else:
        servers = [
            line.split(":", 1)[1].strip().strip("\"'")
            for line in text.splitlines()
            if line.strip().startswith("server:")
        ]
    if len(servers) != 1:
        raise SystemExit(f"{path}: not exactly one cluster server; refusing")
    return servers[0]


def k8s_api_url(
    form: str | None = None,
    server: str | None = None,
    *,
    container: bool | None = None,
) -> str:
    """The one place both API addresses come from (A1-6): ``host``
    (``container=False``) is the lab kubeconfig's loopback server, re-read
    from KUBECONFIG every time unless given (its port is random per
    cluster); ``container`` (``container=True``) the kind node container's
    in-network address for the control-side containers."""
    if container is not None:
        form = "container" if container else "host"
    if form == "container":
        return f"https://{NODE_CONTAINER}:{K8S_NODE_API_PORT}"
    if form != "host":
        raise ValueError(f"unknown API address form {form!r}")
    if server is None:
        server = _kubeconfig_server(KUBECONFIG)
    parts = urllib.parse.urlsplit(server)
    try:
        port = parts.port
    except ValueError:
        port = None
    if (
        parts.scheme != "https"
        or parts.hostname != "127.0.0.1"
        or port is None
        or parts.path not in ("", "/")
        or parts.query
        or parts.username
    ):
        raise SystemExit(
            "the lab kubeconfig's server is not https://127.0.0.1:<port>; refusing"
        )
    return f"https://127.0.0.1:{port}"


def lab_kubeconfig_cluster() -> tuple[str, bytes] | None:
    """``(server, CA PEM)`` of the lab context, from the lab kubeconfig's
    ``certificate-authority-data`` (A1-5); ``None`` (reason on stderr) if
    kubectl cannot read it."""
    proc = capture(kubectl("config", "view", "--raw", "--minify", "-o", "json"))
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return None
    try:
        (cluster,) = json.loads(proc.stdout)["clusters"]
        server = str(cluster["cluster"]["server"])
        ca = base64.b64decode(
            cluster["cluster"]["certificate-authority-data"], validate=True
        )
    except (KeyError, TypeError, ValueError):
        print(
            "lab kubeconfig: no single cluster with server and CA data", file=sys.stderr
        )
        return None
    if not ca:
        print("lab kubeconfig: empty certificate-authority-data", file=sys.stderr)
        return None
    return server, ca


def apply_k8s_identity() -> int:
    """Apply the read-only identity's Namespace, ServiceAccount, ClusterRole
    and ClusterRoleBinding (idempotent)."""
    return run(kubectl("apply", "-f", str(K8S_RBAC_MANIFEST))).returncode


def issue_k8s_token() -> int:
    """Issue a fresh token for the identity and write the host-form env file,
    the CA and the token record under LAB_DIR (mode 600). The token is never
    printed; the record holds its expiry only (A1-3, A1-4)."""
    cluster = lab_kubeconfig_cluster()
    if cluster is None:
        return 1
    server, ca = cluster
    host_url = k8s_api_url("host", server)
    proc = capture(
        kubectl(
            "-n",
            K8S_IDENTITY_NAMESPACE,
            "create",
            "token",
            K8S_SERVICE_ACCOUNT,
            "--duration",
            K8S_TOKEN_DURATION,
            "-o",
            "json",
        )
    )
    if proc.returncode != 0:
        # kubectl's error text, never stdout (which would hold the token)
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    parsed = _token_output(proc.stdout)
    if parsed is None:
        print("kubectl create token: unusable token in the output", file=sys.stderr)
        return 1
    token, expires_at, expiry_source = parsed
    try:
        ca_text = ca.decode("ascii")
    except UnicodeDecodeError:
        print("lab kubeconfig: certificate-authority-data is not text", file=sys.stderr)
        return 1
    files = k8s_lab_files()
    _private_write(files["ca"], ca_text)
    _private_write(
        files["env"],
        f"{K8S_API_URL_VAR}={host_url}\n"
        f"{K8S_TOKEN_VAR}={token}\n"
        f"{K8S_CA_FILE_VAR}={files['ca']}\n",
    )
    record = {
        "service_account": f"{K8S_IDENTITY_NAMESPACE}/{K8S_SERVICE_ACCOUNT}",
        "cluster_role": K8S_CLUSTER_ROLE,
        "requested_duration": K8S_TOKEN_DURATION,
        "issued_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at,
        "expires_at_source": expiry_source,
        "api_url": {"host": host_url, "container": k8s_api_url("container")},
        "rbac_manifest_sha256": hashlib.sha256(
            K8S_RBAC_MANIFEST.read_bytes()
        ).hexdigest(),
    }
    _private_write(files["record"], json.dumps(record, indent=2) + "\n")
    print(
        f"+ wrote {files['env']}, {files['ca']} and {files['record']} "
        f"(token expires {expires_at}; value not shown)"
    )
    return 0


def _token_output(stdout: str) -> tuple[str, str, str] | None:
    """``(token, expires_at, source)`` from ``kubectl create token``: the
    TokenRequest JSON ``-o json`` prints (expiry as the API server set it),
    or a bare token, whose expiry is then the JWT ``exp`` claim if it has
    one, else the requested duration from now."""
    text = stdout.strip()
    try:
        status = json.loads(text)["status"]
        token = str(status["token"])
        expires_at = str(status["expirationTimestamp"])
        source = "token_request"
    except (KeyError, TypeError, ValueError):
        token, expires_at, source = text, "", ""
    if not token or any(c.isspace() for c in token):
        return None
    if not expires_at:
        try:
            payload = token.split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            expiry = datetime.datetime.fromtimestamp(int(claims["exp"]), datetime.UTC)
            source = "jwt_exp"
        except (IndexError, KeyError, TypeError, ValueError):
            hours = int(K8S_TOKEN_DURATION.removesuffix("h"))
            expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                hours=hours
            )
            source = "requested_duration"
        expires_at = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    return token, expires_at, source


def cmd_k8s_token(args: argparse.Namespace) -> int:
    """Re-issue only the read-only identity's token on a running lab, without
    the rest of ``up``. Containers pick it up with ``control-secrets``."""
    return issue_k8s_token()


def _read_env_values(path: Path) -> dict[str, str]:
    """First active ``KEY=value`` per key; never executes the file."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), value.strip())
    return values


def _replace_env(path: Path, lines: Mapping[str, str]) -> str:
    """Set each ``KEY=value`` in ``path``: the first active line of a key is
    replaced, later active lines of it dropped, a missing key appended;
    every other line is kept. ``unchanged`` / ``replaced`` / ``written``."""
    current = path.read_text() if path.exists() else ""
    out: list[str] = []
    seen: set[str] = set()
    for raw in current.splitlines():
        stripped = raw.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if stripped and not stripped.startswith("#") and key in lines:
            if key not in seen:
                out.append(f"{key}={lines[key]}")
                seen.add(key)
            continue
        out.append(raw)
    out.extend(f"{k}={v}" for k, v in lines.items() if k not in seen)
    text = "\n".join(out) + "\n"
    if text == current:
        os.chmod(path, 0o600)
        return "unchanged"
    _private_write(path, text)
    return "replaced" if seen else "written"


COMPOSE_DIR = ROOT / "deploy" / "compose"
#: install.env keys the worker's Compose interpolation reads (install.sh
#: SETTINGS minus the agent switch).
INSTALL_SETTINGS = (
    "OPSPILOT_PROJECT",
    "OPSPILOT_WEB_HOST_PORT",
    "OPSPILOT_IMAGE",
    "OPSPILOT_TOOL_PROFILE",
    "OPSPILOT_OBSERVER_PROMETHEUS_URL",
    "OPSPILOT_TRACE",
)


def worker_has_k8s_ca(container: str) -> bool:
    """Whether a worker container mounts the CA secret (it does only when
    created with the lab override; ``install.sh`` creates it without)."""
    proc = capture(
        ["docker", "inspect", container, "-f", "{{json .Mounts}}"],
        timeout=30,
        env=docker_env(),
    )
    try:
        mounts = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
    except ValueError:
        mounts = []
    return isinstance(mounts, list) and any(
        isinstance(m, dict) and m.get("Destination") == K8S_CONTAINER_CA_FILE
        for m in mounts
    )


def restart_control_worker(project: str, secrets_dir: Path, *, changed: bool) -> str:
    """Recreate a running worker of ``project`` with the lab override (which
    mounts the CA) so its entrypoint exports the new ``OPSPILOT_K8S_*``, then
    rejoin it to the kind network (recreating drops the connection). Only
    when the values changed or the worker lacks the CA mount; ``not
    running`` when the project has no worker yet."""
    ids = control_containers(project)["worker"]
    if not ids:
        return "not running"
    if not changed and all(worker_has_k8s_ca(i) for i in ids):
        return "unchanged"
    env = docker_env()
    settings = secrets_dir / "install.env"
    if settings.exists():
        saved = _read_env_values(settings)
        env.update({k: saved[k] for k in INSTALL_SETTINGS if k in saved})
    env.update(
        OPSPILOT_SECRETS_DIR=str(secrets_dir),
        OPSPILOT_PROJECT=project,
        OPSPILOT_RUNTIME_UID=str(os.getuid()),
        OPSPILOT_RUNTIME_GID=str(os.getgid()),
        COMPOSE_PROFILES="agent",
    )
    proc = run(
        [
            "docker",
            "compose",
            "-p",
            project,
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
        ],
        env=env,
        timeout=300,
    )
    if proc.returncode != 0:
        return "restart failed"
    code = cmd_connect_control(
        argparse.Namespace(project=project, services=("worker",))
    )
    return "restarted" if code == 0 else "restarted, kind network join failed"


def control_k8s_wiring(secrets_dir: Path, project: str) -> dict[str, str] | None:
    """The container form of the identity in ``worker_env`` (A1-7): the CA
    as ``<secrets dir>/k8s_ca.crt``; the token, the in-network address and
    the CA's mount path replacing older values (a host-form address or CA
    path would not work in the container); then a running worker is
    restarted. ``None`` (nothing written) when the lab has issued no token
    yet; a lab CA file that is missing is reported, not invented."""
    files = k8s_lab_files()
    if not files["env"].exists():
        return None
    token = _read_env_values(files["env"]).get(K8S_TOKEN_VAR, "")
    if not token:
        raise SystemExit(f"{files['env']} holds no {K8S_TOKEN_VAR}; run k8s-token")
    ca_target = secrets_dir / K8S_CONTROL_CA_NAME
    if files["ca"].exists():
        ca = files["ca"].read_text()
        ca_state = "unchanged"
        if not ca_target.exists() or ca_target.read_text() != ca:
            ca_state = "written"
            _private_write(ca_target, ca)
        os.chmod(ca_target, 0o600)
    else:
        ca_state = "missing in the lab (run k8s-token)"
        print(
            f"{files['ca']} is missing: the worker's CA secret cannot be written; "
            "run `kind_lab.py k8s-token`",
            file=sys.stderr,
        )
    worker_env = secrets_dir / "worker_env"
    replaced = _replace_env(
        worker_env,
        {
            K8S_API_URL_VAR: k8s_api_url("container"),
            K8S_TOKEN_VAR: token,
            K8S_CA_FILE_VAR: K8S_CONTAINER_CA_FILE,
        },
    )
    changed = ca_state != "unchanged" or replaced != "unchanged"
    report = {K8S_CONTROL_CA_NAME: ca_state, "worker_env.k8s": replaced}
    report["worker"] = restart_control_worker(project, secrets_dir, changed=changed)
    return report


def prometheus_accounts(path: Path = AUTH_FILE) -> dict[str, str]:
    """``{account: password}`` from the private auth file, created on first use.

    Product read accounts authenticate only at the readonly proxy. Older labs
    gain only the missing proxy/holmes passwords; all other drift is refused.
    """
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SystemExit(f"{path} must not be group/world readable; refusing")
        accounts = json.loads(path.read_text())
        missing = set(PROMETHEUS_ACCOUNTS) - set(accounts)
        if (
            missing
            and missing <= ADDED_ACCOUNTS
            and set(accounts) < set(PROMETHEUS_ACCOUNTS)
        ):
            for name in sorted(missing):
                accounts[name] = secrets.token_urlsafe(24)
            _private_write(path, json.dumps(accounts, indent=2) + "\n")
        if set(accounts) != set(PROMETHEUS_ACCOUNTS):
            raise SystemExit(f"{path} does not list exactly {PROMETHEUS_ACCOUNTS}")
        return {name: str(accounts[name]) for name in PROMETHEUS_ACCOUNTS}
    accounts = {name: secrets.token_urlsafe(24) for name in PROMETHEUS_ACCOUNTS}
    _private_write(path, json.dumps(accounts, indent=2) + "\n")
    return accounts


HTPASSWD = ["htpasswd", "-niBC", "10", "x"]


def bcrypt_hash(password: str) -> str:
    """bcrypt via Apache ``htpasswd`` (ships with macOS); Prometheus checks
    the same ``$2y$`` hashes. The password goes in on stdin (``-i``), never
    on argv, so neither ``ps`` nor a ``TimeoutExpired``/``CalledProcessError``
    message (which quote the command line) can carry it (PR #119 recheck)."""
    try:
        proc = subprocess.run(
            HTPASSWD,
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # the exception text quotes argv only; re-raise without the object
        # so no traceback can reach the stdin payload either
        raise SystemExit(f"htpasswd failed: {type(exc).__name__}") from None
    if proc.returncode != 0:
        raise SystemExit(
            "htpasswd -B failed; bcrypt is required for Prometheus basic auth"
        )
    return proc.stdout.strip().split(":", 1)[1]


def web_config_yaml(
    accounts: dict[str, str], hasher: Callable[[str], str] = bcrypt_hash
) -> str:
    """Prometheus ``--web.config.file`` content: upstream-only accounts, bcrypt-hashed."""
    lines = ["basic_auth_users:"]
    for name in ("collector", "proxy"):
        lines.append(f'  {name}: "{hasher(accounts[name])}"')
    return "\n".join(lines) + "\n"


PROXY_SECRET = "prometheus-readonly-proxy"
READ_PATHS = (
    "/api/v1/query",
    "/api/v1/query_range",
    "/api/v1/series",
    "/api/v1/labels",
    "/api/v1/status/buildinfo",
)


def proxy_htpasswd(
    accounts: dict[str, str], hasher: Callable[[str], str] = bcrypt_hash
) -> str:
    return "".join(f"{name}:{hasher(accounts[name])}\n" for name in ROLE_ENV_VARS)


def proxy_nginx_config(accounts: dict[str, str]) -> str:
    # try_files runs AFTER basic authentication. A rewrite-phase return at
    # the public location would incorrectly give anonymous callers 403.
    #
    # #396 (decision Q6): upstream HolmesGPT sends query/query_range as POST
    # form bodies. Only the authenticated ``holmes`` account, only those two
    # paths, only a form body (<= 64k, else 413) jumps from @read to
    # @holmes_form, the one location that forwards a body. The 64k limit lives
    # only there; other accounts keep nginx's default 1m limit as before.
    # Every other request keeps the GET/HEAD-only, body-dropping @read rule.
    # ``$remote_user`` is read after
    # auth_basic accepted the password, so a wrong password never gets here.
    # Only holmes requests are logged, without headers, args or bodies.
    locations = "\n".join(
        f"    location = {path} {{ try_files /__absent__ @read; }}"
        for path in READ_PATHS
    )
    return (
        """worker_processes 1;
error_log /dev/stderr crit;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
  access_log off;
  map "$remote_user:$request_method:$uri:$content_type" $opspilot_holmes_form {
    default 0;
    "~^holmes:POST:/api/v1/query(_range)?:application/x-www-form-urlencoded(;.*)?$" 1;
  }
  map $content_length $opspilot_holmes_form_too_large {
    default 0;
    "~^0*[1-9][0-9]{5,}$" 1;
    "~^0*(6553[7-9]|655[4-9][0-9]|65[6-9][0-9][0-9]|6[6-9][0-9]{3}|[7-9][0-9]{4})$" 1;
  }
  map $remote_user $opspilot_holmes_log {
    default 0;
    holmes 1;
  }
  log_format opspilot_holmes '$time_iso8601 $remote_user $request_method $uri '
                             '$status $request_length $body_bytes_sent $request_time';
  upstream prometheus_backend { server prometheus:9090; }
  server {
    listen 8080;
    root /var/empty;
    access_log /dev/stdout opspilot_holmes if=$opspilot_holmes_log;
    auth_basic "lab readonly";
    auth_basic_user_file /etc/opspilot-proxy/htpasswd;
"""
        + locations
        + """
    location ~ ^/api/v1/label/[A-Za-z_][A-Za-z0-9_]*/values$ {
      try_files /__absent__ @read;
    }
    location / { try_files /__absent__ @denied; }
    location @denied { return 403; }
    location @read {
      error_page 418 = @holmes_form;
      if ($opspilot_holmes_form) { return 418; }
      if ($request_method !~ ^(GET|HEAD)$) { return 403; }
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Authorization "AUTHORIZATION";
      proxy_pass http://prometheus_backend$uri$is_args$args;
    }
    location @holmes_form {
      # a named location skips nginx's Content-Length check against
      # client_max_body_size (only chunked bodies are checked here), so the
      # 64k form limit is enforced on Content-Length explicitly
      client_max_body_size 64k;
      if ($opspilot_holmes_form_too_large) { return 413; }
      proxy_pass_request_body on;
      proxy_set_header Authorization "AUTHORIZATION";
      proxy_pass http://prometheus_backend$uri$is_args$args;
    }
  }
}
""".replace("AUTHORIZATION", basic_auth_header(accounts, "proxy"))
    )


def apply_readonly_proxy(accounts: dict[str, str]) -> int:
    with tempfile.TemporaryDirectory(prefix="prom-proxy-") as scratch:
        config = Path(scratch) / "nginx.conf"
        auth = Path(scratch) / "htpasswd"
        _private_write(config, proxy_nginx_config(accounts))
        _private_write(auth, proxy_htpasswd(accounts))
        code = _apply_secret(
            PROXY_SECRET,
            f"--from-file=nginx.conf={config}",
            f"--from-file=htpasswd={auth}",
        )
    if code:
        return code
    code = run(
        kubectl("apply", "-f", str(CONFIG / "prometheus-readonly-proxy.yaml"))
    ).returncode
    if code:
        return code
    code = roll_readonly_proxy_on_config_change()
    if code:
        return code
    return run(
        kubectl(
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            "deployment/prometheus-readonly-proxy",
            "--timeout=180s",
        )
    ).returncode


def roll_readonly_proxy_on_config_change() -> int:
    proc = capture(
        kubectl("-n", NAMESPACE, "get", "secret", PROXY_SECRET, "-o", "json")
    )
    if proc.returncode:
        print("cannot read proxy configuration Secret", file=sys.stderr)
        return proc.returncode
    try:
        digest = config_sha256(json.loads(proc.stdout))
    except ValueError:
        print("invalid proxy configuration Secret", file=sys.stderr)
        return 1
    patch = {
        "spec": {
            "template": {"metadata": {"annotations": {CONFIG_HASH_ANNOTATION: digest}}}
        }
    }
    return run(
        kubectl(
            "-n",
            NAMESPACE,
            "patch",
            "deployment",
            "prometheus-readonly-proxy",
            "-p",
            json.dumps(patch),
        )
    ).returncode


def webhook_token(path: Path | None = None) -> str:
    """A webhook bearer token (Alertmanager's by default, or the release
    deployer's) from its private file, created on first use (mode 600, like
    the Prometheus auth file)."""
    path = WEBHOOK_TOKEN_FILE if path is None else path
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SystemExit(f"{path} must not be group/world readable; refusing")
        token = path.read_text().strip()
        if not token:
            raise SystemExit(f"{path} is empty; refusing")
        return token
    token = secrets.token_urlsafe(32)
    _private_write(path, token + "\n")
    return token


def workbench_event_env(
    token: str,
    path: Path | None = None,
    *,
    actor: str = WEBHOOK_ACTOR,
    also: Mapping[str, str] | None = None,
) -> Path:
    """``OPSPILOT_EVENT_TOKENS`` entries (sha256 of the token = the dedicated
    ``actor``, ``alertmanager`` by default, then one per ``also`` actor ->
    token) for the workbench, sourced with ``set -a; . <file>``.
    It appends to an ``OPSPILOT_EVENT_TOKENS`` already in the environment
    instead of replacing it (the workbench parses ``,``-separated pairs into
    a map, so sourcing twice is harmless). Holds the hashes only."""
    path = WORKBENCH_EVENT_ENV if path is None else path
    entry = ",".join(
        f"{hashlib.sha256(value.encode()).hexdigest()}={name}"
        for name, value in ((actor, token), *(also or {}).items())
    )
    _private_write(
        path,
        "OPSPILOT_EVENT_TOKENS="
        f'"${{OPSPILOT_EVENT_TOKENS:+$OPSPILOT_EVENT_TOKENS,}}{entry}"\n',
    )
    return path


def role_env_file(role: str, accounts: dict[str, str], path: Path) -> Path:
    """One ``KEY=value`` file (mode 600) a product role sources or points its
    ``*_ENV_FILE`` at: only that role's variable names, only its account."""
    user_var, password_var = ROLE_ENV_VARS[role]
    _private_write(path, f"{user_var}={role}\n{password_var}={accounts[role]}\n")
    return path


def apply_prometheus_auth_secrets(accounts: dict[str, str]) -> int:
    """Create/update the two Secrets the charts mount; values go through a
    private temp file and ``kubectl --dry-run -o yaml | apply``, never argv."""
    proc = capture(
        kubectl("create", "namespace", NAMESPACE, "--dry-run=client", "-o", "yaml")
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    proc = subprocess.run(
        kubectl("apply", "-f", "-"), input=proc.stdout, text=True, capture_output=True
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    with tempfile.TemporaryDirectory(prefix="prom-auth-") as scratch:
        web = Path(scratch) / "web.yml"
        _private_write(web, web_config_yaml(accounts))
        push = Path(scratch) / "PROMETHEUS_COLLECTOR_PASSWORD"
        _private_write(push, accounts["collector"])
        for secret, source in (
            (WEB_CONFIG_SECRET, f"--from-file=web.yml={web}"),
            (
                COLLECTOR_AUTH_SECRET,
                f"--from-file=PROMETHEUS_COLLECTOR_PASSWORD={push}",
            ),
        ):
            code = _apply_secret(secret, source)
            if code != 0:
                return code
    return 0


def _apply_secret(secret: str, *sources: str) -> int:
    """``kubectl create secret generic --dry-run -o yaml | kubectl apply``;
    ``source`` is a ``--from-file`` argument naming a private file."""
    rendered = capture(
        kubectl(
            "-n",
            NAMESPACE,
            "create",
            "secret",
            "generic",
            secret,
            *sources,
            "--dry-run=client",
            "-o",
            "yaml",
        )
    )
    if rendered.returncode != 0:
        print("Secret rendering failed (diagnostics suppressed)", file=sys.stderr)
        return rendered.returncode
    applied = subprocess.run(
        kubectl("apply", "-f", "-"),
        input=rendered.stdout,
        text=True,
        capture_output=True,
    )
    if applied.returncode != 0:
        print("Secret apply failed (diagnostics suppressed)", file=sys.stderr)
        return applied.returncode
    print(f"+ kubectl apply secret/{secret} (values not shown)")
    return 0


CONFIG_HASH_ANNOTATION = "opspilot.lab/config-sha256"


def config_sha256(configmap: dict[str, object]) -> str:
    data = configmap.get("data") or {}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def roll_prometheus_on_config_change() -> int:
    """Stamp the Prometheus ConfigMap's hash on the Deployment's pod template.

    The chart's pod template carries no config checksum and the lab turns the
    configmap-reload sidecar off, so ``helm upgrade`` changes the ConfigMap
    without Prometheus loading it (seen 2026-10-10 adding the M1-04 alert
    rule). A changed hash rolls the pod; the same hash patches nothing.
    """
    proc = capture(
        kubectl("-n", NAMESPACE, "get", "configmap", "prometheus", "-o", "json")
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    try:
        configmap = json.loads(proc.stdout)
    except ValueError:
        print("configmap/prometheus: kubectl returned no JSON", file=sys.stderr)
        return 1
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {CONFIG_HASH_ANNOTATION: config_sha256(configmap)}
                }
            }
        }
    }
    return run(
        kubectl(
            "-n",
            NAMESPACE,
            "patch",
            "deployment",
            "prometheus",
            "-p",
            json.dumps(patch),
        )
    ).returncode


COLLECTOR_DEPLOYMENT = "otel-collector"


def roll_collector_on_auth_change() -> int:
    """Stamp the push-credential Secret's hash on the collector's pod template.

    The collector reads its Prometheus password from ``prometheus-web-auth``
    as an env var (``extraEnvsFrom`` in values.yaml), fixed at pod start.
    ``up`` re-applies that Secret from the auth file, and Prometheus picks up
    a new web config from its mounted Secret, but a running collector kept
    pushing with the old password until rolled by hand (#488). A changed
    Secret rolls the pod; the same Secret patches nothing.
    """
    proc = capture(
        kubectl("-n", NAMESPACE, "get", "secret", COLLECTOR_AUTH_SECRET, "-o", "json")
    )
    if proc.returncode:
        # stderr stays out: it is diagnostics about a Secret
        print("cannot read collector auth Secret", file=sys.stderr)
        return proc.returncode
    try:
        digest = config_sha256(json.loads(proc.stdout))
    except ValueError:
        print("invalid collector auth Secret", file=sys.stderr)
        return 1
    patch = {
        "spec": {
            "template": {"metadata": {"annotations": {CONFIG_HASH_ANNOTATION: digest}}}
        }
    }
    return run(
        kubectl(
            "-n",
            NAMESPACE,
            "patch",
            "deployment",
            COLLECTOR_DEPLOYMENT,
            "-p",
            json.dumps(patch),
        )
    ).returncode


def apply_webhook_secret(token: str) -> int:
    """The Secret Alertmanager reads its bearer token from
    (``credentials_file``, scripts/kind_lab/alertmanager-values.yaml)."""
    with tempfile.TemporaryDirectory(prefix="am-webhook-") as scratch:
        source = Path(scratch) / "token"
        _private_write(source, token)
        return _apply_secret(WEBHOOK_SECRET, f"--from-file=token={source}")


def basic_auth_header(accounts: dict[str, str], account: str) -> str:
    raw = f"{account}:{accounts[account]}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def lab_authorization(
    env: dict[str, str] | None = None, path: Path | None = None
) -> str | None:
    """The ``Authorization`` header engineer scripts send to the lab Prometheus.

    The ``lab`` account from ``OPSPILOT_LAB_PROMETHEUS_USERNAME/_PASSWORD``
    (``kind_lab.py env-file lab``), else from the private auth file when it
    exists; ``None`` when neither is set (an authenticated Prometheus then
    answers 401, which the scripts report instead of hiding).
    """
    env = dict(os.environ) if env is None else env
    # resolved at call time so a test (or a relocated LAB_DIR) can point it
    path = AUTH_FILE if path is None else path
    user_var, password_var = ROLE_ENV_VARS["lab"]
    username, password = env.get(user_var), env.get(password_var)
    if username and password:
        raw = f"{username}:{password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")
    if path.exists():
        return basic_auth_header(prometheus_accounts(path), "lab")
    return None


def colima_status() -> str:
    proc = capture(["colima", "list", "--json"])
    for line in proc.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("name") == PROFILE:
            return str(item.get("status", "unknown"))
    return "absent"


def _meminfo_kib() -> dict[str, int]:
    try:
        text = MEMINFO.read_text()
    except OSError:
        return {}
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key.strip()] = int(fields[0])
    return values


def host_swap_summary() -> str:
    """Host swap for run records: ``sysctl vm.swapusage`` on macOS, the
    ``/proc/meminfo`` swap lines elsewhere."""
    if sys.platform == "darwin":
        proc = capture(["sysctl", "-n", "vm.swapusage"])
        return proc.stdout.strip()
    info = _meminfo_kib()
    if "SwapTotal" not in info or "SwapFree" not in info:
        return ""
    total, free = info["SwapTotal"] // 1024, info["SwapFree"] // 1024
    return f"total = {total}M  used = {total - free}M  free = {free}M"


def host_reclaimable_gib() -> float | None:
    """Memory the host can hand to the lab without swapping.

    macOS: free + inactive + speculative + purgeable pages (``vm_stat``);
    Linux: ``MemAvailable`` from ``/proc/meminfo``.
    """
    if sys.platform != "darwin":
        available = _meminfo_kib().get("MemAvailable")
        return None if available is None else available / 2**20
    proc = capture(["vm_stat"])
    if proc.returncode != 0:
        return None
    pages = 0
    page_size = 16384
    for line in proc.stdout.splitlines():
        if line.startswith("Mach Virtual Memory Statistics"):
            digits = "".join(ch for ch in line if ch.isdigit())
            page_size = int(digits) if digits else page_size
            continue
        key, _, value = line.partition(":")
        if key.strip() in (
            "Pages free",
            "Pages inactive",
            "Pages speculative",
            "Pages purgeable",
        ):
            pages += int(value.strip().rstrip("."))
    return pages * page_size / 2**30


def get_json(
    url: str, timeout: float = 10, *, authorization: str | None = None
) -> tuple[int, object]:
    headers = {} if authorization is None else {"Authorization": authorization}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, None


def prom_instant(expr: str) -> tuple[int, list[object]]:
    query = urllib.parse.urlencode({"query": expr, "time": int(time.time())})
    # the lab account; without it the request is anonymous and an
    # authenticated Prometheus answers 401 (health then reports it)
    status, payload = get_json(
        f"{PROMETHEUS}/api/v1/query?{query}", authorization=lab_authorization()
    )
    if (
        status == 200
        and isinstance(payload, dict)
        and payload.get("status") == "success"
    ):
        return status, list(payload["data"]["result"])
    return status, []


def deployments_not_ready() -> list[str] | None:
    """Deployments and StatefulSets (Alertmanager) below their spec replicas."""
    proc = capture(
        kubectl("-n", NAMESPACE, "get", "deployments,statefulsets", "-o", "json")
    )
    if proc.returncode != 0:
        return None
    pending = []
    for item in json.loads(proc.stdout).get("items", []):
        spec = item.get("spec", {}).get("replicas", 1)
        ready = item.get("status", {}).get("readyReplicas", 0) or 0
        if ready < spec:
            pending.append(item["metadata"]["name"])
    return sorted(pending)


def frontend_status() -> int:
    try:
        with OPENER.open(FRONTEND, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def health(quiet: bool = False) -> dict[str, object]:
    """What the product profile reads, plus the Kubernetes state signals.

    Every component reported carries its own ``ok`` and the top-level ``ok``
    is their conjunction (Codex review on PR #111: the frontend status used
    to be reported but not counted).
    """
    prom_status, span_series = prom_instant("count(traces_span_metrics_calls_total)")
    prom_ok = bool(span_series)
    _, ksm_series = prom_instant(
        f'count(kube_deployment_status_replicas_available{{namespace="{NAMESPACE}"}})'
    )
    ksm_ok = bool(ksm_series)
    jaeger_status, jaeger = get_json(f"{JAEGER}/api/services")
    services = sorted(jaeger.get("data") or []) if isinstance(jaeger, dict) else []
    jaeger_ok = jaeger_status == 200 and "checkout" in services
    frontend = frontend_status()
    frontend_ok = frontend == 200
    pending = deployments_not_ready()
    deployments_ok = pending == []
    report = {
        "prometheus": {
            "url": PROMETHEUS,
            "status": prom_status,
            "span_metrics": prom_ok,
            "kube_state_metrics": ksm_ok,
            "ok": prom_ok and ksm_ok,
        },
        "jaeger": {
            "url": JAEGER,
            "status": jaeger_status,
            "ok": jaeger_ok,
            "services": services,
        },
        "frontend": {"url": FRONTEND, "status": frontend, "ok": frontend_ok},
        "deployments": {"not_ready": pending, "ok": deployments_ok},
        "ok": prom_ok and ksm_ok and jaeger_ok and frontend_ok and deployments_ok,
    }
    if not quiet:
        print(json.dumps(report, indent=2))
    return report


def cmd_up(args: argparse.Namespace) -> int:
    runtime = lab_runtime()
    reclaimable = host_reclaimable_gib()
    print(json.dumps({"runtime": runtime, "host_reclaimable_gib": reclaimable}))
    if runtime == "docker":
        # No VM: an existing cluster plays the part of a running VM (its
        # memory is already in use, so the check is skipped like colima's).
        clusters = capture(["kind", "get", "clusters"], timeout=60, env=docker_env())
        if clusters.returncode != 0:
            print(clusters.stderr, file=sys.stderr)
            return clusters.returncode
        running = CLUSTER in clusters.stdout.split()
    else:
        running = colima_status() == "Running"
    if (
        not args.skip_memory_check
        and not running
        and reclaimable is not None
        and reclaimable < MIN_HOST_RECLAIMABLE_GIB
    ):
        print(
            f"refusing to start: {reclaimable:.1f} GiB reclaimable on the host, "
            f"need {MIN_HOST_RECLAIMABLE_GIB} GiB (D4); free memory or pass "
            "--skip-memory-check",
            file=sys.stderr,
        )
        return 3
    if runtime == "colima" and colima_status() != "Running":
        proc = run(
            [
                "colima",
                "start",
                PROFILE,
                "--cpu",
                COLIMA_CPU,
                "--memory",
                COLIMA_MEMORY_GIB,
                "--disk",
                COLIMA_DISK_GIB,
                "--activate=false",
                "--ssh-agent=false",
                "--ssh-config=false",
            ]
        )
        if proc.returncode != 0:
            return proc.returncode
    env = docker_env()
    clusters = capture(["kind", "get", "clusters"], timeout=60, env=env)
    if clusters.returncode != 0:
        print(clusters.stderr, file=sys.stderr)
        return clusters.returncode
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    if CLUSTER not in clusters.stdout.split():
        proc = run(kind_create_cmd(), env=env)
        if proc.returncode != 0:
            return proc.returncode
    else:
        if runtime == "docker":
            # ``stop`` on Linux stops the node container; start it again
            # (a no-op when it is running).
            proc = run(["docker", "start", NODE_CONTAINER], env=env, timeout=120)
            if proc.returncode != 0:
                return proc.returncode
        # Refresh the lab kubeconfig (the cluster may predate it, or the
        # VM may have been restarted); never the user's ~/.kube/config.
        proc = run(
            [
                "kind",
                "export",
                "kubeconfig",
                "--name",
                CLUSTER,
                "--kubeconfig",
                str(KUBECONFIG),
            ],
            env=env,
            timeout=60,
        )
        if proc.returncode != 0:
            return proc.returncode
    # After ``colima start`` the API server answers before its RBAC is loaded
    # (``nodes is forbidden`` for kubernetes-admin for a few seconds, seen on
    # the 2026-10-06 stop/start cycle), so the node wait is retried.
    deadline = time.monotonic() + 180
    while True:
        proc = run(
            kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s")
        )
        if proc.returncode == 0 or time.monotonic() >= deadline:
            break
        time.sleep(5)
    if proc.returncode != 0:
        return proc.returncode
    # Prometheus basic auth: accounts, Secrets and the per-role env files
    # before the charts (the Prometheus and collector pods mount the Secrets).
    accounts = prometheus_accounts()
    code = apply_prometheus_auth_secrets(accounts)
    if code != 0:
        return code
    for role in ROLE_ENV_VARS:
        role_env_file(role, accounts, LAB_DIR / f"prometheus-{role}.env")
    print(f"+ wrote {LAB_DIR}/prometheus-<role>.env for {sorted(ROLE_ENV_VARS)}")
    token = webhook_token()
    code = apply_webhook_secret(token)
    if code != 0:
        return code
    # One file for every webhook actor: the documented start-up sources only
    # this one, so a second file would leave the release harness at 401.
    event_env = workbench_event_env(
        token, also={RELEASE_ACTOR: webhook_token(RELEASE_TOKEN_FILE)}
    )
    print(f"+ wrote {event_env} ({WEBHOOK_ACTOR}, {RELEASE_ACTOR}; token hashes only)")
    # Helm repo config and chart cache stay under LAB_DIR (``lab_env``).
    for repo, url in (
        ("open-telemetry", DEMO_REPO),
        ("prometheus-community", KSM_REPO),
    ):
        proc = run(
            ["helm", "repo", "add", "--force-update", repo, url], timeout=120, env=env
        )
        if proc.returncode != 0:
            return proc.returncode
    proc = run(
        ["helm", "repo", "update", "open-telemetry", "prometheus-community"], env=env
    )
    if proc.returncode != 0:
        # GitHub Pages answered EOF three times on 2026-10-07 while the chart
        # versions are pinned and both repository indexes are already cached
        # under LAB_DIR; a refresh failure then changes nothing and must not
        # block restoring a lab that is already installed.
        cache = LAB_DIR / "helm/cache/repository"
        cached = all(
            (cache / f"{repo}-index.yaml").exists()
            for repo in ("open-telemetry", "prometheus-community")
        )
        if not cached:
            return proc.returncode
        print("helm repo update failed; using the cached indexes (pinned versions)")
    endpoints = lab_endpoints()
    print(json.dumps({"lab_endpoints": endpoints or COLIMA_ENDPOINTS}))
    proc = run(
        helm(
            "upgrade",
            "--install",
            RELEASE,
            "open-telemetry/opentelemetry-demo",
            "--version",
            DEMO_CHART_VERSION,
            "--namespace",
            NAMESPACE,
            "--create-namespace",
            "--values",
            str(lab_values_file("values.yaml", endpoints)),
            # The fault hook's ``kubectl patch`` (and a 2026-10-10 hand patch
            # of the rule file) own ConfigMap fields under server-side apply,
            # so a changed value there conflicts (seen 2026-10-31 adding the
            # #414 rules). These values are the lab's source of truth: take
            # the fields back. ``up`` with a fault injected restores flagd.
            "--force-conflicts",
            "--timeout",
            "15m",
        ),
        env=env,
    )
    if proc.returncode != 0:
        return proc.returncode
    code = apply_readonly_proxy(accounts)
    if code != 0:
        return code
    code = roll_prometheus_on_config_change()
    if code != 0:
        return code
    code = roll_collector_on_auth_change()
    if code != 0:
        return code
    proc = run(
        helm(
            "upgrade",
            "--install",
            KSM_RELEASE,
            "prometheus-community/kube-state-metrics",
            "--version",
            KSM_CHART_VERSION,
            "--namespace",
            NAMESPACE,
            "--values",
            str(CONFIG / "kube-state-metrics-values.yaml"),
            "--timeout",
            "5m",
        ),
        env=env,
    )
    if proc.returncode != 0:
        return proc.returncode
    proc = run(
        helm(
            "upgrade",
            "--install",
            AM_RELEASE,
            "prometheus-community/alertmanager",
            "--version",
            AM_CHART_VERSION,
            "--namespace",
            NAMESPACE,
            "--values",
            str(lab_values_file("alertmanager-values.yaml", endpoints)),
            "--timeout",
            "5m",
        ),
        env=env,
    )
    if proc.returncode != 0:
        return proc.returncode
    proc = run(kubectl("apply", "-f", str(CONFIG / "jaeger-nodeport.yaml")))
    if proc.returncode != 0:
        return proc.returncode
    # The read-only cluster identity and a fresh token (A1-1, A1-3): every
    # ``up`` re-issues it, before the waits so a slow demo still gets one.
    code = apply_k8s_identity()
    if code != 0:
        return code
    code = issue_k8s_token()
    if code != 0:
        return code
    proc = run(
        kubectl(
            "-n",
            NAMESPACE,
            "wait",
            "--for=condition=Available",
            "deployment",
            "--all",
            f"--timeout={int(args.wait)}s",
        )
    )
    if proc.returncode != 0:
        print(json.dumps(health(quiet=True), indent=2))
        return proc.returncode
    deadline = time.monotonic() + args.wait
    while True:
        report = health(quiet=True)
        if report["ok"] or time.monotonic() >= deadline:
            break
        time.sleep(10)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def cmd_health(args: argparse.Namespace) -> int:
    return 0 if health()["ok"] else 1


def cmd_env_file(args: argparse.Namespace) -> int:
    """(Re)write one role's env file from the private auth file; prints the
    path only, never the values."""
    accounts = prometheus_accounts()
    out = Path(args.out) if args.out else LAB_DIR / f"prometheus-{args.role}.env"
    print(role_env_file(args.role, accounts, out))
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    """Re-render the readonly proxy (accounts, per-role env files, Secret,
    rollout) on a running lab without the chart upgrades of ``up`` (#396)."""
    try:
        listed = set(json.loads(AUTH_FILE.read_text()))
    except (OSError, ValueError):
        listed = set()
    accounts = prometheus_accounts(AUTH_FILE)
    if "proxy" not in listed:
        # the proxy password was just created: Prometheus' web config must
        # list it too, or every proxied request gets 401
        code = apply_prometheus_auth_secrets(accounts)
        if code != 0:
            return code
    for role in ROLE_ENV_VARS:
        role_env_file(role, accounts, LAB_DIR / f"prometheus-{role}.env")
    print(f"+ wrote {LAB_DIR}/prometheus-<role>.env for {sorted(ROLE_ENV_VARS)}")
    return apply_readonly_proxy(accounts)


#: What ``rules-check`` validates (M1-04 R11, F8-R6): each chart-rendered
#: file, the release that renders it, and the tool that checks it inside
#: the lab pod running that version (promtool v3.5.0, amtool v0.28.1); the
#: host has neither binary.
PROMETHEUS_CM = "charts/prometheus/templates/cm.yaml"
ALERTMANAGER_CM = "templates/configmap.yaml"
SELF_ALERT_LABEL = 'opspilot_self="true"'
SELF_RECEIVER = "opspilot-self"


def rendered_configmap_data(manifest: str) -> dict[str, str] | None:
    """The ``data`` of the one ConfigMap ``helm template --show-only`` printed,
    as JSON from ``kubectl create --dry-run=client`` (no YAML parser here)."""
    proc = subprocess.run(
        kubectl("create", "--dry-run=client", "-o", "json", "-f", "-"),
        input=manifest,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout).get("data")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _render(release: str, chart: str, version: str, values: str, show: str) -> str:
    proc = capture(
        helm(
            "template",
            release,
            chart,
            "--version",
            version,
            "--namespace",
            NAMESPACE,
            "--values",
            str(lab_values_file(values, lab_endpoints())),
            "--show-only",
            show,
        ),
        env=lab_env(),
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return ""
    return proc.stdout


def _pod_check(target: list[str], script: str, content: str) -> tuple[int, str]:
    """Run ``script`` in a lab pod with ``content`` as /tmp/check.yml (stdin);
    prints and returns the exit code and output."""
    wrapped = (
        f"cat > /tmp/check.yml && {{ {script}; }}; rc=$?; rm -f /tmp/check.yml;"
        " exit $rc"
    )
    proc = subprocess.run(
        kubectl("-n", NAMESPACE, "exec", "-i", *target, "--", "sh", "-c", wrapped),
        input=content,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = (proc.stdout + proc.stderr).rstrip()
    print(f"+ {' '.join(target)}: {script}\n{output}")
    return proc.returncode, output


def cmd_rules_check(args: argparse.Namespace) -> int:
    """promtool/amtool on the rule, scrape and route files the committed
    values render to, before ``up`` applies them: ``promtool check rules``
    and ``check config --syntax-only`` in the Prometheus pod, ``amtool
    check-config`` in the Alertmanager pod, and the route a self-alert takes
    (``opspilot_self="true"`` must reach ``opspilot-self`` only, F8-Q3)."""
    prom = rendered_configmap_data(
        _render(
            RELEASE,
            "open-telemetry/opentelemetry-demo",
            DEMO_CHART_VERSION,
            "values.yaml",
            PROMETHEUS_CM,
        )
    )
    am = rendered_configmap_data(
        _render(
            AM_RELEASE,
            "prometheus-community/alertmanager",
            AM_CHART_VERSION,
            "alertmanager-values.yaml",
            ALERTMANAGER_CM,
        )
    )
    if not prom or not am:
        print("rendering the chart ConfigMaps failed", file=sys.stderr)
        return 1
    server = ["deploy/prometheus", "-c", "prometheus-server"]
    alertmanager = [f"{AM_RELEASE}-0"]
    rules, _ = _pod_check(
        server, "promtool check rules /tmp/check.yml", prom["alerting_rules.yml"]
    )
    config, _ = _pod_check(
        server,
        "promtool check config --syntax-only /tmp/check.yml",
        prom["prometheus.yml"],
    )
    am_config, _ = _pod_check(
        alertmanager, "amtool check-config /tmp/check.yml", am["alertmanager.yml"]
    )
    route, receivers = _pod_check(
        alertmanager,
        "amtool config routes test --config.file=/tmp/check.yml"
        f" '{SELF_ALERT_LABEL}' alertname=OpsPilotAgentAbsent",
        am["alertmanager.yml"],
    )
    results = {
        "rules": rules,
        "config": config,
        "alertmanager": am_config,
        "self_route": 0 if route == 0 and receivers.split() == [SELF_RECEIVER] else 1,
    }
    ok = not any(results.values())
    print(json.dumps({"ok": ok, "exit": results}))
    return 0 if ok else 1


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the VM only: the kind cluster and both releases stay installed.

    Linux (``docker`` runtime): stop the kind node container instead; ``up``
    starts it again.
    """
    if lab_runtime() == "docker":
        return run(["docker", "stop", NODE_CONTAINER], env=docker_env()).returncode
    if colima_status() != "Running":
        print(json.dumps({"colima": colima_status()}))
        return 0
    return run(["colima", "stop", PROFILE]).returncode


def mutate_flags(raw: bytes, variant: str, expected: str, *, flag: str = FLAG) -> bytes:
    """``demo.flagd.json`` with ``flag`` moved from ``expected`` to ``variant``.

    Refuses when the flag is not in the expected state so a repeat injection
    or a restore of an untouched file is never silently accepted, and refuses
    a flag outside ``FAULT_FLAGS`` or one the live file does not define.
    """
    if flag not in FAULT_FLAGS:
        raise SystemExit(f"{flag} is not an allowed fault flag; refusing")
    data = json.loads(raw)
    entry = data["flags"].get(flag)
    if not isinstance(entry, dict):
        raise SystemExit(f"{flag} is not a flag of {FLAG_FILE}; refusing")
    if entry["defaultVariant"] != expected:
        raise SystemExit(
            f"{flag} is {entry['defaultVariant']!r}, expected {expected!r}; refusing"
        )
    if variant not in entry["variants"]:
        raise SystemExit(f"{variant!r} is not a variant of {flag}")
    entry["defaultVariant"] = variant
    return (json.dumps(data, indent=2) + "\n").encode()


def read_live_flags() -> bytes | None:
    proc = capture(
        kubectl("-n", NAMESPACE, "get", "configmap", "flagd-config", "-o", "json")
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return None
    return str(json.loads(proc.stdout)["data"][FLAG_FILE]).encode()


def cmd_fault(args: argparse.Namespace) -> int:
    if args.experiment_id is not None and (
        not args.experiment_id or not args.experiment_id.replace("-", "").isalnum()
    ):
        raise SystemExit("invalid experiment identity")
    history = history_dir(args.experiment_id)
    history.mkdir(parents=True, exist_ok=True)
    original = history / "flags-original.json"
    changed = history / "flags-injected.json"
    raw = read_live_flags()
    if raw is None:
        return 1
    if args.action == "inject":
        if original.exists() or changed.exists():
            raise SystemExit("Existing experiment history; refuse repeat injection.")
        flag = args.flag
        variant = FAULT_FLAGS[flag]
        payload = mutate_flags(raw, variant, NORMAL_VARIANT, flag=flag)
        # The history files are written only after the patch is verified
        # below, so a failed or unverified injection leaves no record that
        # would make a retry refuse (Codex review P2 on PR #111); the
        # fault-log.jsonl line still records the failed attempt.
    else:
        if not changed.exists():
            raise SystemExit(
                "No injection recorded for this experiment; nothing to restore."
            )
        if json.loads(raw) != json.loads(changed.read_bytes()):
            raise SystemExit(
                "Live flags differ from the recorded injection; refusing to overwrite."
            )
        variant = NORMAL_VARIANT
        payload = original.read_bytes()
        # The flag this experiment moved, read from its own snapshots.
        before = json.loads(payload)["flags"]
        after = json.loads(changed.read_bytes())["flags"]
        moved = sorted(
            name
            for name in after
            if name in before
            and after[name].get("defaultVariant") != before[name].get("defaultVariant")
        )
        flag = moved[0] if len(moved) == 1 else FLAG
    patch = json.dumps({"data": {FLAG_FILE: payload.decode()}})
    print(
        f"+ kubectl -n {NAMESPACE} patch configmap flagd-config ({flag} -> {variant})"
    )
    proc = capture(
        kubectl(
            "-n",
            NAMESPACE,
            "patch",
            "configmap",
            "flagd-config",
            "--type=merge",
            "-p",
            patch,
        ),
        timeout=60,
    )
    expected_sha = hashlib.sha256(payload).hexdigest()
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        live_sha = None
        verified = False
    else:
        # A successful patch is not proof: re-read the ConfigMap and compare
        # its SHA-256 with the bytes we meant to install (PR #111 review P1).
        live = read_live_flags()
        live_sha = None if live is None else hashlib.sha256(live).hexdigest()
        verified = live_sha == expected_sha
    if verified and args.action == "inject":
        original.write_bytes(raw)
        changed.write_bytes(payload)
    with (history / "fault-log.jsonl").open("a") as log:
        log.write(
            json.dumps(
                {
                    "at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "action": args.action,
                    "flag": flag,
                    "before_sha256": hashlib.sha256(raw).hexdigest(),
                    "after_sha256": expected_sha,
                    "live_sha256": live_sha,
                    "verified": verified,
                }
            )
            + "\n"
        )
    if not verified:
        print(
            f"{args.action} NOT verified: live flagd-config sha256 {live_sha} != "
            f"expected {expected_sha}; the ConfigMap was not changed as intended",
            file=sys.stderr,
        )
        return 2
    print(
        f"{args.action} completed and verified on the lab ConfigMap (sha256 "
        f"{expected_sha[:12]}); flagd reloads the mounted file after the kubelet "
        "sync (about a minute)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser(
        "up", help="start colima (macOS), create kind, install the charts"
    )
    up.add_argument(
        "--wait", type=float, default=900, help="seconds to wait for the deployments"
    )
    up.add_argument(
        "--skip-memory-check",
        action="store_true",
        help=f"start the VM even with < {MIN_HOST_RECLAIMABLE_GIB} GiB reclaimable",
    )
    up.set_defaults(func=cmd_up)
    sub.add_parser(
        "health", help="check Prometheus, kube-state-metrics, Jaeger"
    ).set_defaults(func=cmd_health)
    sub.add_parser(
        "proxy", help="re-render and roll only the readonly Prometheus proxy"
    ).set_defaults(func=cmd_proxy)
    sub.add_parser(
        "rules-check",
        help="promtool/amtool on the rendered rules, scrape config and routes",
    ).set_defaults(func=cmd_rules_check)
    sub.add_parser(
        "stop", help="stop the VM (Linux: the node); keep cluster and releases"
    ).set_defaults(func=cmd_stop)
    connect = sub.add_parser(
        "connect-control",
        help="join a Compose project's web/worker/observer to the kind network",
    )
    connect.add_argument("--project", required=True)
    connect.set_defaults(func=cmd_connect_control)
    control = sub.add_parser(
        "control-secrets",
        help="prepare install.sh's secrets for this lab on the same host (#531)",
    )
    control.add_argument(
        "--secrets-dir",
        help="default $OPSPILOT_SECRETS_DIR, else ${XDG_CONFIG_HOME:-~/.config}/opspilot",
    )
    control.add_argument(
        "--project",
        help="Compose project whose running worker gets the new cluster token "
        "(default: install.env's OPSPILOT_PROJECT, else opspilot)",
    )
    control.set_defaults(func=cmd_control_secrets)
    sub.add_parser(
        "k8s-token",
        help="re-issue only the read-only cluster identity's 24h token (A1)",
    ).set_defaults(func=cmd_k8s_token)
    env_file = sub.add_parser(
        "env-file", help="write a role's Prometheus credential env file (mode 600)"
    )
    env_file.add_argument("role", choices=sorted(ROLE_ENV_VARS))
    env_file.add_argument("--out")
    env_file.set_defaults(func=cmd_env_file)
    fault = sub.add_parser("fault", help="developer-only fault hook (flagd ConfigMap)")
    fault.add_argument("action", choices=["inject", "restore"])
    fault.add_argument("--experiment-id")
    fault.add_argument("--flag", choices=sorted(FAULT_FLAGS), default=FLAG)
    fault.set_defaults(func=cmd_fault)
    args = parser.parse_args(argv)
    # before any command touches colima, docker or kind (#479 review)
    check_instance()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
