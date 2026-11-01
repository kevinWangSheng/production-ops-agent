"""Independent fixtures for the PR-A public contract; no evaluation internals.

Report JSON and visible span rows are authored here, not copied from evaluator
output. Evidence hashes use the product's public canonical_hash oracle.
"""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from opspilot.acceptance import IncidentOutcome, IncidentScenario
from opspilot.evaluation import (
    Answer,
    AttemptRecord,
    CalibrationResult,
    DeliveredRecord,
    DimensionValue,
    GateResult,
    InjectedFault,
    JudgedDiagnosis,
    JudgeResult,
    Manifest,
    ReportCapture,
    RunEvaluation,
    ScenarioSpec,
    SignatureRule,
    SpanMatcher,
    Stimulus,
    TraceEntry,
    Window,
)
from opspilot.tools.registry import canonical_hash

DIMENSIONS = (
    "code",
    "upstream",
    "scenarios",
    "answers",
    "evidence_rules",
    "model_request",
    "prompt",
    "tools_schema",
    "projection",
    "registry",
    "profile",
    "adapter",
    "evaluator",
    "judge_config",
    "calibration_pack",
    "permissions",
    "limits",
    "kind_environment",
    "subject_initial_state",
    "session",
    "human_feedback",
    "workload",
    "knowledge_snapshot",
    "replay_fixture",
    "n",
    "seed",
    "command",
    "clock_policy",
    "runbook",
    "dataset",
)
SCENARIO_IDS = (
    "eval-cart-failure",
    "eval-normal-1",
    "eval-normal-2",
    "eval-payment-failure-1",
    "eval-payment-failure-2",
    "eval-product-catalog-failure",
)
START = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=5)
TARGET = "target-demo"
SCOPE = "incident-window"
EID = "ev-cart-visible"
OPERATION = "oteldemo.CartService/EmptyCart"
CAUSE = "cart EmptyCart failed and caused checkout failures."
OBSERVATION = "The visible query returned observations."
CHECKS = ("execution", "report", "permissions", "citations", "signature")


def micros(value):
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def span(**changes):
    row = {
        "service": "cart",
        "operation": OPERATION,
        "span_id": "span-cart",
        "trace_id": "trace-cart",
        "start_us": micros(START + timedelta(seconds=1)),
        "duration_us": 1000,
        "operation_truncated": False,
        "status_tags": {"error": True},
        "status_state": "recorded",
        "error_by_visible_tags": True,
        "error_details": [],
        "parent_references": [],
    }
    row.update(changes)
    return row


def record(*, rows=None, evidence_id=EID, tool="traces_search", **changes):
    view = {
        "tool": tool,
        "status": "ok",
        "trust": "untrusted-evidence",
        "content": [span()] if rows is None else rows,
    }
    base = DeliveredRecord(
        evidence_id=evidence_id,
        target_id=TARGET,
        status="ok",
        citable_as_fact=True,
        time_scope_refs=frozenset({SCOPE}),
        window=Window(START, END),
        view=view,
        view_sha256=canonical_hash(view),
        delivered=True,
    )
    return replace(base, **changes)


def rule(**changes):
    return replace(
        SignatureRule(
            "cart-error-v1",
            (
                SpanMatcher("cart", OPERATION),
                SpanMatcher("checkout", OPERATION),
            ),
            1,
        ),
        **changes,
    )


def claim(text=CAUSE, **changes):
    payload = {
        "kind": "fact",
        "text": text,
        "evidence_ids": [EID],
        "target_refs": [TARGET],
        "time_scope_ref": SCOPE,
    }
    payload.update(changes)
    return payload


def report_payload(*, fault=True, **changes):
    text = CAUSE if fault else OBSERVATION
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": text,
        "claims": [claim(text)],
        "gaps": ["Broader impact remains unmeasured."],
        "next_steps": ["Review the visible evidence with the operator."],
    }
    payload.update(changes)
    return payload


def capture(payload=None, **changes):
    if payload is None:
        payload = report_payload(**changes)
    return ReportCapture(json.dumps(payload, ensure_ascii=False), "stop")


def diagnosis(**changes):
    return replace(
        JudgedDiagnosis(
            location="claims[0]",
            quote=CAUSE,
            factual_causal=True,
            service="cart",
            operation=OPERATION,
            cited_evidence_ids=(EID,),
            linked_claim=None,
            supported=True,
            reason="The cited error span supports it.",
        ),
        **changes,
    )


