"""Engineer CLI for the F14 step 1 baseline (#396). Run inside the exclusive
kind-lab window (``lockf ~/.agent/locks/kind-lab ...``).

  python -m scripts.holmes_baseline.run toolsets        # builtin names in the image
  python -m scripts.holmes_baseline.run pin             # write pin.json
  python -m scripts.holmes_baseline.run check-request   # thinking/effort, offline
  python -m scripts.holmes_baseline.run live            # one live Run
  python -m scripts.holmes_baseline.run upstream-files  # F14 step 2, offline
  python -m scripts.holmes_baseline.run run1-errors     # F14 step 2, offline
  python -m scripts.holmes_baseline.run repro <case>    # F14 step 3, one case
  python -m scripts.holmes_baseline.run opspilot-freeze # F14 step 3, offline
  python -m scripts.holmes_baseline.run records [--check]    # step 3, offline
  python -m scripts.holmes_baseline.run writeback [--check]  # step 3, offline

Docker is the colima ``m1-kind`` daemon. The Holmes container joins only an
internal network shared with two helpers (``relay.py``): the lab relay
(-> kind NodePorts 30090 proxy, 30686 Jaeger) and the egress proxy (CONNECT
to the model API only). Raw Holmes output stays under the git-ignored
``RAW_ROOT``; only hashes, counts and the holmes proxy log go to evidence.
The model key comes from ``DEEPSEEK_API_KEY`` or ``OPSPILOT_ENV_FILE``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # type: ignore[import-untyped]

from scripts import kind_lab
from scripts.holmes_baseline import config as hconfig
from scripts.holmes_baseline import mapping as hmapping
from scripts.holmes_baseline.container import run_args
from scripts.holmes_baseline.project import project_outcome

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EVIDENCE = ROOT / "docs/evidence/f14-holmes-baseline"
# Raw output (Holmes messages incl. reasoning) never enters the repository.
RAW_ROOT = Path(
    os.environ.get(
        "F14_HOLMES_RAW_DIR",
        str(Path.home() / "dev/AI/production-ops-agent/tmp/f14-holmes"),
    )
)
UPSTREAM_DIR = Path(
    os.environ.get(
        "F14_HOLMES_UPSTREAM",
        str(
            Path.home() / "dev/AI/production-ops-agent/tmp/upstream/holmesgpt-a0009195"
        ),
    )
)
IMAGE = "opspilot-f14/holmesgpt:a0009195"
HELPER_IMAGE = "python:3.11-alpine"
NETWORK = "f14-holmes-internal"
RELAY = "f14-lab-relay"
EGRESS = "f14-egress"
CAPTURE = "f14-capture"
HOLMES = "f14-holmes"
ALERTMANAGER = "f14-alertmanager"
KIND_NODE = f"{kind_lab.CLUSTER}-control-plane"
PROMETHEUS_URL = f"http://{RELAY}:9090"
JAEGER_URL = f"http://{RELAY}:16686/jaeger/ui"
SCENARIO = ROOT / "evals/scenarios/eval-payment-failure-1/scenario.json"
MAPPING_DIR = ROOT / "docs/evidence/f14-upstream-mapping"
FAULT_SETTLE_SECONDS = 300
WINDOW_SECONDS = 300
MAX_CLOCK_SKEW_SECONDS = 2.0


def docker_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    }
    # F14_DOCKER_CONTEXT: offline smoke in another, already running daemon
    # (#385); the default is the kind lab's daemon.
    context = os.environ.get("F14_DOCKER_CONTEXT") or kind_lab.CONTEXT
    return {**env, "DOCKER_CONTEXT": context}


def docker(*args: str, timeout: float = 120, check: bool = True, **kw: Any) -> str:
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=kw.pop("env", docker_env()),
        check=False,
        **kw,
    )
    if check and proc.returncode:
        raise SystemExit(f"docker {args[0]} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- helpers' lifecycle


def helper_args(name: str, network: str, *command: str) -> list[str]:
    return [
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "--user",
        "65534:65534",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "64m",
        "-v",
        f"{HERE}:/harness:ro",
        HELPER_IMAGE,
        "python",
        "-I",
        *command,
    ]


def teardown() -> None:
    for name in (HOLMES, CAPTURE, ALERTMANAGER, EGRESS, RELAY):
        docker("rm", "-f", name, check=False)
    docker("network", "rm", NETWORK, check=False)


def capture_args(script: Path | None) -> list[str]:
    """The capture helper; with ``script`` its directory is mounted read-only
    and the endpoint runs scripted (#385, R7)."""
    args = helper_args(CAPTURE, NETWORK, "/harness/capture.py", "8000")
    if script is None:
        return args
    at = args.index(HELPER_IMAGE)
    mount = ["-v", f"{script.parent}:/script:ro"]
    return [*args[:at], *mount, *args[at:], "--script", f"/script/{script.name}"]


def alertmanager_args(alerts: Path) -> list[str]:
    """The read-only Alertmanager API stand-in serving ``alerts`` (#385)."""
    args = helper_args(
        ALERTMANAGER,
        NETWORK,
        "/harness/alertmanager_stub.py",
        "9093",
        f"/alerts/{alerts.name}",
    )
    at = args.index(HELPER_IMAGE)
    return [*args[:at], "-v", f"{alerts.parent}:/alerts:ro", *args[at:]]


def setup(
    *,
    capture: bool = False,
    script: Path | None = None,
    alerts: Path | None = None,
) -> None:
    teardown()
    docker("network", "create", "--internal", NETWORK)
    docker(
        *helper_args(
            RELAY,
            "kind",
            "/harness/relay.py",
            "forward",
            "--route",
            f"9090={KIND_NODE}:30090",
            "--route",
            f"16686={KIND_NODE}:30686",
        )
    )
    docker("network", "connect", NETWORK, RELAY)
    docker(
        *helper_args(
            EGRESS,
            "bridge",
            "/harness/relay.py",
            "connect",
            "--allow",
            f"{hconfig.MODEL_HOST}:443",
        )
    )
    docker("network", "connect", NETWORK, EGRESS)
    if capture:
        docker(*capture_args(script))
    if alerts is not None:
        docker(*alertmanager_args(alerts))
    time.sleep(2)


def write_config(directory: Path, extra: dict[str, Any] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    content = {**hconfig.holmes_config(PROMETHEUS_URL, JAEGER_URL), **(extra or {})}
    path.write_text(yaml.safe_dump(content, sort_keys=False))
    path.chmod(0o644)
    return path


def output_dir(directory: Path) -> Path:
    out = directory / "out"
    out.mkdir(parents=True, exist_ok=True)
    # the container runs as 65534 and must be able to write here
    out.chmod(0o777)
    return out


def holmes_secret_env(model_key: str) -> dict[str, str]:
    accounts = kind_lab.prometheus_accounts()
    return {
        **docker_env(),
        "DEEPSEEK_API_KEY": model_key,
        "HOLMES_PROMETHEUS_AUTHORIZATION": kind_lab.basic_auth_header(
            accounts, "holmes"
        ),
    }


def model_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    env_file = os.environ.get("OPSPILOT_ENV_FILE")
    if not key and env_file:
        for line in Path(env_file).expanduser().read_text().splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY (or OPSPILOT_ENV_FILE) is required")
    return key


def holmes_command(prompt: str) -> list[str]:
    return [
        "ask",
        "--no-interactive",
        "--config",
        "/etc/holmes/config.yaml",
        "--json-output-file",
        "/out/holmes.json",
        "--no-echo",
        prompt,
    ]


def run_holmes(
    directory: Path,
    config_path: Path,
    prompt: str,
    env: dict[str, str],
    *,
    no_proxy: str,
    wall_seconds: float,
    command: list[str] | None = None,
    model_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    out = output_dir(directory)
    args = run_args(
        IMAGE,
        network=NETWORK,
        egress_proxy=f"http://{EGRESS}:3128",
        config_path=str(config_path),
        output_dir=str(out),
        no_proxy=no_proxy,
        name=HOLMES,
        command=holmes_command(prompt) if command is None else command,
        model_env=model_env,
    )
    (directory / "docker-run.argv.txt").write_text(
        shlex.join(args[:-1]) + " <prompt>\n"
    )
    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=wall_seconds, env=env
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        docker("kill", HOLMES, check=False)
        exit_code = -9
        stdout = (
            (exc.stdout or b"").decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            (exc.stderr or b"").decode()
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
    finished = time.time()
    (directory / "holmes.stdout.txt").write_text(stdout)
    (directory / "holmes.stderr.txt").write_text(stderr)
    raw = out / "holmes.json"
    output: Any = json.loads(raw.read_text()) if raw.exists() else {}
    if not isinstance(output, dict):
        # ``holmes investigate`` writes a list of results (#385)
        output = {"results": output}
    output.update(exit_code=exit_code, timed_out=timed_out)
    if exit_code:
        output["error"] = (stderr or stdout)[-2000:]
    return {
        "output": output,
        "started": started,
        "finished": finished,
        "raw_sha256": sha256_file(raw) if raw.exists() else None,
    }


# --- subcommands


def cmd_toolsets(_: argparse.Namespace) -> int:
    script = (
        "from holmes.plugins.toolsets import load_builtin_toolsets;"
        "import json;print(json.dumps(sorted(t.name for t in load_builtin_toolsets())))"
    )
    out = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "65534:65534",
        "-e",
        "HOME=/tmp",
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        script,
        timeout=300,
    )
    print(out.strip().splitlines()[-1])
    return 0


def base_images(build_log: Path) -> dict[str, str]:
    images = {}
    for line in build_log.read_text().splitlines():
        # "#3 resolve docker.io/library/python:3.11-alpine@sha256:..." (buildx)
        if "resolve docker.io/" in line and "@sha256:" in line:
            ref = line.split("resolve ", 1)[1].split()[0]
            name, digest = ref.split("@", 1)
            images[name] = digest
    return images


def cmd_pin(args: argparse.Namespace) -> int:
    inspect = json.loads(docker("image", "inspect", IMAGE))[0]
    archive = RAW_ROOT / "holmesgpt-a0009195.image.tar"
    if not archive.exists() or args.resave:
        with archive.open("wb") as handle:
            proc = subprocess.run(
                ["docker", "save", IMAGE], stdout=handle, env=docker_env(), timeout=1800
            )
        if proc.returncode:
            raise SystemExit("docker save failed")
    commit = subprocess.run(
        ["git", "-C", str(UPSTREAM_DIR), "rev-parse", "HEAD", "HEAD^{tree}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if commit[0] != hconfig.UPSTREAM_COMMIT:
        raise SystemExit(f"upstream checkout is at {commit[0]}, not the pin")
    build_log = RAW_ROOT / "build.log"
    config = hconfig.holmes_config(PROMETHEUS_URL, JAEGER_URL)
    pin = {
        "upstream": {
            "repository": "https://github.com/HolmesGPT/holmesgpt",
            "commit": commit[0],
            "tree": commit[1],
            "dockerfile_sha256": sha256_file(UPSTREAM_DIR / "Dockerfile"),
            "dockerfile_modified": False,
        },
        "image": IMAGE,
        "image_id": inspect["Id"],
        "image_created": inspect.get("Created"),
        "image_architecture": inspect.get("Architecture"),
        "archive_sha256": sha256_file(archive),
        "archive_bytes": archive.stat().st_size,
        "base_image_digests": base_images(build_log),
        "build": {
            "command": "docker buildx build --load -t "
            f"{IMAGE} <checkout of {commit[0][:8]}> (colima m1-kind, buildx "
            "docker driver, no build args)",
            "log_sha256": sha256_file(build_log),
            "published": False,
        },
        "reproducible": False,
        "non_reproducible_because": [
            "base images referenced by tag, not digest (Dockerfile FROM lines)",
            "apk packages from alpine edge repositories",
            "poetry installed unpinned (pip install poetry)",
            "apk upgrade in the final stage",
        ],
        "license": {
            "spdx": "Apache-2.0",
            "file_sha256": sha256_file(UPSTREAM_DIR / "LICENSE"),
        },
        "config": {
            "sha256": hashlib.sha256(
                json.dumps(config, sort_keys=True).encode()
            ).hexdigest(),
            "enabled_toolsets": sorted(
                n for n, t in config["toolsets"].items() if t.get("enabled")
            ),
            "disabled_toolsets": sorted(
                n for n, t in config["toolsets"].items() if not t.get("enabled")
            ),
            "prometheus_url": PROMETHEUS_URL,
            "jaeger_url": JAEGER_URL,
            "kubernetes_credentials": False,
            "trace_export": False,
        },
        "model": {
            "name": hconfig.MODEL,
            "host": hconfig.MODEL_HOST,
            "env": hconfig.MODEL_ENV,
        },
        "budget": {
            "max_steps": hconfig.MAX_STEPS,
            "run_wall_seconds": hconfig.RUN_WALL_SECONDS,
            "tool_timeout_seconds": hconfig.TOOL_TIMEOUT_SECONDS,
            "step_counting_note": (
                "Holmes does not count compaction or retries as steps; "
                "OpsPilot counts every model request"
            ),
        },
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "pin.json").write_text(json.dumps(pin, indent=2) + "\n")
    print(
        json.dumps(
            {"image_id": pin["image_id"], "archive_sha256": pin["archive_sha256"]}
        )
    )
    return 0


def cmd_check_request(args: argparse.Namespace) -> int:
    directory = RAW_ROOT / f"check-{int(time.time())}"
    # same config, only api_base pointed at the capture endpoint; dummy key
    config_path = write_config(directory, {"api_base": f"http://{CAPTURE}:8000"})
    env = holmes_secret_env("sk-capture-dummy")
    setup(capture=True)
    try:
        result = run_holmes(
            directory,
            config_path,
            "Reply with the single word ok.",
            env,
            no_proxy=f"{RELAY},{CAPTURE}",
            wall_seconds=600,
        )
        captured = [
            json.loads(line)
            for line in docker("logs", CAPTURE).splitlines()
            if line.startswith("{")
        ]
    finally:
        teardown()
    checks = [c["check"] for c in captured]
    report = {
        "checked_at": utc(time.time()),
        "image": IMAGE,
        "model_env": hconfig.MODEL_ENV,
        "requests_captured": len(captured),
        "requests": [
            {k: c[k] for k in ("path", "body", "message_count", "tool_names", "check")}
            for c in captured
        ],
        "holmes_exit_code": result["output"].get("exit_code"),
        "ok": bool(checks) and all(c["ok"] for c in checks),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "request-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("requests_captured", "ok")}))
    return 0 if report["ok"] else 1


def frontend_span_rate() -> float:
    _, series = kind_lab.prom_instant(
        'sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[2m]))'
    )
    try:
        return float(series[0]["value"][1])  # type: ignore[index]
    except (IndexError, KeyError, TypeError, ValueError):
        return 0.0


def clock_skew() -> dict[str, float]:
    host = time.time()
    vm = float(
        subprocess.run(
            ["colima", "ssh", "-p", kind_lab.PROFILE, "--", "date", "+%s.%N"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
    )
    container = float(
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            HELPER_IMAGE,
            "python",
            "-c",
            "import time;print(time.time())",
        ).strip()
    )
    after = time.time()
    return {
        "host_vm": round(vm - host, 3),
        "host_container": round(container - host, 3),
        "measure_seconds": round(after - host, 3),
    }


def balance(key: str) -> dict[str, Any] | None:
    req = urllib.request.Request(
        f"https://{hconfig.MODEL_HOST}/user/balance",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001 - balance is best effort evidence
        return None


def proxy_log_lines(since: float) -> list[str]:
    proc = subprocess.run(
        kind_lab.kubectl(
            "-n",
            kind_lab.NAMESPACE,
            "logs",
            "deployment/prometheus-readonly-proxy",
            f"--since-time={utc(since - 5)}",
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return [line for line in proc.stdout.splitlines() if " holmes " in line]


def egress_log(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in docker("logs", name, check=False).splitlines()
        if line.startswith("{")
    ]


def tool_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for call in output.get("tool_calls") or []:
        result = call.get("result") or {}
        rows.append(
            {
                "tool": call.get("tool_name"),
                "toolset": call.get("toolset_name"),
                "status": result.get("status"),
                "elapsed_seconds": result.get("elapsed_seconds"),
            }
        )
    return rows


def restore_fault(run_id: str) -> int:
    """``kind_lab fault restore``'s exit code; a refusal (SystemExit) or a
    crash counts as a failed restore, never as restored."""
    try:
        return int(kind_lab.main(["fault", "restore", "--experiment-id", run_id]))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) and exc.code else 1
    except Exception:  # noqa: BLE001 - reported as a failed restore
        return 1


def fault_record(injected_at: float, restore_code: int) -> dict[str, Any]:
    return {
        "flag": kind_lab.FLAG,
        "injected_at": utc(injected_at),
        "restored": restore_code == 0,
        "restore_exit_code": restore_code,
    }


def cmd_live(args: argparse.Namespace) -> int:
    key = model_key()
    swap = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
    ).stdout.strip()
    rate = frontend_span_rate()
    skew = clock_skew()
    print(json.dumps({"swap": swap, "frontend_span_rate": rate, "clock_skew": skew}))
    if rate <= 0:
        raise SystemExit("frontend span rate is 0; restart otel-collector first")
    if max(abs(skew["host_vm"]), abs(skew["host_container"])) > MAX_CLOCK_SKEW_SECONDS:
        raise SystemExit("clock skew above 2 s; refusing (R4)")
    run_id = args.run_id
    directory = RAW_ROOT / run_id
    config_path = write_config(directory)
    scenario = json.loads(SCENARIO.read_text())
    question = scenario["stimulus"]["question"]
    balance_before = balance(key)
    injected = False
    restore_code: int | None = None
    try:
        code = kind_lab.main(["fault", "inject", "--experiment-id", run_id])
        if code:
            raise SystemExit(f"fault inject failed ({code})")
        injected = True
        injected_at = time.time()
        time.sleep(args.settle)
        end = time.time()
        prompt = (
            f"{question}\nInvestigation window (UTC): {utc(end - WINDOW_SECONDS)} "
            f"to {utc(end)}."
        )
        setup()
        try:
            result = run_holmes(
                directory,
                config_path,
                prompt,
                holmes_secret_env(key),
                no_proxy=RELAY,
                wall_seconds=hconfig.RUN_WALL_SECONDS,
            )
            egress = egress_log(EGRESS)
            relay = egress_log(RELAY)
        finally:
            teardown()
        proxy_lines = proxy_log_lines(result["started"])
    finally:
        if injected:
            restore_code = restore_fault(run_id)
            if restore_code:
                print(
                    f"FAULT RESTORE FAILED ({restore_code}); restore {kind_lab.FLAG} "
                    "by hand before any other lab use",
                    file=sys.stderr,
                )
    assert restore_code is not None
    balance_after = balance(key)
    # the vendor balance lags behind the calls; read it again later
    time.sleep(args.balance_recheck)
    balance_later = balance(key)
    output = result["output"]
    outcome = project_outcome(output, proxy_lines)
    proxy_counts = Counter(
        " ".join(line.split()[2:5]) for line in proxy_lines if len(line.split()) >= 5
    )
    connects = [e for e in egress if e.get("mode") == "connect" and "client" in e]
    summary = {
        "run_id": run_id,
        "scenario_id": scenario["scenario_id"],
        "diagnosis_scored": False,
        "outcome": outcome,
        "fault": fault_record(injected_at, restore_code),
        "started_at": utc(result["started"]),
        "finished_at": utc(result["finished"]),
        "wall_seconds": round(result["finished"] - result["started"], 1),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_window_seconds": WINDOW_SECONDS,
        "exit_code": output.get("exit_code"),
        "timed_out": output.get("timed_out"),
        "finish_reason": output.get("finish_reason"),
        "usage": {
            k: output.get(k)
            for k in (
                "num_llm_calls",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
                "total_cost",
                "num_compactions",
            )
        },
        "tool_calls": tool_rows(output),
        "proxy_requests_holmes": dict(sorted(proxy_counts.items())),
        "egress": {
            "allowed": sum(1 for e in connects if e.get("allowed")),
            "denied": [e.get("reason") for e in connects if not e.get("allowed")],
            "targets": sorted({e.get("target") for e in connects if e.get("target")}),
        },
        "relay_connections": sum(1 for e in relay if "client" in e),
        "pre_run": {"frontend_span_rate": rate, "clock_skew": skew, "host_swap": swap},
        "versions": {
            "upstream": f"{hconfig.UPSTREAM_COMMIT[:8]} "
            + json.loads(docker("image", "inspect", IMAGE))[0]["Id"],
            "config_sha256": sha256_file(config_path),
        },
        "raw_output_sha256": result["raw_sha256"],
        "balance": {
            "source": f"GET https://{hconfig.MODEL_HOST}/user/balance",
            "before": balance_before,
            "after": balance_after,
            "after_delay_seconds": args.balance_recheck,
            "after_delayed": balance_later,
            # LiteLLM's own estimate; 0.0 when the model is not in its cost map
            "litellm_total_cost": output.get("total_cost"),
        },
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (EVIDENCE / "proxy-log-holmes.txt").write_text("\n".join(proxy_lines) + "\n")
    print(json.dumps(outcome))
    return 0 if restore_code == 0 else 1


def cmd_upstream_files(args: argparse.Namespace) -> int:
    """Write (or with ``--check`` compare) upstream-files.json from the full
    checkout, then validate mapping.json against it and the checkout."""
    hmapping.check_checkout(UPSTREAM_DIR)
    table = json.loads((MAPPING_DIR / "mapping.json").read_text())
    manifest = hmapping.upstream_files(UPSTREAM_DIR, hmapping.cited_paths(table))
    target = MAPPING_DIR / "upstream-files.json"
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not target.exists() or target.read_text() != text:
            print(f"{target} is stale")
            return 1
    else:
        target.write_text(text)
    features = json.loads((ROOT / "feature_list.json").read_text())
    found = hmapping.problems(table, manifest, features, ROOT, UPSTREAM_DIR)
    for problem in found:
        print(problem)
    print(
        json.dumps(
            {
                "rows": len(table["rows"]),
                "files": len(manifest["files"]),
                "classes": dict(Counter(row["class"] for row in table["rows"])),
                "problems": len(found),
            }
        )
    )
    return 1 if found else 0


def cmd_run1_errors(_: argparse.Namespace) -> int:
    summary = json.loads((EVIDENCE / "summary.json").read_text())
    raw = RAW_ROOT / summary["run_id"] / "out" / "holmes.json"
    frozen = hmapping.run1_tool_errors(raw, summary)
    (EVIDENCE / "run1-tool-errors.json").write_text(
        json.dumps(frozen, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps({"non_success": len(frozen["non_success"])}))
    return 0


def cmd_records(args: argparse.Namespace) -> int:
    """Write (or with ``--check`` compare) the step 3 upstream file list, then
    validate every record.json (seams section 2)."""
    from scripts.holmes_baseline import issues

    records = issues.load_records(ROOT)
    target = ROOT / issues.EVIDENCE_PREFIX / "upstream-files.json"
    if not args.check:
        hmapping.check_checkout(UPSTREAM_DIR)
        manifest = hmapping.upstream_files(UPSTREAM_DIR, issues.cited_paths(records))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    found = issues.all_problems(ROOT)
    for problem in found:
        print(problem)
    print(json.dumps({"records": len(records), "problems": len(found)}))
    return 1 if found else 0


def cmd_writeback(args: argparse.Namespace) -> int:
    from scripts.holmes_baseline import issues

    path = MAPPING_DIR / "mapping.json"
    table = json.loads(path.read_text())
    records = issues.load_records(ROOT)
    try:
        updated = issues.writeback(table, records)
    except SystemExit as exc:
        print(exc)
        return 1
    text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        stale = text != path.read_text()
        found = issues.writeback_problems(table, records)
        for problem in found:
            print(problem)
        print(json.dumps({"stale": stale, "problems": len(found)}))
        return 1 if stale or found else 0
    path.write_text(text)
    counts = Counter(row["class"] for row in updated["rows"])
    print(json.dumps({"classes": dict(counts)}))
    return 0


def cmd_opspilot_freeze(args: argparse.Namespace) -> int:
    """Copy one OpsPilot Run's frozen summary into the paired record's
    directory and add its S7 fields to ``opspilot_side`` (L5)."""
    from scripts.holmes_baseline import issues

    directory = ROOT / issues.EVIDENCE_PREFIX / issues.PAIRED
    summary = json.loads(args.summary.read_text())
    rel = f"opspilot/{summary['run_id']}/summary.json"
    (directory / rel).parent.mkdir(parents=True, exist_ok=True)
    (directory / rel).write_bytes(args.summary.read_bytes())
    sidecar = args.first_request.with_name(args.first_request.name + ".versions.json")
    entry = issues.opspilot_run(
        directory / rel,
        rel,
        args.first_request.read_bytes(),
        args.group,
        json.loads(sidecar.read_text()) if sidecar.exists() else None,
    )
    # working file for assembling record.json; stays out of the evidence dir
    out = RAW_ROOT / "f14s3-opspilot-runs.json"
    rows = (
        json.loads(out.read_text())
        if out.exists()
        else {"prometheus": [], "jaeger": []}
    )
    rows[args.group] = [r for r in rows[args.group] if r["run_id"] != entry["run_id"]]
    rows[args.group].append(entry)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                k: entry[k]
                for k in (
                    "run_id",
                    "first_request_mentions_unreachable",
                    "report_names_missing_source",
                )
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("toolsets").set_defaults(func=cmd_toolsets)
    pin = sub.add_parser("pin")
    pin.add_argument("--resave", action="store_true")
    pin.set_defaults(func=cmd_pin)
    sub.add_parser("check-request").set_defaults(func=cmd_check_request)
    live = sub.add_parser("live")
    live.add_argument("--run-id", default=f"f14-holmes-{int(time.time())}")
    live.add_argument("--settle", type=float, default=FAULT_SETTLE_SECONDS)
    live.add_argument("--balance-recheck", type=float, default=300)
    live.set_defaults(func=cmd_live)
    files = sub.add_parser("upstream-files")
    files.add_argument("--check", action="store_true")
    files.set_defaults(func=cmd_upstream_files)
    sub.add_parser("run1-errors").set_defaults(func=cmd_run1_errors)
    from scripts.holmes_baseline import repro

    rep = sub.add_parser("repro")
    rep.add_argument("case", choices=sorted(repro.CASES))
    rep.add_argument("--run-id", default=None)
    rep.add_argument("--wall-seconds", type=float, default=None)
    rep.add_argument("--balance-recheck", type=float, default=120)
    rep.add_argument("--fault-flag", default=None)
    rep.add_argument("--fault-experiment-id", default=None)
    rep.add_argument(
        "--no-proxy-log",
        dest="proxy_log",
        action="store_false",
        help="skip reading the lab proxy log (offline smoke without kind)",
    )
    rep.set_defaults(func=repro.cmd_repro)
    freeze = sub.add_parser("opspilot-freeze")
    freeze.add_argument("--group", choices=("prometheus", "jaeger"), required=True)
    freeze.add_argument("--summary", type=Path, required=True)
    freeze.add_argument("--first-request", type=Path, required=True)
    freeze.set_defaults(func=cmd_opspilot_freeze)
    records = sub.add_parser("records")
    records.add_argument("--check", action="store_true")
    records.set_defaults(func=cmd_records)
    back = sub.add_parser("writeback")
    back.add_argument("--check", action="store_true")
    back.set_defaults(func=cmd_writeback)
    args = parser.parse_args(argv)
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
