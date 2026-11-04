"""S7c acceptance: paired scheduling, isolation records, pre-registered
void/rerun rules and the ``dataset`` dimension.

Independent acceptance tests (written without the implementation). Contract
sources: F1 closure prereview R7 (same injection and window, alternating
order, same scenario >= 60 min apart, wash >= 600 s, ``predecessor`` /
``contamination`` per slot), r0.1-3 (steal/load per slot, ``host_noise``;
shared-vCPU pair rerun: steal > 5 % or baseline p95 > 2x the probe
baseline, both systems, once, original kept as "host noise, pair rerun per
rule"), R2 / #432 rule (b) (setup failure before any product Run: one
rerun, "environment not ready, rerun per rule"; a rerun is never rerun),
R8 (``<dataset name>:<tree hash>``), U2 (3 attempts per scenario), §3.3;
closure rulings revision 07:40 PT (void/rerun rules written before the
exam questions); V2-R12 (alert-entry scenarios have no Holmes row).

Entry points the implementation provides:

module ``scripts.eval_pair_schedule``

``plan_schedule(scenarios: Sequence[tuple[ScenarioSpecV2, AnswerV2]], *, k: int = 3, labs: int = 1, slot_seconds: float, wash_seconds: float = 600, same_scenario_gap_seconds: float = 3600, seed: str) -> dict``
    Deterministic for the same arguments. Result::

        {"k": int, "labs": int, "rules_sha256": str,
         "pairs": [{"pair_id": str, "lab": int (0..labs-1),
                    "scenario_id": str, "attempt": int (1..k),
                    "systems": ["opspilot", "holmes"] | ["holmes", "opspilot"]
                               | ["opspilot"],
                    "start_s": float, "end_s": float}, ...]}

    One pair = one injection and one window; its systems run back to back
    in the listed order. ``start_s``/``end_s`` are planned seconds from the
    lab's own start; each lab's timeline is independent. Lower
    ``wash_seconds`` than 600 or ``same_scenario_gap_seconds`` than 3600 is
    refused with ``ValueError``.

``RULES: Mapping[str, Any]`` and ``rules_sha256() -> str``
    The pre-registered void/rerun rules and their hash (64 hex), recorded in
    every schedule manifest.

``rerun_decision(pair: Mapping, *, shared_host: bool, probe_baseline_p95_ms: float) -> dict``
    ``pair``: ``{"pair_id", "rerun_of": str | None, "baseline_p95_ms": float,
    "slots": [{"system", "status": "ran" | "setup_failed",
    "product_run_created": bool, "host_noise": {"steal_percent": float}}]}``.
    Result ``{"action": "keep" | "rerun_pair" | "rerun_slot", "systems":
    [system, ...] (what reruns; empty for keep), "note": str | None}``.

``steal_percent(before: str, after: str) -> float``
    CPU steal share (percent) between two ``/proc/stat`` snapshots, from the
    aggregate ``cpu`` line (user..steal; guest fields excluded).

``annotate_executed(slots: Sequence[Mapping]) -> list[dict]``
    ``slots``: executed slots ``{"lab", "pair_id", "scenario_id", "system",
    "started_at", "ended_at"}`` (ISO-8601 UTC). Returns them in input order,
    each with ``predecessor`` (``{"pair_id", "scenario_id", "gap_minutes"}``
    of the latest earlier pair on the same lab, gap from that pair's end to
    this pair's start, or ``None``) and ``violations`` (``"WASH_SHORT"``,
    ``"SAME_SCENARIO_GAP"``). A pair's partner slot is never its
    predecessor.

existing function ``scripts.eval_live.dataset_dimension(root)``
    ``value`` becomes ``"<root directory name>:<dataset_tree_hash(root)>"``.
"""

from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation.model import dataset_tree_hash, load_scenarios_v2
from scripts import eval_pair_schedule as s7c
from scripts.eval_live import dataset_dimension

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals/datasets/dev-v2"
SLOT = 510.0  # about 8.5 min per slot (V2-R14)
HOST_NOISE_NOTE = "host noise, pair rerun per rule"
SETUP_NOTE = "environment not ready, rerun per rule"


