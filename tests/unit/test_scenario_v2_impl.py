"""Implementation tests for scenario schema v2 and the dev-v2 dataset (#430)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    DimensionValue,
    Manifest,
    ScenarioError,
    dataset_tree_hash,
    load_scenario,
    load_scenario_v2,
    load_scenarios,
    load_scenarios_v2,
    manifest_unknowns,
    validate_manifest,
)
from scripts.eval_live import dataset_dimension

ROOT = Path(__file__).resolve().parents[2]
V1 = ROOT / "evals/scenarios"
V2 = ROOT / "evals/datasets/dev-v2"
MAPPING = {
    "dv2-f725cb": "eval-cart-failure",
    "dv2-8e9455": "eval-normal-1",
    "dv2-f80b9b": "eval-normal-2",
    "dv2-2b214b": "eval-payment-failure-1",
    "dv2-455c6d": "eval-payment-failure-2",
    "dv2-c690ce": "eval-product-catalog-failure",
}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy(tmp_path: Path, sid: str = "dv2-f725cb") -> Path:
    target = tmp_path / sid
    shutil.copytree(V2 / "regression" / sid, target)
    return target


def _edit(directory: Path, name: str, change: Any) -> None:
    data = _read(directory / name)
    change(data)
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


def test_regression_group_loads_six_scenarios() -> None:
    loaded = load_scenarios_v2(V2)
    assert [spec.scenario_id for spec, _ in loaded] == sorted(MAPPING)
    for spec, answer in loaded:
        assert spec.version == answer.version == "2"
        assert spec.entry == "question"
        assert answer.category == "regression"


@pytest.mark.parametrize("sid", sorted(MAPPING))
def test_regression_copies_v1_stimulus_and_answer(sid: str) -> None:
    v1 = V1 / MAPPING[sid]
    v2 = V2 / "regression" / sid
    old_spec, old_answer = _read(v1 / "scenario.json"), _read(v1 / "answer.json")
    new_spec, new_answer = _read(v2 / "scenario.json"), _read(v2 / "answer.json")
    assert new_spec["stimulus"] == old_spec["stimulus"]
    assert new_spec["incident"] == old_spec["incident"]
    assert new_answer["expected_output"] == old_answer["expected_output"]
    assert new_answer["signature"] == old_answer["signature"]
    fault = old_spec["kind"] == "fault"
    assert new_answer["polarity"] == ("positive" if fault else "negative")
    injected = old_answer["injected"]
    expected = (
        []
        if injected is None
        else [
            {
                "kind": "flagd",
                "params": {"flag": injected["flag"], "variant": injected["variant"]},
            }
        ]
    )
    assert new_answer["injectors"] == expected
    assert new_answer["reference_report"] is None


def test_scenario_files_carry_no_answer_side_words() -> None:
    flags = {"cartfailure", "paymentfailure", "productcatalogfailure"}
    words = {
        "regression",
        "noise",
        "missing_telemetry",
        "missing telemetry",
        "duplicate_event",
        "prompt_injection",
        "wrong_target",
        "model_failure",
        "positive",
        "negative",
        "category",
        "polarity",
        "injectors",
        "expected_outcome",
        "signature",
    }
    for path in sorted(V2.rglob("scenario.json")):
        text = path.read_text(encoding="utf-8").lower()
        assert not [w for w in flags | words if w in text], path


def test_v1_loader_unchanged_and_still_reads_v1() -> None:
    assert len(load_scenarios(V1)) == 6
    with pytest.raises(ScenarioError):
        load_scenario(V2 / "regression" / "dv2-f725cb")


def test_v2_loader_refuses_v1_directory() -> None:
    with pytest.raises(ScenarioError):
        load_scenario_v2(V1 / "eval-cart-failure")


@pytest.mark.parametrize(
    ("name", "change", "code"),
    [
        ("scenario.json", lambda d: d.update(extra=1), "UNEXPECTED_FIELDS"),
        ("scenario.json", lambda d: d.pop("entry"), "MISSING_FIELDS"),
        ("scenario.json", lambda d: d.update(entry="webhook"), "BAD_ENTRY"),
        ("scenario.json", lambda d: d.update(version="1"), "BAD_VERSION"),
        ("scenario.json", lambda d: d["stimulus"].update(x="y"), "UNEXPECTED_FIELDS"),
        ("answer.json", lambda d: d.update(kind="fault"), "UNEXPECTED_FIELDS"),
        ("answer.json", lambda d: d.pop("reference_report"), "MISSING_FIELDS"),
        ("answer.json", lambda d: d.update(category="other"), "BAD_CATEGORY"),
        ("answer.json", lambda d: d.update(polarity="neutral"), "BAD_POLARITY"),
        ("answer.json", lambda d: d.update(injectors={}), "BAD_INJECTORS"),
        (
            "answer.json",
            lambda d: d["injectors"][0].update(kind="kubectl"),
            "BAD_INJECTOR_KIND",
        ),
        (
            "answer.json",
            lambda d: d["injectors"][0]["params"].update(extra="x"),
            "UNEXPECTED_FIELDS",
        ),
        (
            "answer.json",
            lambda d: d["expected_outcome"].update(final_states=[]),
            "EMPTY_FINAL_STATES",
        ),
        (
            "answer.json",
            lambda d: d["expected_outcome"]["counts"].update(runs=-1),
            "BAD_COUNT",
        ),
        (
            "answer.json",
            lambda d: d["expected_outcome"]["counts"].update(runs=True),
            "BAD_COUNT",
        ),
        (
            "answer.json",
            lambda d: d["expected_outcome"]["counts"].pop("episodes"),
            "MISSING_FIELDS",
        ),
        ("answer.json", lambda d: d.update(expected_output=[]), "EMPTY_EXPECTED"),
        (
            "answer.json",
            lambda d: d.update(reference_report="../x.json"),
            "BAD_REFERENCE_REPORT",
        ),
        ("answer.json", lambda d: d.update(scenario_id="dv2-000000"), "MISMATCH"),
    ],
)
def test_v2_loader_rejects(tmp_path: Path, name: str, change: Any, code: str) -> None:
    directory = _copy(tmp_path)
    _edit(directory, name, change)
    with pytest.raises(ScenarioError, match=code):
        load_scenario_v2(directory)


def test_v2_loader_accepts_other_injector_kinds_and_references(
    tmp_path: Path,
) -> None:
    directory = _copy(tmp_path)

    def change(d: dict[str, Any]) -> None:
        d["injectors"].append({"kind": "model_transport", "params": {"mode": "x"}})
        d["reference_report"] = "reference/report.json"

    _edit(directory, "answer.json", change)
    _spec, answer = load_scenario_v2(directory)
    assert [i.kind for i in answer.injectors] == ["flagd", "model_transport"]
    assert answer.reference_report == "reference/report.json"


def test_v2_loader_refuses_id_not_matching_directory(tmp_path: Path) -> None:
    directory = tmp_path / "dv2-abcdef"
    shutil.copytree(V2 / "regression" / "dv2-f725cb", directory)
    with pytest.raises(ScenarioError, match="SCENARIO_ID_NOT_DIRECTORY"):
        load_scenario_v2(directory)


def test_v2_loader_refuses_bad_id(tmp_path: Path) -> None:
    directory = _copy(tmp_path)
    for name in ("scenario.json", "answer.json"):
        _edit(directory, name, lambda d: d.update(scenario_id="dv2-ABCDEF"))
    with pytest.raises(ScenarioError, match="BAD_SCENARIO_ID"):
        load_scenario_v2(directory)


def test_v2_loader_refuses_flag_name_in_stimulus(tmp_path: Path) -> None:
    directory = _copy(tmp_path)
    _edit(
        directory,
        "scenario.json",
        lambda d: d["stimulus"].update(question="Is cartFailure on?"),
    )
    with pytest.raises(ScenarioError, match="STIMULUS_LEAK:cartfailure"):
        load_scenario_v2(directory)


def test_load_scenarios_v2_skips_underscore_and_checks_groups(tmp_path: Path) -> None:
    root = tmp_path / "dev-v2"
    shutil.copytree(V2, root)
    assert len(load_scenarios_v2(root)) == 6
    # A regression answer under capability/ is misplaced.
    shutil.copytree(
        root / "regression" / "dv2-f725cb", root / "capability" / "dv2-f725cb"
    )
    with pytest.raises(ScenarioError, match="CATEGORY_GROUP_MISMATCH"):
        load_scenarios_v2(root)


def test_schema_example_is_valid_when_named_by_its_id(tmp_path: Path) -> None:
    example = V2 / "capability" / "_example"
    sid = _read(example / "scenario.json")["scenario_id"]
    shutil.copytree(example, tmp_path / sid)
    _spec, answer = load_scenario_v2(tmp_path / sid)
    assert answer.category != "regression"


def test_dataset_tree_hash_algorithm(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.json").write_bytes(b"2")
    (tmp_path / "a.txt").write_bytes(b"1")
    expected = hashlib.sha256(b"a.txt\x001\x00b/y.json\x002\x00").hexdigest()
    assert dataset_tree_hash(tmp_path) == expected
    (tmp_path / "a.txt").write_bytes(b"3")
    assert dataset_tree_hash(tmp_path) != expected


def test_manifest_has_thirty_dimensions_and_old_ones_read_unknown() -> None:
    assert len(REQUIRED_DIMENSIONS) == 30
    assert REQUIRED_DIMENSIONS[-2:] == ("runbook", "dataset")
    old = Manifest(
        "r",
        {
            name: DimensionValue("hash", "h", None)
            for name in REQUIRED_DIMENSIONS
            if name not in ("runbook", "dataset")
        },
    )
    validate_manifest(old)
    assert manifest_unknowns(old) == ("runbook", "dataset")


def test_dataset_dimension_for_v2_and_v1_roots() -> None:
    v2 = dataset_dimension(V2)
    assert v2 == DimensionValue("hash", f"dev-v2:{dataset_tree_hash(V2)}", None)
    v1 = dataset_dimension(V1)
    assert v1.kind == "none" and v1.reason
