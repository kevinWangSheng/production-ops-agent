"""Report -> benchmark answer, deterministic, no model (#871 U3 = a, A4, A7).

The benchmark scores rank 1 of ``top_3_predictions``: ``fault_object``
(``app/<name>``, ``node/<name>`` or ``namespace/<name>``) and ``root_cause``
(a List A label), each compared exactly after lower-casing
(``evaluation.py`` ``prediction_scores``). OpsPilot's report has one
``root_cause`` object (kind, namespace, name) and free text; this module turns
one into the other by fixed rules, and says which rule produced each field.

Fault object, first rule that yields a List B name:

1. ``root_cause``: kind ``Node`` -> ``node/<name>``; kind ``Namespace`` ->
   ``namespace/<name>``; kind ``ResourceQuota`` -> ``namespace/<its
   namespace>``; any other kind -> ``app/<service>``, the service being the
   name itself or, after hash suffixes are stripped (``-<hash>-<id>`` of a
   Pod, ``-<hash>`` of a ReplicaSet), the longest List B application name
   the object name starts with followed by ``-``;
2. the summary's ``fault object: <kind>/<name>`` (the question's reporting
   rule), then the first ``<kind>/<name>`` in the summary, with a List B name.

Root cause label, first that yields a List A label: the summary's
``Root cause: <label>``; the first label written in the summary; the first
label written in a ``fact`` or ``hypothesis`` claim, in claim order. Labels
match whole tokens only (``pod_network_delay`` is not ``node_network_delay``).

Neither found: the field is empty and scores 0 (U3). No report at all: the
case file has no final answer and the scorer counts it 0 (A7).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from scripts.cloudopsbench_eval import catalog

_LABEL_RE = re.compile(
    r"(?<![a-z0-9_])("
    + "|".join(sorted(catalog.LABELS, key=len, reverse=True))
    + r")(?![a-z0-9_])",
    re.IGNORECASE,
)
_ROOT_CAUSE_RE = re.compile(r"root[ _-]?cause\s*[:=]\s*`?([a-z0-9_]+)", re.IGNORECASE)
_OBJECT_RE = re.compile(
    r"(?<![a-z0-9_/-])(app|node|namespace)/([a-z0-9][a-z0-9.-]*[a-z0-9])",
    re.IGNORECASE,
)
_FAULT_OBJECT_RE = re.compile(
    r"fault[ _-]?object\s*[:=]\s*`?(app|node|namespace)/([a-z0-9][a-z0-9.-]*[a-z0-9])",
    re.IGNORECASE,
)
_POD_SUFFIX = re.compile(r"-[a-z0-9]{6,10}-[a-z0-9]{5}$")
_RS_SUFFIX = re.compile(r"-[a-z0-9]{6,10}$")
_LABEL_CLAIM_KINDS = ("fact", "hypothesis")


@dataclass(frozen=True)
class Converted:
    fault_object: str
    root_cause: str
    #: ``root_cause_field`` / ``summary_fault_object`` / ``summary_token`` /
    #: ``not_found``.
    object_rule: str
    #: ``summary_root_cause`` / ``summary_first`` / ``claims_first`` /
    #: ``not_found``.
    label_rule: str


def _services(system: str) -> tuple[str, ...]:
    return catalog.VALID_SERVICES[system]


def service_of(name: str, system: str) -> str | None:
    """The List B application an object name belongs to, or ``None``."""
    services = _services(system)
    lowered = name.strip().lower()
    if lowered in services:
        return lowered
    for suffix in (_POD_SUFFIX, _RS_SUFFIX):
        stripped = suffix.sub("", lowered)
        if stripped != lowered and stripped in services:
            return stripped
    prefixes = [s for s in services if lowered.startswith(s + "-")]
    return max(prefixes, key=len) if prefixes else None


def _listed(kind: str, name: str, system: str) -> str | None:
    kind, name = kind.lower(), name.strip().lower()
    if kind == "node":
        return f"node/{name}" if name in catalog.VALID_NODES else None
    if kind == "namespace":
        return f"namespace/{name}" if name in catalog.VALID_NAMESPACES[system] else None
    if kind == "app":
        return f"app/{name}" if name in _services(system) else None
    return None


def object_from_root_cause(root_cause: Any, system: str) -> str | None:
    if not isinstance(root_cause, Mapping):
        return None
    kind = str(root_cause.get("kind") or "").strip().lower()
    name = str(root_cause.get("name") or "").strip()
    namespace = str(root_cause.get("namespace") or "").strip()
    if kind == "node":
        return _listed("node", name, system)
    if kind == "namespace":
        return _listed("namespace", name, system)
    if kind == "resourcequota":
        return _listed("namespace", namespace, system) if namespace else None
    service = service_of(name, system) if name else None
    return f"app/{service}" if service else None


def _object_in_text(text: str, system: str) -> tuple[str | None, str]:
    for match in _FAULT_OBJECT_RE.finditer(text):
        found = _listed(match.group(1), match.group(2), system)
        if found:
            return found, "summary_fault_object"
    for match in _OBJECT_RE.finditer(text):
        found = _listed(match.group(1), match.group(2), system)
        if found:
            return found, "summary_token"
    return None, "not_found"


def first_label(text: str) -> str | None:
    match = _LABEL_RE.search(text)
    return match.group(1).lower() if match else None


def _label(report: Mapping[str, Any]) -> tuple[str | None, str]:
    summary = str(report.get("summary") or "")
    for match in _ROOT_CAUSE_RE.finditer(summary):
        if match.group(1).lower() in catalog.LABEL_TARGETS:
            return match.group(1).lower(), "summary_root_cause"
    found = first_label(summary)
    if found:
        return found, "summary_first"
    for claim in report.get("claims") or ():
        if isinstance(claim, Mapping) and claim.get("kind") in _LABEL_CLAIM_KINDS:
            found = first_label(str(claim.get("text") or ""))
            if found:
                return found, "claims_first"
    return None, "not_found"


def convert(report: Mapping[str, Any], system: str) -> Converted:
    """``report`` is the product's validated report (``ReportV2`` dumped)."""
    fault = object_from_root_cause(report.get("root_cause"), system)
    object_rule = "root_cause_field" if fault else "not_found"
    if fault is None:
        fault, object_rule = _object_in_text(str(report.get("summary") or ""), system)
    label, label_rule = _label(report)
    return Converted(fault or "", label or "", object_rule, label_rule)


