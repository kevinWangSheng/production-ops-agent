"""Final-report protocol at the actual wire and externally visible result seam."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

MODEL_REPORT_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs/evidence/m0-real-environment/ModelReport.v1.schema.json"
    ).read_text()
)
LEGACY_REPORT_VERSION = MODEL_REPORT_SCHEMA["properties"]["schema_version"]["const"]
REPORT_VERSION = "m0-report-v2"
CLAIM_KINDS = tuple(MODEL_REPORT_SCHEMA["$defs"]["Claim"]["properties"]["kind"]["enum"])
REPORT_EXAMPLE = {
    "schema_version": LEGACY_REPORT_VERSION,
    "assessment_status": "completed",
    "conclusion": "inconclusive",
    "summary": "Write the evidence-based assessment here.",
    "claims": [
        {
            "kind": "fact",
            "text": "A specific visible observation.",
            "evidence_ids": ["REPLACE_WITH_COMPLETE_VISIBLE_EVIDENCE_ID"],
        }
    ],
    "gaps": ["State the specific missing information, if any."],
    "next_steps": ["An advisory next step, without executing it."],
}


def report_instruction(final=False, *, version=REPORT_VERSION):
    if version not in (LEGACY_REPORT_VERSION, REPORT_VERSION):
        raise ValueError("unsupported report version")
    example = copy.deepcopy(REPORT_EXAMPLE)
    example["schema_version"] = version
    strict = version == REPORT_VERSION
    if strict:
        example["claims"][0].update(
            target_refs=["COPY_ACTUALLY_DELIVERED_TARGET_REF"],
            time_scope_ref="COPY_FIXED_TIME_POLICY_REF",
        )
    stage = (
        "Collection is now CLOSED. This is the final report request. No further tools or fresh queries are permitted. "
        if final
        else "When you end this investigation, including an early answer before the final request, "
    )
    return stage + (
        "Return only one JSON object in the following format; replace the example values with your actual assessment. "
        "Do not emit DSML, tool invocation markup, Markdown fences or requests for more tools. "
        f"schema_version must be {version}. assessment_status is completed or incomplete; "
        "conclusion is supported, partial, or inconclusive. Incomplete requires inconclusive and nonempty gaps. "
        "Supported requires at least one evidence-backed fact; partial means useful findings but unresolved conclusions. "
        + (
            "Each claim has kind, text, evidence_ids, target_refs and time_scope_ref. "
            if strict
            else "Each claim has exactly kind, text, evidence_ids. "
        )
        + "kind is fact, hypothesis, recommendation, counter_evidence, or rejected_hypothesis. "
        "Every fact, counter_evidence and rejected_hypothesis must cite at least one complete evidence_id actually supplied in this Run; cite grounding for hypotheses and recommendations when available and label unsupported ideas provisional. Do not abbreviate or invent IDs. "
        "Use gaps for missing information and next_steps for advisory human follow-up. "
        "Do not invent a supported cause to fill the format. JSON example: "
        + json.dumps(example)
        + (
            " In report-v2 every claim also has target_refs (an array) and time_scope_ref (a policy ID or null). Fact/counter_evidence/rejected_hypothesis require nonempty target_refs and time_scope_ref from the actual opspilot-evidence-context-v4 message. Each target must be supported by at least one of that claim's cited views. Permission alone is not observed evidence. Never default to the investigation subject or invent target refs, policy IDs, thresholds or source times. Missing policy/source timing cannot support current facts; return explicit gaps and inconclusive/incomplete when needed. The fixed policy reference rule is evaluated by the trusted runtime against this physical request's clocks, not a time chosen by you."
            if strict
            else ""
        )
    )


def final_payload(payload, tool_schemas, *, version=REPORT_VERSION, context=None):
    result = copy.deepcopy(payload)
    if version == REPORT_VERSION:
        if context is None:
            raise ValueError("strict evidence context required")
        result["messages"].append(
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, sort_keys=True),
            }
        )
    # Keep the upstream no-tools final request: thinking + explicit choices
    # lacks a verified compatibility guarantee. Preserve all message protocol.
    result.pop("tools", None)
    result.pop("tool_choice", None)
    result["response_format"] = {"type": "json_object"}
    result["messages"].append(
        {"role": "user", "content": report_instruction(final=True, version=version)}
    )
    return result


def _validate_report_v1(content, finish_reason, evidence_ids):
    if not isinstance(content, str) or not content.strip() or finish_reason != "stop":
        raise ValueError("final report incomplete")
    if re.search(r"<[^>\n]*DSML", content, flags=re.IGNORECASE):
        raise ValueError("final report contains tool protocol")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report field")
            result[key] = value
        return result

    try:
        report = json.loads(content, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise ValueError("final report JSON invalid") from None
    expected = {
        "schema_version",
        "assessment_status",
        "conclusion",
        "summary",
        "claims",
        "gaps",
        "next_steps",
    }
    if not isinstance(report, dict) or set(report) != expected:
        raise ValueError("final report schema invalid")
    if (
        report["schema_version"] != LEGACY_REPORT_VERSION
        or report["assessment_status"] not in ("completed", "incomplete")
        or report["conclusion"] not in ("supported", "partial", "inconclusive")
    ):
        raise ValueError("final report state invalid")
    if not isinstance(report["summary"], str) or not report["summary"].strip():
        raise ValueError("final report summary invalid")
    for field in ("gaps", "next_steps"):
        if not isinstance(report[field], list) or any(
            not isinstance(item, str) or not item.strip() for item in report[field]
        ):
            raise ValueError("final report list invalid")
    claims = report["claims"]
    if not isinstance(claims, list):
        raise ValueError("final report claims invalid")
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {
            "kind",
            "text",
            "evidence_ids",
        }:
            raise ValueError("final report claim schema invalid")
        if (
            claim["kind"] not in CLAIM_KINDS
            or not isinstance(claim["text"], str)
            or not claim["text"].strip()
        ):
            raise ValueError("final report claim invalid")
        refs = claim["evidence_ids"]
        if (
            not isinstance(refs, list)
            or (
                not refs
                and claim["kind"] in ("fact", "counter_evidence", "rejected_hypothesis")
            )
            or any(not isinstance(ref, str) or ref not in evidence_ids for ref in refs)
        ):
            raise ValueError("final report evidence reference invalid")
    if report["assessment_status"] == "incomplete" and (
        report["conclusion"] != "inconclusive" or not report["gaps"]
    ):
        raise ValueError("final report incomplete state invalid")
    if report["conclusion"] == "supported" and not any(
        c["kind"] == "fact" for c in claims
    ):
        raise ValueError("final report supported without facts")
    return report


def prepare_wire(
    raw, tool_schemas, final_phase, *, version=REPORT_VERSION, context=None
):
    """The exact serialization seam used immediately before guarded transmission."""
    if version not in (LEGACY_REPORT_VERSION, REPORT_VERSION):
        raise ValueError("unsupported report version")
    if final_phase:
        result = final_payload(
            json.loads(raw), tool_schemas, version=version, context=context
        )
    elif version == REPORT_VERSION:
        if context is None:
            raise ValueError("strict evidence context required")
        result = json.loads(raw)
        result["messages"].append(
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, sort_keys=True),
            }
        )
    else:
        return raw
    return json.dumps(result, ensure_ascii=False).encode()


def parse_report(content, *, version=REPORT_VERSION):
    """Preserve a schema-valid candidate independently of its evidence qualification."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("final report incomplete")
    if re.search(r"<[^>\n]*DSML", content, flags=re.IGNORECASE):
        raise ValueError("final report contains tool protocol")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report field")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise ValueError("final report JSON invalid") from None
    if version == REPORT_VERSION:
        from scripts.m0.outcomes_v4 import ModelReportV2

        return ModelReportV2.model_validate(value).model_dump(mode="json")
    if version == LEGACY_REPORT_VERSION:
        from scripts.m0.outcomes_v3 import ModelReport

        return ModelReport.model_validate(value).model_dump(mode="json")
    raise ValueError("unsupported report version")


