"""Coverage-tiered score table; tiers here are synthetic labels, not census data."""

from __future__ import annotations

import pytest

from scripts.itbench_lite import coverage

IDS = sorted(coverage.SCENARIO_IDS, key=int)


def tiers():
    # Official scenario ids with made-up tier labels.
    return {f"Scenario-{i}": ("T1" if n % 2 else "T2") for n, i in enumerate(IDS)}


def test_official_scenario_set():
    assert len(coverage.SCENARIO_IDS) == 35
    assert coverage.DATA_INCOMPLETE <= coverage.SCENARIO_IDS


def test_all_scenarios_stay_in_denominator_missing_runs_score_zero():
    table = coverage.build_table(
        coverage.load_tiers(tiers()),
        {"S1": [{"f1": 1.0}, {"f1": 1.0}, {"f1": 1.0}], "4": [{"f1": 1.0}]},
        ["f1"],
    )
    assert table["overall"]["scenarios"] == 35
    rows = {r["scenario"]: r for r in table["scenarios"]}
    assert rows["1"]["means"]["f1"] == 1.0
    assert rows["4"]["means"]["f1"] == pytest.approx(1 / 3)
    assert rows["4"]["trials_missing"] == 2
    assert rows["5"]["means"]["f1"] == 0.0
    assert table["overall"]["means"]["f1"] == pytest.approx((1 + 1 / 3) / 35)
    t1 = next(t for t in table["tiers"] if t["tier"] == "T1")
    assert t1["scenarios"] == 17


def test_data_incomplete_scenarios_flagged():
    table = coverage.build_table(coverage.load_tiers(tiers()), {}, ["f1"])
    flagged = [r["scenario"] for r in table["scenarios"] if r["data_incomplete"]]
    assert flagged == ["18", "38"]


def test_tier_file_must_list_exactly_the_official_set():
    with pytest.raises(ValueError, match="exactly the 35"):
        coverage.load_tiers({"1": "T1"})
    # 35 ids, includes S18 and S38, but one id is not an official scenario.
    wrong = {f"S{i}": "T" for i in IDS if i != "105"} | {"S3": "T"}
    with pytest.raises(ValueError, match=r"missing \['105'\], unexpected \['3'\]"):
        coverage.load_tiers(wrong)
    assert len(coverage.load_tiers({f"S{i}": "T" for i in IDS})) == 35


def test_score_for_unknown_scenario_rejected():
    ids = coverage.load_tiers({f"S{i}": "T" for i in IDS})
    with pytest.raises(ValueError, match="no tier"):
        coverage.build_table(ids, {"S99": [{}]}, ["f1"])


def test_markdown_labels():
    ids = coverage.load_tiers({f"S{i}": "T" for i in IDS})
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


@pytest.mark.parametrize(
    "metric", ["root_cause_entity_f1", "root_cause_entity_pass", "propagation_chain"]
)
def test_trial_with_a_report_but_without_the_metric_is_refused_not_zeroed(metric):
    # #641: a scored trial that had a report but lacks a selected metric is an
    # incomplete judge result; the error names the trial and the key.
    ids = coverage.load_tiers({f"S{i}": "T" for i in IDS})
    complete = {metric: 1.0, "trial": 1.0, "report_present": 1.0}
    lacking = {"root_cause_entity_precision": 1.0, "trial": 2.0, "report_present": 1.0}
    with pytest.raises(ValueError, match=f"S33 trial-2 has a report but no '{metric}'"):
        coverage.build_table(ids, {"S33": [complete, lacking]}, [metric])
    # A row without report_present (older scores files) counts as having one.
    with pytest.raises(ValueError, match="S33 trial row 1"):
        coverage.build_table(ids, {"S33": [{}]}, [metric])


def test_reportless_and_missing_trials_still_count_zero():
    ids = coverage.load_tiers({f"S{i}": "T" for i in IDS})
    reportless = {"trial": 2.0, "report_present": 0.0}
    table = coverage.build_table(
        ids,
        {"S33": [{"root_cause_entity_f1": 1.0, "trial": 1.0}, reportless]},
        ["root_cause_entity_f1"],
    )
    row = next(r for r in table["scenarios"] if r["scenario"] == "33")
    assert row["means"]["root_cause_entity_f1"] == pytest.approx(1 / 3)
    assert row["trials_missing"] == 1  # the absent third trial counts 0


def test_cli_exits_2_naming_the_trial_and_key(tmp_path, capsys):
    tiers_file = tmp_path / "tiers.json"
    tiers_file.write_text(__import__("json").dumps({f"S{i}": "T" for i in IDS}))
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(
        __import__("json").dumps({"38": [{"trial": 2.0, "report_present": 1.0}]})
    )
    code = coverage.main(
        ["--tiers", str(tiers_file), "--scores", str(scores_file),
         "--metric", "root_cause_entity_f1"]
    )  # fmt: skip
    assert code == 2
    assert "S38 trial-2 has a report but no 'root_cause_entity_f1'" in (
        capsys.readouterr().err
    )


def test_non_finite_main_metric_is_refused():
    ids = coverage.load_tiers({f"S{i}": "T" for i in IDS})
    with pytest.raises(ValueError, match="root_cause_entity_f1"):
        coverage.build_table(
            ids,
            {"S1": [{"root_cause_entity_f1": float("nan")}]},
            ["root_cause_entity_f1"],
        )
