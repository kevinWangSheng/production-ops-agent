"""Read-only collector for F3 step 5 frozen evidence (PR-3b).

Builds one ``summary.json`` per Run from the lab PostgreSQL (read-only
snapshot transaction) and the driver ledger of that Run. Engineer-only
evidence script, not product code; it writes nothing to the database.

Usage (repository root)::

    F3_LAB_DSN=<read-only DSN> .venv/bin/python \\
        docs/evidence/f3-step5-competing-cause/collect.py \\
        --incident <uuid> --ledger <driver ledger dir> \\
        [--lab-readings <json file>] --out <summary.json>

The DSN is read from the environment and never written. The output holds no
credentials and no host paths.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from opspilot.acceptance import IncidentScenario, outcome_from_durable  # noqa: E402
from opspilot.acceptance_audit import audit_records  # noqa: E402
from opspilot.acceptance_investigation import investigation_evidence  # noqa: E402
from opspilot.persistence import DurableStore  # noqa: E402
from opspilot.tools.otel_demo import otel_demo_versions  # noqa: E402
from opspilot.web import DurableEventLog, DurableIncidentStore  # noqa: E402

SCENARIO_ID = "F3:competing-cause"


def plain(value: Any) -> Any:
    """Dataclasses, mappings, tuples, datetimes and UUIDs as plain JSON."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        raise TypeError("raw bytes are not frozen into evidence")
    return value


def relabel(value: Any) -> Any:
    # Evidence names idempotency keys *_label (gitleaks false positives).
    if isinstance(value, dict):
        return {
            ("idempotency_label" if k == "idempotency_key" else k): relabel(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [relabel(v) for v in value]
    return value


def report_of(steps: list[Mapping[str, Any]]) -> Any:
    for step in reversed(steps):
        response = step.get("response") or {}
        if response.get("kind") == "conclusion":
            content = response["conclusion"].get("report_content")
            try:
                return json.loads(content) if isinstance(content, str) else content
            except ValueError:
                return content
    return None


def collect(dsn: str, incident: UUID, ledger: Path) -> dict[str, Any]:
    plan = json.loads((ledger / "plan.json").read_text())
    frozen = {k: plan["frozen"][k] for k in ("rubric_sha256", "stimulus_sha256", "answer_sha256")}

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET TRANSACTION READ ONLY")
        evidence = investigation_evidence(conn, incident)
        timeline, _ = audit_records(conn, incident)
        runs = conn.execute(
            "SELECT run_id, control_generation, state, input FROM opspilot_runs"
            " WHERE incident_id=%s ORDER BY run_id",
            (incident,),
        ).fetchall()
        deliveries = conn.execute(
            "SELECT delivery_id, fingerprint, status, outcome, starts_at, received_at"
            " FROM opspilot_alert_deliveries WHERE incident_id=%s ORDER BY delivery_id",
            (incident,),
        ).fetchall()
        steps = conn.execute(
            "SELECT s.run_id, s.sequence, s.response FROM opspilot_steps s"
            " JOIN opspilot_runs r ON r.run_id=s.run_id WHERE r.incident_id=%s"
            " ORDER BY s.run_id, s.sequence",
            (incident,),
        ).fetchall()
        report = report_of(steps)
        cited_ids = sorted(
            {e for c in (report or {}).get("claims", []) for e in c.get("evidence_ids", [])}
        )
        cited_rows = conn.execute(
            "SELECT evidence_id, run_id, subject_id, status, view FROM opspilot_evidence"
            " WHERE evidence_id = ANY(%s)",
            (cited_ids,),
        ).fetchall()
        db_now = conn.execute("SELECT now() AS now").fetchone()["now"]

    store = DurableStore(dsn)
    try:
        snapshot = store.rebuild(incident)
        controls = tuple(
            dataclasses.asdict(row) for row in DurableIncidentStore(store).control_audit(incident)
        )
        handoffs = tuple(
            dict(event.payload)
            for event in DurableEventLog(store).read_after(incident, 0, limit=1000)
            if event.kind == "run_handoff"
        )
        scenario = IncidentScenario(
            scenario_id=SCENARIO_ID,
            feature_id="F3",
            acceptance_step="5",
            kind="incident",
            subject_id=str(incident),
        )
        outcome = outcome_from_durable(
            scenario, snapshot, controls=controls, handoff_events=handoffs
        )
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()

    if len(runs) != 1:
        raise SystemExit(f"expected exactly one Run, found {len(runs)}")
    run = runs[0]
    policy = run["input"]["evidence_context"]["time_policies"][0]
    block = {
        "scenario_id": SCENARIO_ID,
        "incident_id": str(incident),
        "run_id": str(run["run_id"]),
        "bound_target_id": run["input"]["bound_target_id"],
        "authorized_window": dict(policy["window"]),
        "outcome": plain(outcome),
        "report": report,
        "investigation_evidence": plain(evidence),
        "cited_evidence": {
            r["evidence_id"]: {
                "run_id": r["run_id"],
                "subject_id": r["subject_id"],
                "status": r["status"],
                "view": r["view"],
            }
            for r in cited_rows
        },
        "audit_queries": [
            {"run_id": None if e.run_id is None else str(e.run_id), **plain(e.detail)}
            for e in timeline.entries
            if e.category == "query"
        ],
        "incident_runs": [
            {"run_id": str(r["run_id"]), "control_generation": r["control_generation"], "state": r["state"]}
            for r in runs
        ],
        "alert_delivery_outcomes": plain([dict(d) for d in deliveries]),
        "run_versions": otel_demo_versions(),
        "collected_at_db": plain(db_now),
    }
    return relabel({"frozen": frozen, "f3_acceptance": block})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident", type=UUID, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--lab-readings", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = collect(os.environ["F3_LAB_DSN"], args.incident, args.ledger)
    if args.lab_readings:
        summary["lab_readings"] = json.loads(args.lab_readings.read_text())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"out": str(args.out), "run_id": summary["f3_acceptance"]["run_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
