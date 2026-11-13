"""Freeze one given Run: ``summary.json`` in the evidence directory, trace links.

Engineer-side evidence script (ADR-0006), not product code; shared by the
ITBench-Lite driver (#604, decision L-D13) and meant for the A7 driver too.
Given a Run id and a DSN that can read the product tables it:

* reads, in one ``REPEATABLE READ``/``READ ONLY`` transaction, the Run row,
  its incident, steps, alert deliveries, the F3 projection
  (``acceptance_investigation.investigation_evidence``) and the audit
  timeline (``acceptance_audit.audit_records``);
* freezes them through ``lab_evidence.freeze``: the raw rows go to the
  ignored ledger directory, ``summary.json`` keeps the report, the projection
  of this Run, its tool requests, the versions it was recorded under, token
  usage, alert deliveries and the trace block;
* reads back the LangSmith root runs whose metadata ``run_id`` is this Run
  (best effort, bounded polling, recorded as found or not, never invented).

The DSN is read from the environment (``RUN_FREEZE_DSN`` for the CLI) and
never written; ``idempotency_key`` fields are relabelled ``idempotency_label``
(gitleaks false positives on evidence). Usage::

    RUN_FREEZE_DSN=<dsn> .venv/bin/python -m scripts.run_freeze \\
        --run-id <uuid> --experiment <name> --evidence-dir <dir> \\
        [--project opspilot-lab-<round>]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from scripts import lab_evidence

DSN_ENV = "RUN_FREEZE_DSN"
#: LangSmith ingests asynchronously; same bound as ``lab_evidence``.
READ_BACK_ATTEMPTS = lab_evidence.READ_BACK_ATTEMPTS
READ_BACK_DELAY_S = lab_evidence.READ_BACK_DELAY_S


def plain(value: Any) -> Any:
    """Dataclasses, mappings, tuples, datetimes and UUIDs as plain JSON."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        raise TypeError("raw bytes are not frozen into evidence")
    return value


