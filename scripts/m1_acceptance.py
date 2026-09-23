"""Run the deterministic external M1-01 acceptance scenarios and print a table.

Evidence levels are labels for what each row actually exercises. The
``durable`` rows read snapshots in the ``DurableStore.rebuild`` shape rendered
by ``MemoryStepStore.snapshot()`` after a real loop attempt; PostgreSQL itself
is not touched here. Rows that say ``hand-set`` add a state or marker the
product has no path to emit yet (``late_result_rejected``, ``worker_resumed``,
a cancelled/paused/blocked row state), so they prove the seam's projection of
that state, not DurableStore or worker behaviour.
"""

from __future__ import annotations

import subprocess
import sys

SCENARIOS = (
    (
        "normal-report",
        "F3",
        "deterministic loop + read-only evidence",
        "test_normal_report_is_observable_through_external_seam",
    ),
    (
        "fault-report",
        "F3",
        "deterministic provider failure handoff",
        "test_provider_failure_is_a_visible_handoff_not_a_report",
    ),
    (
        "budget-refusal",
        "F2",
        "deterministic budget refusal",
        "test_budget_refusal_never_looks_completed",
    ),
    (
        "deadline-refusal",
        "F2",
        "deterministic deadline refusal",
        "test_deadline_refusal_never_dispatches_a_model_request",
    ),
    (
        "durable-completed",
        "F3/F8",
        "real loop Run published through MemoryStepStore: conclusion payload read",
        "test_durable_completed_run_projects_its_committed_conclusion",
    ),
    (
        "durable-handoff",
        "F3/F8",
        "real handoff Run published: row says completed, seam reports failed",
        "test_durable_handoff_run_is_not_reported_as_a_completed_report",
    ),
    (
        "pause-cancel",
        "F2/F12",
        "seam projection of hand-set paused/cancelled row states",
        "test_human_pause_and_cancel_are_the_observable_final_authority",
    ),
    (
        "late-result",
        "F2/F12",
        "real committed tool_result rows + hand-set cancelled state and marker",
        "test_late_result_is_rejected_after_newer_human_decision",
    ),
    (
        "worker-restart",
        "F2/F8",
        "real committed conclusion + hand-set worker_resumed marker",
        "test_worker_restart_resumes_from_committed_evidence",
    ),
    (
        "incompatible-state",
        "F2/F8",
        "seam projection of a hand-set blocked row state",
        "test_incompatible_state_is_a_blocked_handoff",
    ),
    (
        "real-deepseek-records",
        "F3",
        "recorded real Runs: projection equals ledger",
        "test_recorded_real_deepseek_runs_project_exactly_what_their_ledger_says",
    ),
    (
        "real-deepseek-report",
        "F3",
        "one recorded real Run: bound report, no handoff",
        "test_a_real_deepseek_run_has_produced_a_bound_report_without_handoff",
    ),
)


def main() -> int:
    rows = []
    for name, feature, evidence, test_name in SCENARIOS:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"tests/acceptance/test_m1_01_acceptance.py::{test_name}",
                "-q",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        rows.append(
            (name, feature, evidence, "PASS" if result.returncode == 0 else "FAIL")
        )
    print("| scenario | feature | evidence level | result |")
    print("|---|---|---|---|")
    for name, feature, evidence, row_status in rows:
        print(f"| {name} | {feature} | {evidence} | {row_status} |")
    return 0 if all(row[-1] == "PASS" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