# -- the benchmark's per-case file ------------------------------------------------


def classify_output(output: str, is_error: bool) -> str:
    """One recorded tool answer: ``hit`` (a recorded result), ``not_found``
    (the snapshot's synthetic NotFound for an unrecorded query, A3),
    ``not_recorded`` (another "not recorded"/error text of the backend) or
    ``adapter_error`` (arguments the server refused)."""
    if is_error:
        return "adapter_error"
    text = output.strip()
    if text.startswith("Error from server (NotFound)"):
        return "not_found"
    if text.startswith(("Error", "An unexpected error")):
        return "not_recorded"
    return "hit"


def read_trace(lines: Sequence[str]) -> list[dict[str, Any]]:
    """The MCP server's JSONL trace; malformed lines are skipped."""
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("tool_name"), str):
            events.append(event)
    return events


def cache_stats(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {"hit": 0, "not_found": 0, "not_recorded": 0, "adapter_error": 0}
    for event in events:
        counts[
            classify_output(str(event.get("output") or ""), bool(event.get("is_error")))
        ] += 1
    total = sum(counts.values())
    return {
        "calls": total,
        **counts,
        "hit_rate": round(counts["hit"] / total, 4) if total else None,
    }


def trajectory_steps(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The trace as the scorer's ``steps`` (A4: process metrics from it are
    approximate -- OpsPilot's calls that the gateway refused before the
    server are not in the trace)."""
    return [
        {
            "step_id": index,
            "action_type": "tool",
            "action_name": event["tool_name"],
            "action_input": event.get("arguments")
            if isinstance(event.get("arguments"), dict)
            else {},
            "observation": str(event.get("output") or ""),
            "error": str(event.get("output") or "") if event.get("is_error") else None,
        }
        for index, event in enumerate(events, 1)
    ]


def case_file(
    converted: Converted | None, steps: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """``<case>/<case>.json`` as ``evaluation.py`` reads it: ``final_answer``
    (``None`` when there was no report, A7) and ``steps``."""
    final = None
    if converted is not None:
        final = {
            "key_evidence_summary": "",
            "top_3_predictions": [
                {
                    "rank": 1,
                    "fault_object": converted.fault_object,
                    "root_cause": converted.root_cause,
                }
            ],
        }
    return {"final_answer": final, "steps": list(steps)}


def conversion_record(converted: Converted | None) -> dict[str, Any] | None:
    """What the frozen summary keeps: the rules used and whether each field
    was found -- never the predicted values (A6)."""
    if converted is None:
        return None
    record = asdict(converted)
    return {
        "object_rule": record["object_rule"],
        "label_rule": record["label_rule"],
        "object_found": bool(converted.fault_object),
        "label_found": bool(converted.root_cause),
    }
