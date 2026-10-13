"""Per-Run evaluation: gate, judge-output checks and diagnosis recomputation.

The judge decides which statements are factual causal diagnoses (r6); the code
re-checks every location, quote and citation it returns and recomputes the
verdict, so a judge can fail a Run but cannot pass one the rules reject
(r7 P6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.gate import (
    RunScope,
    cited_ids,
    fact_binding_reasons,
    in_scope,
    parsed_report,
    report_signature_matches,
    run_gate,
    run_scope,
)
from opspilot.evaluation.model import (
    FAILURE_PRIORITY,
    Answer,
    AttemptRecord,
    DeliveredRecord,
    FailureClass,
    JudgedDiagnosis,
    JudgeResult,
    ReportCapture,
    RunEvaluation,
    ScenarioSpec,
    TraceEntry,
    Window,
)
from opspilot.investigation.reports import ReportV2

_LOCATION = re.compile(r"^(summary)$|^(claims|gaps|next_steps)\[(\d+)\]$")
_SPACE = re.compile(r"\s+")
_ATTEMPT_REASON = {
    "setup_failed": "SETUP_FAILED",
    "aborted": "ATTEMPT_ABORTED",
    "unknown": "ATTEMPT_UNKNOWN",
}


def _squash(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _location_text(report: ReportV2, location: str) -> tuple[str, int | None] | None:
    """The text at ``location`` and, for a claim, its index."""
    match = _LOCATION.match(location)
    if match is None:
        return None
    if match.group(1):
        return report.summary, None
    field, index = match.group(2), int(match.group(3))
    if field == "claims":
        if index >= len(report.claims):
            return None
        return report.claims[index].text, index
    items = report.gaps if field == "gaps" else report.next_steps
    if index >= len(items):
        return None
    return items[index], None


def _judge_output_reasons(report: ReportV2, judge: JudgeResult) -> list[str]:
    reasons: list[str] = []
    for diagnosis in judge.diagnoses:
        located = _location_text(report, diagnosis.location)
        if located is None:
            reasons.append("JUDGE_BAD_LOCATION")
            continue
        text, claim_index = located
        quote = _squash(diagnosis.quote)
        if not quote or quote not in _squash(text):
            reasons.append("JUDGE_QUOTE_NOT_FOUND")
        cited = set(diagnosis.cited_evidence_ids)
        if claim_index is not None:
            allowed = set(report.claims[claim_index].evidence_ids)
        elif diagnosis.linked_claim is None:
            allowed = set()
        elif 0 <= diagnosis.linked_claim < len(report.claims):
            allowed = set(report.claims[diagnosis.linked_claim].evidence_ids)
        else:
            allowed = None
        if allowed is None or not cited <= allowed:
            reasons.append("JUDGE_CITATION_NOT_IN_CLAIM")
    return list(dict.fromkeys(reasons))


def _names(value: str | None, canonical: str, aliases: tuple[str, ...]) -> bool:
    if value is None:
        return False
    wanted = {name.casefold() for name in (canonical, *aliases)}
    return value.strip().casefold() in wanted


def _diagnosis_pass(
    spec: ScenarioSpec,
    answer: Answer,
    diagnoses: Sequence[JudgedDiagnosis],
    matches: Mapping[str, tuple[str, ...]],
) -> bool:
    causal = [d for d in diagnoses if d.factual_causal]
    if spec.kind == "normal":
        return not causal
    injected = answer.injected
    if injected is None or not causal:
        return False
    return all(
        _names(d.service, injected.service, injected.service_aliases)
        and _names(d.operation, injected.operation, injected.operation_aliases)
        and d.supported
        and any(eid in matches for eid in d.cited_evidence_ids)
        for d in causal
    )


def _anchor_claim(report: ReportV2, diagnosis: JudgedDiagnosis) -> int | None:
    """The claim whose target/time refs a diagnosis's citations rest on."""
    located = _location_text(report, diagnosis.location)
    if located is not None and located[1] is not None:
        return located[1]
    return diagnosis.linked_claim


def _unqualified_diagnoses(
    report: ReportV2,
    diagnoses: Sequence[JudgedDiagnosis],
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
    scope: RunScope,
) -> bool:
    """True when a factual causal diagnosis cites evidence that cannot back a fact.

    The report's own claim kind does not matter: a hypothesis the judge reads
    as a factual causal diagnosis is held to fact evidence rules (review P1).
    """
    for diagnosis in diagnoses:
        if not diagnosis.factual_causal:
            continue
        index = _anchor_claim(report, diagnosis)
        claim = report.claims[index] if index is not None else None
        for eid in diagnosis.cited_evidence_ids:
            record = delivered.get(eid)
            if (
                claim is None
                or not in_scope(eid, record, scope)
                or fact_binding_reasons(
                    claim.target_refs, claim.time_scope_ref, record, authorized, scope
                )
            ):
                return True
    return False


def _evidence_complete(traces: Sequence[TraceEntry], attempt: AttemptRecord) -> bool:
    """Every required trace read back and bound to this attempt's product Run.

    An attempt that never produced a product Run (setup failure) keeps its
    evaluation root trace with ``product_run_id`` None on both sides (r7 P2);
    a Run that ran must name its product Run.
    """
    if attempt.status == "ran" and attempt.product_run_id is None:
        return False
    required = [entry for entry in traces if entry.required]
    return bool(required) and all(
        entry.read_back == "found"
        and entry.run_id is not None
        and entry.run_id == entry.expected_run_id
        and entry.product_run_id == attempt.product_run_id
        for entry in required
    )


