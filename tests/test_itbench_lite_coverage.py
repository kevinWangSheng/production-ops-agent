"""Coverage-tiered score table; tiers here are synthetic labels, not census data."""

from __future__ import annotations

import pytest

from scripts.itbench_lite import coverage


def tiers():
    # Synthetic ids (1..34 plus 38) with made-up tier labels.
    return {f"Scenario-{i}": ("T1" if i % 2 else "T2") for i in [*range(1, 35), 38]}


def test_all_scenarios_stay_in_denominator_missing_runs_score_zero():
    table = coverage.build_table(
        coverage.load_tiers(tiers()),
        {"S1": [{"f1": 1.0}, {"f1": 1.0}, {"f1": 1.0}], "3": [{"f1": 1.0}]},
        ["f1"],
    )
    assert table["overall"]["scenarios"] == 35
    rows = {r["scenario"]: r for r in table["scenarios"]}
    assert rows["1"]["means"]["f1"] == 1.0
    assert rows["3"]["means"]["f1"] == pytest.approx(1 / 3)
    assert rows["3"]["trials_missing"] == 2
    assert rows["5"]["means"]["f1"] == 0.0
    assert table["overall"]["means"]["f1"] == pytest.approx((1 + 1 / 3) / 35)
    t1 = next(t for t in table["tiers"] if t["tier"] == "T1")
    assert t1["scenarios"] == 17


def test_data_incomplete_scenarios_flagged():
    table = coverage.build_table(coverage.load_tiers(tiers()), {}, ["f1"])
    flagged = [r["scenario"] for r in table["scenarios"] if r["data_incomplete"]]
    assert flagged == ["18", "38"]


def test_tier_file_must_list_35_and_include_s38_s18():
    with pytest.raises(ValueError, match="expected 35"):
        coverage.load_tiers({"1": "T1"})
    ids = {f"S{i}": "T" for i in range(1, 36)}
    with pytest.raises(ValueError, match="data-incomplete"):
        coverage.load_tiers(ids)
    ids = {f"S{i}": "T" for i in [*range(1, 35), 38]}
    assert len(coverage.load_tiers(ids)) == 35


def test_score_for_unknown_scenario_rejected():
    ids = coverage.load_tiers({f"S{i}": "T" for i in [*range(1, 35), 38]})
    with pytest.raises(ValueError, match="no tier"):
        coverage.build_table(ids, {"S99": [{}]}, ["f1"])


def test_markdown_labels():
    ids = coverage.load_tiers({f"S{i}": "T" for i in [*range(1, 35), 38]})
    text = coverage.render_markdown(
        coverage.build_table(ids, {}, ["root_cause_entity_f1", "propagation_chain"])
    )
    assert "S38（数据残缺）" in text and "S18（数据残缺）" in text
    assert "propagation_chain（受转换限制）" in text
    assert "启发式" in text
    assert "| 全部 | 35 |" in text


def test_scenario_key_forms():
    assert {
        coverage.scenario_key(v) for v in ["S38", "Scenario-38", "scenario_38", "38"]
    } == {"38"}
    with pytest.raises(ValueError):
        coverage.scenario_key("abc")
