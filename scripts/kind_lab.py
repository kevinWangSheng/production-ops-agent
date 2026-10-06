"""Engineer entry point for the M1-02 kind lab: OTel Demo 2.0.2 on Kubernetes.

Successor of ``scripts/otel_demo_lab.py`` (the M0 Compose lab, whose
directory no longer exists) for decision D2/D4 of
``docs/tasks/2026-10-03-m1-02-recovery-observation.md``: a kind cluster on its
own colima profile, the official OTel Demo Helm chart at a pinned version
trimmed to the checkout fault path (``scripts/kind_lab/values.yaml``), and
kube-state-metrics so Prometheus carries deployment and pod state.

``up`` checks host memory, starts the colima profile, creates the kind
cluster and installs or upgrades the two Helm releases, then waits for the
backends; ``health`` checks what the product profile and the future Observer
read (Prometheus span metrics, kube-state-metrics series, Jaeger lists
``checkout``, frontend answers); ``stop`` stops the colima VM and keeps the
cluster and releases installed (no ``kind delete``, no ``helm uninstall``);
``fault inject|restore`` patches the ``flagd-config`` ConfigMap
(``paymentFailure`` -> ``100%`` / back to ``off``), recording original and
changed bytes with SHA-256 under an engineer-only history directory and
refusing a second injection or a restore over an unexpected edit, like the
M0 ``development_fault.py`` hook.

This is an engineer script: it starts, stops and mutates the *lab*, which
PRODUCT-CONSTRAINTS reserves for engineers and isolated harnesses. Nothing
here is reachable from the product, the worker or the model; the product
profile (``opspilot.tools.otel_demo``) only ever reads Prometheus and Jaeger
over the host ports kind publishes (``scripts/kind_lab/kind-config.yaml``).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts/kind_lab"

PROFILE = "m1-kind"
CONTEXT = f"colima-{PROFILE}"
CLUSTER = "opspilot-m1"
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
COLIMA_CPU = "4"
COLIMA_MEMORY_GIB = "8"
COLIMA_DISK_GIB = "30"
# D4: 16 GB host shared with other agents. ``up`` refuses to start the VM when
# less than this is reclaimable (free + inactive + speculative + purgeable
# pages), unless ``--skip-memory-check`` is given.
MIN_HOST_RECLAIMABLE_GIB = 3.0

PROMETHEUS = os.environ.get("OPSPILOT_OTEL_PROMETHEUS_URL", "http://127.0.0.1:19090")
# Same variable and meaning as the product profile: the Jaeger UI base.
JAEGER = os.environ.get(
    "OPSPILOT_OTEL_JAEGER_URL", "http://127.0.0.1:16686/jaeger/ui"
).rstrip("/")
FRONTEND = "http://127.0.0.1:18080/"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FLAG = "paymentFailure"
FLAG_FILE = "demo.flagd.json"
NORMAL_VARIANT = "off"
FAULT_VARIANT = "100%"
# Everything this script writes on the host lives under this git-ignored,
# lab-owned directory: fault history, the lab kubeconfig and Helm's
# repository config/cache/data. Not configurable (PR #111 review P1: an
# environment override could point the history at another lab's data).
LAB_DIR = ROOT / "tmp/m1-kind-lab"
HISTORY_ROOT = LAB_DIR / "engineer-only"
KUBECONFIG = LAB_DIR / "kubeconfig"
KUBECTL = "kubectl"


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
    return {**env, "DOCKER_CONTEXT": CONTEXT}


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


def kind_create_cmd() -> list[str]:
    return [
        "kind",
        "create",
        "cluster",
        "--config",
        str(CONFIG / "kind-config.yaml"),
        "--kubeconfig",
        str(KUBECONFIG),
        "--wait",
        "120s",
    ]


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


def host_reclaimable_gib() -> float | None:
    """macOS: pages the kernel can hand to a new VM without swapping."""
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


def get_json(url: str, timeout: float = 10) -> tuple[int, object]:
    try:
        with OPENER.open(url, timeout=timeout) as response:
            return response.status, json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, None


def prom_instant(expr: str) -> tuple[int, list[object]]:
    query = urllib.parse.urlencode({"query": expr, "time": int(time.time())})
    status, payload = get_json(f"{PROMETHEUS}/api/v1/query?{query}")
    if (
        status == 200
        and isinstance(payload, dict)
        and payload.get("status") == "success"
    ):
        return status, list(payload["data"]["result"])
    return status, []


def deployments_not_ready() -> list[str] | None:
    proc = capture(kubectl("-n", NAMESPACE, "get", "deployments", "-o", "json"))
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
    reclaimable = host_reclaimable_gib()
    print(json.dumps({"host_reclaimable_gib": reclaimable}))
    if (
        not args.skip_memory_check
        and colima_status() != "Running"
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
    if colima_status() != "Running":
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
        return proc.returncode
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
            str(CONFIG / "values.yaml"),
            "--timeout",
            "15m",
        ),
        env=env,
    )
    if proc.returncode != 0:
        return proc.returncode
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
    proc = run(kubectl("apply", "-f", str(CONFIG / "jaeger-nodeport.yaml")))
    if proc.returncode != 0:
        return proc.returncode
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


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the VM only: the kind cluster and both releases stay installed."""
    if colima_status() != "Running":
        print(json.dumps({"colima": colima_status()}))
        return 0
    return run(["colima", "stop", PROFILE]).returncode


def mutate_flags(raw: bytes, variant: str, expected: str) -> bytes:
    """``demo.flagd.json`` with ``paymentFailure`` moved from ``expected`` to ``variant``.

    Refuses when the flag is not in the expected state so a repeat injection
    or a restore of an untouched file is never silently accepted.
    """
    data = json.loads(raw)
    flag = data["flags"][FLAG]
    if flag["defaultVariant"] != expected:
        raise SystemExit(
            f"{FLAG} is {flag['defaultVariant']!r}, expected {expected!r}; refusing"
        )
    if variant not in flag["variants"]:
        raise SystemExit(f"{variant!r} is not a variant of {FLAG}")
    flag["defaultVariant"] = variant
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
        variant = FAULT_VARIANT
        payload = mutate_flags(raw, variant, NORMAL_VARIANT)
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
    patch = json.dumps({"data": {FLAG_FILE: payload.decode()}})
    print(
        f"+ kubectl -n {NAMESPACE} patch configmap flagd-config ({FLAG} -> {variant})"
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
    up = sub.add_parser("up", help="start colima, create kind, install the charts")
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
    sub.add_parser("stop", help="stop the VM; keep cluster and releases").set_defaults(
        func=cmd_stop
    )
    fault = sub.add_parser("fault", help="developer-only fault hook (flagd ConfigMap)")
    fault.add_argument("action", choices=["inject", "restore"])
    fault.add_argument("--experiment-id")
    fault.set_defaults(func=cmd_fault)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
