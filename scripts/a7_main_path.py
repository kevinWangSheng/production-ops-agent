"""A7 (#559) main-path Run: driver and deterministic verdicts.

Engineer-side lab script (ADR-0006), not product code; it grants the product
nothing. Decisions: docs/tasks/2026-11-12-tool-expansion-contract.md, r7
(A7-8 .. A7-13, A7-17).

PUBLIC INTERFACE
================

Pure functions over a frozen ``summary.json`` (``scripts.run_freeze``'s
shape: the Run block under ``"run"``; the block itself is accepted too):

* ``cited_categories(summary) -> dict[str, dict[str, int]]``: evidence
  category (``"metrics"``, ``"traces"``, ``"cluster_objects"``, ``"logs"``)
  -> {report claim kind -> number of claims citing that category}. Only a
  published report (Run ``completed``, report an object) counts; each cited
  ``evidence_id`` is followed through this Run's ``tool_requests`` to its
  tool; only ``ok`` evidence counts; evidence this Run did not produce
  (inherited from an earlier Run) does not; a ``read_evidence`` slice counts
  for its parent tool's category. Categories never cited are absent.
* ``all_four_cited(summary) -> bool``.
* ``human_free(summary) -> bool`` (A7-10): the incident was opened by the
  ``alertmanager`` identity with this Run queued at intake, and the frozen
  audit shows no follow-up, pause, takeover, close or other human control.

CLI (``.venv/bin/python -m scripts.a7_main_path [--dry-run] <command>``):

* ``run --side before|after [--attempt N]``: prepare the LangSmith project,
  check the worker is running and no earlier A7 incident is open, inject
  ``paymentFailure`` (``kind_lab.py fault inject``), wait for the alert's Run
  to end, freeze ``summary.json`` with verdicts, restore the fault and cool
  down. ``after`` may be attempted at most 3 times (A7-12 a: one Run plus at
  most two re-runs of the same code); ``before`` once. ``--dry-run`` prints
  the steps and touches nothing (no cluster, database, docker or LangSmith).
* ``verdict SUMMARY``: print the verdicts of one frozen summary.
* ``compare --before SUMMARY --after SUMMARY [--after SUMMARY ...]
  [--out PATH]``: the A7-11 deterministic comparison and the x/n tally.

Environment: ``RUN_FREEZE_DSN`` (read-only DSN, never printed or stored) and
the ``LANGSMITH_*`` keys ``lab_evidence.langsmith_client`` reads.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opspilot.observation.store import (
    AUTO_OBSERVATION_ACTION,
    AUTO_OBSERVATION_ACTOR,
)
from opspilot.tools import (
    jaeger_query,
    kubernetes_core,
    kubernetes_logs,
    kubernetes_tabular,
    otel_demo,
    prometheus_ext,
)
from opspilot.tools.executor import EVIDENCE_TOOL

ROOT = Path(__file__).resolve().parents[1]

CATEGORIES = ("metrics", "traces", "cluster_objects", "logs")

#: A7-8 a: categories from the code's own tool-name constants, never retyped.
CATEGORY_TOOLS: Mapping[str, frozenset[str]] = {
    "metrics": frozenset(otel_demo.PROMETHEUS_TOOLS)
    | frozenset(prometheus_ext.TOOL_NAMES),
    "traces": frozenset({otel_demo.TRACES_TOOL}) | frozenset(jaeger_query.TOOL_NAMES),
    "cluster_objects": frozenset(kubernetes_core.TOOL_NAMES)
    | frozenset(kubernetes_tabular.TOOL_NAMES),
    "logs": frozenset(kubernetes_logs.TOOL_NAMES),
}
OK = "ok"
ALERTMANAGER_ACTOR = "alertmanager"
#: ``persistence/alerts.py``: a first firing alert that opened an incident
#: *with* a Run (``handoff_created`` opens one without).
OPENING_OUTCOME = "created"

# -- verdicts (pure) ----------------------------------------------------------


def _block(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    """The Run block: ``summary["run"]`` in a frozen summary, else itself
    (the block's own ``"run"`` key is the Run row and has no tool_requests)."""
    if "tool_requests" in summary:
        return summary
    inner = summary.get("run")
    return inner if isinstance(inner, Mapping) else {}


def category_of(tool: object) -> str | None:
    for category in CATEGORIES:
        if tool in CATEGORY_TOOLS[category]:
            return category
    return None


def _operation(evidence_id: str) -> str:
    """``<operation_id>:<dispatch uuid>`` -> operation_id."""
    return evidence_id.rpartition(":")[0] or evidence_id


def published_report(summary: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The report when the Run published it: a validated report is published
    only by a ``completed`` Run (a failed validation hands off instead)."""
    block = _block(summary)
    run = block.get("run")
    report = block.get("report")
    if not isinstance(run, Mapping) or run.get("state") != "completed":
        return None
    if not isinstance(report, Mapping) or not isinstance(report.get("claims"), list):
        return None
    return report


def _requests_by_operation(
    block: Mapping[str, Any],
) -> dict[str, list[Mapping[str, Any]]]:
    run_id = block.get("run_id")
    found: dict[str, list[Mapping[str, Any]]] = {}
    for entry in block.get("tool_requests") or []:
        if not isinstance(entry, Mapping):
            continue
        if run_id is not None and entry.get("run_id") not in (None, run_id):
            continue
        operation = entry.get("operation_id")
        if isinstance(operation, str):
            found.setdefault(operation, []).append(entry)
    return found


def _cited(block: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        item["evidence_id"]: item
        for item in block.get("cited_evidence") or []
        if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
    }


def evidence_category(
    evidence_id: str, block: Mapping[str, Any], *, _depth: int = 0
) -> str | None:
    """The A7-8 category one cited id counts for, or None when it does not
    count (not this Run's, not ok, unknown tool, unresolvable slice)."""
    if not isinstance(evidence_id, str) or _depth > 1:
        return None
    requests = _requests_by_operation(block).get(_operation(evidence_id))
    if not requests:
        return None  # not a tool request of this Run: inherited or unknown
    cited = _cited(block).get(evidence_id)
    stored = cited.get("stored") if isinstance(cited, Mapping) else None
    if isinstance(stored, Mapping):
        producer = stored.get("run_id")
        if producer is not None and producer != block.get("run_id"):
            return None
    if not any(entry.get("result_code") == OK for entry in requests):
        return None
    if isinstance(cited, Mapping) and cited.get("status") not in (None, OK):
        return None
    tools = {entry.get("tool") for entry in requests}
    if len(tools) != 1:
        return None
    (tool,) = tools
    if tool != EVIDENCE_TOOL:
        return category_of(tool)
    # A read_evidence slice: its parent tool's category (#308 view fields).
    view = stored.get("view") if isinstance(stored, Mapping) else None
    if not isinstance(view, Mapping):
        return None
    parent_tool = view.get("parent_tool")
    if parent_tool is not None:
        return category_of(parent_tool)
    piece = view.get("slice")
    parent = piece.get("parent_evidence_id") if isinstance(piece, Mapping) else None
    if not isinstance(parent, str):
        return None
    parent_requests = _requests_by_operation(block).get(_operation(parent)) or []
    parent_tools = {entry.get("tool") for entry in parent_requests}
    if len(parent_tools) != 1:
        return None
    return category_of(next(iter(parent_tools)))


def cited_categories(summary: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    report = published_report(summary)
    if report is None:
        return {}
    block = _block(summary)
    counts: dict[str, dict[str, int]] = {}
    for claim in report["claims"]:
        if not isinstance(claim, Mapping):
            continue
        kind = claim.get("kind")
        if not isinstance(kind, str):
            continue
        hit = {
            category
            for eid in claim.get("evidence_ids") or []
            if (category := evidence_category(eid, block)) is not None
        }
        for category in hit:
            per_kind = counts.setdefault(category, {})
            per_kind[kind] = per_kind.get(kind, 0) + 1
    return {c: counts[c] for c in CATEGORIES if c in counts}


def all_four_cited(summary: Mapping[str, Any]) -> bool:
    return set(cited_categories(summary)) == set(CATEGORIES)


def _audit_entries(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    audit = block.get("audit")
    entries = audit.get("entries") if isinstance(audit, Mapping) else None
    return [e for e in entries or [] if isinstance(e, Mapping)]


def _detail(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = entry.get("detail")
    return detail if isinstance(detail, Mapping) else {}


def human_free_problems(summary: Mapping[str, Any]) -> list[str]:
    """Why A7-10 does not hold; empty when it holds."""
    block = _block(summary)
    run_id = block.get("run_id")
    problems: list[str] = []
    entries = _audit_entries(block)
    deliveries = [e for e in entries if e.get("source") == "opspilot_alert_deliveries"]
    if not deliveries:
        problems.append("NO_ALERT_DELIVERY")
    if any(e.get("actor") != ALERTMANAGER_ACTOR for e in deliveries):
        problems.append("DELIVERY_NOT_ALERTMANAGER")
    openings = [d for d in deliveries if _detail(d).get("outcome") == OPENING_OUTCOME]
    if len(openings) != 1:
        problems.append("NOT_OPENED_BY_ONE_ALERT")
    runs = block.get("incident_runs") or []
    run_ids = [r.get("run_id") for r in runs if isinstance(r, Mapping)]
    if run_ids != [run_id]:
        problems.append("NOT_THE_INCIDENTS_ONLY_RUN")
    queued = any(
        e.get("category") == "state_change"
        and e.get("source") == "opspilot_subject_events"
        and _detail(e).get("kind") == "intake_accepted"
        and e.get("run_id") == run_id
        for e in entries
    )
    if not queued:
        problems.append("RUN_NOT_QUEUED_AT_INTAKE")
    # Every input other than the alert's own (an attached re-firing appends an
    # alertmanager input) is a person's follow-up or note.
    if any(
        e.get("category") == "input"
        and e.get("source") == "opspilot_inputs"
        and e.get("actor") != ALERTMANAGER_ACTOR
        for e in entries
    ):
        problems.append("HUMAN_INPUT")
    # Follow-up, correct, pause, resume, takeover, cancel, close, rebind ...
    # The one control row the product writes itself is the automatic
    # recovery-observation grant after publication (``observation/store.py``).
    if any(
        e.get("category") == "human_correction"
        and not (
            e.get("actor") == AUTO_OBSERVATION_ACTOR
            and _detail(e).get("action") == AUTO_OBSERVATION_ACTION
        )
        for e in entries
    ):
        problems.append("HUMAN_CONTROL")
    if any(
        e.get("source") == "opspilot_suspension_audit"
        and not _detail(e).get("baseline")
        for e in entries
    ):
        problems.append("TARGET_SUSPENSION")
    driver = summary.get("a7")
    if isinstance(driver, Mapping) and "worker_running_before_alert" in driver:
        if driver["worker_running_before_alert"] is not True:
            problems.append("WORKER_NOT_RUNNING_BEFORE_ALERT")
    return problems


def human_free(summary: Mapping[str, Any]) -> bool:
    return not human_free_problems(summary)


def verdicts(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "report_published": published_report(summary) is not None,
        "cited_categories": cited_categories(summary),
        "all_four_cited": all_four_cited(summary),
        "human_free": human_free(summary),
        "human_free_problems": human_free_problems(summary),
    }


# -- comparison (A7-11, A7-12; pure) ------------------------------------------


def run_metrics(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The deterministic per-Run facts A7-11 compares."""
    block = _block(summary)
    calls: dict[str, int] = {}
    failures = 0
    for entry in block.get("tool_requests") or []:
        if not isinstance(entry, Mapping):
            continue
        tool = entry.get("tool")
        label = category_of(tool) or (
            "read_evidence" if tool == EVIDENCE_TOOL else "other"
        )
        calls[label] = calls.get(label, 0) + 1
        if entry.get("result_code") != OK:
            failures += 1
    run = block.get("run") if isinstance(block.get("run"), Mapping) else {}
    versions = (
        block.get("versions") if isinstance(block.get("versions"), Mapping) else {}
    )
    return {
        "run_id": block.get("run_id"),
        "run_state": run.get("state"),
        "versions": dict(versions),
        "cited_categories": cited_categories(summary),
        "all_four_cited": all_four_cited(summary),
        "human_free": human_free(summary),
        "tool_calls_by_category": dict(sorted(calls.items())),
        "tool_failures": failures,
        "model_requests_used": run.get("model_requests_used"),
        "tokens": block.get("tokens"),
        "report_published": published_report(summary) is not None,
    }


def compare(
    before: Mapping[str, Any], afters: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if not 1 <= len(afters) <= MAX_AFTER_ATTEMPTS:
        raise ValueError(f"1..{MAX_AFTER_ATTEMPTS} after-side summaries")
    rows = [run_metrics(s) for s in afters]
    reached = sum(1 for r in rows if r["all_four_cited"] and r["human_free"])
    return {
        "before": run_metrics(before),
        "after": rows,
        # A7-12 a: x/n of the after side, every attempt recorded.
        "after_reached": f"{reached}/{len(rows)}",
        "done": reached > 0,
        "note": "one Run per side: a single observation, no causal claim (A7-11)",
    }


# -- driver -------------------------------------------------------------------

DSN_ENV = "RUN_FREEZE_DSN"
ALERTNAME = "CheckoutPlaceOrderErrorRatioHigh"
ALERT_NAMESPACE = "otel-demo"
ALERT_WORKLOAD = "checkout"
FAULT_FLAG = "paymentFailure"
MAX_AFTER_ATTEMPTS = 3
SIDES = ("before", "after")
#: The ext profile's L1a variant per side (A7-11): the A7 start commit runs
#: ``replay-candidate``, the A7 commit ``replay-candidate-k8s``.
SIDE_PROMPT_PREFIX = {
    "before": "prompt-replay-candidate-",
    "after": "prompt-replay-candidate-k8s-",
}
TOOL_SCHEMA_PREFIX = "otel-demo-ext-"
FINISHED_RUN_STATES = frozenset(
    {"completed", "cancelled", "failed", "blocked", "budget_exhausted", "waiting_human"}
)
EVIDENCE_ROOT = ROOT / "docs/evidence/a7-main-path"


def default_project(today: datetime | None = None) -> str:
    """A7-17: ``opspilot-lab-a7-main-path-<YYYYMMDD>`` (local date). Both
    sides of one lab session should pass the same ``--project`` explicitly
    when the session may cross midnight."""
    from scripts import lab_evidence

    day = (today or datetime.now().astimezone()).strftime("%Y%m%d")
    return lab_evidence.lab_project(f"a7-main-path-{day}")


def experiment_id(side: str, attempt: int) -> str:
    return f"a7-main-path-{side}-{attempt}"


def side_versions_ok(side: str, versions: Mapping[str, Any]) -> bool:
    prompt = versions.get("prompt_revision")
    schema = versions.get("tool_schema_revision")
    if not isinstance(prompt, str) or not isinstance(schema, str):
        return False
    if not schema.startswith(TOOL_SCHEMA_PREFIX):
        return False
    if side == "before":
        return prompt.startswith(SIDE_PROMPT_PREFIX["before"]) and not (
            prompt.startswith(SIDE_PROMPT_PREFIX["after"])
        )
    return prompt.startswith(SIDE_PROMPT_PREFIX["after"])


def plan_steps(args: argparse.Namespace) -> list[str]:
    exp = experiment_id(args.side, args.attempt)
    return [
        f"set LangSmith project {args.project} to long-lived retention",
        f"check the {args.compose_project} worker container is running",
        f"check no open incident from {ALERTNAME} on "
        f"{ALERT_NAMESPACE}/{ALERT_WORKLOAD} (close the previous one first)",
        f"kind_lab.py fault inject --experiment-id {exp} --flag {FAULT_FLAG}",
        f"wait up to {args.alert_timeout}s for {ALERTNAME} to open an incident "
        "with a queued Run",
        f"wait up to {args.run_timeout}s for the Run to end",
        f"freeze summary.json with verdicts to {args.evidence_dir}",
        f"kind_lab.py fault restore --experiment-id {exp}",
        f"cool down {args.cooldown}s",
    ]


_OPEN_INCIDENT_SQL = """
SELECT i.incident_id::text AS incident_id, a.run_id::text AS run_id,
       i.lifecycle, r.state AS run_state, a.created_at,
       (SELECT d.alert_json FROM opspilot_alert_deliveries d
         WHERE d.delivery_key = a.delivery_key ORDER BY d.delivery_id LIMIT 1)
         AS alert_json
FROM opspilot_alert_identities a
JOIN opspilot_incidents i ON i.incident_id = a.incident_id
LEFT JOIN opspilot_runs r ON r.run_id = a.run_id
WHERE a.namespace = %s AND a.workload = %s
ORDER BY a.created_at
"""


def _alertname(alert_json: object) -> str | None:
    try:
        value = json.loads(str(alert_json or ""))["labels"]["alertname"]
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


class Lab:
    """Real side effects; every method is one bounded external call."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def alert_incidents(self) -> list[dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(
            self._dsn, connect_timeout=5, row_factory=dict_row
        ) as conn:
            conn.read_only = True
            with conn.transaction():
                rows = conn.execute(
                    _OPEN_INCIDENT_SQL, (ALERT_NAMESPACE, ALERT_WORKLOAD)
                ).fetchall()
        return [r for r in rows if _alertname(r.pop("alert_json")) == ALERTNAME]

    def worker_running(self, project: str) -> bool:
        from scripts import kind_lab

        return bool(kind_lab.control_containers(project)["worker"])

    def fault(self, action: str, experiment: str) -> None:
        command = [
            sys.executable,
            str(ROOT / "scripts/kind_lab.py"),
            "fault",
            action,
            "--experiment-id",
            experiment,
        ]
        if action == "inject":
            command += ["--flag", FAULT_FLAG]
        subprocess.run(command, check=True, timeout=180)

    def prepare_project(self, project: str) -> dict[str, Any]:
        from scripts import lab_evidence

        return lab_evidence.ensure_longlived_project(
            lab_evidence.langsmith_client(), project
        )

    def freeze(
        self,
        run_id: str,
        *,
        experiment: str,
        evidence_dir: Path,
        project: str,
        driver: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Path]:
        from scripts import lab_evidence, run_freeze

        block, ledger = run_freeze.collect(self._dsn, run_id)
        trace = run_freeze.trace_roots(
            project, run_id, client_factory=lab_evidence.langsmith_client
        )
        summary: dict[str, Any] = {"run": block, "trace": trace, "a7": dict(driver)}
        summary["a7"]["verdicts"] = verdicts(summary)
        summary["a7"]["side_versions_ok"] = side_versions_ok(
            driver["side"], block.get("versions") or {}
        )
        return lab_evidence.freeze(
            ledger,
            experiment=experiment,
            run_id=run_id,
            evidence_dir=evidence_dir,
            summary=summary,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wait(
    probe: Callable[[], Any],
    *,
    timeout: float,
    poll: float,
    sleep: Callable[[float], None],
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        sleep(poll)


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def check_attempt(side: str, attempt: int) -> None:
    limit = MAX_AFTER_ATTEMPTS if side == "after" else 1
    if not 1 <= attempt <= limit:
        raise SystemExit(f"--attempt for {side} must be 1..{limit} (A7-12 a)")


def cmd_run(args: argparse.Namespace, *, lab: Lab | None = None) -> int:
    check_attempt(args.side, args.attempt)
    if args.evidence_dir is None:
        args.evidence_dir = EVIDENCE_ROOT / f"{args.side}-{args.attempt}"
    if args.project is None:
        args.project = default_project()
    if args.dry_run:
        _emit({"command": "run", "dry_run": True, "steps": plan_steps(args)})
        return 0
    if args.evidence_dir.exists():
        raise SystemExit(f"{args.evidence_dir} exists; refusing to overwrite")
    if lab is None:
        dsn = os.environ.get(DSN_ENV)
        if not dsn:
            raise SystemExit(f"{DSN_ENV} is not set")
        lab = Lab(dsn)
    exp = experiment_id(args.side, args.attempt)
    _emit({"step": "project", **lab.prepare_project(args.project)})
    worker = lab.worker_running(args.compose_project)
    if not worker:
        raise SystemExit("worker is not running; start it before the alert (A7-10)")
    open_incidents = [i for i in lab.alert_incidents() if i["lifecycle"] != "closed"]
    if open_incidents:
        raise SystemExit(
            "an incident from this alert is still open; close it in the workbench "
            "first, or the new alert attaches to it without a Run: "
            + ",".join(i["incident_id"] for i in open_incidents)
        )
    known = {i["incident_id"] for i in lab.alert_incidents()}
    injected_at = _now()
    lab.fault("inject", exp)
    _emit({"step": "inject", "at": injected_at.isoformat(), "experiment": exp})
    driver: dict[str, Any] = {
        "side": args.side,
        "attempt": args.attempt,
        "max_attempts": MAX_AFTER_ATTEMPTS if args.side == "after" else 1,
        "experiment": exp,
        "scenario": {"flag": FAULT_FLAG, "alertname": ALERTNAME, "kind": "switch"},
        "worker_running_before_alert": worker,
        "langsmith_project": args.project,
        "injected_at": injected_at.isoformat(),
    }
    restored = False
    try:
        incident = _wait(
            lambda: next(
                (
                    i
                    for i in lab.alert_incidents()
                    if i["incident_id"] not in known and i["run_id"]
                ),
                None,
            ),
            timeout=args.alert_timeout,
            poll=args.poll,
            sleep=time.sleep,
        )
        if incident is None:
            _emit({"step": "alert", "result": "timeout"})
            return 1
        _emit({"step": "alert", **incident})
        run_id = incident["run_id"]
        ended = _wait(
            lambda: next(
                (
                    i
                    for i in lab.alert_incidents()
                    if i["run_id"] == run_id and i["run_state"] in FINISHED_RUN_STATES
                ),
                None,
            ),
            timeout=args.run_timeout,
            poll=args.poll,
            sleep=time.sleep,
        )
        driver["run_finished_wait"] = "ended" if ended else "timeout"
        frozen, path = lab.freeze(
            run_id,
            experiment=exp,
            evidence_dir=args.evidence_dir,
            project=args.project,
            driver=driver,
        )
        _emit({"step": "freeze", "summary": str(path), **frozen["a7"]["verdicts"]})
        lab.fault("restore", exp)
        restored = True
        _emit({"step": "restore", "at": _now().isoformat()})
    finally:
        if not restored:
            lab.fault("restore", exp)
            _emit({"step": "restore", "at": _now().isoformat(), "after_error": True})
    _emit({"step": "cooldown", "seconds": args.cooldown})
    time.sleep(args.cooldown)
    return 0


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def cmd_verdict(args: argparse.Namespace) -> int:
    _emit(verdicts(_load(args.summary)))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    result = compare(_load(args.before), [_load(p) for p in args.after])
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    if args.out is not None and not args.dry_run:
        args.out.write_text(text)
    print(text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one live Run of one side")
    run.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    run.add_argument("--side", choices=SIDES, required=True)
    run.add_argument("--attempt", type=int, default=1)
    run.add_argument("--project", help="LangSmith project (default: A7-17 name)")
    run.add_argument("--evidence-dir", type=Path)
    run.add_argument("--compose-project", default="opspilot")
    run.add_argument("--alert-timeout", type=float, default=1500.0)
    run.add_argument("--run-timeout", type=float, default=1800.0)
    run.add_argument("--poll", type=float, default=10.0)
    run.add_argument("--cooldown", type=float, default=900.0)
    run.set_defaults(func=cmd_run)
    verdict = sub.add_parser("verdict", help="verdicts of one frozen summary")
    verdict.add_argument("summary", type=Path)
    verdict.set_defaults(func=cmd_verdict)
    comp = sub.add_parser("compare", help="A7-11 comparison and A7-12 tally")
    comp.add_argument("--before", type=Path, required=True)
    comp.add_argument("--after", type=Path, action="append", required=True)
    comp.add_argument("--out", type=Path)
    comp.set_defaults(func=cmd_compare)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    return int(args.func(args))


__all__ = [
    "CATEGORIES",
    "CATEGORY_TOOLS",
    "all_four_cited",
    "cited_categories",
    "compare",
    "human_free",
    "human_free_problems",
    "main",
    "run_metrics",
    "verdicts",
]

if __name__ == "__main__":
    raise SystemExit(main())