def validate_report(
    content,
    finish_reason,
    evidence_ids,
    *,
    version=REPORT_VERSION,
    context=None,
):
    """Parse and check delivered scope/status; public-v4 owns full timing/output checks."""
    if version == LEGACY_REPORT_VERSION:
        return _validate_report_v1(content, finish_reason, evidence_ids)
    if version != REPORT_VERSION:
        raise ValueError("unsupported report version")
    if not isinstance(content, str) or not content.strip() or finish_reason != "stop":
        raise ValueError("final report incomplete")
    if re.search(r"<[^>\n]*DSML", content, flags=re.IGNORECASE):
        raise ValueError("final report contains tool protocol")
    from scripts.m0.outcomes_v4 import (
        EvidenceContext,
        ModelReportV2,
        validate_report_context,
    )

    report = ModelReportV2.model_validate(parse_report(content, version=version))
    if context is None:
        raise ValueError("strict evidence context required")
    context_model = (
        context
        if isinstance(context, EvidenceContext)
        else EvidenceContext.model_validate_json(json.dumps(context))
    )
    errors = validate_report_context(report, context_model)
    if errors:
        raise ValueError("strict report context rejected: " + ",".join(errors))
    if any(
        ref not in evidence_ids for claim in report.claims for ref in claim.evidence_ids
    ):
        raise ValueError("final report evidence reference invalid")
    return report.model_dump(mode="json")


def source_timing(record, view):
    """Only visible event timestamps; query/evaluation/HTTP times are not sample ages."""
    import math
    from datetime import datetime, timedelta, timezone

    timing = {
        "operation_started_at": record.get("operation_started_at"),
        "collection_completed_at": record.get("collection_completed_at"),
        "source_start_at": None,
        "source_end_at": None,
        "source_time_basis": "unknown",
    }
    data = view.get("data", {})
    intervals = []
    try:
        if view.get("tool") == "otel_logs":
            for row in data.get("displayed_logs", []):
                value = row.get("timestamp")
                if not isinstance(value, str):
                    return timing
                moment = datetime.fromisoformat(value)
                if moment.tzinfo is None:
                    return timing
                intervals.append((moment, moment))
        elif view.get("tool") == "otel_traces":
            for span in data.get("sampled_spans", []):
                start, duration = span.get("start_us"), span.get("duration_us")
                if (
                    any(
                        type(value) not in (int, float) or not math.isfinite(value)
                        for value in (start, duration)
                    )
                    or duration < 0
                ):
                    return timing
                moment = datetime.fromtimestamp(start / 1_000_000, timezone.utc)
                intervals.append((moment, moment + timedelta(microseconds=duration)))
        # Instant Prometheus result timestamps are evaluation times, not samples.
    except (ValueError, TypeError, OverflowError):
        return timing
    if intervals:
        timing.update(
            source_start_at=min(start for start, _ in intervals).isoformat(),
            source_end_at=max(end for _, end in intervals).isoformat(),
            source_time_basis="event_time",
        )
    return timing