def relabel(value: Any) -> Any:
    """``idempotency_key`` -> ``idempotency_label`` at any depth."""
    if isinstance(value, dict):
        return {
            ("idempotency_label" if k == "idempotency_key" else k): relabel(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [relabel(v) for v in value]
    return value


def report_of(steps: list[Mapping[str, Any]]) -> tuple[Any, Mapping[str, Any] | None]:
    """The last conclusion step's ``report_content`` (parsed) and that step."""
    for step in reversed(steps):
        response = step.get("response") or {}
        if isinstance(response, Mapping) and response.get("kind") == "conclusion":
            conclusion = response.get("conclusion") or {}
            content = conclusion.get("report_content")
            return lab_evidence.parse_report(content), step
    return None, None


_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


def token_usage(steps: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Provider ``usage`` summed over the steps that carry one."""
    totals = dict.fromkeys(_TOKEN_KEYS, 0)
    rounds = 0
    for step in steps:
        response = step.get("response") or {}
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        rounds += 1
        for key in _TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return {"model_rounds_with_usage": rounds, **totals}


def _response_kind(response: Any) -> Any:
    return response.get("kind") if isinstance(response, Mapping) else None


def cited_evidence_ids(report: Any) -> list[str]:
    """Evidence ids the report's claims cite; empty for a non-object report
    (a text that is not JSON, or None)."""
    if not isinstance(report, Mapping):
        return []
    claims = report.get("claims")
    if not isinstance(claims, list):
        return []
    return sorted(
        {
            eid
            for claim in claims
            if isinstance(claim, Mapping)
            for eid in (claim.get("evidence_ids") or [])
            if isinstance(eid, str)
        }
    )


def collect_run(conn: Any, run_id: UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(summary block, raw ledger)`` of one Run; ``conn`` is a psycopg
    connection the caller opened, used here for SELECTs only.

    Field set follows the F3 collector
    (``docs/evidence/f3-step5-competing-cause/collect.py``): the incident's
    full projection, the report with the views of the evidence it cites, the
    incident's Runs, the full audit timeline and the alert deliveries. The
    durable ``outcome`` needs a store of its own and is added by ``collect``.
    """
    from psycopg.rows import dict_row

    from opspilot.acceptance_audit import audit_records
    from opspilot.acceptance_investigation import investigation_evidence

    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    conn.execute("SET TRANSACTION READ ONLY")
    with conn.cursor(row_factory=dict_row) as cursor:
        run = cursor.execute(
            "SELECT run_id,incident_id,state,control_generation,budget_limit,"
            "budget_reserved,budget_spent,budget_unknown,deadline,versions,input"
            " FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        if run is None:
            raise LookupError(f"run {run_id} not found")
        incident_id = run["incident_id"]
        incident = cursor.execute(
            "SELECT incident_id,target_id,lifecycle,state,mode,control_generation,"
            "current_run_id,created_at FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
        runs = cursor.execute(
            "SELECT run_id,control_generation,state FROM opspilot_runs"
            " WHERE incident_id=%s ORDER BY control_generation,run_id",
            (incident_id,),
        ).fetchall()
        steps = cursor.execute(
            "SELECT step_id,sequence,logical_key,status,control_generation,"
            "observed_at,response,tool_results FROM opspilot_steps"
            " WHERE run_id=%s ORDER BY sequence,step_id",
            (run_id,),
        ).fetchall()
        deliveries = cursor.execute(
            "SELECT delivery_id,fingerprint,status,outcome,starts_at,received_at"
            " FROM opspilot_alert_deliveries WHERE incident_id=%s"
            " ORDER BY delivery_id",
            (incident_id,),
        ).fetchall()
        report, conclusion = report_of(steps)
        cited = cited_evidence_ids(report)
        cited_rows = cursor.execute(
            "SELECT evidence_id,run_id,subject_id,status,view FROM opspilot_evidence"
            " WHERE evidence_id = ANY(%s)",
            (cited,),
        ).fetchall()
        db_now = cursor.execute("SELECT now() AS now").fetchone()["now"]
    evidence = investigation_evidence(conn, incident_id)
    timeline, undated = audit_records(conn, incident_id)

    run_key = str(run_id)
    observations = [o for o in evidence.observations if o.run_id == run_key]
    by_id = {o.evidence_id: o for o in evidence.observations}
    views = {str(r["evidence_id"]): r for r in cited_rows}
    run_input = run["input"] if isinstance(run["input"], Mapping) else {}
    policies = (run_input.get("evidence_context") or {}).get("time_policies") or []
    window = policies[0].get("window") if policies else None
    block = {
        "run_id": run_key,
        "incident_id": str(incident_id),
        "run": {
            "state": run["state"],
            "control_generation": run["control_generation"],
            "budget_limit": run["budget_limit"],
            "model_requests_used": run["budget_reserved"]
            + run["budget_spent"]
            + run["budget_unknown"],
            "deadline": plain(run["deadline"]),
            "bound_target_id": run_input.get("bound_target_id"),
            "question": run_input.get("question"),
            "authorized_window": plain(window),
        },
        "incident": plain(dict(incident)) if incident else None,
        "incident_runs": plain([dict(r) for r in runs]),
        # The versions the Run row persisted, never the collector checkout's.
        "versions": plain(run["versions"]),
        "report": report,
        "report_present": report is not None,
        "conclusion_step": None
        if conclusion is None
        else {
            "sequence": conclusion["sequence"],
            "logical_key": conclusion["logical_key"],
            "status": conclusion["status"],
            "observed_at": plain(conclusion["observed_at"]),
        },
        "steps": [
            {
                "sequence": s["sequence"],
                "logical_key": s["logical_key"],
                "status": s["status"],
                "observed_at": plain(s["observed_at"]),
                "kind": _response_kind(s["response"]),
            }
            for s in steps
        ],
        "tokens": token_usage(steps),
        "tool_requests": [
            {"run_id": None if e.run_id is None else str(e.run_id), **plain(e.detail)}
            for e in timeline.entries
            if e.category == "query" and str(e.run_id) == run_key
        ],
        "observations": plain(observations),
        # The incident's whole F3 projection: observations, refusals, report
        # claims with their evidence references, per-Run coverage.
        "investigation_evidence": plain(evidence),
        # Every cited id with the tool that produced it (A7's category rule
        # follows these) and the stored view, as F3 froze them.
        "cited_evidence": [
            {
                "evidence_id": eid,
                "resolvable": eid in by_id,
                "tool": by_id[eid].tool if eid in by_id else None,
                "status": by_id[eid].status if eid in by_id else None,
                "stored": None
                if eid not in views
                else {
                    "run_id": str(views[eid]["run_id"]),
                    "subject_id": plain(views[eid]["subject_id"]),
                    "status": views[eid]["status"],
                    "view": plain(views[eid]["view"]),
                },
            }
            for eid in cited
        ],
        "audit": plain(timeline),
        "audit_undated": plain(undated),
        "alert_delivery_outcomes": [d["outcome"] for d in deliveries],
        "alert_deliveries": plain([dict(d) for d in deliveries]),
        "collected_at_db": plain(db_now),
    }
    ledger = {
        "run": plain(dict(run)),
        "incident": plain(dict(incident)) if incident else None,
        "steps": plain([dict(s) for s in steps]),
        "alert_deliveries": plain([dict(d) for d in deliveries]),
        "audit": plain(timeline),
    }
    return relabel(block), relabel(ledger)


def durable_outcome(dsn: str, incident_id: UUID, *, scenario_id: str) -> Any:
    """The incident's ``IncidentOutcome`` from the durable rows (F3 collector)."""
    from opspilot.acceptance import IncidentScenario, outcome_from_durable
    from opspilot.persistence import DurableStore
    from opspilot.web import DurableEventLog, DurableIncidentStore

    store = DurableStore(dsn)
    try:
        snapshot = store.rebuild(incident_id)
        controls = tuple(
            dataclasses.asdict(row)
            for row in DurableIncidentStore(store).control_audit(incident_id)
        )
        handoffs = tuple(
            dict(event.payload)
            for event in DurableEventLog(store).read_after(incident_id, 0, limit=1000)
            if event.kind == "run_handoff"
        )
        scenario = IncidentScenario(
            scenario_id=scenario_id,
            feature_id="lab",
            acceptance_step="frozen-run",
            kind="incident",
            subject_id=str(incident_id),
        )
        return outcome_from_durable(
            scenario, snapshot, controls=controls, handoff_events=handoffs
        )
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def collect(
    dsn: str,
    run_id: UUID | str,
    *,
    connect: Callable[[str], Any] | None = None,
    scenario_id: str = "frozen-run",
    outcome: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``collect_run`` over a fresh connection to ``dsn``, plus the durable
    outcome of the incident."""
    if connect is None:
        import psycopg

        def connect(value: str) -> Any:
            return psycopg.connect(value, connect_timeout=5)

    with connect(dsn) as conn:
        block, ledger = collect_run(conn, UUID(str(run_id)))
    reader = outcome or durable_outcome
    try:
        value = plain(reader(dsn, UUID(block["incident_id"]), scenario_id=scenario_id))
    except ValueError as error:
        # A Run frozen before a final state (a timed-out wait) has no
        # durable outcome yet; recorded, never invented.
        value = {"available": False, "reason": str(error)}
    block["outcome"] = relabel(value)
    return block, ledger


def flat_evidence(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    """This Run's observations in the shape ``answer.convert`` reads (L-D12)."""
    return [
        {
            "evidence_id": o["evidence_id"],
            "tool": o.get("tool"),
            "query": o.get("query"),
        }
        for o in block.get("observations") or []
        if isinstance(o, Mapping) and isinstance(o.get("evidence_id"), str)
    ]


def trace_roots(
    project: str | None,
    run_id: str,
    *,
    client_factory: Callable[[], Any],
    attempts: int = READ_BACK_ATTEMPTS,
    delay_seconds: float = READ_BACK_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """The LangSmith root runs whose metadata ``run_id`` is this Run.

    Every attempt of a Run is its own root with the same business ``run_id``,
    so all of them are listed. Polls until at least one appears; a project of
    ``None`` means tracing was off. Errors are recorded, never raised.
    """
    if not project:
        return {"project": None, "read_back": "not_exported", "roots": []}
    try:
        client = client_factory()
        session = client.read_project(project_name=project)
        roots: list[Any] = []
        for attempt in range(attempts):
            roots = list(
                client.list_runs(
                    project_name=project,
                    is_root=True,
                    filter=(
                        'and(eq(metadata_key, "run_id"), '
                        f'eq(metadata_value, "{run_id}"))'
                    ),
                    limit=20,
                )
            )
            if roots:
                break
            if attempt + 1 < attempts:
                sleep(delay_seconds)
        entries = [
            {
                "langsmith_run_id": str(root.id),
                "start_time": plain(getattr(root, "start_time", None)),
                "end_time": plain(getattr(root, "end_time", None)),
                "url": lab_evidence.run_url(
                    client, project_id=str(session.id), langsmith_run_id=str(root.id)
                ),
            }
            for root in roots
        ]
        return {
            "project": project,
            "project_id": str(session.id),
            "trace_tier": getattr(session, "trace_tier", None),
            "read_back": "found" if entries else "root_run_not_found",
            "roots": entries,
        }
    except Exception as exc:  # noqa: BLE001 - recorded, the summary still freezes
        return {
            "project": project,
            "read_back": f"error:{type(exc).__name__}",
            "roots": [],
        }


def freeze_run(
    dsn: str,
    run_id: UUID | str,
    *,
    experiment: str,
    evidence_dir: Path,
    project: str | None = None,
    client_factory: Callable[[], Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    connect: Callable[[str], Any] | None = None,
    trace_attempts: int = READ_BACK_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], Path]:
    """Collect one Run, read back its trace and freeze ``summary.json``.

    ``extra`` is merged into the summary (the caller's own block, e.g. the
    ITBench attempt); it must not carry answer content. Returns
    ``(summary_written, summary_path)``.
    """
    rid = UUID(str(run_id))
    block, ledger = collect(dsn, rid, connect=connect)
    trace = trace_roots(
        project,
        str(rid),
        client_factory=client_factory or lab_evidence.langsmith_client,
        attempts=trace_attempts,
        sleep=sleep,
    )
    summary = {"run": block, "trace": trace, **dict(extra or {})}
    return lab_evidence.freeze(
        ledger,
        experiment=experiment,
        run_id=str(rid),
        evidence_dir=evidence_dir,
        summary=summary,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--project", help="LangSmith project; omit when not traced")
    args = parser.parse_args(argv)
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        print(f"{DSN_ENV} is not set", file=sys.stderr)
        return 2
    frozen, path = freeze_run(
        dsn,
        args.run_id,
        experiment=args.experiment,
        evidence_dir=args.evidence_dir,
        project=args.project,
    )
    print(
        json.dumps(
            {
                "summary": str(path),
                "run_id": frozen["run_id"],
                "report_present": frozen["run"]["report_present"],
                "trace_read_back": frozen["trace"]["read_back"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
