"""Trusted launcher: reads DEEPSEEK_API_KEY from the private .env, passes it to a
runner subprocess on stdin only, and books every attempt into the round-07 ledger.

The key is never printed, logged, written or placed in argv/environment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = ROOT / ".env"
LEDGER = ROOT / "docs/evidence/m0-real-investigation/round-07-wp5-ledger.json"
HOLMES_ENV_ROOT = ROOT.parent / "production-ops-agent-m0-environment/tmp/m0-environment"
DEFAULT_HOLMES_PYTHON = HOLMES_ENV_ROOT / "holmes-venv/bin/python"
DEFAULT_HOLMES_ROOT = (
    HOLMES_ENV_ROOT / "holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4"
)
HOLMES_PYTHON_SHA256 = (
    "eb9d74b9c7cfdfb2c9b91614edb2c3607360ba46c5aa7fc4557b3a4a23e97cff"
)
HOLMES_CODE_SHA256 = "b4f72fc577f2910d9b68bb775c97dd51c58279478090939f5ee4f7cfa7bcee01"
DOCKER_PATH = Path("/opt/homebrew/bin/docker")
DOCKER_SHA256 = "20f2dc84f2c0adef9cbf92cb3bfa6a631e3ad8be2645e1f4ecbc27d2f2d1d546"
# The container arm is intentionally pinned to the audited, mount-free image
# produced by the round-07 holdout build.  It must never accept an arbitrary
# executable or image from runner_args.
CONTAINER_IMAGE = "opspilot-m0-r07-runner@sha256:83bd4247d4eca8cc276b0af3626ce96f7bfc93e65151128739420ebc75abd25c"
CONTAINER_PREFIX = ("run", "--rm", "-i", "--network", "none")
CONTAINER_PACKETS = {
    "normal": "/runner/packets/packet-m004-normal.json",
    "fault": "/runner/packets/packet-m004-fault.json",
}
PACKET_ROOT = ROOT / "docs/evidence/m0-real-investigation/round-07-upstream-runs"
PACKET_MANIFEST = {
    "packet-m004-normal.json": {
        "scenario": "normal",
        "sha256": "ac80c078b6e13f53792943d404e7424eb5dfc8808b4bd796d297306a4e99c7ff",
    },
    "packet-m004-fault.json": {
        "scenario": "fault",
        "sha256": "61ca39b0bf29c111dd73ee65efd83616de96f2d95169d4dca25a618d91c3fd28",
    },
}
TOTAL_HTTP = 8
TOTAL_RESERVATION_CNY = 8.0
RESERVATION_PER_HTTP = 1.0


def canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


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
    value = explicit or DEFAULT_HOLMES_PYTHON
    path = value.expanduser().resolve()
    if path != DEFAULT_HOLMES_PYTHON.resolve():
        raise ValueError(f"Holmes Python path is not pinned: {path}")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Holmes Python unavailable or not executable: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != HOLMES_PYTHON_SHA256:
        raise ValueError(f"Holmes Python hash mismatch: {path}")
    return path


def resolve_holmes_root(explicit: Path | None = None) -> Path:
    value = explicit or DEFAULT_HOLMES_ROOT
    path = value.expanduser().resolve()
    if path != DEFAULT_HOLMES_ROOT.resolve():
        raise ValueError(f"Holmes checkout path is not pinned: {path}")
    if not (path / "holmes").is_dir():
        raise ValueError(f"Holmes checkout unavailable: {path}")
    digest = hashlib.sha256()
    for source in sorted((path / "holmes").rglob("*.py")):
        digest.update(
            str(source.relative_to(path)).encode() + b"\0" + source.read_bytes() + b"\0"
        )
    if digest.hexdigest() != HOLMES_CODE_SHA256:
        raise ValueError(f"Holmes checkout hash mismatch: {path}")
    return path


def resolve_packet(explicit: Path) -> tuple[Path, str]:
    path = explicit.expanduser().resolve()
    if PACKET_ROOT.resolve() not in path.parents or not path.is_file():
        raise ValueError(
            f"replay packet unavailable or outside frozen packet root: {path}"
        )
    manifest = PACKET_MANIFEST.get(path.name)
    if manifest is None:
        raise ValueError(f"replay packet is not in the frozen hash manifest: {path}")
    try:
        packet = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"replay packet is not valid JSON: {path}") from exc
    if canonical_hash(packet) != manifest["sha256"]:
        raise ValueError(f"replay packet hash mismatch: {path}")
    packet_scenario = packet.get("scenario", manifest["scenario"])
    if packet_scenario != manifest["scenario"]:
        raise ValueError(f"replay packet scenario mismatch: {path}")
    return path, manifest["scenario"]


def resolve_docker() -> Path:
    path = DOCKER_PATH.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"trusted Docker executable unavailable: {DOCKER_PATH}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != DOCKER_SHA256:
        raise ValueError(f"trusted Docker hash mismatch: {path}")
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
    docker = resolve_docker()
    if len(command) != 17 or command[:7] != [str(docker), *CONTAINER_PREFIX, "--mount"]:
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
        "reserved_http": 0,
        "known_cost_upper_cny": 0.0,
        "unknown_reservation_cny": 0.0,
        "cost_basis": "peak all-cache-miss upper bound (prompt*3 + completion*9 CNY per 1M tokens); not invoice",
    }


def write_ledger(ledger: dict) -> None:
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def redact_output(value, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    return text.replace(key, "[REDACTED]") if key else text


def reserve_run(run_entry: dict) -> dict:
    """Atomically check the ID/cap and reserve a run before launching it."""
    lock_path = LEDGER.with_name(LEDGER.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = load_ledger()
            ledger.setdefault("reserved_http", 0)
            if any(run["run_id"] == run_entry["run_id"] for run in ledger["runs"]):
                raise SystemExit("run id already booked")
            if (
                ledger["http_count"] + ledger["reserved_http"] + run_entry["max_http"]
                > TOTAL_HTTP
            ):
                raise SystemExit("allocation HTTP cap would be exceeded; not launched")
            ledger["runs"].append(run_entry)
            ledger["reserved_http"] += run_entry["max_http"]
            write_ledger(ledger)
            return ledger
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def update_run(
    run_id: str, result: dict, proc: subprocess.CompletedProcess, key: str
) -> dict:
    """Merge a completed run while serializing with other launcher writers."""
    lock_path = LEDGER.with_name(LEDGER.name + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = load_ledger()
            ledger.setdefault("reserved_http", 0)
            run_entry = next(run for run in ledger["runs"] if run["run_id"] == run_id)
            ledger["reserved_http"] = max(
                0, ledger["reserved_http"] - run_entry["max_http"]
            )
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
                        ledger["known_cost_upper_cny"] + entry["cost_upper_cny"],
                        6,
                    )
            run_entry.update(
                status=result.get("status", "runner_failed"),
                failure=redact_output(result.get("failure"), key),
                exit_code=proc.returncode,
                ended_at=time.time(),
                stdout_tail=redact_output(proc.stdout, key)[-400:],
                stderr_tail=redact_output(proc.stderr, key)[-400:],
            )
            write_ledger(ledger)
            return ledger
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
    parser.add_argument("--holmes-root", type=Path)
    parser.add_argument("--packet", type=Path)
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
        holmes_root = (
            resolve_holmes_root(args.holmes_root) if args.arm == "upstream" else None
        )
        packet_info = (
            resolve_packet(args.packet)
            if args.arm in ("candidate", "upstream") and args.packet
            else None
        )
        if args.arm in ("candidate", "upstream") and packet_info is None:
            raise ValueError("--packet is required for candidate/upstream arms")
        packet, packet_scenario = packet_info or (None, None)
        if args.arm in ("candidate", "upstream") and args.scenario != packet_scenario:
            raise ValueError("--scenario does not match replay packet")
    except ValueError as exc:
        parser.error(str(exc))
    if args.arm == "candidate":
        if args.runner_args:
            raise ValueError("runner arguments are not allowlisted")
        command = [
            sys.executable,
            str(ROOT / "scripts/m0_lab/round07/candidate_runner.py"),
        ]
    elif args.arm == "upstream":
        if args.runner_args:
            raise ValueError("runner arguments are not allowlisted")
        command = [
            str(holmes_python),
            str(ROOT / "scripts/m0_lab/round07/upstream_runner.py"),
        ]
    else:
        if args.runner_args:
            raise ValueError("container command is not allowlisted")
        docker = resolve_docker()
        command = build_container_command(
            str(docker),
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
            "--packet",
            str(packet),
            "--out",
            str(args.out),
            "--run-id",
            args.run_id,
            "--max-http",
            str(args.max_http),
        ]
        if args.arm == "upstream":
            command += ["--upstream-root", str(holmes_root)]
    run_entry = {
        "run_id": args.run_id,
        "arm": args.arm,
        "scenario": args.scenario,
        "max_http": args.max_http,
        "started_at": time.time(),
        "attempts": [],
        "status": "launched",
    }
    ledger = reserve_run(run_entry)
    try:
        proc = subprocess.run(
            command, input=key + "\n", capture_output=True, text=True, timeout=1200
        )
        runner_failure = None
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(
            command,
            124,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )
        runner_failure = "RUNNER_TIMEOUT"
    except Exception as exc:
        proc = subprocess.CompletedProcess(command, 1, stdout="", stderr=str(exc))
        runner_failure = f"RUNNER_FAILED:{type(exc).__name__}"
    result_path = args.out / "result-business.json"
    if runner_failure:
        result = {}
    elif result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            if not isinstance(result, dict):
                raise ValueError("result is not an object")
        except (OSError, ValueError):
            result = {}
            runner_failure = "RUNNER_RESULT_INVALID"
    else:
        result = {}
    if runner_failure:
        result = {"status": "failed", "failure": runner_failure, "attempts": []}
    ledger = update_run(args.run_id, result, proc, key)
    del key
    run_entry = next(run for run in ledger["runs"] if run["run_id"] == args.run_id)
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
    return (
        0
        if proc.returncode == 0
        and run_entry["status"] not in {"failed", "runner_failed"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
