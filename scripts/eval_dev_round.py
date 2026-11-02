"""The v2 development round summary (#432, V2-R14, Q20): engineer CLI.

Interface: ``~/.agent/handoffs/m3-b3-2026-11-02/seams-432.md`` section 5.

    .venv/bin/python -m scripts.eval_dev_round --dataset evals/datasets/dev-v2 \\
        --round docs/evidence/eval/<R1> [--round ...] \\
        --out docs/evidence/432-dev-round/summary.json

Offline and pure apart from reading the rounds' public evidence: every
scenario of the dataset gets exactly ``--n`` (2) slots; every started
attempt counts and none is drawn again (more than ``n`` started attempts of
one scenario is ``REDRAW_DETECTED``); a slot that never started counts as a
Q20 failure. Q20 is re-checked from each attempt's public ``projection.json``
and ``intake.json`` and must equal the stored ``expectations.json``
(``EXPECTATIONS_MISMATCH``). All rounds must share the code, dataset,
evaluator, runbook and upstream dimensions (``MANIFEST_DRIFT``). Regression
and capability groups are reported separately; the gate is a ratio only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from opspilot.evaluation import REQUIRED_DIMENSIONS
from opspilot.evaluation.expectations import check_expectations
from opspilot.evaluation.model import (
    AnswerV2,
    ScenarioSpecV2,
    dataset_tree_hash,
    load_scenarios_v2,
)

SCHEMA = "opspilot-eval-dev-round-1"
SLOTS_PER_SCENARIO = 2
#: Dimensions that must not move between the rounds of one development round.
FROZEN_DIMENSIONS = ("code", "dataset", "evaluator", "runbook", "upstream")


class DevRoundError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise DevRoundError(f"EVIDENCE_INVALID:{path.name}") from None


def upstream_applicability(spec: ScenarioSpecV2, answer: AnswerV2) -> str:
    """V2-R12: what the HolmesGPT comparison can cover for this scenario."""
    if spec.entry == "alertmanager":
        return "no_upstream_entry"
    if answer.category == "model_failure":
        return "same_proxy_injection"
    return "comparable"


def _dimensions(summary: Mapping[str, Any]) -> dict[str, Any]:
    dims = summary.get("capture", {}).get("manifest", {}).get("dimensions")
    if not isinstance(dims, Mapping):
        raise DevRoundError("EVIDENCE_INVALID:summary.json")
    return dict(dims)


def _binding(
    loaded: Mapping[str, tuple[ScenarioSpecV2, AnswerV2]], ids: set[str]
) -> list[str]:
    classes = set()
    for sid in ids:
        spec, answer = loaded[sid]
        source = next(
            (
                str(i.params.get("source"))
                for i in answer.injectors
                if i.kind == "source_unreachable"
            ),
            "none",
        )
        classes.add((spec.entry, source))
    if len(classes) != 1:
        raise DevRoundError("ROUND_BINDING_MIXED")
    return list(next(iter(classes)))


def _attempt(
    run_dir: Path,
    slot: Mapping[str, Any],
    answer: AnswerV2,
    target_id: str,
) -> dict[str, Any]:
    summary = _json(run_dir / "summary.json")
    if not isinstance(summary, Mapping):
        raise DevRoundError("SUMMARY_MISSING")
    capture = summary.get("capture") or {}
    attempt = capture.get("attempt") or {}
    status = str(attempt.get("status") or slot.get("status"))
    projection = _json(run_dir / "projection.json")
    intake = _json(run_dir / "intake.json")
    stored = _json(run_dir / "expectations.json")
    checked = check_expectations(
        answer,
        attempt_status=status,
        attempt_reason=slot.get("reason"),
        projection=projection,
        intake=intake,
        target_id=target_id,
    ).as_json()
    if not isinstance(stored, Mapping) or any(
        stored.get(key) != checked[key] for key in ("passed", "checks", "reasons")
    ):
        raise DevRoundError(f"EXPECTATIONS_MISMATCH:{run_dir.name}")
    gate = (summary.get("evaluation") or {}).get("gate") or {}
    traces = (summary.get("trace") or {}).get("entries") or []
    return {
        "started_at": capture.get("started_at"),
        "status": status,
        "reason": slot.get("reason"),
        "expectations": checked,
        "gate": {"passed": gate.get("passed"), "reasons": gate.get("reasons")},
        "trace_url": next(
            (t.get("url") for t in traces if isinstance(t, Mapping) and t.get("url")),
            None,
        ),
        "cost": capture.get("cost") or {},
        "dimensions": _dimensions(summary),
    }


def aggregate(
    round_dirs: Sequence[Path],
    dataset_root: Path,
    *,
    n: int = SLOTS_PER_SCENARIO,
) -> dict[str, Any]:
    """The development round summary (seams-432 section 5)."""
    loaded = {
        spec.scenario_id: (spec, answer)
        for spec, answer in load_scenarios_v2(dataset_root)
    }
    dataset = f"dev-v2:{dataset_tree_hash(dataset_root)}"
    rounds: list[dict[str, Any]] = []
    attempts: dict[str, list[dict[str, Any]]] = {sid: [] for sid in loaded}
    dims_seen: list[dict[str, Any]] = []
    for round_dir in round_dirs:
        lifecycle = _json(round_dir / "lifecycle.json")
        if not isinstance(lifecycle, Mapping):
            raise DevRoundError(f"LIFECYCLE_MISSING:{round_dir.name}")
        slots = [s for s in lifecycle.get("slots") or [] if isinstance(s, Mapping)]
        unknown = {str(s.get("scenario_id")) for s in slots} - set(loaded)
        if unknown:
            raise DevRoundError(f"UNKNOWN_SCENARIO:{sorted(unknown)[0]}")
        round_id = str(lifecycle.get("round"))
        rounds.append(
            {
                "round": round_id,
                "manifest_hash": lifecycle.get("manifest_hash"),
                "binding_class": _binding(
                    loaded, {str(s["scenario_id"]) for s in slots}
                )
                if slots
                else None,
                "lifecycle_status": lifecycle.get("round_status"),
                "exit_code": None,
                "reason": lifecycle.get("reason"),
            }
        )
        for slot in slots:
            attempt_id = slot.get("attempt_id")
            if not attempt_id:
                continue  # never started (ROUND_STOPPED): a resume may run it
            sid = str(slot["scenario_id"])
            spec, answer = loaded[sid]
            item = _attempt(
                round_dir / "runs" / str(attempt_id),
                slot,
                answer,
                spec.stimulus.target_id,
            )
            dims_seen.append(item.pop("dimensions"))
            attempts[sid].append({"round": round_id, "attempt_id": attempt_id, **item})
    for sid, items in attempts.items():
        if len(items) > n:
            raise DevRoundError(f"REDRAW_DETECTED:{sid}")
    reference = dims_seen[0] if dims_seen else {}
    for dims in dims_seen:
        for name in FROZEN_DIMENSIONS:
            if dims.get(name) != reference.get(name):
                raise DevRoundError(f"MANIFEST_DRIFT:{name}")
    if reference and (reference.get("dataset") or {}).get("value") != dataset:
        raise DevRoundError("MANIFEST_DRIFT:dataset")
    out_slots: list[dict[str, Any]] = []
    for sid in sorted(loaded):
        spec, answer = loaded[sid]
        items = sorted(attempts[sid], key=lambda a: str(a.get("started_at")))
        for index in range(n):
            base = {
                "scenario_id": sid,
                "group": "regression"
                if answer.category == "regression"
                else "capability",
                "category": answer.category,
                "polarity": answer.polarity,
                "entry": spec.entry,
                "slot": index + 1,
                "upstream": upstream_applicability(spec, answer),
            }
            if index < len(items):
                item = items[index]
                out_slots.append(
                    {
                        **base,
                        "round": item["round"],
                        "attempt_id": item["attempt_id"],
                        "status": item["status"],
                        "reason": item["reason"],
                        "expectations": item["expectations"],
                        "gate": item["gate"],
                        "trace_url": item["trace_url"],
                    }
                )
            else:
                out_slots.append(
                    {
                        **base,
                        "round": None,
                        "attempt_id": None,
                        "status": "not_started",
                        "reason": "NOT_STARTED",
                        "expectations": {
                            "passed": False,
                            "checks": {},
                            "reasons": ["NOT_STARTED"],
                        },
                        "gate": {"passed": None, "reasons": None},
                        "trace_url": None,
                    }
                )
    totals: dict[str, dict[str, int]] = {}
    for group in ("regression", "capability"):
        mine = [s for s in out_slots if s["group"] == group]
        totals[group] = {
            "slots": len(mine),
            "started": sum(1 for s in mine if s["attempt_id"] is not None),
            "q20_passed": sum(1 for s in mine if s["expectations"]["passed"] is True),
            "gate_passed": sum(1 for s in mine if s["gate"]["passed"] is True),
        }
    failed = [
        {
            "scenario_id": s["scenario_id"],
            "slot": s["slot"],
            "reasons": s["expectations"]["reasons"],
        }
        for s in out_slots
        if s["expectations"]["passed"] is not True
    ]
    deltas: list[Decimal] = []
    unknown_cost = 0
    for items in attempts.values():
        for item in items:
            cost = item.get("cost") or {}
            try:
                deltas.append(Decimal(str(cost["delta_cny"])))
            except (KeyError, InvalidOperation, TypeError):
                unknown_cost += 1
    code = reference.get("code") or {}
    return {
        "schema": SCHEMA,
        "dataset": dataset,
        "code": code.get("value"),
        "rounds": rounds,
        "step3": {
            name: reference.get(name) for name in ("runbook", "dataset", "upstream")
        }
        | {
            "missing_dimensions": sorted(set(REQUIRED_DIMENSIONS) - set(reference))
            if reference
            else sorted(REQUIRED_DIMENSIONS)
        },
        "slots": out_slots,
        "totals": totals,
        "f1_step2": {
            "achieved": not failed and bool(out_slots),
            "failed_slots": failed,
        },
        "cost": {
            "measured_delta_cny": str(sum(deltas, Decimal(0))),
            "attempts_measured": len(deltas),
            "attempts_unknown": unknown_cost,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.eval_dev_round")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--round", action="append", required=True)
    parser.add_argument("--n", type=int, default=SLOTS_PER_SCENARIO)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        document = aggregate(
            [Path(p) for p in args.round], Path(args.dataset), n=args.n
        )
    except DevRoundError as exc:
        print(json.dumps({"exit_code": 1, "reason": exc.code}))
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "exit_code": 0,
                "out": str(out),
                "f1_step2_achieved": document["f1_step2"]["achieved"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