@pytest.fixture(scope="module")
def scenarios() -> list[tuple[Any, Any]]:
    return list(load_scenarios_v2(DATASET))


def _plan(scenarios: list[tuple[Any, Any]], **kw: Any) -> dict[str, Any]:
    kw.setdefault("slot_seconds", SLOT)
    kw.setdefault("seed", "s7c-acceptance")
    return s7c.plan_schedule(scenarios, **kw)


# --- shape: k attempts, pairs, systems (U2, R7, V2-R12)


def test_every_scenario_gets_k_pairs_with_the_right_systems(
    scenarios: list[tuple[Any, Any]],
) -> None:
    plan = _plan(scenarios)
    assert plan["k"] == 3
    pairs = plan["pairs"]
    assert len({p["pair_id"] for p in pairs}) == len(pairs)
    attempts = Counter(p["scenario_id"] for p in pairs)
    assert attempts == {spec.scenario_id: 3 for spec, _ in scenarios}
    entry = {spec.scenario_id: spec.entry for spec, _ in scenarios}
    for pair in pairs:
        assert sorted(
            p["attempt"] for p in pairs if p["scenario_id"] == pair["scenario_id"]
        ) == [1, 2, 3]
        if entry[pair["scenario_id"]] == "question":
            assert sorted(pair["systems"]) == ["holmes", "opspilot"]
        else:
            assert pair["systems"] == ["opspilot"]


def test_order_alternates_and_balances(scenarios: list[tuple[Any, Any]]) -> None:
    pairs = [p for p in _plan(scenarios)["pairs"] if len(p["systems"]) == 2]
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_scenario[pair["scenario_id"]].append(pair)
    for items in by_scenario.values():
        firsts = [p["systems"][0] for p in sorted(items, key=lambda p: p["attempt"])]
        assert all(a != b for a, b in zip(firsts, firsts[1:], strict=False)), firsts
    first = Counter(p["systems"][0] for p in pairs)
    assert abs(first["opspilot"] - first["holmes"]) <= 1


def _check_timeline(pairs: list[dict[str, Any]], wash: float, gap: float) -> None:
    by_lab: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_lab[pair["lab"]].append(pair)
    for items in by_lab.values():
        items.sort(key=lambda p: p["start_s"])
        for pair in items:
            assert pair["end_s"] - pair["start_s"] >= len(pair["systems"]) * SLOT
        for prev, nxt in zip(items, items[1:], strict=False):
            assert nxt["start_s"] >= prev["end_s"] + wash, (prev, nxt)
        last_end: dict[str, float] = {}
        for pair in items:
            sid = pair["scenario_id"]
            if sid in last_end:
                assert pair["start_s"] - last_end[sid] >= gap, pair
            last_end[sid] = pair["end_s"]


def test_wash_and_same_scenario_gap(scenarios: list[tuple[Any, Any]]) -> None:
    _check_timeline(_plan(scenarios)["pairs"], 600, 3600)


def test_gap_holds_with_few_scenarios(scenarios: list[tuple[Any, Any]]) -> None:
    # too few other scenarios to fill an hour: the plan must wait instead
    plan = _plan(scenarios[:2])
    assert len(plan["pairs"]) == 6
    _check_timeline(plan["pairs"], 600, 3600)


def test_minimums_cannot_be_lowered(scenarios: list[tuple[Any, Any]]) -> None:
    with pytest.raises(ValueError):
        _plan(scenarios, wash_seconds=300)
    with pytest.raises(ValueError):
        _plan(scenarios, same_scenario_gap_seconds=1800)
    stricter = _plan(scenarios, wash_seconds=900, same_scenario_gap_seconds=5400)
    _check_timeline(stricter["pairs"], 900, 5400)


