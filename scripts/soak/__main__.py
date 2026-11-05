"""``python3 -m scripts.soak <command>`` from the repository root.

plan        freeze the schedule (with its T0) into <data>/plan.json, print SHA-256
run         da-server: wait for T0, execute the plan, close incidents, sample
sample      the rental host: resource samples every 60 s (S-R10)
note-interruption   record an environment-only interruption (S-R4)
note-rental-lost    record that the rental host was lost or reinstalled (S-R4)
finish      da-server, >= 30 min after ``run`` ended: PG export, meta.json, pg_dump
summarize   <data> -> summary.json (offline; same as ``-m scripts.soak.summarize``)
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.soak import pg
from scripts.soak import plan as soak_plan
from scripts.soak import summarize as soak_summarize
from scripts.soak.common import append_jsonl, iso, parse_time, read_jsonl, utcnow
from scripts.soak.runner import (
    CSV_HEADERS,
    Config,
    Runner,
    append_csv,
    write_resource_sample,
)

TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}


def _runner(data: Path) -> Runner:
    config = Config.load(data / "soak.json")
    plan, sha = soak_plan.load(data / "plan.json")
    return Runner(config=config, data=data, plan=plan, plan_sha256=sha)


def cmd_plan(args: argparse.Namespace) -> int:
    plan = soak_plan.build_plan(
        parse_time(args.t0),
        price_input=args.price_input,
        price_output=args.price_output,
        price_source=args.price_source,
        expected_deployments=args.deployment,
    )
    print(soak_plan.freeze(args.data / "plan.json", plan))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _runner(args.data).run_until_end()
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    until = parse_time(args.until) if args.until else None
    while until is None or utcnow() < until:
        write_resource_sample(args.data, args.host, args.group, utcnow())
        time.sleep(args.interval)
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    start, end = parse_time(args.start), parse_time(args.end)
    if end <= start:
        raise SystemExit("end must be after start")
    events = args.data / "events.jsonl"
    append_jsonl(
        events,
        {
            "ts": iso(start),
            "type": "env_interruption_start",
            "cause": args.cause,
            "noted_at": iso(utcnow()),
        },
    )
    append_jsonl(
        events,
        {
            "ts": iso(end),
            "type": "env_interruption_end",
            "cause": args.cause,
            "noted_at": iso(utcnow()),
        },
    )
    return 0


def cmd_rental_lost(args: argparse.Namespace) -> int:
    append_jsonl(
        args.data / "events.jsonl",
        {
            "ts": iso(parse_time(args.at)),
            "type": "rental_lost",
            "reason": args.reason,
            "noted_at": iso(utcnow()),
        },
    )
    return 0


def _first(
    log: list[dict[str, Any]], run_id: str, states: set[str] | None
) -> float | None:
    times = [
        parse_time(str(r["ts"])).timestamp()
        for r in log
        if r["run_id"] == run_id and (states is None or r["state"] in states)
    ]
    return min(times) if times else None


def _t(raw: Any) -> float | None:
    return None if raw in (None, "") else parse_time(str(raw)).timestamp()


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else iso(datetime.fromtimestamp(epoch, timezone.utc))


def raw_runs(
    rows: list[dict[str, Any]], log: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``runs.jsonl``. PG keeps no Run state times, so: created = the alert
    identity's creation for an incident's first Run, else the runner's first
    sight; started = first sight as ``running`` (else the first step);
    finished = first sight in a terminal state (else the last step, when
    terminal); the wall-clock limit is the Run's deadline minus its start."""
    out = []
    for row in rows:
        run_id = str(row["run_id"])
        created = _t(row.get("intake_created_at")) or _first(log, run_id, None)
        first_step = _t(row.get("first_step_at"))
        started = _first(log, run_id, {"running"}) or first_step
        if started is not None and first_step is not None:
            started = min(started, first_step)
        if created is None:
            created = started
        terminal = row.get("state") in TERMINAL
        finished = _first(log, run_id, TERMINAL) if terminal else None
        if terminal and finished is None:
            finished = _t(row.get("last_step_at"))
        deadline = _t(row.get("deadline"))
        anchor = started if started is not None else created
        out.append(
            {
                "run_id": run_id,
                "incident_id": row.get("incident_id"),
                "state": row.get("state"),
                "created_at": _iso(created),
                "started_at": _iso(started),
                "finished_at": _iso(finished),
                "wall_clock_limit_s": None
                if deadline is None or anchor is None
                else round(deadline - anchor, 3),
                "input_tokens": int(float(row.get("prompt_tokens") or 0)),
                "output_tokens": int(float(row.get("completion_tokens") or 0)),
            }
        )
    return out


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )


def _merge_csvs(data: Path, other: Path) -> None:
    """Append the rental host's resource CSV rows (its ``sample`` output)."""
    for name in ("docker_stats.csv", "host_stats.csv", "sizes.csv"):
        source = other / name
        if not source.exists():
            continue
        with source.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header != CSV_HEADERS[name]:
                raise SystemExit(f"{source}: unexpected header {header}")
            append_csv(data / name, list(reader))


