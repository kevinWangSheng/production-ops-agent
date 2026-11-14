"""Pure contract tests for the A7 main-path deterministic predicates.

The implementation is intentionally absent from this worktree. The import of
scripts.a7_main_path is therefore expected to fail on main until the
implementation worktree supplies the public functions.

接缝假设（合成输入必须与当前冻结脚本一致）：

* The public functions accept the complete frozen object returned by
  scripts.run_freeze.freeze_run. Its run-specific data is under the
  summary["run"] mapping; this test does not flatten that object or add
  convenience aliases that run_freeze does not write.
* A published, validated report is represented by
  summary["run"]["run"]["state"] == "completed", together with
  summary["run"]["report_present"] and a parsed
  summary["run"]["report"] in the m0-report-v2 shape. A missing/invalid report
  has no parsed report; an unpublished handoff keeps a report but has a
  non-completed Run state.
* Claims are read from summary["run"]["report"]["claims"]. Each cited
  evidence id is resolved by matching the part before ":" to the same-Run
  summary["run"]["tool_requests"] entry's operation_id; the request's
  result_code is the status gate. An evidence id that only exists in the
  frozen incident projection is treated as inherited and does not count.
* A read_evidence request is classified by the source tool in its frozen cited
  view's existing stored.view.parent_tool field. No new parent field is
  invented in the synthetic summary.
* human_free reads the frozen run.audit.entries timeline. A firing delivery
  by actor alertmanager plus an intake-accepted state entry is the automatic
  queue path; any human follow-up, pause, takeover or close entry makes the
  predicate false. The worker-precedes-alert fact is intentionally outside
  this pure summary seam.

All identifiers and credentials are synthetic low-entropy placeholders.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest

from scripts.a7_main_path import all_four_cited, cited_categories, human_free

RUN_ID = "run-aaaaaaaa357"
INCIDENT_ID = "incident-aaaaaaaa357"
_DEFAULT_REPORT = object()


def _claim(kind: str, evidence_id: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": f"synthetic {kind}",
        "evidence_ids": [evidence_id],
        "target_refs": ["m0-otel-20260909"],
        "time_scope_ref": "policy-window-1",
    }


def _evidence_id(label: str) -> str:
    return f"op-{label}:dispatch-{label}"


def _request(
    label: str,
    tool: str,
    *,
    result_code: str = "ok",
) -> dict[str, Any]:
    operation_id = f"op-{label}"
    return {
        "run_id": RUN_ID,
        "operation_id": operation_id,
        "epoch": 1,
        "tool": tool,
        "target_id": "m0-otel-20260909",
        "result_code": result_code,
        "reason": None if result_code == "ok" else "synthetic-failure",
        "outbound": result_code == "ok",
        "source_contact": "confirmed" if result_code == "ok" else "none",
        "time_basis": "tool_event",
        "record_kind": "result",
    }


def _cited_view(
    evidence_id: str,
    tool: str,
    *,
    status: str = "ok",
    parent_tool: str | None = None,
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "evidence_id": evidence_id,
        "tool": tool,
        "status": status,
        "source": "synthetic-source",
        "target_id": "m0-otel-20260909",
    }
    if parent_tool is not None:
        view["parent_tool"] = parent_tool
        view["slice"] = {
            "parent_evidence_id": _evidence_id("parent-aaaaaaaa357"),
            "returned": 1,
        }
    return {
        "evidence_id": evidence_id,
        "resolvable": True,
        "tool": tool,
        "status": status,
        "stored": {
            "run_id": run_id,
            "subject_id": INCIDENT_ID,
            "status": status,
            "view": view,
        },
    }


def _report(claims: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": "synthetic report",
        "claims": claims,
        "gaps": [],
        "next_steps": [],
    }


def _audit(*entries: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "incident_id": INCIDENT_ID,
        "entries": [dict(entry) for entry in entries],
        "counts": {},
        "missing": [],
    }


def _entry(
    *,
    category: str,
    source: str,
    actor: str | None,
    detail: Mapping[str, Any],
    sequence: int,
    run_id: str | None = RUN_ID,
) -> dict[str, Any]:
    return {
        "recorded_at": "2026-11-13T12:00:00+00:00",
        "sequence": sequence,
        "category": category,
        "source": source,
        "record_id": f"record-{sequence}",
        "run_id": run_id,
        "actor": actor,
        "detail": dict(detail),
    }


def _summary(
    claims: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    *,
    cited_views: list[dict[str, Any]] | None = None,
    state: str = "completed",
    report: Mapping[str, Any] | None | object = _DEFAULT_REPORT,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parsed_report = (
        _report(claims)
        if report is _DEFAULT_REPORT
        else dict(report)
        if isinstance(report, Mapping)
        else None
    )
    report_present = report is _DEFAULT_REPORT or isinstance(report, Mapping)
    return {
        "schema": "opspilot-lab-summary-1",
        "experiment": "opspilot-lab-a7-main-path",
        "run_id": RUN_ID,
        "frozen_at": "2026-11-13T12:01:00+00:00",
        "run": {
            "run_id": RUN_ID,
            "incident_id": INCIDENT_ID,
            "run": {
                "state": state,
                "control_generation": 0,
                "budget_limit": 100,
                "model_requests_used": 4,
                "deadline": "2026-11-13T12:04:00+00:00",
                "bound_target_id": "m0-otel-20260909",
                "question": "Why is checkout degraded?",
                "authorized_window": {
                    "start": "2026-11-13T11:00:00+00:00",
                    "end": "2026-11-13T12:00:00+00:00",
                },
            },
            "report": parsed_report,
            "report_present": report_present,
            "conclusion_step": {
                "sequence": 5,
                "logical_key": "ctx0:final",
                "status": "response_committed",
                "observed_at": "2026-11-13T12:00:30+00:00",
            },
            "incident_runs": [
                {
                    "run_id": RUN_ID,
                    "control_generation": 0,
                    "state": state,
                }
            ],
            "tool_requests": deepcopy(requests),
            "observations": [],
            "cited_evidence": cited_views or [],
            "audit": audit or _audit(),
            "audit_undated": {},
            "alert_delivery_outcomes": ["created"],
            "alert_deliveries": [
                {
                    "delivery_id": 1,
                    "fingerprint": "fingerprint-aaaaaaaa357",
                    "incident_id": INCIDENT_ID,
                    "outcome": "created",
                    "received_at": "2026-11-13T12:00:00+00:00",
                    "starts_at": "2026-11-13T11:59:00+00:00",
                    "status": "firing",
                }
            ],
        },
        "trace": {"project": None, "read_back": "not_exported", "roots": []},
    }


def _four_category_summary() -> dict[str, Any]:
    claims = [
        _claim("fact", _evidence_id("metric-aaaaaaaa357")),
        _claim("hypothesis", _evidence_id("trace-aaaaaaaa357")),
        _claim("counter_evidence", _evidence_id("cluster-aaaaaaaa357")),
        _claim("rejected_hypothesis", _evidence_id("log-aaaaaaaa357")),
        _claim("recommendation", _evidence_id("metric-2-aaaaaaaa357")),
    ]
    requests = [
        _request("metric-aaaaaaaa357", "metrics_range_query"),
        _request("metric-2-aaaaaaaa357", "get_metric_names"),
        _request("trace-aaaaaaaa357", "traces_search"),
        _request("cluster-aaaaaaaa357", "kubernetes_jq_query"),
        _request("log-aaaaaaaa357", "fetch_pod_logs"),
    ]
    return _summary(
        claims,
        requests,
        cited_views=[
            _cited_view(eid, tool)
            for eid, tool in (
                (_evidence_id("metric-aaaaaaaa357"), "metrics_range_query"),
                (_evidence_id("metric-2-aaaaaaaa357"), "get_metric_names"),
                (_evidence_id("trace-aaaaaaaa357"), "traces_search"),
                (_evidence_id("cluster-aaaaaaaa357"), "kubernetes_jq_query"),
                (_evidence_id("log-aaaaaaaa357"), "fetch_pod_logs"),
            )
        ],
    )


def test_cited_categories_counts_all_claim_kinds_and_all_four_sources() -> None:
    summary = _four_category_summary()
    assert cited_categories(summary) == {
        "metrics": {"fact": 1, "recommendation": 1},
        "traces": {"hypothesis": 1},
        "cluster_objects": {"counter_evidence": 1},
        "logs": {"rejected_hypothesis": 1},
    }
    assert all_four_cited(summary) is True


def test_missing_one_source_category_fails_the_four_source_predicate() -> None:
    summary = _four_category_summary()
    summary["run"]["report"]["claims"] = [
        claim
        for claim in summary["run"]["report"]["claims"]
        if claim["kind"] != "rejected_hypothesis"
    ]
    assert "logs" not in cited_categories(summary)
    assert all_four_cited(summary) is False


def test_failed_evidence_does_not_count_as_a_citation() -> None:
    evidence_id = _evidence_id("failed-metric-aaaaaaaa357")
    summary = _summary(
        [_claim("fact", evidence_id)],
        [
            _request(
                "failed-metric-aaaaaaaa357", "metrics_range_query", result_code="error"
            )
        ],
        cited_views=[
            _cited_view(
                evidence_id,
                "metrics_range_query",
                status="error",
            )
        ],
    )
    assert cited_categories(summary) == {}


def test_inherited_evidence_without_a_same_run_tool_request_does_not_count() -> None:
    evidence_id = _evidence_id("inherited-trace-aaaaaaaa357")
    summary = _summary(
        [_claim("fact", evidence_id)],
        [],
        cited_views=[
            _cited_view(
                evidence_id,
                "traces_search",
                run_id="prior-run-aaaaaaaa357",
            )
        ],
    )
    assert cited_categories(summary) == {}


def test_read_evidence_slice_is_classified_by_its_parent_tool() -> None:
    evidence_id = _evidence_id("trace-slice-aaaaaaaa357")
    summary = _summary(
        [_claim("counter_evidence", evidence_id)],
        [_request("trace-slice-aaaaaaaa357", "read_evidence")],
        cited_views=[
            _cited_view(
                evidence_id,
                "read_evidence",
                parent_tool="traces_search",
            )
        ],
    )
    assert cited_categories(summary) == {"traces": {"counter_evidence": 1}}


@pytest.mark.parametrize(
    "summary",
    [
        _summary([], [], report=None, state="failed"),
        _summary(
            [_claim("fact", _evidence_id("metric-aaaaaaaa357"))],
            [_request("metric-aaaaaaaa357", "metrics_range_query")],
            state="waiting_human",
        ),
    ],
)
def test_unvalidated_or_unpublished_report_has_no_cited_categories(summary) -> None:
    assert cited_categories(summary) == {}


def test_human_free_is_true_for_alertmanager_auto_queued_run_without_controls() -> None:
    summary = _four_category_summary()
    summary["run"]["audit"] = _audit(
        _entry(
            category="input",
            source="opspilot_alert_deliveries",
            actor="alertmanager",
            detail={"status": "firing", "outcome": "created"},
            sequence=0,
            run_id=None,
        ),
        _entry(
            category="state_change",
            source="opspilot_subject_events",
            actor=None,
            detail={"kind": "intake_accepted"},
            sequence=1,
        ),
        _entry(
            category="state_change",
            source="opspilot_runs",
            actor=None,
            detail={"action": "created"},
            sequence=2,
        ),
    )
    assert human_free(summary) is True


def test_human_free_is_false_when_a_human_follow_up_is_in_the_audit() -> None:
    summary = _four_category_summary()
    summary["run"]["audit"] = _audit(
        _entry(
            category="input",
            source="opspilot_alert_deliveries",
            actor="alertmanager",
            detail={"status": "firing", "outcome": "created"},
            sequence=0,
            run_id=None,
        ),
        _entry(
            category="human_correction",
            source="opspilot_controls",
            actor="operator-aaaaaaaa357",
            detail={"action": "follow_up"},
            sequence=1,
        ),
    )
    assert human_free(summary) is False
