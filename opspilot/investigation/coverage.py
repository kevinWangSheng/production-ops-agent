"""F3 step 3: per-source coverage of one Run and the collection-truncated mark.

Pure functions over what a Run already committed: its input snapshot, its
step rows and its conclusion payload. Nothing here is stored or reaches the
model (U5-a: no prompt change); the workbench page and the acceptance
projection (``opspilot.acceptance_investigation``) both render these values,
so the two show one reading.

A source is what the Run's recorded ``evidence_context.data_sources`` names
(#311 U5): ``available`` maps a source to its tools, ``not_integrated`` lists
sources the profile cannot query. A Run recorded before that list existed is
counted per tool name and claims nothing about unauthorized or missing
sources (``sources_recorded=False``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from opspilot.investigation.context import CONCLUSION_KIND

__all__ = [
    "DUPLICATE_REASON",
    "INTERNAL_SOURCE",
    "UNKNOWN_SOURCE",
    "RunCoverage",
    "SourceCoverage",
    "collection_truncated",
    "run_coverage",
]

# B5's view for a call that was never re-executed (loop._duplicate_call_view).
DUPLICATE_REASON = "DUPLICATE_TOOL_CALL"
EVIDENCE_TOOL = "read_evidence"
# R-4: a refused read of the Run's own stored evidence is not a data source.
INTERNAL_SOURCE = "opspilot-evidence"
UNKNOWN_SOURCE = "unknown"
_OK = "ok"
_NO_DATA = "no_data"


@dataclass(frozen=True)
class SourceCoverage:
    """One source's committed results in one Run.

    ``state`` is one of ``all_ok``, ``partial_failure``, ``failed``,
    ``no_data_only``, ``not_queried``, ``not_authorized``, ``not_integrated``
    and, for the internal evidence store only, ``internal``. ``gap`` is true
    for every data-source state except ``all_ok`` (D3-a).
    """

    name: str
    ok: int
    no_data: int
    non_ok: int
    reasons: Mapping[str, int]
    state: str
    gap: bool


@dataclass(frozen=True)
class RunCoverage:
    run_id: Any
    sources_recorded: bool
    sources: tuple[SourceCoverage, ...]
    duplicates_skipped: int


class _Tally:
    def __init__(self) -> None:
        self.ok = 0
        self.no_data = 0
        self.non_ok = 0
        self.reasons: dict[str, int] = {}

    def add(self, result: Mapping[str, Any]) -> None:
        status = result.get("status")
        if status == _OK:
            self.ok += 1
        elif status == _NO_DATA:
            self.no_data += 1
        else:
            self.non_ok += 1
            reason = result.get("reason")
            key = reason if isinstance(reason, str) else str(status)
            self.reasons[key] = self.reasons.get(key, 0) + 1

    def sorted_reasons(self) -> Mapping[str, int]:
        # R-6: most frequent first; ties in name order for a stable page.
        ordered = sorted(self.reasons.items(), key=lambda item: (-item[1], item[0]))
        return MappingProxyType(dict(ordered))


def _data_sources(
    input_snapshot: Mapping[str, Any],
) -> tuple[dict[str, list[str]], list[str]] | None:
    context = input_snapshot.get("evidence_context")
    sources = context.get("data_sources") if isinstance(context, Mapping) else None
    if not isinstance(sources, Mapping):
        return None
    available = sources.get("available")
    if not isinstance(available, Mapping):
        return None
    mapping = {
        str(name): [tool for tool in tools if isinstance(tool, str)]
        for name, tools in available.items()
        if isinstance(tools, list)
    }
    missing = sources.get("not_integrated")
    names = (
        [name for name in missing if isinstance(name, str)]
        if isinstance(missing, list)
        else []
    )
    return mapping, names


def _schema_tools(input_snapshot: Mapping[str, Any]) -> set[str]:
    names = set()
    for item in input_snapshot.get("tool_schemas") or []:
        function = item.get("function") if isinstance(item, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _planned_names(response: Mapping[str, Any]) -> list[str | None]:
    assistant = response.get("assistant")
    calls = assistant.get("tool_calls") if isinstance(assistant, Mapping) else None
    names: list[str | None] = []
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        names.append(name if isinstance(name, str) else None)
    return names


def _live(steps: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [step for step in steps if step.get("status") != "late_result"]


def _state(tally: _Tally) -> str:
    if tally.non_ok:
        return "partial_failure" if tally.ok else "failed"
    if tally.ok:
        return "all_ok"
    return "no_data_only" if tally.no_data else "not_queried"


def _row(name: str, tally: _Tally, state: str | None = None) -> SourceCoverage:
    state = _state(tally) if state is None else state
    return SourceCoverage(
        name=name,
        ok=tally.ok,
        no_data=tally.no_data,
        non_ok=tally.non_ok,
        reasons=tally.sorted_reasons(),
        state=state,
        gap=state not in {"all_ok", "internal"},
    )


def run_coverage(
    run_id: Any,
    input_snapshot: Mapping[str, Any] | None,
    steps: Iterable[Mapping[str, Any]],
) -> RunCoverage:
    """Count one Run's own committed tool results per source.

    ``steps`` are that Run's ``opspilot_steps`` rows (``status``,
    ``response``, ``tool_results``); ``late_result`` rows are history and do
    not count (R-2). Continuation-inherited evidence is not in these rows, so
    it is not counted either.
    """
    snapshot = input_snapshot if isinstance(input_snapshot, Mapping) else {}
    recorded = _data_sources(snapshot)
    owner: dict[str, str] = {}
    if recorded is not None:
        for source, names in recorded[0].items():
            for listed in names:
                owner.setdefault(listed, source)
    tallies: dict[str, _Tally] = {}
    internal = _Tally()
    duplicates = 0

    def tally(name: str) -> _Tally:
        return tallies.setdefault(name, _Tally())

    for step in _live(steps):
        response = step.get("response")
        planned = _planned_names(response) if isinstance(response, Mapping) else []
        for item in step.get("tool_results") or []:
            if not isinstance(item, Mapping):
                continue
            result = item.get("result")
            if not isinstance(result, Mapping):
                continue
            if result.get("reason") == DUPLICATE_REASON:
                # R-3: never reached a source.
                duplicates += 1
                continue
            tool = result.get("tool")
            if not isinstance(tool, str):
                ordinal = item.get("ordinal")
                tool = (
                    planned[ordinal]
                    if isinstance(ordinal, int) and 0 <= ordinal < len(planned)
                    else None
                ) or UNKNOWN_SOURCE
            if tool == EVIDENCE_TOOL:
                parent = result.get("parent_tool")
                if result.get("status") in {_OK, _NO_DATA} and isinstance(parent, str):
                    # R-4: a slice is the parent's observation.
                    tool = parent
                else:
                    internal.add(result)
                    continue
            tally(owner.get(tool, tool)).add(result)

    rows: list[SourceCoverage] = []
    if recorded is not None:
        available, not_integrated = recorded
        authorized = _schema_tools(snapshot)
        for source, tools in available.items():
            counts = tallies.pop(source, _Tally())
            # D4-a: none of the source's tools was offered to this Run.
            withheld = not any(tool in authorized for tool in tools)
            rows.append(_row(source, counts, "not_authorized" if withheld else None))
        for source in not_integrated:
            if source in available:
                continue
            rows.append(_row(source, tallies.pop(source, _Tally()), "not_integrated"))
    unknown = tallies.pop(UNKNOWN_SOURCE, None)
    for name in sorted(tallies):
        rows.append(_row(name, tallies[name]))
    if unknown is not None:
        rows.append(_row(UNKNOWN_SOURCE, unknown))
    if internal.ok or internal.no_data or internal.non_ok:
        rows.append(_row(INTERNAL_SOURCE, internal, "internal"))
    return RunCoverage(
        run_id=run_id,
        sources_recorded=recorded is not None,
        sources=tuple(rows),
        duplicates_skipped=duplicates,
    )


def collection_truncated(
    input_snapshot: Mapping[str, Any] | None,
    steps: Iterable[Mapping[str, Any]],
    conclusion: Mapping[str, Any] | None,
) -> bool | None:
    """U7-a / D5-a: whether the loop-guard bound forced the final round.

    True when the conclusion used the whole ``input.model_requests``, its
    source round was a final round, and that round was not the L1a/B3 report
    retry (the model round before it made no tool call, rejected none, and was
    not itself final). ``None`` when the rows lack what the rule reads
    (R-10); nothing is inferred from a missing field.
    """
    if not isinstance(conclusion, Mapping):
        return None
    inner = conclusion.get("conclusion")
    if conclusion.get("kind") != CONCLUSION_KIND or not isinstance(inner, Mapping):
        return None
    snapshot = input_snapshot if isinstance(input_snapshot, Mapping) else {}
    limit = snapshot.get("model_requests")
    used = inner.get("model_requests_used")
    if type(limit) is not int or type(used) is not int:
        return None
    if used < limit:
        return False
    source = inner.get("source_step_id")
    rounds = [
        step
        for step in _live(steps)
        if isinstance(step.get("response"), Mapping)
        and step["response"].get("kind") is None
    ]
    index = next(
        (i for i, step in enumerate(rounds) if str(step.get("step_id")) == source),
        None,
    )
    if index is None:
        return None
    context = rounds[index]["response"].get("context")
    final = context.get("final") if isinstance(context, Mapping) else None
    if not isinstance(final, bool):
        return None
    if not final:
        return False
    if index == 0:
        return True
    prior = rounds[index - 1]["response"]
    prior_context = prior.get("context")
    prior_final = (
        prior_context.get("final") if isinstance(prior_context, Mapping) else None
    )
    l1a_retry = (
        not _planned_names(prior)
        and prior.get("rejected_plan") is None
        and prior_final is False
    )
    return not l1a_retry
