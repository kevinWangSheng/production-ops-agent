"""Engineer entry point for the pinned OTel Demo 2.0.2 lab on colima/docker.

``up`` starts the dedicated colima profile and the pinned Compose project
that M0 froze (26 images by digest, ``compose-pinned.json``, ``collector.yml``
and ``prometheus.yaml`` under the lab directory); ``health`` checks that both
telemetry backends the product profile reads answer; ``stop`` stops the
containers and the VM without removing anything (no ``down``/``rm``/``prune``,
the Prometheus and OpenSearch volumes stay); ``fault inject|restore`` calls
the M0 developer fault hook (``scripts/m0_environment/development_fault.py``).

This is an engineer script: it starts, stops and mutates the *lab*, which
PRODUCT-CONSTRAINTS reserves for engineers and isolated harnesses. Nothing
here is reachable from the product, the worker or the model, and the product
profile (``opspilot.tools.otel_demo``) only ever reads the two backends.

The lab directory is the one the pinned Compose file was generated for: its
bind mounts are absolute paths under that directory, so it defaults to the
M0 environment worktree's ``tmp/m0-environment`` and may be pointed
elsewhere with ``OPSPILOT_OTEL_LAB``. ``prepare.py``/``freeze_images.py``
are how that directory is (re)built; this script does not rebuild it and
refuses to start without the pinned file.
"""

from __future__ import annotations

import argparse
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
DEFAULT_LAB = Path(
    "/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment"
)
PROFILE = "m0-otel"
CONTEXT = f"colima-{PROFILE}"
PROJECT = "opspilot-m0"
PROMETHEUS = os.environ.get("OPSPILOT_OTEL_PROMETHEUS_URL", "http://127.0.0.1:19090")
JAEGER = os.environ.get("OPSPILOT_OTEL_JAEGER_URL", "http://127.0.0.1:16686")
# The lab's frontend-proxy; only used to report that the workload answers.
FRONTEND = "http://127.0.0.1:18080/"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def lab_dir() -> Path:
    lab = Path(os.environ.get("OPSPILOT_OTEL_LAB") or DEFAULT_LAB)
    if not (lab / "compose-pinned.json").is_file():
        raise SystemExit(
            f"{lab / 'compose-pinned.json'} is missing: point OPSPILOT_OTEL_LAB at the "
            "lab directory M0 froze (see docs/evidence/m0-real-environment/reproduce.md)"
        )
    return lab


def run(cmd: list[str], *, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, timeout=timeout, check=False)


def colima_status() -> str:
    proc = subprocess.run(
        ["colima", "list", "--json"], capture_output=True, text=True, check=False
    )
    for line in proc.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("name") == PROFILE:
            return str(item.get("status", "unknown"))
    return "absent"


def get_json(url: str, timeout: float = 10) -> tuple[int, object]:
    try:
        with OPENER.open(url, timeout=timeout) as response:
            return response.status, json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, None


def health(quiet: bool = False) -> dict[str, object]:
    """Both backends the product profile reads must answer a read query."""
    # This Prometheus is fed by the collector (no scrape targets, so no
    # ``up``); the span-metrics counter is what the product profile reads.
    query = urllib.parse.urlencode(
        {"query": "count(traces_span_metrics_calls_total)", "time": int(time.time())}
    )
    prom_status, prom = get_json(f"{PROMETHEUS}/api/v1/query?{query}")
    prom_ok = (
        prom_status == 200
        and isinstance(prom, dict)
        and prom.get("status") == "success"
        and bool(prom["data"]["result"])
    )
    jaeger_status, jaeger = get_json(f"{JAEGER}/jaeger/ui/api/services")
    services = sorted(jaeger.get("data") or []) if isinstance(jaeger, dict) else []
    jaeger_ok = jaeger_status == 200 and "checkout" in services
    try:
        with OPENER.open(FRONTEND, timeout=10) as response:
            frontend = response.status
    except (urllib.error.URLError, TimeoutError, OSError):
        frontend = 0
    report = {
        "prometheus": {"url": PROMETHEUS, "status": prom_status, "ok": prom_ok},
        "jaeger": {
            "url": JAEGER,
            "status": jaeger_status,
            "ok": jaeger_ok,
            "services": services,
        },
        "frontend": {"url": FRONTEND, "status": frontend},
        "ok": prom_ok and jaeger_ok,
    }
    if not quiet:
        print(json.dumps(report, indent=2))
    return report


def cmd_up(args: argparse.Namespace) -> int:
    lab = lab_dir()
    status = colima_status()
    if status != "Running":
        proc = run(
            [
                "colima",
                "start",
                PROFILE,
                "--cpu",
                "4",
                "--memory",
                "6",
                "--disk",
                "24",
                "--activate=false",
                "--ssh-agent=false",
                "--ssh-config=false",
                "--mount",
                f"{lab}:w",
            ]
        )
        if proc.returncode != 0:
            return proc.returncode
    proc = run(
        [
            "docker",
            "--context",
            CONTEXT,
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(lab / "compose-pinned.json"),
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
        ]
    )
    if proc.returncode != 0:
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
    lab = lab_dir()
    proc = run(
        [
            "docker",
            "--context",
            CONTEXT,
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(lab / "compose-pinned.json"),
            "stop",
        ]
    )
    if proc.returncode != 0:
        return proc.returncode
    return run(["colima", "stop", PROFILE]).returncode


def cmd_fault(args: argparse.Namespace) -> int:
    lab = lab_dir()
    cmd = [
        sys.executable,
        str(ROOT / "scripts/m0_environment/development_fault.py"),
        args.action,
    ]
    if args.experiment_id:
        cmd += ["--experiment-id", args.experiment_id]
    env = {**os.environ, "OPSPILOT_OTEL_LAB": str(lab)}
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start the colima profile and the pinned demo")
    up.add_argument(
        "--wait", type=float, default=600, help="seconds to wait for both backends"
    )
    up.set_defaults(func=cmd_up)
    sub.add_parser("health", help="check Prometheus and Jaeger answer").set_defaults(
        func=cmd_health
    )
    sub.add_parser("stop", help="stop containers and the VM; keep data").set_defaults(
        func=cmd_stop
    )
    fault = sub.add_parser("fault", help="developer-only fault hook (M0)")
    fault.add_argument("action", choices=["inject", "restore"])
    fault.add_argument("--experiment-id")
    fault.set_defaults(func=cmd_fault)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
