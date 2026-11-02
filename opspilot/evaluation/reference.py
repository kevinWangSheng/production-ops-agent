"""Reference reports of the v2 capability group (#432, V2-R6).

Interface: ``~/.agent/handoffs/m3-b3-2026-11-02/seams-432.md`` sections 3-4
and 10 (A2).

A fresh-context agent writes each scenario's reference ReportV2 by hand from
the evidence a probe Run actually delivered to its model, frozen as
``reference/evidence.json`` beside ``reference/report.json`` (answer side,
never model input). ``check_reference`` validates it offline with the same
deterministic gate the evaluator runs on a live report, against a reference
outcome that grants exactly the probe's evidence. A scenario whose correct
behaviour is no report (decision D6) instead keeps the probe's PG projection
as ``reference/projection.json`` and is checked with ``check_expectations``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.expectations import (
    ExpectationResult,
    category_rules,
    check_expectations,
)
from opspilot.evaluation.gate import parsed_report, run_gate
from opspilot.evaluation.model import (
    DeliveredRecord,
    GateResult,
    ReportCapture,
    Window,
    load_scenario_v2,
)

EVIDENCE_SCHEMA = "opspilot-eval-reference-evidence-1"
REFERENCE_DIR = "reference"
EVIDENCE_FILE = "reference/evidence.json"
REPORT_FILE = "reference/report.json"
PROJECTION_FILE = "reference/projection.json"
UNSUPPORTED_CONCLUSION = "UNSUPPORTED_CONCLUSION"

_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "scenario_id",
        "probe",
        "stimulus",
        "target_id",
        "authorized",
        "outcome_evidence_ids",
        "delivered",
    }
)
_RECORD_KEYS = frozenset(
    {
        "evidence_id",
        "target_id",
        "status",
        "citable_as_fact",
        "time_scope_refs",
        "window",
        "view",
        "view_sha256",
        "delivered",
    }
)


class ReferenceError(Exception):
    """A reference file that cannot be read as such (not a gate failure)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _invalid() -> ReferenceError:
    return ReferenceError("REFERENCE_EVIDENCE_INVALID")


