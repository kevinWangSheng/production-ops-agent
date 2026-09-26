"""Run the external M1-01 acceptance scenarios and print a table.

Evidence levels are labels for what each row actually exercises:

* ``deterministic`` rows drive the real investigation loop with scripted
  model/tool doubles and no persistence.
* ``MemoryStepStore`` rows read a snapshot in the ``DurableStore.rebuild``
  shape after a real loop attempt; PostgreSQL itself is not touched.
* ``real PostgreSQL`` rows are produced by the product on the lab database
  (``DurableStore``, ``InvestigationRunner``/``Worker``, ``control()``, the
  deadline sweep) and read back before the seam projects them; no run state
  or marker is set by hand. They need the lab instance (``scripts/m0/
  postgres_lab.py``) and ``M1_DURABLE_POSTGRES=1``; without the opt-in they
  are reported as ``SKIPPED``, never as ``PASS``, and the run fails.
* ``recorded`` rows replay ledgers of real provider Runs already on disk.
"""

from __future__ import annotations

import re
import subprocess
import sys

UNIT = "tests/acceptance/test_m1_01_acceptance.py"
PG = "tests/integration/test_m1_01_acceptance_postgres.py"

SCENARIOS = (
    (
        "normal-report",
        "F3",
        "deterministic loop + read-only evidence",
        f"{UNIT}::test_normal_report_is_observable_through_external_seam",
    ),
    (
        "fault-report",
        "F3",
        "deterministic provider failure handoff",
        f"{UNIT}::test_provider_failure_is_a_visible_handoff_not_a_report",
    ),
    (
        "budget-refusal",
        "F2",
        "deterministic budget refusal",
        f"{UNIT}::test_budget_refusal_never_looks_completed",
    ),
    (
        "deadline-refusal",
        "F2",
        "deterministic deadline refusal",
        f"{UNIT}::test_deadline_refusal_never_dispatches_a_model_request",
    ),
    (
        "durable-completed",
        "F3/F8",
        "real loop Run published through MemoryStepStore: conclusion payload read",
        f"{UNIT}::test_durable_completed_run_projects_its_committed_conclusion",
    ),
    (
        "durable-handoff",
        "F3/F8",
        "MemoryStepStore publish of a handoff conclusion (pre-ADR-0005 row): "
        "seam still refuses to call it a report",
        f"{UNIT}::test_durable_handoff_run_is_not_reported_as_a_completed_report",
    ),
    (
        "durable-handoff-pg",
        "F2/F8",
        "real PostgreSQL: runner parks a MODEL_UNAVAILABLE handoff as waiting_human",
        f"{PG}::test_durable_handoff_parks_the_run_for_a_human",
    ),
    (
        "deadline-exceeded",
        "F2/F8",
        "real PostgreSQL: sweep parks an overdue running Run; reason from its event",
        f"{PG}::test_deadline_exceeded_run_is_swept_into_a_visible_timeout_handoff",
    ),
    (
        "pause-cancel",
        "F2/F12",
        "real PostgreSQL: control() pause then cancel over a dead worker's rows",
        f"{PG}::test_human_pause_and_cancel_are_the_observable_final_authority",
    ),
    (
        "late-result",
        "F2/F12",
        "real PostgreSQL: cancel during a live query; commit_tool writes late_result",
        f"{PG}::test_late_result_is_rejected_after_newer_human_decision",
    ),
    (
        "worker-restart",
        "F2/F8",
        "real PostgreSQL: crash, lease lapse, new Worker resumes (epoch 2) and publishes",
        f"{PG}::test_worker_restart_resumes_from_committed_evidence",
    ),
    (
        "incompatible-state",
        "F2/F8",
        "real PostgreSQL: version-incompatible Worker; claim() blocks the Run",
        f"{PG}::test_incompatible_state_is_a_blocked_handoff",
    ),
    (
        "real-deepseek-records",
        "F3",
        "recorded real Runs: projection equals ledger",
        f"{UNIT}::test_recorded_real_deepseek_runs_project_exactly_what_their_ledger_says",
    ),
    (
        "real-deepseek-report",
        "F3",
        "one recorded real Run: bound report, no handoff",
        f"{UNIT}::test_a_real_deepseek_run_has_produced_a_bound_report_without_handoff",
    ),
)

_SUMMARY = re.compile(r"(\d+) (passed|skipped|failed|error)")


def _status(result: subprocess.CompletedProcess[str]) -> str:
    """PASS only when pytest reports passed tests and nothing else.

    A skipped node exits 0 as well; reading the exit code alone would print
    PASS for a PostgreSQL row that never ran.
    """
    counts = dict((kind, int(count)) for count, kind in _SUMMARY.findall(result.stdout))
    if result.returncode == 0 and counts.get("passed") and not counts.get("skipped"):
        return "PASS"
    if result.returncode == 0 and counts.get("skipped") and not counts.get("passed"):
        return "SKIPPED"
    return "FAIL"


def main() -> int:
    rows = []
    for name, feature, evidence, node in SCENARIOS:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", node, "-q", "-p", "no:cacheprovider"],
            check=False,
            capture_output=True,
            text=True,
        )
        rows.append((name, feature, evidence, _status(result)))
    print("| scenario | feature | evidence level | result |")
    print("|---|---|---|---|")
    for name, feature, evidence, row_status in rows:
        print(f"| {name} | {feature} | {evidence} | {row_status} |")
    if any(row[-1] == "SKIPPED" for row in rows):
        print("SKIPPED rows need the PostgreSQL lab instance and M1_DURABLE_POSTGRES=1")
    return 0 if all(row[-1] == "PASS" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
