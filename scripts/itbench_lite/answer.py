"""Deterministic OpsPilot report to ITBench ``agent_output.json`` conversion (#582).

Evaluation-side only: no model call, the product report format is unchanged.
The rules are decision table r4 L1-L5 (docs/tasks/2026-11-12-tool-expansion-contract.md):

* L1 entities: a caller-supplied vocabulary (snapshot object names plus service
  names) is matched in the report ``summary`` and claim texts on name
  boundaries (case-insensitive, a boundary is any character outside
  ``[A-Za-z0-9_-]``; overlapping hits keep the longest name).  ``Kind`` comes
  from the tool parameters of evidence cited by the claims that mention the
  entity (see ``_resolve_kind``), otherwise ``Service``.
* L2 root cause: the first entity mentioned in ``summary`` is the only
  ``contributing_factor: true`` entity; an entity mentioned by any
  ``rejected_hypothesis`` claim is false; ``conclusion == "inconclusive"``
  marks no root cause.  Entity order is first mention in ``summary``, then
  first mention across claims.  This is a heuristic and the result says so.
* L3 propagations: only explicit ``A -> B`` / ``A → B`` arrows in ``summary``
  whose both ends are vocabulary entities directly adjacent to the arrow;
  otherwise empty.  The official propagation score is conversion-limited.
* L4 ``reasoning`` joins the texts of the claims that mention the entity and
  ``evidence`` joins ``tool <canonical params>`` of their cited evidence, each
  cut to 2000 characters.
* L5 a Run without a report yields the valid empty result.

``alerts_explained`` is always empty: L1-L5 define no rule for it and the
pinned judge prompts do not score it.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opspilot.investigation.reports import ReportV2

METHOD = "heuristic"
RULES = "r4 L1-L5"
MAX_FIELD_CHARS = 2000
DEFAULT_NAMESPACE = "otel-demo"
PROPAGATION_LABEL = "受转换限制"
ROOT_CAUSE_LABEL = "启发式"

# Plural resource name (the A2 tool ``kind`` parameter) to Kubernetes Kind.
# ``events`` is deliberately absent: an Event is evidence about an object,
# not an entity.
RESOURCE_KINDS: Mapping[str, str] = {
    "pods": "Pod",
    "services": "Service",
    "deployments": "Deployment",
    "replicasets": "ReplicaSet",
    "statefulsets": "StatefulSet",
    "daemonsets": "DaemonSet",
    "jobs": "Job",
    "cronjobs": "CronJob",
    "configmaps": "ConfigMap",
    "secrets": "Secret",
    "namespaces": "Namespace",
    "nodes": "Node",
    "networkpolicies": "NetworkPolicy",
    "resourcequotas": "ResourceQuota",
    "limitranges": "LimitRange",
    "persistentvolumeclaims": "PersistentVolumeClaim",
    "persistentvolumes": "PersistentVolume",
    "serviceaccounts": "ServiceAccount",
    "endpoints": "Endpoints",
    "endpointslices": "EndpointSlice",
    "ingresses": "Ingress",
    "horizontalpodautoscalers": "HorizontalPodAutoscaler",
    "poddisruptionbudgets": "PodDisruptionBudget",
}

_NAME_CHAR = r"A-Za-z0-9_\-"
_ARROW = re.compile(r"(?:->|→|⟶)")
_ARROW_PAD = frozenset(" \t`'\"*")
_POD_KEYS = ("pod", "pod_name")


def empty_output() -> dict[str, list[Any]]:
    """The valid empty ``agent_output.json`` (L5)."""

    return {"entities": [], "propagations": [], "alerts_explained": []}


@dataclass(frozen=True)
class Conversion:
    """``agent_output`` is written for the evaluator; ``meta`` is a sidecar."""

    agent_output: dict[str, list[Any]]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Evidence:
    tool: str
    query: Mapping[str, Any] | None
    query_text: str


class _Matcher:
    def __init__(self, vocabulary: Iterable[str]) -> None:
        names: dict[str, str] = {}
        for raw in vocabulary:
            name = raw.strip() if isinstance(raw, str) else ""
            if name and name.casefold() not in names:
                names[name.casefold()] = name
        # Longest first so that the alternation prefers the longer name.
        self.names = sorted(names.values(), key=lambda n: (-len(n), n))
        self._canon = names
        self._re = (
            re.compile(
                rf"(?<![{_NAME_CHAR}])(?:"
                + "|".join(re.escape(n) for n in self.names)
                + rf")(?![{_NAME_CHAR}])",
                re.IGNORECASE,
            )
            if self.names
            else None
        )

    def find(self, text: str) -> list[tuple[int, int, str]]:
        """Non-overlapping ``(start, end, canonical name)`` hits, in order."""

        if self._re is None or not text:
            return []
        # The alternation is longest-first, so each hit is the longest name
        # that fits at its position; ``finditer`` hits never overlap.
        return [
            (m.start(), m.end(), self._canon[m.group(0).casefold()])
            for m in self._re.finditer(text)
        ]

    def names_in(self, text: str) -> list[str]:
        seen: list[str] = []
        for _, _, name in self.find(text):
            if name not in seen:
                seen.append(name)
        return seen


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence_index(evidence: Iterable[Mapping[str, Any]]) -> dict[str, _Evidence]:
    """Index evidence records by ``evidence_id``.

    Accepts the audit shape ``{"evidence_id", "operation": {"tool", "query"}}``
    and a flat ``{"evidence_id", "tool", "query"}``; ``query`` is the canonical
    JSON string of the tool parameters or an already-parsed mapping.
    """

    index: dict[str, _Evidence] = {}
    for record in evidence:
        evidence_id = record.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        operation = record.get("operation")
        source = operation if isinstance(operation, Mapping) else record
        tool = source.get("tool")
        raw_query = source.get("query")
        query: Mapping[str, Any] | None = None
        if isinstance(raw_query, Mapping):
            query = raw_query
        elif isinstance(raw_query, str):
            try:
                parsed = json.loads(raw_query)
            except ValueError:
                parsed = None
            if isinstance(parsed, Mapping):
                query = parsed
        if query is not None:
            query_text = _canonical(query)
        elif isinstance(raw_query, str):
            query_text = raw_query
        else:
            query_text = ""
        index.setdefault(
            evidence_id,
            _Evidence(
                tool=tool if isinstance(tool, str) else "",
                query=query,
                query_text=query_text,
            ),
        )
    return index


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _service_label(name: str) -> re.Pattern[str]:
    return re.compile(
        r"service_name\s*=~?\s*\"" + re.escape(name) + r"\"", re.IGNORECASE
    )


def _kind_of(query: Mapping[str, Any]) -> str | None:
    kind = query.get("kind")
    if isinstance(kind, str):
        return RESOURCE_KINDS.get(kind.strip().casefold())
    return None


def _namespace_of(query: Mapping[str, Any] | None, default: str) -> str:
    if query is not None:
        value = query.get("namespace")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _resolve_kind(
    name: str,
    cited: Sequence[tuple[_Evidence, bool]],
    matcher: _Matcher,
    default_namespace: str,
) -> tuple[str, str, str]:
    """Return ``(namespace, Kind, rule)`` for ``name``.

    ``cited`` is the ordered evidence cited by claims mentioning ``name``; the
    flag says whether that claim mentions ``name`` and no other entity.  Rules,
    first hit wins, evidence in order and rules a-c per evidence:

    a. the evidence has a known ``kind`` and names ``name`` in another
       parameter (``name``, a jq expression, a selector...);
    b. ``pod``/``pod_name`` equals ``name``: ``Pod``;
    c. ``service`` equals ``name`` or a ``service_name="name"`` label matcher
       appears in a parameter: ``Service``;
    d. fallback: the first evidence with a known ``kind`` cited by a claim that
       mentions only ``name``;
    otherwise ``Service`` in the default namespace.
    """

    folded = name.casefold()
    label = _service_label(name)
    for item, _ in cited:
        query = item.query
        if query is None:
            continue
        kind = _kind_of(query)
        if kind is not None:
            others = {k: v for k, v in query.items() if k != "kind"}
            if any(name in matcher.names_in(text) for text in _strings(others)):
                return _namespace_of(query, default_namespace), kind, "a"
        for key in _POD_KEYS:
            value = query.get(key)
            if isinstance(value, str) and value.strip().casefold() == folded:
                return _namespace_of(query, default_namespace), "Pod", "b"
        service = query.get("service")
        if (isinstance(service, str) and service.strip().casefold() == folded) or any(
            label.search(text) for text in _strings(query)
        ):
            return _namespace_of(query, default_namespace), "Service", "c"
    for item, only_this in cited:
        if only_this and item.query is not None:
            kind = _kind_of(item.query)
            if kind is not None:
                return _namespace_of(item.query, default_namespace), kind, "d"
    return default_namespace, "Service", "default"


def _cut(text: str) -> str:
    return text[:MAX_FIELD_CHARS]


def _propagations(
    summary: str, matcher: _Matcher, full: Mapping[str, str]
) -> list[dict[str, str]]:
    hits = matcher.find(summary)
    by_end = {end: name for _, end, name in hits}
    by_start = {start: name for start, _, name in hits}
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for arrow in _ARROW.finditer(summary):
        left = arrow.start()
        while left > 0 and summary[left - 1] in _ARROW_PAD:
            left -= 1
        right = arrow.end()
        while right < len(summary) and summary[right] in _ARROW_PAD:
            right += 1
        source, target = by_end.get(left), by_start.get(right)
        if source is None or target is None or source == target:
            continue
        pair = (full[source], full[target])
        if pair in seen:
            continue
        seen.add(pair)
        edges.append(
            {"source": pair[0], "target": pair[1], "condition": "", "effect": ""}
        )
    return edges


def _meta(report_present: bool, **extra: Any) -> dict[str, Any]:
    return {
        "method": METHOD,
        "rules": RULES,
        "report_present": report_present,
        "root_cause": ROOT_CAUSE_LABEL,
        "propagation_chain": PROPAGATION_LABEL,
        **extra,
    }


def convert(
    report: Mapping[str, Any] | None,
    evidence: Iterable[Mapping[str, Any]],
    vocabulary: Iterable[str],
    *,
    namespace: str = DEFAULT_NAMESPACE,
) -> Conversion:
    """Convert one Run's report and evidence into ``agent_output.json``.

    ``report`` is the ``m0-report-v2`` JSON object or ``None`` when the Run
    ended without a report (L5).  A present but invalid report raises
    ``ValueError``; the caller decides whether that Run counts as reportless.
    """

    if report is None:
        return Conversion(empty_output(), _meta(False, entity_count=0))
    parsed = ReportV2.model_validate(dict(report))
    matcher = _Matcher(vocabulary)
    index = _evidence_index(evidence)

    claim_names = [matcher.names_in(claim.text) for claim in parsed.claims]
    order = matcher.names_in(parsed.summary)
    for names in claim_names:
        for name in names:
            if name not in order:
                order.append(name)

    rejected = {
        name
        for claim, names in zip(parsed.claims, claim_names, strict=True)
        if claim.kind == "rejected_hypothesis"
        for name in names
    }
    summary_names = matcher.names_in(parsed.summary)
    root = (
        summary_names[0]
        if summary_names
        and parsed.conclusion != "inconclusive"
        and summary_names[0] not in rejected
        else None
    )

    unresolved: list[str] = []
    entities: list[dict[str, Any]] = []
    full: dict[str, str] = {}
    kind_rules: dict[str, str] = {}
    for name in order:
        mentioning = [
            (claim, names)
            for claim, names in zip(parsed.claims, claim_names, strict=True)
            if name in names
        ]
        cited: list[tuple[_Evidence, bool]] = []
        cited_ids: list[str] = []
        for claim, names in mentioning:
            for evidence_id in claim.evidence_ids:
                item = index.get(evidence_id)
                if item is None:
                    if evidence_id not in unresolved:
                        unresolved.append(evidence_id)
                    continue
                cited.append((item, names == [name]))
                if evidence_id not in cited_ids:
                    cited_ids.append(evidence_id)
        entity_namespace, kind, rule = _resolve_kind(name, cited, matcher, namespace)
        entity_name = f"{entity_namespace}/{kind}/{name}"
        full[name] = entity_name
        kind_rules[entity_name] = rule
        if any(entity["name"] == entity_name for entity in entities):
            continue
        entities.append(
            {
                "name": entity_name,
                "contributing_factor": name == root,
                "reasoning": _cut("\n".join(claim.text for claim, _ in mentioning)),
                "evidence": _cut(
                    "; ".join(
                        f"{index[eid].tool} {index[eid].query_text}".strip()
                        for eid in cited_ids
                    )
                ),
            }
        )

    output = {
        "entities": entities,
        "propagations": _propagations(parsed.summary, matcher, full),
        "alerts_explained": [],
    }
    return Conversion(
        output,
        _meta(
            True,
            entity_count=len(entities),
            root_cause_entity=full.get(root) if root else None,
            kind_rules=kind_rules,
            unresolved_evidence_ids=unresolved,
        ),
    )


def write_trial(
    root: Path, scenario_dir: str, trial: int, conversion: Conversion
) -> Path:
    """Write ``<root>/<scenario_dir>/<trial>/outputs/agent_output.json``.

    The layout is the first candidate of the pinned evaluator's loader.  The
    sidecar ``conversion_meta.json`` sits in the trial directory, outside
    ``outputs/``, so it never reaches the judge prompt.
    """

    if trial < 0:
        raise ValueError("trial must be a non-negative integer")
    trial_dir = Path(root) / scenario_dir / str(trial)
    outputs = trial_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    path = outputs / "agent_output.json"
    path.write_text(
        json.dumps(conversion.agent_output, ensure_ascii=False, indent=2) + "\n"
    )
    (trial_dir / "conversion_meta.json").write_text(
        json.dumps(conversion.meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return path


def _read_json(path: str | None) -> Any:
    if path is None:
        return None
    return json.loads(Path(path).read_text())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report", help="m0-report-v2 JSON; omit for a Run without a report"
    )
    parser.add_argument("--evidence", help="JSON list of evidence records")
    parser.add_argument(
        "--vocabulary", required=True, help="text file, one name per line"
    )
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--out", required=True, help="evaluator outputs root (git-ignored)"
    )
    parser.add_argument("--scenario-dir", required=True)
    parser.add_argument("--trial", required=True, type=int)
    args = parser.parse_args(argv)
    vocabulary = Path(args.vocabulary).read_text().splitlines()
    conversion = convert(
        _read_json(args.report),
        _read_json(args.evidence) or [],
        vocabulary,
        namespace=args.namespace,
    )
    print(write_trial(Path(args.out), args.scenario_dir, args.trial, conversion))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
