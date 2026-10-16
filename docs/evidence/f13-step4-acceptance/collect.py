"""Engineer-side freeze for the F13 step 4 acceptance runs (copied from the
m1-clean-rerun collector: experiment name, round, artifacts and output
directory changed; not product code).

Every product step went through the product's own processes: the incident
came from the real Alertmanager webhook, the investigation was the resident
worker with a real model, the observation was authorized by the web
process's automatic loop (no human control), the samples were taken by the
separately started Observer. This script only reads, after the fact:

    collect.py --scenario S1|S2 --incident <uuid> --owner-dsn ... --observer-dsn ...
               --web http://127.0.0.1:8080 --user-file <name:password>
               --experiment-id <kind fault id> --project-file <pre.json>
               --executed-commit <sha the product processes ran>
               [--verdicts <verify.py output>] [--timeline <watch log>]

Owner reads run in a read-only session; the Observer login is used only to
measure its own privileges. The raw ledger (record dumps, page HTML) goes to
the ignored ``tmp/lab-ledgers`` via ``lab_evidence.freeze``; the summary
keeps hashes and the fields the verdicts need (ADR-0006). The UI password is
read from ``--user-file`` and never printed.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from opspilot.acceptance import IncidentScenario, outcome_from_durable  # noqa: E402
from opspilot.acceptance_recovery import RecoveryRecords, recovery_outcome  # noqa: E402
from opspilot.observation import ObservationStore  # noqa: E402
from opspilot.observer.replay import replay_history  # noqa: E402
from opspilot.persistence import DurableStore  # noqa: E402
from scripts.kind_lab import OPENER  # noqa: E402  (no proxy, no redirects)
from scripts.lab_evidence import freeze, langsmith_client, run_url  # noqa: E402

LAB_HISTORY = ROOT.parent / "production-ops-agent/tmp/m1-kind-lab/engineer-only"
PAGE_STATEMENT = "recovered, no remediation recorded"
PAGE_PHRASES = (PAGE_STATEMENT, "recovery confirmed")


def jd(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return json.loads(json.dumps(value, default=_default))


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def _rows(conn: psycopg.Connection, sql: str, args: tuple) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _page(web: str, incident: UUID, user_file: Path) -> tuple[int, str]:
    auth = base64.b64encode(user_file.read_text().strip().encode()).decode()
    request = urllib.request.Request(
        f"{web}/incidents/{incident}", headers={"Authorization": f"Basic {auth}"}
    )
    with OPENER.open(request, timeout=30) as response:
        return response.status, response.read().decode()


def _fault_log(experiment_id: str) -> list[dict]:
    path = LAB_HISTORY / experiment_id / "fault-log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _digest(path: Path) -> dict:
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


TEXT_EXCERPT = 1500


def _bounded(actual: Any) -> Any:
    """What a verdict was judged on, kept inspectable in the summary.

    Bulky values are projected, not dropped (bot review P1 on #217): page
    text keeps its leading excerpt, whether it carries the recovery
    statement, and the hash of the full text; per-sample readings keep one
    verdict per signal. The full value stays in the ledger.
    """
    if isinstance(actual, str) and len(actual) > TEXT_EXCERPT:
        data = actual.encode()
        return {
            "excerpt": actual[:TEXT_EXCERPT],
            # every phrase whose presence or absence a verify.py page
            # predicate depends on (S1-4, S2-8; bot review P2 on #217)
            "contains": {phrase: phrase in actual for phrase in PAGE_PHRASES},
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    if isinstance(actual, dict):
        if isinstance(actual.get("readings"), list):
            actual = actual | {
                "readings": {
                    r.get("signal_name"): r.get("verdict", r.get("status"))
                    for r in actual["readings"]
                    if isinstance(r, dict)
                }
            }
        return {key: _bounded(value) for key, value in actual.items()}
    if isinstance(actual, list):
        return [_bounded(value) for value in actual]
    return actual


def _compact(verdicts: dict) -> dict:
    """verify.py output with every ``actual`` bounded by ``_bounded``."""
    items = []
    for item in verdicts["verdicts"]:
        blob = json.dumps(item["actual"], sort_keys=True).encode()
        bounded = _bounded(item["actual"])
        if bounded != item["actual"]:
            bounded = {
                "projection": bounded,
                "full_in_ledger": {
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "bytes": len(blob),
                },
            }
        items.append(item | {"actual": bounded})
    return verdicts | {"verdicts": items}


def _traces(project: dict, run_ids: set[str]) -> list[dict]:
    os.environ["LANGSMITH_PROJECT"] = project["project"]
    client = langsmith_client()
    traces = []
    for root in client.list_runs(project_name=project["project"], is_root=True):
        metadata = (root.extra or {}).get("metadata", {})
        business = {str(v) for k, v in metadata.items() if k.endswith("run_id")}
        if run_ids and not business & run_ids:
            continue
        kinds: dict[str, int] = {}
        for child in client.list_runs(
            project_name=project["project"], trace_id=root.trace_id, is_root=False
        ):
            kinds[child.run_type] = kinds.get(child.run_type, 0) + 1
        traces.append(
            {
                "name": root.name,
                "langsmith_run_id": str(root.id),
                "otel_trace_id": metadata.get("OTEL_TRACE_ID"),
                "business_run_ids": sorted(business & run_ids),
                "url": run_url(
                    client,
                    project_id=project["project_id"],
                    langsmith_run_id=str(root.id),
                ),
                "read_back": "found",
                "start_time": str(root.start_time),
                "end_time": str(root.end_time),
                "status": root.status,
                "prompt_tokens": root.prompt_tokens,
                "completion_tokens": root.completion_tokens,
                "child_runs": kinds,
            }
        )
    traces.sort(key=lambda t: t["start_time"])
    return traces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("S1", "S2"), required=True)
    parser.add_argument("--incident", type=UUID, required=True)
    parser.add_argument("--owner-dsn", required=True)
    parser.add_argument("--observer-dsn", required=True)
    parser.add_argument("--web", required=True)
    parser.add_argument("--user-file", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--project-file", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--verdicts", type=Path)
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--balance-file", type=Path)
    # the commit whose product processes ran the experiment, recorded when
    # the run started; HEAD at freeze time is only the collector's checkout
    # (bot review P2 on #217)
    parser.add_argument("--executed-commit", required=True)
    # phase B (#238/#246 runs) freezes beside phase A under its own directory
    parser.add_argument("--evidence-subdir", default="run")
    args = parser.parse_args(argv)
    incident = args.incident

    with psycopg.connect(args.owner_dsn, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        incident_row = _rows(
            conn,
            "SELECT incident_id, lifecycle, state, mode, control_generation,"
            " observation_generation, current_run_id, target_id, created_at,"
            " conclusion IS NOT NULL AS has_conclusion"
            " FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        )[0]
        runs = _rows(
            conn,
            "SELECT run_id, state, control_generation, budget_spent,"
            " tool_operations_used, tool_seconds_used, versions, deadline"
            " FROM opspilot_runs WHERE incident_id=%s ORDER BY deadline",
            (incident,),
        )
        deliveries = _rows(
            conn,
            "SELECT delivery_id, status, outcome, actor, fingerprint, starts_at,"
            " received_at, incident_id FROM opspilot_alert_deliveries"
            " WHERE incident_id=%s ORDER BY delivery_id",
            (incident,),
        )
        decisions = _rows(
            conn,
            "SELECT * FROM opspilot_self_heal_authorizations"
            " WHERE incident_id=%s ORDER BY control_generation",
            (incident,),
        )
        postmortem_attempts = _rows(
            conn,
            "SELECT attempt_id, status, version, model, model_requests,"
            " prompt_tokens, completion_tokens, started_at, finished_at"
            " FROM opspilot_postmortem_generation_attempts"
            " WHERE incident_id=%s ORDER BY started_at",
            (incident,),
        )
        conclusion = conn.execute(
            "SELECT conclusion FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()[0]
        schema_head = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]

    store = DurableStore(args.owner_dsn)
    try:
        snapshot = store.rebuild(incident)
    finally:
        store.close()
    investigation = outcome_from_durable(
        IncidentScenario(
            f"f13-step4-acceptance-{args.evidence_subdir}",
            "F6",
            "f13-step4-acceptance",
            "alertmanager_live",
            str(incident),
        ),
        snapshot,
    )

    controller = ObservationStore(args.owner_dsn)
    observer = ObservationStore(args.observer_dsn)
    try:
        records = controller.incident_records(incident)
        grants = observer.table_privileges()
    finally:
        controller.close()
        observer.close()
    replays = [replay_history(h) for h in records["sessions"]]
    recovery = recovery_outcome(
        SimpleNamespace(
            scenario_id=f"f13-step4-acceptance-{args.evidence_subdir}",
            subject_id=str(incident),
        ),
        RecoveryRecords(
            incident=records["incident"],
            sessions=tuple(records["sessions"]),
            controls=tuple(records["controls"]),
            grants=grants,
        ),
    )

    status, html = _page(args.web, incident, args.user_file)
    project = json.loads(args.project_file.read_text())
    run_ids = {str(r["run_id"]) for r in runs}
    traces = _traces(project, run_ids)

    report_content = (conclusion or {}).get("conclusion", {}).get("report_content")
    try:
        report = json.loads(report_content) if report_content else None
    except ValueError:
        report = None
    sessions = []
    for history in records["sessions"]:
        s = history["session"]
        sessions.append(
            {
                "session_id": str(s["session_id"]),
                "authorization_source": s["authorization_source"],
                "authorized_by": s["authorized_by"],
                "target": jd(s["target"]),
                "state": s["state"],
                "ended_reason": s["ended_reason"],
                "authorized_at": jd(s["authorized_at"]),
                "deadline_at": jd(s["deadline_at"]),
                "max_samples": s["max_samples"],
                "sample_interval_seconds": s["sample_interval_seconds"],
                "sustained_window_seconds": s["sustained_window_seconds"],
                "adopted_count": s["adopted_count"],
                "healthy_since": jd(s["healthy_since"]),
                "health_profile_revision": s["health_profile_revision"],
                "samples": [
                    {
                        "sequence": x["sequence"],
                        "window_end": jd(x["window_end"]),
                        "outcome": x["outcome"],
                        "disposition": x["disposition"],
                        "health_basis": x["health_basis"],
                        "transition": x["transition"],
                        "readings": {
                            r["signal_name"]: [
                                r["status"],
                                r["value"],
                                r["sample_count"],
                            ]
                            for r in x["readings"]
                        },
                    }
                    for x in history["samples"]
                ],
                "endings": jd(history["endings"]),
            }
        )
    rec = jd(recovery)
    profile_content = rec.pop("recovery_profile_content", None)
    rec["recovery_profile_content_sha256"] = (
        None
        if profile_content is None
        else hashlib.sha256(profile_content.encode()).hexdigest()
    )
    # the per-sample projection and replay detail stays in the ledger; the
    # summary keeps its size and hash (ADR-0006: summary, not raw records)
    for key in ("recovery_samples", "replay", "action_events"):
        if key in rec:
            blob = json.dumps(rec.pop(key), sort_keys=True).encode()
            rec[f"{key}_digest"] = {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
            }
    full_verdicts = json.loads(args.verdicts.read_text()) if args.verdicts else None
    verdicts = None if full_verdicts is None else _compact(full_verdicts)
    balance = json.loads(args.balance_file.read_text()) if args.balance_file else None

    summary = {
        "round": "f13-step4-20261016",
        "scenario": args.scenario,
        "versions": {
            "collector_branch": subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True, cwd=ROOT
            ).strip(),
            "executed_commit": args.executed_commit,
            "collector_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True, cwd=ROOT
            ).strip(),
            "schema_head": schema_head,
            "worker_versions": jd(runs[-1]["versions"]) if runs else None,
            "tool_profile": "otel-demo",
            "otel_demo_chart": "0.37.8",
        },
        "health_profile": {
            "path": str(args.profile.relative_to(ROOT))
            if args.profile.is_relative_to(ROOT)
            else args.profile.name,
            "sha256": hashlib.sha256(args.profile.read_bytes()).hexdigest(),
            "session": json.loads(args.profile.read_text()).get("session"),
        },
        "lab_actions": _fault_log(args.experiment_id),
        "deliveries": jd(deliveries),
        "incident": jd(incident_row),
        "investigation": {
            "runs": jd(runs),
            "decision": investigation.decision,
            "handoff_reasons": jd(investigation.handoff_reasons),
            "evidence_ids": len(investigation.evidence_ids),
            "report": None
            if not isinstance(report, dict)
            else {
                k: report.get(k) for k in ("assessment_status", "conclusion", "summary")
            },
        },
        "automatic_decisions": jd(decisions),
        "controls": jd(records["controls"]),
        "sessions": sessions,
        "replay": [
            {
                "session_id": str(r.session_id),
                "consistent": r.consistent,
                "recovery_verdict": r.recovery_verdict,
                "recomputed_verdict": r.recomputed_verdict,
                "healthy_window_seconds": r.healthy_window_seconds,
            }
            for r in replays
        ],
        "recovery_outcome": rec,
        "workbench_page": {
            "status": status,
            "bytes": len(html.encode()),
            "sha256": hashlib.sha256(html.encode()).hexdigest(),
            "shows_recovered_no_remediation": PAGE_STATEMENT in html,
            "shows_source_automatic": "automatic" in html,
        },
        "verdicts": verdicts,
        # the watch log stays outside the repository; run.md carries the
        # key events, the summary its hash (ADR-0006, review P2)
        "timeline": None if args.timeline is None else _digest(args.timeline),
        "postmortem_attempts": jd(postmortem_attempts),
        "artifacts": {
            name: _digest(ROOT / name)
            for name in (
                "docs/evidence/f13-step4-acceptance/collect.py",
                "docs/evidence/f13-step4-acceptance/knowledge_use.py",
                "docs/evidence/f13-step4-acceptance/workbench.py",
                "opspilot/observer/profiles/otel-demo-checkout.json",
            )
        }
        | {
            "git_diff_head_sha256": hashlib.sha256(
                subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)
            ).hexdigest(),
        },
        "trace": {"project": project, "root_runs_found": len(traces), "traces": traces},
        "cost": balance,
    }
    ledger = {
        "summary": summary,
        "snapshot": jd(snapshot),
        "incident_records": jd(records),
        "recovery_outcome": jd(recovery),
        "observer_grants": jd(grants),
        "verdicts": full_verdicts,
        "page_html": html,
        "timeline": None if args.timeline is None else args.timeline.read_text(),
    }
    run_id = str(runs[-1]["run_id"])
    _, path = freeze(
        ledger,
        experiment="f13-step4-acceptance",
        run_id=run_id,
        evidence_dir=ROOT / "docs/evidence/f13-step4-acceptance" / args.evidence_subdir,
        summary=summary,
    )
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
