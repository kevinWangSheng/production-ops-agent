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
  Refused (exit 2, the reasons on stdout) unless: one ``before`` and 1..3
  distinct ``after`` attempts; unique Run ids; one LangSmith project, one
  fault scenario and one bound target across all; and every Run recorded
  under exactly its side's versions (``expected_versions``: computed from
  this checkout's code, not given on the command line; run ``compare`` from
  the A7 commit).

``run`` does not start the product. Host topology only (the rental runbook's
stage 2; the Compose stack is not supported): the operator starts web
(``python -m opspilot.web serve``) and worker (``python -m
opspilot.worker_main``) as host processes, from inside one checkout with that
checkout's own ``.venv`` and ``OPSPILOT_TOOL_PROFILE=otel-demo-ext``. For
``--side before`` that checkout is at the A7 start commit (``dc9156b7``), for
``--side after`` at the commit this driver runs from. ``--deployment-checkout``
names it. Before injecting, ``run`` refuses (nothing injected) unless:

* ``git -C <checkout> rev-parse HEAD`` is that commit, no tracked file is
  modified and no file is untracked (ignored files such as a ``.venv``
  directory do not count; a ``.venv`` symlink does, build the venv in place);
* ``<checkout>/.venv/bin/python -I`` with the checkout first on ``sys.path``
  (what ``python -m`` from the checkout imports) computes exactly
  ``expected_versions(side)`` (variant, prompt, context-policy and tool-schema
  revisions), importing ``opspilot`` from that checkout;
* at least one web and one worker process are running (Linux ``/proc``,
  read-only: argv, cwd and the single ``OPSPILOT_TOOL_PROFILE`` value of the
  environment; nothing else is read or kept), each with cwd = the checkout
  and ``OPSPILOT_TOOL_PROFILE=otel-demo-ext``. The worker running before the
  alert is A7-10's last condition and is recorded.

How an attempt ends (``a7.run_end``, exit code): ``terminal`` (a terminal Run
state, 0); ``parked`` (``waiting_human``/``paused``: resumable but waiting for
a person, which A7-10 rules out, so the attempt is a failed one, frozen as is
and never resumed, 4); ``timeout`` (still running at ``--run-timeout``, 5: the
summary is frozen and marked incomplete, and ``compare`` refuses it). A
``parked`` attempt is a valid attempt and counts in A7-12's n.

The ``run`` command restores the fault on every exit path once the
injection was attempted, re-reads ``flagd-config`` and exits 3 with a loud
message when ``paymentFailure`` is not back to its normal variant.

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
from typing import Any, get_args

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
from opspilot.tools.outcomes import ToolStatus

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
    if block.get("report_present") is False:
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


