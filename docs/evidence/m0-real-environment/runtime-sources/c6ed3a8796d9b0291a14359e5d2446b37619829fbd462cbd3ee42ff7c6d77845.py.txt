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
REPORT_VERSION = MODEL_REPORT_SCHEMA["properties"]["schema_version"]["const"]
CLAIM_KINDS = tuple(MODEL_REPORT_SCHEMA["$defs"]["Claim"]["properties"]["kind"]["enum"])
REPORT_EXAMPLE = {
    "schema_version": REPORT_VERSION,
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


def report_instruction(final=False):
    stage = (
        "Collection is now CLOSED. This is the final report request. No further tools or fresh queries are permitted. "
        if final
        else "When you end this investigation, including an early answer before the final request, "
    )
    return stage + (
        "Return only one JSON object in the following format; replace the example values with your actual assessment. "
        "Do not emit DSML, tool invocation markup, Markdown fences or requests for more tools. "
        "schema_version must be m0-report-v1. assessment_status is completed or incomplete; "
        "conclusion is supported, partial, or inconclusive. Incomplete requires inconclusive and nonempty gaps. "
        "Supported requires at least one evidence-backed fact; partial means useful findings but unresolved conclusions. "
        "Each claim has exactly kind, text, evidence_ids. kind is fact, hypothesis, recommendation, counter_evidence, or rejected_hypothesis. "
        "Every fact, counter_evidence and rejected_hypothesis must cite at least one complete evidence_id actually supplied in this Run; cite grounding for hypotheses and recommendations when available and label unsupported ideas provisional. Do not abbreviate or invent IDs. "
        "Use gaps for missing information and next_steps for advisory human follow-up. "
        "Do not invent a supported cause to fill the format. JSON example: "
        + json.dumps(REPORT_EXAMPLE)
    )


def final_payload(payload, tool_schemas):
    result = copy.deepcopy(payload)
    # Keep the upstream no-tools final request: thinking + explicit choices
    # lacks a verified compatibility guarantee. Preserve all message protocol.
    result.pop("tools", None)
    result.pop("tool_choice", None)
    result["response_format"] = {"type": "json_object"}
    result["messages"].append(
        {"role": "user", "content": report_instruction(final=True)}
    )
    return result


def validate_report(content, finish_reason, evidence_ids):
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
        report["schema_version"] != REPORT_VERSION
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


def prepare_wire(raw, tool_schemas, final_phase):
    """The exact serialization seam used immediately before guarded transmission."""
    if not final_phase:
        return raw
    return json.dumps(
        final_payload(json.loads(raw), tool_schemas), ensure_ascii=False
    ).encode()
