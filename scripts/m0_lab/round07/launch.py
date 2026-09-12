"""Trusted launcher: reads DEEPSEEK_API_KEY from the private .env, passes it to a
runner subprocess on stdin only, and books every attempt into the round-07 ledger.

The key is never printed, logged, written or placed in argv/environment.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")
LEDGER = ROOT / "docs/evidence/m0-real-investigation/round-07-wp5-ledger.json"
HOLMES_PYTHON = Path(
    "/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python"
)
TOTAL_HTTP = 8
TOTAL_RESERVATION_CNY = 8.0
RESERVATION_PER_HTTP = 1.0


def validate_max_http(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("max-http must be a positive integer")
    return value


def read_key() -> str:
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


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
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        args.max_http = validate_max_http(args.max_http)
    except ValueError as exc:
        parser.error(str(exc))
    if args.runner_args and args.runner_args[0] == "--":
        args.runner_args = args.runner_args[1:]
    ledger = load_ledger()
    if any(run["run_id"] == args.run_id for run in ledger["runs"]):
        raise SystemExit("run id already booked")
    if ledger["http_count"] + args.max_http > TOTAL_HTTP:
        raise SystemExit("allocation HTTP cap would be exceeded; not launched")
    key = read_key()
    if not key:
        raise SystemExit("trusted credential unavailable")
    if args.arm == "candidate":
        command = [
            sys.executable,
            str(ROOT / "scripts/m0_lab/round07/candidate_runner.py"),
        ]
    elif args.arm == "upstream":
        command = [
            str(HOLMES_PYTHON),
            str(ROOT / "scripts/m0_lab/round07/upstream_runner.py"),
        ]
    else:
        command = list(args.runner_args)
        args.runner_args = []
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
