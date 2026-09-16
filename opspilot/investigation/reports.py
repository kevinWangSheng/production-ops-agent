"""L2 report contract and v4/report-v2 parsing.

The contract text is the single source for the model-visible report schema.
Runtime values (this Run's evidence ids, target refs, time-policy ids) stay
out of it: they belong on the evidence-context user message, not in L2, so
two Runs that differ only in window or target set keep the same
``prompt_revision``. Writing rules follow
``docs/design/deepseek-flash-prompt-tool-reference.md`` (the word ``json``
and a complete example are vendor requirements, not formatting).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from opspilot.domain.base import DTO, Text

REPORT_SCHEMA_VERSION = "m0-report-v2"
CLAIM_KINDS = (
    "fact",
    "hypothesis",
    "recommendation",
    "counter_evidence",
    "rejected_hypothesis",
)
_FACTLKE = frozenset({"fact", "counter_evidence", "rejected_hypothesis"})
_DSML = re.compile(r"<[^>\n]*DSML", re.IGNORECASE)

_REPORT_EXAMPLE: dict[str, object] = {
    "schema_version": REPORT_SCHEMA_VERSION,
    "assessment_status": "completed",
    "conclusion": "inconclusive",
    "summary": "Write the evidence-based assessment here.",
    "claims": [
        {
            "kind": "fact",
            "text": "A specific visible observation.",
            "evidence_ids": ["REPLACE_WITH_COMPLETE_VISIBLE_EVIDENCE_ID"],
            "target_refs": ["COPY_ACTUALLY_DELIVERED_TARGET_REF"],
            "time_scope_ref": "COPY_FIXED_TIME_POLICY_REF",
        }
    ],
    "gaps": ["State the specific missing information, if any."],
    "next_steps": ["An advisory next step, without executing it."],
}

# Stable L2 bytes. Instance values are not interpolated here.
REPORT_CONTRACT = (
    "When you end this investigation, including an early answer before the "
    "final request, return only one json object in the following format; "
    "replace the example values with your actual assessment. Do not emit "
    "DSML, tool invocation markup, Markdown fences or requests for more "
    "tools. schema_version must be m0-report-v2. assessment_status is "
    "completed or incomplete; conclusion is supported, partial, or "
    "inconclusive. Incomplete requires inconclusive and nonempty gaps. "
    "Supported requires at least one evidence-backed fact; partial means "
    "useful findings but unresolved conclusions. Each claim has kind, text, "
    "evidence_ids, target_refs and time_scope_ref. kind is fact, hypothesis, "
    "recommendation, counter_evidence, or rejected_hypothesis. Every fact, "
    "counter_evidence and rejected_hypothesis must cite at least one complete "
    "evidence_id actually supplied in this Run, nonempty target_refs from the "
    "delivered evidence context, and a time_scope_ref from that context; never "
    "abbreviate or invent IDs. Permission to query a target is not observed "
    "evidence. Missing policy or source timing cannot support a current fact; "
    "return explicit gaps and inconclusive or incomplete. Use gaps for missing "
    "information and next_steps for advisory human follow-up. Do not invent a "
    "supported cause to fill the format. json example: "
    + json.dumps(_REPORT_EXAMPLE, ensure_ascii=False, separators=(",", ":"))
)

FINAL_REPORT_INSTRUCTION = (
    "Collection is now CLOSED. This is the final report request. No further "
    "tools or fresh queries are permitted. Return only one json object in the "
    "report format already given. Do not emit DSML, tool invocation markup, "
    "Markdown fences or requests for more tools."
)


class ClaimV2(DTO):
    kind: Literal[
        "fact",
        "hypothesis",
        "recommendation",
        "counter_evidence",
        "rejected_hypothesis",
    ]
    text: Text
    evidence_ids: list[Text]
    target_refs: list[Text] = Field(default_factory=list)
    time_scope_ref: Text | None = None

    @model_validator(mode="after")
    def explicit_fact_scope(self) -> ClaimV2:
        if self.kind in _FACTLKE and (
            not self.evidence_ids or not self.target_refs or not self.time_scope_ref
        ):
            raise ValueError("FACT_SCOPE_REQUIRED")
        if len(set(self.target_refs)) != len(self.target_refs):
            raise ValueError("DUPLICATE_TARGET_REF")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("DUPLICATE_EVIDENCE_ID")
        return self


class ReportV2(DTO):
    schema_version: Literal["m0-report-v2"]
    assessment_status: Literal["completed", "incomplete"]
    conclusion: Literal["supported", "partial", "inconclusive"]
    summary: Text
    claims: list[ClaimV2]
    gaps: list[Text]
    next_steps: list[Text]

    @model_validator(mode="after")
    def coherent(self) -> ReportV2:
        if not self.summary.strip():
            raise ValueError("EMPTY_SUMMARY")
        if self.assessment_status == "incomplete" and (
            self.conclusion != "inconclusive" or not self.gaps
        ):
            raise ValueError("INCOMPLETE_REPORT_CONTRACT")
        if self.conclusion == "supported" and not any(
            claim.kind == "fact" and claim.evidence_ids for claim in self.claims
        ):
            raise ValueError("SUPPORTED_REPORT_REQUIRES_FACT")
        return self


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_FIELD")
        result[key] = value
    return result


def parse_report(
    content: str | None, *, finish_reason: str
) -> tuple[ReportV2 | None, str]:
    """Parse a candidate final text into a v2 report.

    Returns ``(report, "")`` on success. On failure ``report`` is None and the
    second value is a fixed reason code. Empty content is a failure, not an
    invented report; that is a vendor-documented DeepSeek failure mode.
    """
    if finish_reason == "length":
        return None, "OUTPUT_LENGTH"
    if finish_reason != "stop":
        return None, "REPORT_INVALID"
    if not isinstance(content, str) or not content.strip():
        return None, "EMPTY_REPORT"
    if _DSML.search(content):
        return None, "REPORT_INVALID"
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None, "REPORT_INVALID"
    try:
        report = ReportV2.model_validate(payload)
    except (ValidationError, ValueError):
        return None, "REPORT_INVALID"
    return report, ""


@dataclass(frozen=True)
class DeliveredView:
    """One adopted view the report is allowed to cite."""

    evidence_id: str
    target_ids: frozenset[str]
    status: str
    time_scope_refs: frozenset[str] = frozenset()


def context_target_catalog(context: object) -> dict[str, str | None]:
    """Opaque v4 target_ref -> optional registry target_id.

    ``scripts/m0/outcomes_v4.py`` keys the catalog with ``target:<digest>``.
    A catalog entry may also carry the authorized registry ``target_id``.
    """
    if not isinstance(context, Mapping):
        return {}
    catalog = context.get("target_catalog")
    if not isinstance(catalog, Mapping):
        return {}
    result: dict[str, str | None] = {}
    for key, entry in catalog.items():
        if not isinstance(key, str) or not key:
            continue
        registry = None
        if isinstance(entry, Mapping) and isinstance(entry.get("target_id"), str):
            registry = str(entry["target_id"]) or None
        result[key] = registry
    return result


def context_time_policy_ids(context: object) -> tuple[str, ...]:
    """Ids from the caller-supplied v4 evidence context, if it has any."""
    if not isinstance(context, Mapping):
        return ()
    policies = context.get("time_policies")
    if not isinstance(policies, list):
        return ()
    ids: list[str] = []
    for item in policies:
        if isinstance(item, Mapping):
            ident = item.get("id")
            if isinstance(ident, str) and ident:
                ids.append(ident)
    return tuple(ids)


def unsupported_citations(
    report: ReportV2,
    *,
    views: Sequence[DeliveredView],
    authorized_targets: frozenset[str],
    time_policy_ids: Sequence[str],
    target_catalog: Mapping[str, str | None] | None = None,
) -> bool:
    """True when a claim fails the v4 evidence/target/time bind.

    Fact-like claims must cite delivered ok views, targets those views
    actually observed, and the time policy each cited view was delivered
    under. When a v4 ``target_catalog`` is present, claim ``target_refs``
    are opaque catalog keys, not registry target ids.
    """
    by_id = {view.evidence_id: view for view in views}
    policies = set(time_policy_ids)
    catalog = dict(target_catalog) if target_catalog else {}
    for claim in report.claims:
        if any(eid not in by_id for eid in claim.evidence_ids):
            return True
        if catalog:
            if any(ref not in catalog for ref in claim.target_refs):
                return True
            if any(
                catalog[ref] is not None and catalog[ref] not in authorized_targets
                for ref in claim.target_refs
            ):
                return True
        elif any(ref not in authorized_targets for ref in claim.target_refs):
            return True
        if claim.time_scope_ref is not None and claim.time_scope_ref not in policies:
            return True
        if claim.kind not in _FACTLKE:
            continue
        if claim.time_scope_ref not in policies:
            return True
        cited = [by_id[eid] for eid in claim.evidence_ids]
        if any(view.status != "ok" for view in cited):
            return True
        if any(claim.time_scope_ref not in view.time_scope_refs for view in cited):
            return True
        observed: set[str] = set()
        for view in cited:
            observed.update(view.target_ids)
        if any(ref not in observed for ref in claim.target_refs):
            return True
    return False


def delivered_from_context(context: object) -> list[DeliveredView]:
    """Seed trusted views from a v4 evidence context's view_bindings."""
    if not isinstance(context, Mapping):
        return []
    bindings = context.get("view_bindings")
    if not isinstance(bindings, Mapping):
        return []
    delivered: list[DeliveredView] = []
    for eid, binding in bindings.items():
        if not isinstance(eid, str) or not eid or not isinstance(binding, Mapping):
            continue
        status = binding.get("status")
        if status != "ok":
            continue
        refs = binding.get("target_refs")
        if isinstance(refs, list):
            targets = frozenset(item for item in refs if isinstance(item, str) and item)
        elif isinstance(binding.get("target_id"), str) and binding["target_id"]:
            targets = frozenset({str(binding["target_id"])})
        else:
            targets = frozenset()
        scopes = binding.get("time_scope_refs")
        if isinstance(scopes, list):
            time_refs = frozenset(
                item for item in scopes if isinstance(item, str) and item
            )
        else:
            time_refs = frozenset()
        delivered.append(
            DeliveredView(
                evidence_id=eid,
                target_ids=targets,
                status="ok",
                time_scope_refs=time_refs,
            )
        )
    return delivered