def test_shards_across_independent_labs(scenarios: list[tuple[Any, Any]]) -> None:
    single = _plan(scenarios)
    sharded = _plan(scenarios, labs=3)
    assert sharded["labs"] == 3
    assert {p["lab"] for p in sharded["pairs"]} == {0, 1, 2}
    key = lambda p: (p["scenario_id"], p["attempt"])  # noqa: E731
    assert sorted(map(key, sharded["pairs"])) == sorted(map(key, single["pairs"]))
    _check_timeline(sharded["pairs"], 600, 3600)
    # sharding shortens the wall clock of each lab
    span = lambda plan, lab: max(p["end_s"] for p in plan["pairs"] if p["lab"] == lab)  # noqa: E731
    assert max(span(sharded, lab) for lab in range(3)) < span(single, 0)


def test_deterministic_and_records_the_rules_hash(
    scenarios: list[tuple[Any, Any]],
) -> None:
    assert _plan(scenarios) == _plan(scenarios)
    digest = s7c.rules_sha256()
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert digest == s7c.rules_sha256()
    assert _plan(scenarios)["rules_sha256"] == digest
    assert s7c.RULES  # written down, not empty


# --- pre-registered rerun rules (R2, #432 rule (b), r0.1-3)


def _slot(
    system: str, *, status: str = "ran", run: bool = True, steal: float = 0.5
) -> dict[str, Any]:
    return {
        "system": system,
        "status": status,
        "product_run_created": run,
        "host_noise": {"steal_percent": steal},
    }


def _pair(
    *slots: dict[str, Any], p95: float = 300.0, rerun_of: str | None = None
) -> dict[str, Any]:
    return {
        "pair_id": "pair-1",
        "rerun_of": rerun_of,
        "baseline_p95_ms": p95,
        "slots": list(slots) or [_slot("opspilot"), _slot("holmes")],
    }


def test_clean_pair_is_kept() -> None:
    out = s7c.rerun_decision(_pair(), shared_host=True, probe_baseline_p95_ms=300.0)
    assert out["action"] == "keep"
    assert out["systems"] == []


@pytest.mark.parametrize(
    "pair",
    [
        _pair(_slot("opspilot", steal=5.1), _slot("holmes")),
        _pair(_slot("opspilot"), _slot("holmes", steal=12.0)),
        _pair(p95=601.0),
    ],
)
def test_shared_host_noise_reruns_the_whole_pair(pair: dict[str, Any]) -> None:
    out = s7c.rerun_decision(pair, shared_host=True, probe_baseline_p95_ms=300.0)
    assert out["action"] == "rerun_pair"
    assert sorted(out["systems"]) == ["holmes", "opspilot"]
    assert out["note"] == HOST_NOISE_NOTE


def test_thresholds_are_strict() -> None:
    at_limit = _pair(_slot("opspilot", steal=5.0), _slot("holmes"), p95=600.0)
    out = s7c.rerun_decision(at_limit, shared_host=True, probe_baseline_p95_ms=300.0)
    assert out["action"] == "keep"


def test_noise_rule_only_on_a_shared_host() -> None:
    noisy = _pair(_slot("opspilot", steal=30.0), _slot("holmes"), p95=5000.0)
    out = s7c.rerun_decision(noisy, shared_host=False, probe_baseline_p95_ms=300.0)
    assert out["action"] == "keep"


def test_a_rerun_is_never_rerun() -> None:
    noisy = _pair(_slot("opspilot", steal=30.0), _slot("holmes"), rerun_of="pair-0")
    out = s7c.rerun_decision(noisy, shared_host=True, probe_baseline_p95_ms=300.0)
    assert out["action"] == "keep"
    failed = _pair(
        _slot("opspilot", status="setup_failed", run=False),
        _slot("holmes"),
        rerun_of="pair-0",
    )
    out = s7c.rerun_decision(failed, shared_host=False, probe_baseline_p95_ms=300.0)
    assert out["action"] == "keep"


def test_setup_failure_before_any_run_reruns_that_slot_once() -> None:
    pair = _pair(_slot("opspilot"), _slot("holmes", status="setup_failed", run=False))
    out = s7c.rerun_decision(pair, shared_host=False, probe_baseline_p95_ms=300.0)
    assert out["action"] == "rerun_slot"
    assert out["systems"] == ["holmes"]
    assert out["note"] == SETUP_NOTE