def _requests_for(
    evidence_id: str, block: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """This Run's tool requests behind one evidence id. ``run_freeze`` writes
    the audit's query detail, which names the ``operation_id`` (the id's part
    before ``:``) but not the evidence id."""
    return _requests_by_operation(block).get(_operation(evidence_id)) or []


def _code(entry: Mapping[str, Any]) -> object:
    """The request's result: ``result_code`` in the audit detail."""
    return entry.get("result_code")


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
    requests = _requests_for(evidence_id, block)
    if not requests:
        return None  # not a tool request of this Run: inherited or unknown
    cited = _cited(block).get(evidence_id)
    stored = cited.get("stored") if isinstance(cited, Mapping) else None
    if isinstance(stored, Mapping):
        producer = stored.get("run_id")
        if producer is not None and producer != block.get("run_id"):
            return None
    if not any(_code(entry) == OK for entry in requests):
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
    parent_requests = _requests_for(parent, block)
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

#: ``opspilot/tools/outcomes.py`` ``ToolStatus``: the statuses that are a
#: failed tool operation. ``no_data`` is a valid empty answer and a request
#: without a recorded result (``plan_reference``, ``None``) is unknown.
FAILED_STATUSES: frozenset[str] = frozenset(get_args(ToolStatus)) - {OK, "no_data"}


def run_metrics(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The deterministic per-Run facts A7-11 compares."""
    block = _block(summary)
    calls: dict[str, int] = {}
    results = {"ok": 0, "no_data": 0, "failed": 0, "unknown": 0}
    for entry in block.get("tool_requests") or []:
        if not isinstance(entry, Mapping):
            continue
        tool = entry.get("tool")
        label = category_of(tool) or (
            "read_evidence" if tool == EVIDENCE_TOOL else "other"
        )
        calls[label] = calls.get(label, 0) + 1
        code = _code(entry)
        if code == OK:
            results["ok"] += 1
        elif code == "no_data":
            results["no_data"] += 1
        elif code in FAILED_STATUSES:
            results["failed"] += 1
        else:
            results["unknown"] += 1
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
        "tool_results": results,
        "tool_failures": results["failed"],
        "model_requests_used": run.get("model_requests_used"),
        "tokens": block.get("tokens"),
        "report_published": published_report(summary) is not None,
    }


#: The L1a variant each side's extension profile runs (A7-11): the A7 start
#: commit's ``otel-demo-ext`` used the loop default, this commit its own.
BEFORE_VARIANT = "replay-candidate"


def expected_versions(side: str) -> dict[str, str]:
    """The exact versions a Run of ``side`` must have been recorded under,
    computed from this checkout's code, never typed in.

    ``after`` is this commit's ``otel-demo-ext`` (``otel_demo_ext_versions``).
    ``before`` is the A7 start commit's ``otel-demo-ext``: that commit ran
    ``replay-candidate`` with the same tool registry, and A7 changes neither
    (A7-5 pins ``replay-candidate``'s bytes; A7-6 leaves the ext
    ``tool_schema_revision`` alone), so this code reproduces its values
    exactly. A checkout where either moved yields other values, and
    ``compare`` then refuses (fail-closed); run ``compare`` from the A7 commit.
    """
    from opspilot.instructions.discipline import discipline_revision
    from opspilot.investigation.loop import investigation_versions
    from opspilot.tools import otel_demo_ext

    if side not in SIDES:
        raise ValueError(f"unknown side {side!r}")
    if side == "after":
        variant = otel_demo_ext.DISCIPLINE_VARIANT
        versions = dict(otel_demo_ext.otel_demo_ext_versions())
    else:
        variant = BEFORE_VARIANT
        versions = {
            **investigation_versions(variant),
            "tool_schema_revision": otel_demo_ext.TOOL_SCHEMA_REVISION,
        }
    return {
        **versions,
        "variant_id": variant,
        "discipline_revision": discipline_revision(variant),
    }


def observed_versions(summary: Mapping[str, Any]) -> dict[str, Any]:
    """What the Run was recorded under: the Run row's ``versions`` and the
    ``variant_id`` of its persisted input (frozen into the ``a7`` block by
    the driver, since ``run_freeze``'s block does not carry the input)."""
    from opspilot.instructions.discipline import (
        UnknownVariantError,
        discipline_revision,
    )

    block = _block(summary)
    versions = block.get("versions")
    observed: dict[str, Any] = dict(versions) if isinstance(versions, Mapping) else {}
    driver = summary.get("a7")
    variant = driver.get("variant_id") if isinstance(driver, Mapping) else None
    observed["variant_id"] = variant
    try:
        observed["discipline_revision"] = (
            discipline_revision(variant) if isinstance(variant, str) else None
        )
    except UnknownVariantError:
        observed["discipline_revision"] = None
    return observed


def version_problems(summary: Mapping[str, Any], side: str) -> list[str]:
    """Each expected version key whose recorded value differs, exactly."""
    observed = observed_versions(summary)
    return [
        f"{side}:{key}"
        for key, value in expected_versions(side).items()
        if observed.get(key) != value
    ]


class ComparisonRefused(ValueError):
    """The summaries are not one A7-11 comparison; ``problems`` says why."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(", ".join(problems))
        self.problems = tuple(problems)


def _driver(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    driver = summary.get("a7")
    return driver if isinstance(driver, Mapping) else {}


def comparison_problems(
    before: Mapping[str, Any], afters: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Why ``before`` and ``afters`` are not one same-session, same-scenario
    comparison of the two exact versions (A7-11) within A7-12 a's limit."""
    problems: list[str] = []
    if not 1 <= len(afters) <= MAX_AFTER_ATTEMPTS:
        problems.append(f"AFTER_COUNT_NOT_1_TO_{MAX_AFTER_ATTEMPTS}")
    everyone = [before, *afters]
    if _driver(before).get("side") != "before":
        problems.append("BEFORE_SIDE_MISMATCH")
    if any(_driver(a).get("side") != "after" for a in afters):
        problems.append("AFTER_SIDE_MISMATCH")
    attempts = [_driver(a).get("attempt") for a in afters]
    if len(set(attempts)) != len(attempts) or not all(
        type(n) is int and 1 <= n <= MAX_AFTER_ATTEMPTS for n in attempts
    ):
        problems.append("AFTER_ATTEMPTS_INVALID")
    run_ids = [_block(s).get("run_id") for s in everyone]
    if not all(isinstance(r, str) and r for r in run_ids) or len(set(run_ids)) != len(
        run_ids
    ):
        problems.append("RUN_IDS_NOT_UNIQUE")
    projects = {_driver(s).get("langsmith_project") for s in everyone}
    if len(projects) != 1 or not all(isinstance(p, str) and p for p in projects):
        problems.append("LANGSMITH_PROJECT_DIFFERS")
    scenarios = [_driver(s).get("scenario") for s in everyone]
    if not isinstance(scenarios[0], Mapping) or any(
        x != scenarios[0] for x in scenarios
    ):
        problems.append("SCENARIO_DIFFERS")
    targets = set()
    for s in everyone:
        run = _block(s).get("run")
        targets.add(run.get("bound_target_id") if isinstance(run, Mapping) else None)
    if len(targets) != 1 or None in targets:
        problems.append("TARGET_DIFFERS")
    if any(_driver(s).get("run_end") not in COMPLETE_RUN_ENDS for s in everyone):
        problems.append("INCOMPLETE_ATTEMPT")
    problems += version_problems(before, "before")
    for a in afters:
        problems += version_problems(a, "after")
    after_versions = [observed_versions(a) for a in afters]
    if any(v != after_versions[0] for v in after_versions):
        problems.append("AFTER_VERSIONS_DIFFER")
    return list(dict.fromkeys(problems))


def compare(
    before: Mapping[str, Any], afters: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The A7-11 comparison and the A7-12 a tally; ``ComparisonRefused``
    unless ``comparison_problems`` is empty."""
    problems = comparison_problems(before, afters)
    if problems:
        raise ComparisonRefused(problems)
    rows = [run_metrics(s) for s in afters]
    reached = sum(1 for r in rows if r["all_four_cited"] and r["human_free"])
    return {
        "before": run_metrics(before),
        "after": rows,
        "expected_versions": {side: expected_versions(side) for side in SIDES},
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
#: A Run in one of these can no longer continue (``acceptance_audit``).
TERMINAL_RUN_STATES = frozenset(
    {"completed", "cancelled", "failed", "blocked", "budget_exhausted"}
)
#: Resumable, waiting for a person: for A7 (no human at all, A7-10) the
#: attempt ends here as a failed attempt; nobody resumes it.
PARKED_RUN_STATES = frozenset({"waiting_human", "paused"})
#: How one attempt ended (``a7.run_end``) and the driver's exit code.
RUN_END_EXIT = {"terminal": 0, "parked": 4, "timeout": 5}
#: Only these attempts are valid entries of an A7-11/A7-12 comparison; a
#: timed-out attempt's summary is incomplete.
COMPLETE_RUN_ENDS = frozenset({"terminal", "parked"})
EVIDENCE_ROOT = ROOT / "docs/evidence/a7-main-path"
#: Exit code when the fault may still be on after the driver ends.
FAULT_LEFT_ON_EXIT = 3


#: The A7 start commit (A7-11): the ``before`` side's web and worker run it.
BEFORE_COMMIT = "dc9156b7"
#: Both create or claim a Run: the web records ``versions`` at alert intake,
#: the worker refuses a claim whose ``versions`` differ from its own.
DEPLOYED_SERVICES = ("web", "worker")
PROFILE_ENV = "OPSPILOT_TOOL_PROFILE"
EXT_PROFILE = "otel-demo-ext"
VERSION_KEYS = ("prompt_revision", "context_policy_revision", "tool_schema_revision")
#: Run as ``<checkout>/.venv/bin/python -I -c _VERSION_PROBE <checkout>``.
_VERSION_PROBE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import opspilot\n"
    "from opspilot.tools import otel_demo_ext as e\n"
    "print(json.dumps({'opspilot_file': opspilot.__file__,"
    " 'variant_id': e.DISCIPLINE_VARIANT, 'versions': e.otel_demo_ext_versions()}))"
)


def git(directory: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def checkout_dirty(checkout: Path) -> bool | None:
    """True when any tracked file is modified or any file is untracked: an
    untracked module would be imported by ``python -m`` from the checkout.
    Ignored files (the ``.venv`` directory, ``tmp/``) do not count; a
    ``.venv`` *symlink* is not covered by ``.gitignore``'s ``.venv/`` and is
    therefore refused too. None when git cannot tell."""
    status = git(checkout, "status", "--porcelain", "--untracked-files=all")
    return None if status is None else bool(status)


#: The only variables the version probe's interpreter gets: it runs the
#: checkout's code, so no DSN, key, token or ``OPSPILOT_*`` setting reaches it.
PROBE_ENV_KEYS = ("PATH", "HOME", "LANG")


def probe_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in PROBE_ENV_KEYS if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def probe_checkout(checkout: Path) -> dict[str, Any] | None:
    """What ``checkout``'s own code would record for an ``otel-demo-ext``
    Run, run by its own ``.venv`` interpreter; None when that fails."""
    try:
        proc = subprocess.run(
            [
                str(checkout / ".venv/bin/python"),
                "-I",
                "-c",
                _VERSION_PROBE,
                str(checkout),
            ],
            cwd=checkout,
            env=probe_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        value = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return value if isinstance(value, dict) else None


def service_of(argv: Sequence[str]) -> str | None:
    """``web`` / ``worker`` for a product process's argv, else None."""
    if "-m" not in argv:
        return None
    rest = list(argv[argv.index("-m") + 1 :])
    if rest[:1] == ["opspilot.worker_main"]:
        return "worker"
    if rest[:1] == ["opspilot.web"] and "serve" in rest[1:]:
        return "web"
    return None


def host_processes(proc_root: Path = Path("/proc")) -> dict[str, list[dict[str, Any]]]:
    """Running web/worker processes, read-only from ``/proc``: pid, cwd and
    the ``OPSPILOT_TOOL_PROFILE`` value only (no other environment value is
    kept). Never signals anything."""
    found: dict[str, list[dict[str, Any]]] = {s: [] for s in DEPLOYED_SERVICES}
    if not proc_root.is_dir():
        raise SystemExit("host topology needs Linux /proc to read web/worker")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        service = service_of(argv)
        if service is None:
            continue
        item: dict[str, Any] = {"pid": int(entry.name)}
        try:
            item["cwd"] = os.readlink(entry / "cwd")
            profile = None
            prefix = PROFILE_ENV.encode() + b"="
            for pair in (entry / "environ").read_bytes().split(b"\0"):
                if pair.startswith(prefix):
                    profile = pair[len(prefix) :].decode(errors="replace")
            item["profile"] = profile
        except OSError:
            item["unreadable"] = True
        found[service].append(item)
    return found


def _same_path(a: object, b: object) -> bool:
    return (
        isinstance(a, str)
        and isinstance(b, str)
        and (os.path.realpath(a) == os.path.realpath(b))
    )


def deployment_problems(side: str, deployed: Mapping[str, Any]) -> list[str]:
    """Why the running host web/worker are not ``side``'s exact code (see the
    module docstring); empty when everything matches. ``deployed`` is
    ``Lab.deployment``'s record."""
    expected = expected_versions(side)
    checkout = deployed.get("checkout")
    problems: list[str] = []
    if not deployed.get("expected_head") or (
        deployed.get("head") != deployed.get("expected_head")
    ):
        problems.append("checkout:head")
    if deployed.get("dirty") is not False:
        problems.append("checkout:dirty")
    probe = deployed.get("probe")
    if not isinstance(probe, Mapping):
        problems.append("checkout:VERSION_UNREADABLE")
    else:
        module = probe.get("opspilot_file")
        if not (
            isinstance(module, str)
            and isinstance(checkout, str)
            and os.path.realpath(module).startswith(
                os.path.join(os.path.realpath(checkout), "")
            )
        ):
            problems.append("checkout:module_path")
        if probe.get("variant_id") != expected["variant_id"]:
            problems.append("checkout:variant_id")
        versions = probe.get("versions")
        versions = versions if isinstance(versions, Mapping) else {}
        problems += [
            f"checkout:{key}"
            for key in VERSION_KEYS
            if versions.get(key) != expected[key]
        ]
    processes = deployed.get("processes")
    processes = processes if isinstance(processes, Mapping) else {}
    for service in DEPLOYED_SERVICES:
        entries = processes.get(service) or []
        if not entries:
            problems.append(f"{service}:NOT_RUNNING")
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("unreadable"):
                problems.append(f"{service}:UNREADABLE")
                continue
            if not _same_path(entry.get("cwd"), checkout):
                problems.append(f"{service}:cwd")
            if entry.get("profile") != EXT_PROFILE:
                problems.append(f"{service}:profile")
    return list(dict.fromkeys(problems))


def normal_variant() -> str:
    from scripts import kind_lab

    return kind_lab.NORMAL_VARIANT


def default_project(today: datetime | None = None) -> str:
    """A7-17: ``opspilot-lab-a7-main-path-<YYYYMMDD>`` (local date). Both
    sides of one lab session should pass the same ``--project`` explicitly
    when the session may cross midnight."""
    from scripts import lab_evidence

    day = (today or datetime.now().astimezone()).strftime("%Y%m%d")
    return lab_evidence.lab_project(f"a7-main-path-{day}")


def experiment_id(side: str, attempt: int) -> str:
    return f"a7-main-path-{side}-{attempt}"


def plan_steps(args: argparse.Namespace) -> list[str]:
    exp = experiment_id(args.side, args.attempt)
    return [
        f"set LangSmith project {args.project} to long-lived retention",
        f"check {args.deployment_checkout or '<--deployment-checkout>'} is at "
        + (BEFORE_COMMIT if args.side == "before" else "this driver's commit")
        + ", unmodified, and its own .venv computes the "
        f"{args.side} side's exact versions",
        "check host web and worker processes run from that checkout with "
        f"{PROFILE_ENV}={EXT_PROFILE} (/proc, read-only)",
        f"check no open incident from {ALERTNAME} on "
        f"{ALERT_NAMESPACE}/{ALERT_WORKLOAD} (close the previous one first)",
        f"check {FAULT_FLAG} is {normal_variant()!r} in flagd-config",
        f"kind_lab.py fault inject --experiment-id {exp} --flag {FAULT_FLAG}",
        f"wait up to {args.alert_timeout}s for {ALERTNAME} to open an incident "
        "with a queued Run",
        f"wait up to {args.run_timeout}s for the Run to end or park "
        "(waiting_human/paused: a failed attempt, exit 4; timeout: incomplete, "
        "exit 5)",
        f"freeze summary.json with verdicts to {args.evidence_dir}",
        f"kind_lab.py fault restore --experiment-id {exp} (also after any error "
        "from the injection on), then re-read flagd-config and exit "
        f"{FAULT_LEFT_ON_EXIT} if {FAULT_FLAG} is not {normal_variant()!r}",
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

    def deployment(self, side: str, checkout: Path) -> dict[str, Any]:
        """The checkout's commit, cleanliness and code-computed versions, and
        the running host web/worker processes (all read-only)."""

        expected_head = (
            git(ROOT, "rev-parse", "--verify", f"{BEFORE_COMMIT}^{{commit}}")
            if side == "before"
            else git(ROOT, "rev-parse", "HEAD")
        )
        head = git(checkout, "rev-parse", "HEAD")
        dirty = checkout_dirty(checkout)
        # The checkout's interpreter runs only once the commit and the tree
        # are the ones expected; otherwise nothing of it is executed.
        trusted = bool(expected_head) and head == expected_head and dirty is False
        return {
            "checkout": str(checkout),
            "head": head,
            "expected_head": expected_head,
            "dirty": dirty,
            "probe": probe_checkout(checkout) if trusted else None,
            "probe_skipped": not trusted,
            "processes": host_processes(),
        }

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

    def flag_variant(self) -> str | None:
        """``paymentFailure``'s live ``defaultVariant``; None when unreadable."""
        from scripts import kind_lab

        raw = kind_lab.read_live_flags()
        if raw is None:
            return None
        try:
            value = json.loads(raw)["flags"][FAULT_FLAG]["defaultVariant"]
        except (ValueError, KeyError, TypeError):
            return None
        return value if isinstance(value, str) else None

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
        run_input = (ledger.get("run") or {}).get("input")
        summary: dict[str, Any] = {"run": block, "trace": trace, "a7": dict(driver)}
        summary["a7"]["variant_id"] = (
            run_input.get("variant_id") if isinstance(run_input, Mapping) else None
        )
        summary["a7"]["expected_versions"] = expected_versions(driver["side"])
        summary["a7"]["version_problems"] = version_problems(summary, driver["side"])
        summary["a7"]["verdicts"] = verdicts(summary)
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
        sleep(max(0.0, min(poll, deadline - time.monotonic())))


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
    if args.deployment_checkout is None:
        raise SystemExit("--deployment-checkout is required")
    args.deployment_checkout = args.deployment_checkout.resolve()
    if args.evidence_dir.exists():
        raise SystemExit(f"{args.evidence_dir} exists; refusing to overwrite")
    if lab is None:
        dsn = os.environ.get(DSN_ENV)
        if not dsn:
            raise SystemExit(f"{DSN_ENV} is not set")
        lab = Lab(dsn)
    exp = experiment_id(args.side, args.attempt)
    _emit({"step": "project", **lab.prepare_project(args.project)})
    deployed = lab.deployment(args.side, args.deployment_checkout)
    mismatch = deployment_problems(args.side, deployed)
    _emit({"step": "deployment", **deployed, "problems": mismatch})
    if mismatch:
        raise SystemExit(
            f"the running deployment is not the {args.side} side's code: "
            + ", ".join(mismatch)
            + f" (before: start web and worker from a {BEFORE_COMMIT} checkout; "
            "after: from this driver's commit)"
        )
    worker = bool(deployed["processes"]["worker"])
    open_incidents = [i for i in lab.alert_incidents() if i["lifecycle"] != "closed"]
    if open_incidents:
        raise SystemExit(
            "an incident from this alert is still open; close it in the workbench "
            "first, or the new alert attaches to it without a Run: "
            + ",".join(i["incident_id"] for i in open_incidents)
        )
    if lab.flag_variant() != normal_variant():
        raise SystemExit(
            f"{FAULT_FLAG} is not {normal_variant()!r} before the injection; "
            "restore the earlier experiment first"
        )
    known = {i["incident_id"] for i in lab.alert_incidents()}
    driver: dict[str, Any] = {
        "side": args.side,
        "attempt": args.attempt,
        "max_attempts": MAX_AFTER_ATTEMPTS if args.side == "after" else 1,
        "experiment": exp,
        "scenario": {
            "flag": FAULT_FLAG,
            "alertname": ALERTNAME,
            "namespace": ALERT_NAMESPACE,
            "workload": ALERT_WORKLOAD,
            "kind": "switch",
        },
        "worker_running_before_alert": worker,
        "deployed_versions": deployed,
        "langsmith_project": args.project,
    }
    try:
        driver["injected_at"] = _now().isoformat()
        # Inside the cleanup scope: kind_lab may have patched the ConfigMap
        # and still fail its read-back check, so an injection that raised has
        # an unknown result and is restored like any other.
        lab.fault("inject", exp)
        _emit({"step": "inject", "at": driver["injected_at"], "experiment": exp})
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
                    if i["run_id"] == run_id
                    and i["run_state"] in TERMINAL_RUN_STATES | PARKED_RUN_STATES
                ),
                None,
            ),
            timeout=args.run_timeout,
            poll=args.poll,
            sleep=time.sleep,
        )
        driver["run_end"] = run_end(ended)
        driver["run_state_at_end"] = None if ended is None else ended["run_state"]
        driver["complete"] = driver["run_end"] in COMPLETE_RUN_ENDS
        frozen, path = lab.freeze(
            run_id,
            experiment=exp,
            evidence_dir=args.evidence_dir,
            project=args.project,
            driver=driver,
        )
        _emit(
            {
                "step": "freeze",
                "summary": str(path),
                "run_end": driver["run_end"],
                "complete": driver["complete"],
                **frozen["a7"]["verdicts"],
            }
        )
    finally:
        restore_and_confirm(lab, exp)
    _emit({"step": "cooldown", "seconds": args.cooldown})
    time.sleep(args.cooldown)
    return RUN_END_EXIT[driver["run_end"]]


def run_end(ended: Mapping[str, Any] | None) -> str:
    """``terminal``, ``parked`` (waiting for a person: a failed attempt under
    A7-10, frozen as is, never resumed) or ``timeout`` (incomplete)."""
    if ended is None:
        return "timeout"
    return "parked" if ended["run_state"] in PARKED_RUN_STATES else "terminal"


def restore_and_confirm(lab: Lab, exp: str) -> None:
    """Restore, then re-read the flag; a flag that is not back to normal ends
    the process loudly with ``FAULT_LEFT_ON_EXIT`` whatever happened before.

    A restore that fails while the flag reads normal is the injection that
    never took effect (``kind_lab`` writes its restore snapshot only after a
    verified patch); it is reported, not hidden."""
    error: str | None = None
    try:
        lab.fault("restore", exp)
    except Exception as exc:  # noqa: BLE001 - re-checked against the live flag
        error = type(exc).__name__
    variant = lab.flag_variant()
    if variant != normal_variant():
        print(
            f"FAULT MAY STILL BE ON: {FAULT_FLAG} reads {variant!r} after restore "
            f"(restore error: {error}); run `kind_lab.py fault restore "
            f"--experiment-id {exp}` or restore flagd-config by hand",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(FAULT_LEFT_ON_EXIT)
    _emit(
        {
            "step": "restore",
            "at": _now().isoformat(),
            "flag": FAULT_FLAG,
            "variant": variant,
            "restore_error": error,
        }
    )


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def cmd_verdict(args: argparse.Namespace) -> int:
    _emit(verdicts(_load(args.summary)))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        result = compare(_load(args.before), [_load(p) for p in args.after])
    except ComparisonRefused as refused:
        _emit({"command": "compare", "refused": list(refused.problems)})
        return 2
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
    run.add_argument(
        "--deployment-checkout",
        type=Path,
        help="the checkout host web and worker were started from (required)",
    )
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
