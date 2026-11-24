"""Deterministic root-cause entity matching for ITBench-Lite (#582, L8).

A deterministic counterpart of the judge's ROOT_CAUSE_ENTITY step in the
pinned ``ITBench-Evaluations@14f026fc``:

* predicted root causes are the ``entities`` with ``contributing_factor: true``,
  in order, after dropping the infrastructure namespaces the official
  ``namespace_filter.py`` excludes;
* a prediction ``namespace/Kind/name`` matches a ground-truth entity when the
  entity's ``kind`` (if it has one) equals ``Kind`` case-insensitively and one
  of its ``filter`` regexes ``re.search``-matches either ``name`` or the full
  predicted string;
* a ground-truth root cause is one alias group: every entity listed with it in
  ``aliases`` counts as that root cause, and recall counts each group once;
* precision, recall, F1 and the @k variants follow the official
  ``recalculate_entity_metrics``.

The ground truth is a caller-supplied mapping; this module never locates or
opens an answer file.  Only the judging process may pass one in (L9).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Copied verbatim from ITBench-Evaluations@14f026fc namespace_filter.py.
DEFAULT_EXCLUDED_NAMESPACES: frozenset[str] = frozenset(
    {
        "kube-system",
        "data-recorders",
        "clickhouse",
        "clickhouse-operator",
        "prometheus",
        "opentelemetry-operator",
        "opentelemetry-collectors",
        "metrics-server",
        "opensearch",
    }
)
K_VALUES = (1, 2, 3, 4, 5)


def split_entity(name: str) -> tuple[str, str, str]:
    """``namespace/Kind/name`` to its parts; namespace as official ``extract_namespace``."""

    parts = name.split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], "/".join(parts[2:])
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", name


def _filters(entity: Mapping[str, Any]) -> list[str]:
    value = entity.get("filter")
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def entity_matches(predicted: str, gt_entity: Mapping[str, Any]) -> bool:
    """Whether one predicted entity name is a confident match for one GT entity."""

    _, kind, name = split_entity(predicted)
    gt_kind = gt_entity.get("kind")
    if isinstance(gt_kind, str) and gt_kind.strip():
        if kind.casefold() != gt_kind.strip().casefold():
            return False
    for pattern in _filters(gt_entity):
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        if compiled.search(name) or compiled.search(predicted):
            return True
    return False


def root_cause_groups(
    gt: Mapping[str, Any],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Ground-truth root causes as ``(group key, member entities)``.

    A root-cause entity's group is itself plus every alias group that contains
    it; groups sharing a member are one root cause.
    """

    entities: dict[str, Mapping[str, Any]] = {}
    for entity in gt.get("groups") or []:
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str):
            entities.setdefault(entity["id"], entity)
    alias_groups = [
        {member for member in group if isinstance(member, str)}
        for group in gt.get("aliases") or []
        if isinstance(group, (list, tuple))
    ]
    groups: list[set[str]] = []
    for entity_id, entity in entities.items():
        if entity.get("root_cause") is not True:
            continue
        members = {entity_id}
        for alias in alias_groups:
            if entity_id in alias:
                members |= alias
        overlapping = [existing for existing in groups if existing & members]
        for existing in overlapping:
            members |= existing
        groups = [existing for existing in groups if not existing & members]
        groups.append(members)
    return [
        (
            "|".join(sorted(members)),
            [entities[m] for m in sorted(members) if m in entities],
        )
        for members in groups
    ]


def _prf(predicted: Sequence[Mapping[str, Any]], gt_count: int) -> dict[str, float]:
    if not predicted or gt_count <= 0:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    tp = sum(1 for item in predicted if item["matches_gt"])
    found = {item["matched_to"] for item in predicted if item["matches_gt"]}
    precision = tp / len(predicted)
    recall = len(found) / gt_count
    f1 = (
        2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def score(
    agent_output: Mapping[str, Any],
    gt: Mapping[str, Any],
    *,
    excluded_namespaces: Iterable[str] = DEFAULT_EXCLUDED_NAMESPACES,
) -> dict[str, Any]:
    """Score one trial's ``agent_output`` against one ground truth."""

    excluded = frozenset(excluded_namespaces)
    groups = root_cause_groups(gt)
    names = [
        entity["name"]
        for entity in agent_output.get("entities") or []
        if isinstance(entity, Mapping)
        and entity.get("contributing_factor") is True
        and isinstance(entity.get("name"), str)
    ]
    predicted: list[dict[str, Any]] = []
    for name in names:
        if split_entity(name)[0] in excluded:
            continue
        matched_to = next(
            (
                key
                for key, members in groups
                if any(entity_matches(name, m) for m in members)
            ),
            None,
        )
        predicted.append(
            {
                "entity": name,
                "matches_gt": matched_to is not None,
                "matched_to": matched_to,
            }
        )
    result: dict[str, Any] = {
        "gt_count": len(groups),
        "predicted": predicted,
        **_prf(predicted, len(groups)),
    }
    for k in K_VALUES:
        for metric, value in _prf(predicted[:k], len(groups)).items():
            result[f"k@{k}_{metric}"] = value
    result["pass"] = result["f1"] == 1.0
    return result