def test_product_side_failure_is_scored_not_rerun() -> None:
    pair = _pair(_slot("opspilot", status="setup_failed", run=True), _slot("holmes"))
    out = s7c.rerun_decision(pair, shared_host=False, probe_baseline_p95_ms=300.0)
    assert out["action"] == "keep"


# --- host noise record (r0.1-3)


def test_steal_percent_from_proc_stat() -> None:
    before = (
        "cpu  100 0 100 700 0 0 0 100 50 0\n"
        "cpu0 50 0 50 350 0 0 0 50 25 0\n"
        "intr 12345\n"
    )
    after = (
        "cpu  200 0 200 1400 0 0 0 200 90 0\n"
        "cpu0 100 0 100 700 0 0 0 100 45 0\n"
        "intr 23456\n"
    )
    assert s7c.steal_percent(before, after) == pytest.approx(10.0)
    assert s7c.steal_percent(before, before.replace("intr 12345", "intr 1")) == 0.0


# --- predecessor and spacing of executed slots (R7, A11/A12)


def _exec(
    lab: str, pair: str, sid: str, system: str, start: str, end: str
) -> dict[str, Any]:
    day = "2026-11-10T"
    return {
        "lab": lab,
        "pair_id": pair,
        "scenario_id": sid,
        "system": system,
        "started_at": f"{day}{start}:00+00:00",
        "ended_at": f"{day}{end}:00+00:00",
    }


def test_annotate_executed_predecessor_and_violations() -> None:
    slots = [
        _exec("a", "P1", "dv2-111111", "opspilot", "10:00", "10:08"),
        _exec("a", "P1", "dv2-111111", "holmes", "10:08", "10:16"),
        _exec("a", "P2", "dv2-222222", "holmes", "10:30", "10:38"),
        _exec("a", "P2", "dv2-222222", "opspilot", "10:38", "10:45"),
        _exec("a", "P3", "dv2-111111", "opspilot", "10:50", "10:58"),
        _exec("b", "Q1", "dv2-111111", "opspilot", "10:20", "10:28"),
    ]
    out = s7c.annotate_executed(slots)
    assert [(o["pair_id"], o["system"]) for o in out] == [
        (s["pair_id"], s["system"]) for s in slots
    ]
    p1_ops, p1_holmes, p2_holmes, p2_ops, p3, q1 = out
    # the first pair of a lab has none; the partner slot is never one
    assert p1_ops["predecessor"] is None
    assert p1_holmes["predecessor"] is None
    for slot in (p2_holmes, p2_ops):
        assert slot["predecessor"]["pair_id"] == "P1"
        assert slot["predecessor"]["scenario_id"] == "dv2-111111"
        assert slot["predecessor"]["gap_minutes"] == pytest.approx(14.0, abs=0.1)
        assert slot["violations"] == []
    assert p3["predecessor"]["pair_id"] == "P2"
    assert p3["predecessor"]["gap_minutes"] == pytest.approx(5.0, abs=0.1)
    # 5 min after P2 (< 600 s wash); 34 min after P1, same scenario (< 60 min)
    assert sorted(p3["violations"]) == ["SAME_SCENARIO_GAP", "WASH_SHORT"]
    # another lab is independent
    assert q1["predecessor"] is None
    assert q1["violations"] == []


# --- dataset dimension prefix (R8)


def test_dataset_dimension_names_the_dataset(tmp_path: Path) -> None:
    dev = dataset_dimension(DATASET)
    assert dev.value == f"dev-v2:{dataset_tree_hash(DATASET)}"
    holdout = tmp_path / "holdout-3f9a2c"
    shutil.copytree(DATASET, holdout)
    (holdout / "salt.txt").write_text("0" * 64)
    value = dataset_dimension(holdout).value
    assert value == f"holdout-3f9a2c:{dataset_tree_hash(holdout)}"
    assert not value.startswith("dev-v2:")
    assert value.split(":", 1)[1] != dev.value.split(":", 1)[1]
