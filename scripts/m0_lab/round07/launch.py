"""Trusted launcher: reads DEEPSEEK_API_KEY from the private .env, passes it to a
runner subprocess on stdin only, and books every attempt into the round-07 ledger.

The key is never printed, logged, written or placed in argv/environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = ROOT / ".env"
LEDGER = ROOT / "docs/evidence/m0-real-investigation/round-07-wp5-ledger.json"
DEFAULT_HOLMES_PYTHON = ROOT / "tmp/m0-environment/holmes-venv/bin/python"
# The container arm is intentionally pinned to the audited, mount-free image
# produced by the round-07 holdout build.  It must never accept an arbitrary
# executable or image from runner_args.
CONTAINER_IMAGE = "opspilot-m0-r07-runner@sha256:83bd4247d4eca8cc276b0af3626ce96f7bfc93e65151128739420ebc75abd25c"
CONTAINER_PREFIX = ("run", "--rm", "-i", "--network", "none")
CONTAINER_PACKETS = {
    "normal": "/runner/packets/packet-m004-normal.json",
    "fault": "/runner/packets/packet-m004-fault.json",
}
TOTAL_HTTP = 8
TOTAL_RESERVATION_CNY = 8.0
RESERVATION_PER_HTTP = 1.0


def validate_max_http(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("max-http must be a positive integer")
    return value


def resolve_env_file(explicit: Path | None = None) -> Path:
    value = explicit or (
        Path(os.environ["M0_ENV_FILE"])
        if os.environ.get("M0_ENV_FILE")
        else DEFAULT_ENV_FILE
    )
    path = value.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"credential file unavailable: {path}")
    return path


def resolve_holmes_python(explicit: Path | None = None) -> Path:
    value = explicit or (
        Path(os.environ["M0_HOLMES_PYTHON"])
        if os.environ.get("M0_HOLMES_PYTHON")
        else DEFAULT_HOLMES_PYTHON
    )
    path = value.expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Holmes Python unavailable or not executable: {path}")
    return path


def read_key(env_file: Path) -> str:
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def build_container_command(
    docker: str, *, scenario: str, run_id: str, max_http: int, out: Path
) -> list[str]:
    if not docker or Path(docker).name != "docker":
        raise ValueError("container executable unavailable: docker")
    if scenario not in CONTAINER_PACKETS:
        raise ValueError("container scenario is not allowlisted")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
        raise ValueError("container run id is not allowlisted")
    output = out.expanduser().resolve()
    tmp_root = (ROOT / "tmp").resolve()
    if output == tmp_root or tmp_root not in output.parents:
        raise ValueError("container output must be under repository tmp/")
    output.mkdir(parents=True, exist_ok=True)
    return [
        docker,
        *CONTAINER_PREFIX,
        "--mount",
        f"type=bind,src={output},dst=/runner/output",
        CONTAINER_IMAGE,
        "--packet",
        CONTAINER_PACKETS[scenario],
        "--out",
        "/runner/output",
        "--run-id",
        run_id,
        "--max-http",
        str(max_http),
    ]


def validate_container_command(command: list[str]) -> list[str]:
    """Accept only the pinned Docker wrapper and its fixed runner arguments."""
    docker = shutil.which("docker")
    if not docker or Path(docker).name != "docker":
        raise ValueError("container executable unavailable: docker")
    if len(command) != 17 or command[:7] != [docker, *CONTAINER_PREFIX, "--mount"]:
        raise ValueError("container command is not allowlisted")
    mount = command[7]
    if not mount.startswith("type=bind,src=") or not mount.endswith(
        ",dst=/runner/output"
    ):
        raise ValueError("container mount is not allowlisted")
    source = Path(mount[len("type=bind,src=") : -len(",dst=/runner/output")])
    tmp_root = (ROOT / "tmp").resolve()
    if source.resolve() == tmp_root or tmp_root not in source.resolve().parents:
        raise ValueError("container mount is not allowlisted")
    if command[8] != CONTAINER_IMAGE:
        raise ValueError("container image is not allowlisted")
    if command[9] != "--packet" or command[10] not in CONTAINER_PACKETS.values():
        raise ValueError("container packet is not allowlisted")
    if command[11:] != [
        "--out",
        "/runner/output",
        "--run-id",
        command[14],
        "--max-http",
        command[16],
    ] or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", command[14]):
        raise ValueError("container command is not allowlisted")
    if not re.fullmatch(r"[1-9][0-9]*", command[16]):
        raise ValueError("container max-http is not allowlisted")
    return command


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {
        "allocation_id": "m0-07-wp5-20260912",
        "http_cap": TOTAL_HTTP,
        "reservation_cap_cny": TOTAL_RESERVATION_CNY,
        "reservation_per_http_cny": RESERVATION_PER_HTTP,
        "trace_uploads": 0,
        "runs": [],
        "http_count": 0,
        "known_cost_upper_cny": 0.0,
        "unknown_reservation_cny": 0.0,
        "cost_basis": "peak all-cache-miss upper bound (prompt*3 + completion*9 CNY per 1M tokens); not invoice",
    }


def cost_upper(usage) -> float | None:
    if not isinstance(usage, dict):
        return None
    p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(p) is int and type(c) is int:
        return round((p * 3 + c * 9) / 1_000_000, 6)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--arm", choices=("candidate", "upstream", "container"), required=True
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--max-http", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--holmes-python", type=Path)
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        args.max_http = validate_max_http(args.max_http)
    except ValueError as exc:
        parser.error(str(exc))
    if args.runner_args and args.runner_args[0] == "--":
        args.runner_args = args.runner_args[1:]
    try:
        env_file = resolve_env_file(args.env_file)
        holmes_python = (
            resolve_holmes_python(args.holmes_python)
            if args.arm == "upstream"
            else None
        )
    except ValueError as exc:
        parser.error(str(exc))
    ledger = load_ledger()
    if any(run["run_id"] == args.run_id for run in ledger["runs"]):
        raise SystemExit("run id already booked")
    if ledger["http_count"] + args.max_http > TOTAL_HTTP:
        raise SystemExit("allocation HTTP cap would be exceeded; not launched")
    if args.arm == "candidate":
        command = [
            sys.executable,
            str(ROOT / "scripts/m0_lab/round07/candidate_runner.py"),
        ]
    elif args.arm == "upstream":
        command = [
            str(holmes_python),
            str(ROOT / "scripts/m0_lab/round07/upstream_runner.py"),
        ]
    else:
        if args.runner_args:
            raise ValueError("container command is not allowlisted")
        docker = shutil.which("docker")
        if not docker:
            raise ValueError("container executable unavailable: docker")
        command = build_container_command(
            docker,
            scenario=args.scenario,
            run_id=args.run_id,
            max_http=args.max_http,
            out=args.out,
        )
        validate_container_command(command)
    key = read_key(env_file)
    if not key:
        raise SystemExit("trusted credential unavailable")
    if args.arm != "container":
        command += [
            "--out",
            str(args.out),
            "--run-id",
            args.run_id,
            "--max-http",
            str(args.max_http),
            *args.runner_args,
        ]
    run_entry = {
        "run_id": args.run_id,
        "arm": args.arm,
        "scenario": args.scenario,
        "max_http": args.max_http,
        "started_at": time.time(),
        "attempts": [],
        "status": "launched",
    }
    ledger["runs"].append(run_entry)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    proc = subprocess.run(
        command, input=key + "\n", capture_output=True, text=True, timeout=1200
    )
    del key
    result_path = args.out / "result-business.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    for attempt in result.get("attempts", []):
        if "http_status" not in attempt:
            continue
        entry = {
            "ordinal": attempt["ordinal"],
            "http_status": attempt["http_status"],
            "usage": attempt.get("usage"),
            "response_model": attempt.get("response_model"),
            "finish_reason": attempt.get("finish_reason"),
            "cost_upper_cny": cost_upper(attempt.get("usage")),
            "reservation_cny": RESERVATION_PER_HTTP,
        }
        run_entry["attempts"].append(entry)
        ledger["http_count"] += 1
        if entry["cost_upper_cny"] is None:
            ledger["unknown_reservation_cny"] += RESERVATION_PER_HTTP
        else:
            ledger["known_cost_upper_cny"] = round(
                ledger["known_cost_upper_cny"] + entry["cost_upper_cny"], 6
            )
    run_entry.update(
        status=result.get("status", "runner_failed"),
        exit_code=proc.returncode,
        ended_at=time.time(),
        stdout_tail=proc.stdout[-400:],
        stderr_tail=proc.stderr[-400:],
    )
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "status": run_entry["status"],
                "exit_code": proc.returncode,
                "http_count": ledger["http_count"],
            }
        )
    )
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