def judge(*, fault=True, **changes):
    return replace(
        JudgeResult(
            status="ok",
            calibrated=True,
            diagnoses=(diagnosis(),) if fault else (),
            verdict=True,
            config_hash="judge-config-v1",
            calibration_hash="labels-v1",
        ),
        **changes,
    )


def manifest(**changes):
    return replace(
        Manifest(
            round_id="round-contract",
            dimensions={
                **{name: DimensionValue("version", "v1", None) for name in DIMENSIONS},
                "judge_config": DimensionValue("hash", "judge-config-v1", None),
                "calibration_pack": DimensionValue("hash", "labels-v1", None),
            },
        ),
        **changes,
    )


def attempt(**changes):
    frozen = manifest()
    return replace(
        AttemptRecord(
            attempt_id="attempt-1",
            round_id=frozen.round_id,
            manifest_hash=canonical_hash(asdict(frozen)),
            scenario_id="contract-fault",
            scenario_version="v1",
            mode="replay",
            repetition=1,
            started=True,
            status="ran",
            replay_miss=False,
            product_run_id="product-1",
        ),
        **changes,
    )


def trace(**changes):
    return replace(
        TraceEntry(
            role="product_run",
            required=True,
            read_back="found",
            run_id="product-1",
            expected_run_id="product-1",
            product_run_id="product-1",
        ),
        **changes,
    )


def case(*, fault=True):
    ident = "contract-fault" if fault else "contract-normal"
    incident = IncidentScenario(ident, "F1", "eval-contract", "alert", "subject-demo")
    spec = ScenarioSpec(
        ident,
        "v1",
        "fault" if fault else "normal",
        incident,
        Stimulus(
            "Investigate checkout symptoms in the authorized window.",
            "subject-demo",
            TARGET,
        ),
    )
    answer = Answer(
        ident,
        "v1",
        ("Cart EmptyCart failed." if fault else "No causal diagnosis.",),
        InjectedFault(
            "cart",
            ("cartservice",),
            OPERATION,
            ("EmptyCart",),
            "private-cart-flag",
            "private-variant",
        )
        if fault
        else None,
        rule() if fault else None,
    )
    outcome = IncidentOutcome(
        scenario_id=ident,
        final_state="completed",
        evidence_ids=(EID,),
        decision="report_available",
        actions=("read_only_query",),
        permissions=("read_only", "human_control"),
        human_interaction=None,
        handoff_reasons=(),
        report_available=True,
    )
    return {
        "spec": spec,
        "answer": answer,
        "attempt": attempt(scenario_id=ident),
        "outcome": outcome,
        "report": capture(fault=fault),
        "delivered": {EID: record()},
        "authorized": {SCOPE: Window(START, END)},
        "traces": (trace(),),
        "judge": judge(fault=fault),
    }


def gate_args(data):
    return {
        key: data[key]
        for key in (
            "spec",
            "answer",
            "outcome",
            "report",
            "delivered",
            "authorized",
        )
    }


def calibration(**changes):
    return replace(
        CalibrationResult(
            True,
            24,
            48,
            48,
            12,
            0,
            (),
            judge_config_hash="judge-config-v1",
            calibration_pack_hash="labels-v1",
        ),
        **changes,
    )


def evaluation(*, attempt_record=None, failure=None, evidence="complete"):
    """Authored aggregate input, independent of evaluate's implementation."""
    assert failure in {None, "diagnosis", "evidence_incomplete"}
    passed = failure is None
    diagnosis_pass = failure != "diagnosis"
    return RunEvaluation(
        attempt=attempt() if attempt_record is None else attempt_record,
        gate=GateResult(True, dict.fromkeys(CHECKS, True), ()),
        signature_matches={EID: ("span-cart",)},
        judge_status="ok",
        judge_config_hash="judge-config-v1",
        judge_calibration_hash="labels-v1",
        diagnosis_pass=diagnosis_pass,
        evidence_status=evidence,
        deterministic_pass=evidence == "complete",
        judge_pass=diagnosis_pass,
        overall=passed and evidence == "complete",
        failure_class=failure,
        failure_reasons=(
            ()
            if passed
            else (
                "DIAGNOSIS_FAILED" if failure == "diagnosis" else "EVIDENCE_INCOMPLETE",
            )
        ),
    )


def calibration_inputs(*, items=24, negatives=12, runs=2):
    labels = {f"item-{i}": i >= negatives for i in range(items)}
    verdicts = [(key, label) for key, label in labels.items() for _ in range(runs)]
    return labels, verdicts