def evaluate(
    spec: ScenarioSpec,
    answer: Answer,
    attempt: AttemptRecord,
    outcome: IncidentOutcome | None,
    report: ReportCapture | None,
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
    traces: Sequence[TraceEntry],
    judge: JudgeResult | None,
) -> RunEvaluation:
    if not attempt.started:
        raise ValueError("ATTEMPT_NOT_STARTED")
    identity = (spec.scenario_id, spec.version)
    mismatched = (
        (attempt.scenario_id, attempt.scenario_version) != identity
        or (answer.scenario_id, answer.version) != identity
        or (outcome is not None and outcome.scenario_id != spec.scenario_id)
    )
    if mismatched:
        raise ValueError("SCENARIO_MISMATCH")
    classes: set[FailureClass] = set()
    reasons: list[str] = []

    if attempt.status != "ran":
        classes.add("setup")
        reasons.append(_ATTEMPT_REASON[attempt.status])
    if attempt.replay_miss:
        classes.add("replay_miss")
        reasons.append("REPLAY_MISS")

    gate = run_gate(spec, answer, outcome, report, delivered, authorized)
    for code in gate.reasons:
        classes.add(
            "execution" if code == "EXECUTION_NOT_COMPLETED" else "deterministic_gate"
        )
        reasons.append(code)

    parsed = parsed_report(report)
    scope = run_scope(spec, outcome)
    matches = report_signature_matches(answer, parsed, delivered, scope)

    judge_status: Any
    diagnosis_pass: bool | None = None
    if judge is None:
        judge_status = "skipped" if not gate.passed else "not_run"
        if gate.passed:
            reasons.append("JUDGE_NOT_RUN")
    elif judge.status != "ok":
        judge_status = judge.status
        reasons.append(f"JUDGE_{judge.status.upper()}")
    elif not judge.calibrated:
        judge_status = "uncalibrated"
        reasons.append("JUDGE_UNCALIBRATED")
    elif parsed is None:
        judge_status = "invalid"
        reasons.append("JUDGE_BAD_LOCATION")
    else:
        output_reasons = _judge_output_reasons(parsed, judge)
        recomputed = _diagnosis_pass(spec, answer, judge.diagnoses, matches)
        if judge.verdict is not recomputed:
            output_reasons.append("JUDGE_VERDICT_MISMATCH")
        if output_reasons:
            judge_status = "invalid"
            reasons.extend(output_reasons)
        else:
            judge_status = "ok"
            unqualified = _unqualified_diagnoses(
                parsed, judge.diagnoses, delivered, authorized, scope
            )
            diagnosis_pass = recomputed and not unqualified
            if unqualified:
                classes.add("diagnosis")
                reasons.append("DIAGNOSIS_EVIDENCE_UNQUALIFIED")
            if not recomputed:
                classes.add("diagnosis")
                reasons.append("DIAGNOSIS_FAILED")
    if judge_status not in ("ok", "skipped"):
        classes.add("judge_error")

    evidence_complete = _evidence_complete(traces, attempt)
    if not evidence_complete:
        classes.add("evidence_incomplete")
        reasons.append("EVIDENCE_INCOMPLETE")

    deterministic_pass = (
        attempt.status == "ran"
        and not attempt.replay_miss
        and gate.passed
        and evidence_complete
    )
    judge_pass = judge_status == "ok" and diagnosis_pass is True
    overall = deterministic_pass and judge_pass
    failure_class: FailureClass | None = None
    if not overall:
        failure_class = next(
            (name for name in FAILURE_PRIORITY if name in classes), "judge_error"
        )
    return RunEvaluation(
        attempt=attempt,
        gate=gate,
        signature_matches=matches,
        judge_status=judge_status,
        diagnosis_pass=diagnosis_pass,
        evidence_status="complete" if evidence_complete else "incomplete",
        deterministic_pass=deterministic_pass,
        judge_pass=judge_pass,
        overall=overall,
        failure_class=failure_class,
        failure_reasons=tuple(dict.fromkeys(reasons)),
    )


def judge_request(
    spec: ScenarioSpec,
    answer: Answer,
    report: ReportCapture,
    delivered: Mapping[str, DeliveredRecord],
    authorized: Mapping[str, Window],
    outcome: IncidentOutcome | None = None,
) -> Mapping[str, Any]:
    """The judge's input (r7 P6); building it sends nothing."""
    parsed = parsed_report(report)
    cited = cited_ids(parsed) if parsed is not None else ()
    # Only this Run's evidence on the scenario target; without an outcome
    # the delivered records stand in for the Run's evidence IDs.
    scope = RunScope(
        evidence_ids=frozenset(outcome.evidence_ids)
        if outcome is not None
        else frozenset(delivered),
        target_id=spec.stimulus.target_id,
    )
    evidence = [
        {
            "evidence_id": eid,
            "view_sha256": delivered[eid].view_sha256,
            "view": dict(delivered[eid].view),
        }
        for eid in cited
        if in_scope(eid, delivered.get(eid), scope)
    ]
    injected = answer.injected
    return {
        "question": spec.stimulus.question,
        "subject_id": spec.stimulus.subject_id,
        "authorized_windows": {
            ref: {"start": w.start.isoformat(), "end": w.end.isoformat()}
            for ref, w in sorted(authorized.items())
        },
        "report_raw": report.raw,
        "cited_evidence": evidence,
        "expected_output": list(answer.expected_output),
        "scenario_kind": spec.kind,
        "injected": None
        if injected is None
        else {
            "service": injected.service,
            "service_aliases": list(injected.service_aliases),
            "operation": injected.operation,
            "operation_aliases": list(injected.operation_aliases),
        },
        "signature_matches": {
            eid: list(spans)
            for eid, spans in report_signature_matches(
                answer, parsed, delivered, scope
            ).items()
        },
    }
