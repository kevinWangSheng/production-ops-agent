"""Implementer's unit tests for ``scripts/a7_main_path.py`` (synthetic
summaries in ``scripts/run_freeze.py``'s shape)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import a7_main_path as a7

ROOT = Path(__file__).resolve().parents[1]
RUN = "11111111-1111-1111-1111-111111111111"
OTHER_RUN = "22222222-2222-2222-2222-222222222222"


def _eid(step: str, n: int) -> str:
    return f"{step}-t{n}:aaaaaaaa-0000-0000-0000-00000000000{n}"


STEP = "33333333-3333-3333-3333-333333333333"
OLD_STEP = "44444444-4444-4444-4444-444444444444"
METRIC = _eid(STEP, 1)
TRACE = _eid(STEP, 2)
K8S = _eid(STEP, 3)
LOGS = _eid(STEP, 4)
SLICE = _eid(STEP, 5)
FAILED = _eid(STEP, 6)
INHERITED = _eid(OLD_STEP, 1)


def _request(eid: str, tool: str, code: str = "ok") -> dict[str, Any]:
    return {
        "run_id": RUN,
        "operation_id": eid.rpartition(":")[0],
        "epoch": 1,
        "tool": tool,
        "target_id": "lab",
        "result_code": code,
    }


def _cited(eid: str, tool: str, status: str = "ok", run: str = RUN, view=None):
    return {
        "evidence_id": eid,
        "resolvable": True,
        "tool": tool,
        "status": status,
        "stored": {"run_id": run, "subject_id": "i", "status": status, "view": view},
    }


def _summary(claims: list[dict[str, Any]], *, state: str = "completed") -> dict:
    block = {
        "run_id": RUN,
        "incident_id": "inc",
        "run": {"state": state, "model_requests_used": 7},
        "incident_runs": [{"run_id": RUN, "control_generation": 0, "state": state}],
        "versions": {
            "prompt_revision": "prompt-replay-candidate-k8s-abc",
            "tool_schema_revision": "otel-demo-ext-def",
        },
        "report": {"claims": claims},
        "tokens": {"total_tokens": 10},
        "tool_requests": [
            _request(METRIC, "metrics_range_query"),
            _request(TRACE, "traces_search_by_tags"),
            _request(K8S, "kubernetes_tabular_query"),
            _request(LOGS, "fetch_pod_logs"),
            _request(SLICE, "read_evidence"),
            _request(FAILED, "kubernetes_count", "SOURCE_UNAVAILABLE"),
        ],
        "cited_evidence": [
            _cited(METRIC, "metrics_range_query"),
            _cited(TRACE, "traces_search_by_tags"),
            _cited(K8S, "kubernetes_tabular_query"),
            _cited(LOGS, "fetch_pod_logs"),
            _cited(
                SLICE,
                "read_evidence",
                view={"parent_tool": "traces_search", "slice": {}},
            ),
            _cited(INHERITED, "kubernetes_jq_query", run=OTHER_RUN),
        ],
        "audit": {
            "entries": [
                {
                    "category": "input",
                    "source": "opspilot_alert_deliveries",
                    "actor": "alertmanager",
                    "run_id": None,
                    "detail": {"status": "firing", "outcome": "created"},
                },
                {
                    "category": "state_change",
                    "source": "opspilot_subject_events",
                    "actor": None,
                    "run_id": RUN,
                    "detail": {"kind": "intake_accepted", "sequence": 1},
                },
            ]
        },
    }
    return {"schema": "opspilot-lab-summary-1", "run": block, "trace": {}}


def _claim(kind: str, *eids: str) -> dict[str, Any]:
    return {"kind": kind, "text": "x", "evidence_ids": list(eids)}


def test_four_categories_and_kind_distribution() -> None:
    summary = _summary(
        [
            _claim("fact", METRIC, TRACE),
            _claim("hypothesis", K8S),
            _claim("counter_evidence", LOGS),
            _claim("fact", METRIC),
        ]
    )
    assert a7.cited_categories(summary) == {
        "metrics": {"fact": 2},
        "traces": {"fact": 1},
        "cluster_objects": {"hypothesis": 1},
        "logs": {"counter_evidence": 1},
    }
    assert a7.all_four_cited(summary)


def test_slice_counts_for_its_parent_tool() -> None:
    summary = _summary([_claim("fact", SLICE)])
    assert a7.cited_categories(summary) == {"traces": {"fact": 1}}


def test_slice_parent_resolved_through_parent_evidence_id() -> None:
    summary = _summary([_claim("fact", SLICE)])
    for item in summary["run"]["cited_evidence"]:
        if item["evidence_id"] == SLICE:
            item["stored"]["view"] = {"slice": {"parent_evidence_id": METRIC}}
    assert a7.cited_categories(summary) == {"metrics": {"fact": 1}}


def test_inherited_failed_and_unknown_evidence_do_not_count() -> None:
    summary = _summary(
        [_claim("fact", INHERITED), _claim("fact", FAILED), _claim("fact", "bogus")]
    )
    assert a7.cited_categories(summary) == {}
    assert not a7.all_four_cited(summary)


def test_not_ok_cited_status_does_not_count() -> None:
    summary = _summary([_claim("fact", LOGS)])
    for item in summary["run"]["cited_evidence"]:
        if item["evidence_id"] == LOGS:
            item["status"] = "no_data"
    assert a7.cited_categories(summary) == {}


def test_unpublished_report_counts_nothing() -> None:
    summary = _summary([_claim("fact", METRIC)], state="waiting_human")
    assert a7.cited_categories(summary) == {}
    text = _summary([_claim("fact", METRIC)])
    text["run"]["report"] = "not json"
    assert a7.cited_categories(text) == {}


def test_block_accepted_directly() -> None:
    summary = _summary([_claim("fact", METRIC)])
    assert a7.cited_categories(summary["run"]) == {"metrics": {"fact": 1}}


def test_category_constants_come_from_code() -> None:
    from opspilot.tools import otel_demo_ext

    served = set(otel_demo_ext.TOOL_NAMES) - {"read_evidence"}
    classified = set().union(*a7.CATEGORY_TOOLS.values())
    assert served <= classified
    assert "get_metric_metadata" in a7.CATEGORY_TOOLS["metrics"]  # B2


def test_human_free_holds_for_alert_opened_run() -> None:
    assert a7.human_free(_summary([]))


@pytest.mark.parametrize(
    "entry, problem",
    [
        (
            {
                "category": "human_correction",
                "source": "opspilot_controls",
                "actor": "alice",
                "detail": {"action": "pause"},
            },
            "HUMAN_CONTROL",
        ),
        (
            {
                "category": "input",
                "source": "opspilot_inputs",
                "actor": "alice",
                "detail": {"kind": "follow_up"},
            },
            "HUMAN_INPUT",
        ),
        (
            {
                "category": "state_change",
                "source": "opspilot_suspension_audit",
                "actor": "alice",
                "detail": {"suspended": True},
            },
            "TARGET_SUSPENSION",
        ),
    ],
)
def test_human_actions_break_human_free(entry: dict, problem: str) -> None:
    summary = _summary([])
    summary["run"]["audit"]["entries"].append(entry)
    assert problem in a7.human_free_problems(summary)
    assert not a7.human_free(summary)


def test_alertmanager_attached_input_and_baseline_suspension_are_fine() -> None:
    summary = _summary([])
    summary["run"]["audit"]["entries"] += [
        {
            "category": "input",
            "source": "opspilot_inputs",
            "actor": "alertmanager",
            "detail": {},
        },
        {
            "category": "state_change",
            "source": "opspilot_suspension_audit",
            "actor": "x",
            "detail": {"baseline": True},
        },
        {
            "category": "human_correction",
            "source": "opspilot_controls",
            "actor": "system",
            "detail": {"action": "auto_authorize_observation"},
        },
    ]
    assert a7.human_free(summary)
    summary["run"]["audit"]["entries"][-1]["detail"]["action"] = "close"
    assert not a7.human_free(summary)


def test_human_opened_or_second_run_is_not_human_free() -> None:
    human = _summary([])
    human["run"]["audit"]["entries"][0]["actor"] = "alice"
    assert "DELIVERY_NOT_ALERTMANAGER" in a7.human_free_problems(human)
    second = _summary([])
    second["run"]["incident_runs"].append({"run_id": OTHER_RUN})
    assert "NOT_THE_INCIDENTS_ONLY_RUN" in a7.human_free_problems(second)
    handoff = _summary([])
    handoff["run"]["audit"]["entries"][0]["detail"]["outcome"] = "handoff_created"
    assert "NOT_OPENED_BY_ONE_ALERT" in a7.human_free_problems(handoff)
    late = _summary([])
    late["a7"] = {"worker_running_before_alert": False}
    assert not a7.human_free(late)


def test_compare_tally_and_attempt_limit() -> None:
    before = _summary([_claim("fact", METRIC)])
    before["run"]["versions"]["prompt_revision"] = "prompt-replay-candidate-x"
    good = _summary(
        [_claim("fact", METRIC, TRACE, K8S, LOGS)],
    )
    bad = copy.deepcopy(before)
    result = a7.compare(before, [bad, good])
    assert result["after_reached"] == "1/2" and result["done"]
    assert result["before"]["tool_failures"] == 1
    assert result["after"][1]["tool_calls_by_category"]["read_evidence"] == 1
    with pytest.raises(ValueError):
        a7.compare(before, [good] * 4)
    assert a7.side_versions_ok("before", before["run"]["versions"])
    assert not a7.side_versions_ok("before", good["run"]["versions"])
    assert a7.side_versions_ok("after", good["run"]["versions"])


def test_dry_run_touches_nothing_and_limits_attempts() -> None:
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "scripts.a7_main_path", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin"},
        )

    ok = run("--dry-run", "run", "--side", "after", "--attempt", "3")
    assert ok.returncode == 0, ok.stderr
    steps = json.loads(ok.stdout)["steps"]
    assert any("fault inject" in s and "paymentFailure" in s for s in steps)
    assert any("fault restore" in s for s in steps)
    assert run("--dry-run", "run", "--side", "after", "--attempt", "4").returncode
    assert run("--dry-run", "run", "--side", "before", "--attempt", "2").returncode
