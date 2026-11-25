"""Deterministic root-cause check against a live ground truth (#564).

The live answer file has its own schema (upstream ``models/ground_truth.py``:
``alerts``, ``entities``, ``solutions``), not Lite's ``groups``/``aliases``.
``entities`` are the Kubernetes objects directly involved in the fault, either
flat (``{kind, name, namespace, apiVersion}``, as upstream
``utils/ground_truth.py`` writes them) or wrapped
(``{kubernetes: [<object>, ...]}``, as the Pydantic model declares).

Rule (Agent default, reversible; the official judge stays the main score):
every involved object is one root-cause group with an exact-name filter and
its Kind, then ``entity_match.score`` (predicted root causes =
``contributing_factor: true``, official namespace filter, P/R/F1 and @k).
``any_hit`` is 1 when at least one predicted root cause names an involved
object. Only the judging process calls this; no answer is opened here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from scripts.itbench_lite import entity_match


def involved_objects(gt: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for entity in gt.get("entities") or []:
        if not isinstance(entity, Mapping):
            continue
        wrapped = entity.get("kubernetes")
        if isinstance(wrapped, list):
            yield from (o for o in wrapped if isinstance(o, Mapping))
        else:
            yield entity


def as_lite(gt: Mapping[str, Any]) -> dict[str, Any]:
    """The live answer in the ``groups`` shape ``entity_match`` reads."""
    groups: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for obj in involved_objects(gt):
        name, kind = obj.get("name"), obj.get("kind")
        if not (isinstance(name, str) and name and isinstance(kind, str) and kind):
            continue
        key = (kind.casefold(), name)
        if key in seen:
            continue
        seen.add(key)
        groups.append(
            {
                "id": f"e{len(groups)}",
                "kind": kind,
                "filter": [f"^{re.escape(name)}$"],
                "root_cause": True,
            }
        )
    return {"groups": groups, "aliases": []}


def score(agent_output: Mapping[str, Any], gt: Mapping[str, Any]) -> dict[str, Any]:
    result = entity_match.score(agent_output, as_lite(gt))
    result["any_hit"] = any(p["matches_gt"] for p in result["predicted"])
    return result
