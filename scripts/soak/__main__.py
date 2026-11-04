"""``python3 -m scripts.soak <command>`` from the repository root.

plan        freeze the schedule into <data>/plan.json and print its SHA-256
run         da-server: execute the plan, close incidents, sample (until the end)
sample      the rental host: resource samples every 60 s (S-R10)
note-interruption   record an environment-only interruption (S-R4)
finish      da-server, >= 30 min after ``run`` ended: PG export, pg_dump, sizes
summarize   <data> -> summary.json (offline)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from scripts.soak import pg
from scripts.soak import plan as soak_plan
from scripts.soak import summarize as soak_summarize
from scripts.soak.common import append_jsonl, iso, parse_time, read_jsonl, utcnow
from scripts.soak.lab_io import Database, run
from scripts.soak.runner import Config, Runner, resource_sample


def _runner(data: Path) -> Runner:
    config = Config.load(data / "soak.json")
    plan, sha = soak_plan.load(data / "plan.json")
    return Runner(config=config, data=data, plan=plan, plan_sha256=sha)


def _sizes(runner: Runner, phase: str) -> None:
    try:
        db_bytes = runner.db.rows(pg.export_queries(runner.t0())["database"])[0][
            "bytes"
        ]
    except (RuntimeError, IndexError, KeyError) as exc:
        db_bytes = None
        runner.event("error", loop="sizes", error=str(exc)[:300])
    df = run(["docker", "system", "df", "--format", "{{json .}}"], timeout=120)
    runner.event(
        "sizes",
        phase=phase,
        db_bytes=db_bytes,
        docker_df=[line for line in df.stdout.splitlines() if line.strip()],
    )


def cmd_plan(args: argparse.Namespace) -> int:
    print(soak_plan.freeze(args.data / "plan.json"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    runner = _runner(args.data)
    started = not any(e.get("kind") == "start" for e in read_jsonl(runner.events_path))
    runner.t0()
    if started:
        _sizes(runner, "start")
    runner.event("runner_started", pid_note="restart-safe")

    def interrupted() -> float:
        return soak_summarize.interruption_seconds(
            list(read_jsonl(runner.events_path)),
            list(read_jsonl(runner.data / "probe.jsonl")),
        )

    runner.run_until_end(interrupted)
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    out = args.data / f"resources-{args.host}.jsonl"
    until = parse_time(args.until) if args.until else None
    while until is None or utcnow() < until:
        append_jsonl(out, resource_sample(args.host, utcnow()))
        time.sleep(args.interval)
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    start, end = parse_time(args.start), parse_time(args.end)
    if end <= start:
        raise SystemExit("end must be after start")
    append_jsonl(
        args.data / "events.jsonl",
        {
            "at": iso(utcnow()),
            "kind": "interruption",
            "start": iso(start),
            "end": iso(end),
            "reason": args.reason,
        },
    )
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    runner = _runner(args.data)
    ended = [e for e in read_jsonl(runner.events_path) if e.get("kind") == "end"]
    if not ended and not args.force:
        raise SystemExit("the runner has not ended; --force exports anyway")
    t0 = runner.t0()
    db = Database(runner.config.compose, env=runner.config.env)
    export: dict[str, object] = {"exported_at": iso(utcnow()), "t0": iso(t0)}
    for name, sql in pg.export_queries(t0).items():
        export[name] = db.rows(sql, timeout=300)
    (args.data / "pg.json").write_text(
        json.dumps(export, indent=1, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _sizes(runner, "end")
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
    runner.event("finish", pg_dump_exit=proc.returncode, pg_json="pg.json")
    return 0 if proc.returncode == 0 else 1


def cmd_summarize(args: argparse.Namespace) -> int:
    summary = soak_summarize.write(args.data, args.out)
    print(json.dumps(summary["step4"], indent=1, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.soak", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("plan", cmd_plan), ("run", cmd_run), ("finish", cmd_finish)):
        p = sub.add_parser(name)
        p.add_argument("--data", type=Path, required=True)
        p.set_defaults(func=func)
        if name == "finish":
            p.add_argument("--force", action="store_true")
    sample = sub.add_parser("sample")
    sample.add_argument("--data", type=Path, required=True)
    sample.add_argument("--host", required=True)
    sample.add_argument("--interval", type=float, default=60)
    sample.add_argument("--until")
    sample.set_defaults(func=cmd_sample)
    note = sub.add_parser("note-interruption")
    note.add_argument("--data", type=Path, required=True)
    note.add_argument("--start", required=True)
    note.add_argument("--end", required=True)
    note.add_argument("--reason", required=True)
    note.set_defaults(func=cmd_note)
    summ = sub.add_parser("summarize")
    summ.add_argument("--data", type=Path, required=True)
    summ.add_argument("--out", type=Path, required=True)
    summ.set_defaults(func=cmd_summarize)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