def _moment(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _invalid()
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid() from None
    if moment.tzinfo is None:
        raise _invalid()
    return moment.astimezone(timezone.utc)


def window_json(window: Window | None) -> dict[str, str] | None:
    if window is None:
        return None
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def _window(value: Any) -> Window | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
        raise _invalid()
    try:
        return Window(_moment(value["start"]), _moment(value["end"]))
    except ValueError:
        raise _invalid() from None


def record_json(record: DeliveredRecord) -> dict[str, Any]:
    """One delivered record as ``evidence.json`` stores it."""
    return {
        "evidence_id": record.evidence_id,
        "target_id": record.target_id,
        "status": record.status,
        "citable_as_fact": record.citable_as_fact,
        "time_scope_refs": sorted(record.time_scope_refs),
        "window": window_json(record.window),
        "view": dict(record.view),
        "view_sha256": record.view_sha256,
        "delivered": record.delivered,
    }


def _record(value: Any) -> DeliveredRecord:
    if not isinstance(value, Mapping) or set(value) != _RECORD_KEYS:
        raise _invalid()
    refs = value["time_scope_refs"]
    view = value["view"]
    if (
        not isinstance(value["evidence_id"], str)
        or not isinstance(value["target_id"], str)
        or not isinstance(value["status"], str)
        or not isinstance(value["citable_as_fact"], bool)
        or not isinstance(value["delivered"], bool)
        or not isinstance(value["view_sha256"], str)
        or not isinstance(view, Mapping)
        or not isinstance(refs, list)
        or not all(isinstance(ref, str) for ref in refs)
    ):
        raise _invalid()
    return DeliveredRecord(
        evidence_id=value["evidence_id"],
        target_id=value["target_id"],
        status=value["status"],
        citable_as_fact=value["citable_as_fact"],
        time_scope_refs=frozenset(refs),
        window=_window(value["window"]),
        view=view,
        view_sha256=value["view_sha256"],
        delivered=value["delivered"],
    )


def load_evidence(
    path: Path,
) -> tuple[Mapping[str, Any], dict[str, DeliveredRecord], dict[str, Window]]:
    """``(document, delivered, authorized)`` of one ``evidence.json``."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _invalid() from None
    if not isinstance(document, Mapping) or set(document) != _EVIDENCE_KEYS:
        raise _invalid()
    if document["schema"] != EVIDENCE_SCHEMA:
        raise _invalid()
    authorized_raw = document["authorized"]
    ids = document["outcome_evidence_ids"]
    records = document["delivered"]
    if (
        not isinstance(authorized_raw, Mapping)
        or not isinstance(ids, list)
        or not all(isinstance(eid, str) for eid in ids)
        or not isinstance(records, list)
        or not isinstance(document["target_id"], str)
    ):
        raise _invalid()
    authorized: dict[str, Window] = {}
    for key, value in authorized_raw.items():
        window = _window(value)
        if window is None:
            raise _invalid()
        authorized[str(key)] = window
    delivered: dict[str, DeliveredRecord] = {}
    for item in records:
        record = _record(item)
        if record.evidence_id in delivered:
            raise _invalid()
        delivered[record.evidence_id] = record
    return document, delivered, authorized


def reference_outcome(scenario_id: str, evidence_ids: list[str]) -> IncidentOutcome:
    """The outcome a hand-written reference is checked against: a completed
    read-only Run whose evidence is exactly the probe's."""
    return IncidentOutcome(
        scenario_id=scenario_id,
        final_state="completed",
        evidence_ids=tuple(evidence_ids),
        decision="report_available",
        actions=("read_only_query",) if evidence_ids else (),
        permissions=("read_only",),
        human_interaction=None,
        handoff_reasons=(),
        report_available=True,
    )


def check_reference(scenario_dir: Path) -> GateResult:
    """Validate ``reference/report.json`` against ``reference/evidence.json``
    with ``run_gate`` (seams-432 section 4).

    The missing-telemetry positive (decisions D3/D4) is exempt from the
    evidence signature and must not be ``supported`` and must name a gap
    (``UNSUPPORTED_CONCLUSION``). A malformed ``evidence.json`` raises
    ``ReferenceError``; a missing or unparsable report fails the gate.
    """
    spec, answer = load_scenario_v2(scenario_dir)
    document, delivered, authorized = load_evidence(scenario_dir / EVIDENCE_FILE)
    if (
        document["scenario_id"] != spec.scenario_id
        or document["target_id"] != spec.stimulus.target_id
    ):
        raise _invalid()
    report_path = scenario_dir / (answer.reference_report or REPORT_FILE)
    try:
        raw: str | None = report_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        raw = None
    rules = category_rules(answer)
    gated = replace(answer, signature=None) if "signature_exempt" in rules else answer
    capture = ReportCapture(raw=raw, finish_reason="stop")
    gate = run_gate(
        spec,
        gated,
        reference_outcome(spec.scenario_id, list(document["outcome_evidence_ids"])),
        capture,
        delivered,
        authorized,
    )
    if "no_supported_conclusion" not in rules:
        return gate
    report = parsed_report(capture)
    ok = report is not None and report.conclusion != "supported" and bool(report.gaps)
    checks = {**gate.checks, "category": ok}
    reasons = gate.reasons if ok else (*gate.reasons, UNSUPPORTED_CONCLUSION)
    return GateResult(passed=gate.passed and ok, checks=checks, reasons=reasons)


def check_reference_projection(scenario_dir: Path) -> ExpectationResult:
    """Decision D6: a scenario whose correct behaviour is no report keeps the
    probe's projection (``reference/projection.json``, with the probe's
    intake under ``"intake"``) and is checked like a live attempt."""
    _spec, answer = load_scenario_v2(scenario_dir)
    try:
        document = json.loads(
            (scenario_dir / PROJECTION_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        raise ReferenceError("REFERENCE_PROJECTION_INVALID") from None
    if not isinstance(document, Mapping) or not isinstance(
        document.get("projection"), Mapping
    ):
        raise ReferenceError("REFERENCE_PROJECTION_INVALID")
    intake = document.get("intake")
    return check_expectations(
        answer,
        attempt_status="ran",
        attempt_reason=None,
        projection=document["projection"],
        intake=intake if isinstance(intake, Mapping) else None,
        target_id=_spec.stimulus.target_id,
    )
