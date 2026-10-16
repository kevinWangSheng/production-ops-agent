"""Freeze aggregate audit evidence; DSN is read from an environment variable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opspilot.acceptance_audit import SOURCES, AuditTimeline, audit_records


def timeline_summary(timeline: AuditTimeline) -> dict[str, Any]:
    entries = [
        {
            "recorded_at": entry.recorded_at.isoformat(),
            "sequence": entry.sequence,
            "category": entry.category,
            "source": entry.source,
            "record_id": entry.record_id,
            "run_id": None if entry.run_id is None else str(entry.run_id),
            "actor": entry.actor,
            "detail": dict(entry.detail),
        }
        for entry in timeline.entries
    ]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "incident_id": str(timeline.incident_id),
        "counts": dict(timeline.counts),
        "entry_count": len(entries),
        "first_at": entries[0]["recorded_at"] if entries else None,
        "last_at": entries[-1]["recorded_at"] if entries else None,
        "missing": list(timeline.missing),
        "entries_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def freeze(dsn: str, incident_ids: list[UUID]) -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout
    )
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
        # SET is first: every read shares a stable, enforced readonly snapshot.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET TRANSACTION READ ONLY")
        summaries = []
        for incident in incident_ids:
            timeline, undated = audit_records(conn, incident)
            summary = timeline_summary(timeline)
            summary["undated_records"] = dict(undated)
            summary["missing_sources"] = [
                source
                for source in SOURCES
                if not any(e.source == source for e in timeline.entries)
            ]
            summary["associated_time_entries"] = sum(
                e.detail.get("time_basis")
                in ("step_recorded", "subject_event", "tool_event", "control_recorded")
                for e in timeline.entries
            )
            summary["step_time_is_plan_commit_not_dispatch_or_return"] = True
            summaries.append(summary)
    return {
        "frozen_at": datetime.now(UTC).isoformat(),
        "execution_commit": commit,
        "worktree_dirty": dirty,
        "incidents": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incident_ids", nargs="+", type=UUID)
    parser.add_argument("--dsn-env", default="OPSPILOT_LAB_DSN")
    parser.add_argument(
        "--out", type=Path, required=True, help="destination summary.json"
    )
    args = parser.parse_args(argv)
    try:
        report = freeze(os.environ[args.dsn_env], args.incident_ids)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    except (KeyError, LookupError, psycopg.Error, OSError, subprocess.SubprocessError):
        # Database exceptions can include DSNs or row values.
        print("audit rebuild failed; no summary published", file=sys.stderr)
        return 1
    print(json.dumps({"summary": str(args.out), "incidents": len(args.incident_ids)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