def cmd_finish(args: argparse.Namespace) -> int:
    runner = _runner(args.data)
    events = runner.events()
    if not any(e.get("type") == "soak_end" for e in events) and not args.force:
        raise SystemExit("the runner has not ended; --force exports anyway")
    queries = pg.export_queries(runner.t0)
    snapshot = iso(utcnow())
    rows = {name: runner.db.rows(sql, timeout=300) for name, sql in queries.items()}
    log = list(read_jsonl(args.data / "run_states.jsonl"))
    _write_jsonl(
        args.data / "deliveries.jsonl",
        [
            {
                "delivery_id": str(d["delivery_id"]),
                "fingerprint": d["fingerprint"],
                "received_at": iso(parse_time(str(d["received_at"]))),
                "outcome": pg.DELIVERY_OUTCOMES.get(str(d["outcome"])),
                # the intake decision is taken in the receiving transaction
                "outcome_at": iso(parse_time(str(d["received_at"])))
                if d["outcome"] in pg.DELIVERY_OUTCOMES
                else None,
                "run_id": d.get("run_id")
                if d["outcome"] in {"created", "replayed"}
                else None,
                "incident_id": d.get("incident_id"),
                "intake_outcome": d["outcome"],
            }
            for d in rows["deliveries"]
        ],
    )
    _write_jsonl(args.data / "runs.jsonl", raw_runs(rows["runs"], log))
    _write_jsonl(
        args.data / "sessions.jsonl",
        [
            {
                "session_id": s["session_id"],
                "incident_id": s["incident_id"],
                "started_at": iso(parse_time(str(s["authorized_at"]))),
                "ended_at": None
                if s.get("ended_at") is None
                else iso(parse_time(str(s["ended_at"]))),
                "interval_s": int(s["sample_interval_seconds"]),
                "max_duration_s": round(
                    (_t(s["deadline_at"]) or 0.0) - (_t(s["authorized_at"]) or 0.0), 3
                ),
                "transition": s.get("transition"),
            }
            for s in rows["sessions"]
        ],
    )
    samples = args.data / "samples.csv"
    samples.write_text(",".join(CSV_HEADERS["samples.csv"]) + "\n", encoding="utf-8")
    append_csv(
        samples,
        [
            [
                iso(parse_time(str(p["submitted_at"]))),
                p["session_id"],
                pg.signal_status(
                    str(p["outcome"]), bool(p["required_signals_present"])
                ),
            ]
            for p in rows["samples"]
        ],
    )
    runner.sizes("end")
    if args.merge:
        _merge_csvs(args.data, args.merge)
    meta: dict[str, Any] = {
        "snapshot_at": snapshot,
        "rental": {
            "hourly_usd": args.rental_hourly_usd,
            "powered_on_at": args.rental_on,
            "powered_off_at": args.rental_off,
            "cpu": "shared vCPU",
        },
        "balance_readings": [
            {"ts": iso(parse_time(stamp)), "balance_cny": float(value)}
            for stamp, _, value in (b.partition("=") for b in args.balance)
        ],
        "postmortem_tokens": rows["postmortem_tokens"],
    }
    (args.data / "meta.json").write_text(
        json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    dump = args.data / "pg_dump.sql.gz"
    cmd = [
        *runner.config.compose,
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-U",
        "opspilot_owner",
        "-d",
        "opspilot",
    ]
    env = {**os.environ, **runner.config.env}
    with (
        subprocess.Popen(cmd, stdout=subprocess.PIPE, env=env) as proc,
        gzip.open(dump, "wb") as out,
    ):
        assert proc.stdout is not None
        shutil.copyfileobj(proc.stdout, out)
    runner.event("finish", pg_dump_exit=proc.returncode, snapshot_at=snapshot)
    return 0 if proc.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.soak", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--data", type=Path, required=True)
    plan.add_argument("--t0", required=True, help="ISO 8601 with zone, e.g. ...Z")
    plan.add_argument(
        "--price-input",
        type=float,
        required=True,
        help="CNY per million input tokens (cache miss)",
    )
    plan.add_argument("--price-output", type=float, required=True)
    plan.add_argument("--price-source", required=True)
    plan.add_argument(
        "--deployment",
        action="append",
        required=True,
        help="an expected lab Deployment (repeat)",
    )
    plan.set_defaults(func=cmd_plan)
    runp = sub.add_parser("run")
    runp.add_argument("--data", type=Path, required=True)
    runp.set_defaults(func=cmd_run)
    sample = sub.add_parser("sample")
    sample.add_argument("--data", type=Path, required=True)
    sample.add_argument("--host", required=True)
    sample.add_argument("--group", default="lab", choices=["lab", "shared"])
    sample.add_argument("--interval", type=float, default=60)
    sample.add_argument("--until")
    sample.set_defaults(func=cmd_sample)
    note = sub.add_parser("note-interruption")
    note.add_argument("--data", type=Path, required=True)
    note.add_argument("--start", required=True)
    note.add_argument("--end", required=True)
    note.add_argument("--cause", required=True)
    note.set_defaults(func=cmd_note)
    lost = sub.add_parser("note-rental-lost")
    lost.add_argument("--data", type=Path, required=True)
    lost.add_argument("--at", required=True)
    lost.add_argument("--reason", required=True)
    lost.set_defaults(func=cmd_rental_lost)
    finish = sub.add_parser("finish")
    finish.add_argument("--data", type=Path, required=True)
    finish.add_argument("--force", action="store_true")
    finish.add_argument("--merge", type=Path, help="rental sample directory to merge")
    finish.add_argument("--rental-hourly-usd", type=float)
    finish.add_argument("--rental-on", help="rental powered on at (ISO)")
    finish.add_argument("--rental-off", help="rental powered off at (ISO)")
    finish.add_argument(
        "--balance",
        action="append",
        default=[],
        help="TS=CNY balance reading (repeat; start and end)",
    )
    finish.set_defaults(func=cmd_finish)
    summ = sub.add_parser("summarize")
    summ.add_argument("raw_dir", type=Path)
    summ.add_argument("--out", type=Path, required=True)
    summ.set_defaults(func=None)
    args = parser.parse_args(argv)
    if args.command == "summarize":
        return soak_summarize.main([str(args.raw_dir), "--out", str(args.out)])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
