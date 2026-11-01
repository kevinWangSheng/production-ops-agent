"""Independent offline acceptance tests for issue #430, scenario set v2 B1.

The tests use only the public v1 loader behavior, the #430 seams, and files in
this repository.  They deliberately do not import or inspect implementation
modules beyond the public evaluation package.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import opspilot.evaluation.model as evaluation_model
from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    DimensionValue,
    Manifest,
    ScenarioError,
    load_scenarios,
    manifest_unknowns,
    read_legacy_summary,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
V1_ROOT = ROOT / "evals" / "scenarios"
V2_ROOT = ROOT / "evals" / "datasets" / "dev-v2"
REGRESSION_ROOT = V2_ROOT / "regression"

V1_IDS = (
    "eval-cart-failure",
    "eval-normal-1",
    "eval-normal-2",
    "eval-payment-failure-1",
    "eval-payment-failure-2",
    "eval-product-catalog-failure",
)
V2_CATEGORIES = {
    "regression",
    "noise",
    "missing_telemetry",
    "duplicate_event",
    "model_failure",
    "prompt_injection",
    "wrong_target",
}
V1_FLAG_NAMES = {
    injected["flag"]
    for scenario_id in V1_IDS
    for injected in [
        json.loads(
            (V1_ROOT / scenario_id / "answer.json").read_text(encoding="utf-8")
        ).get("injected")
    ]
    if injected is not None
}
V1_FLAG_NAMES.discard(None)
# The v2 contract forbids metric names in model-visible stimulus.  These are
# the metric identifiers used by the existing evaluation evidence and schema.
KNOWN_METRIC_NAMES = {
    "http_errors_rate",
    "http_server_request_duration_seconds",
    "rpc_server_duration_milliseconds",
    "rpc_client_duration_milliseconds",
    "span_metrics",
}
ANSWER_SIDE_KEYS = {
    "category",
    "polarity",
    "injectors",
    "expected_outcome",
    "expected_output",
    "signature",
    "reference_report",
    "injected",
}


def _api(name: str):
    """Resolve a v2 public API at test time so this file collects pre-PR."""
    return getattr(evaluation_model, name)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_tree(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _v1_by_v2_answer(answer: dict[str, Any]) -> Path:
    """Map positive v2 answers by flag; negative answers are either v1 normal.

    The two normal v1 stimuli are byte-identical, so either normal directory is
    a valid source for the byte-for-byte comparison.
    """
    injectors = answer.get("injectors", [])
    if answer.get("polarity") == "positive":
        flags = [
            item.get("params", {}).get("flag")
            for item in injectors
            if item.get("kind") == "flagd"
        ]
        assert len(flags) == 1
        matches = [
            scenario_id
            for scenario_id in V1_IDS
            if (_json(V1_ROOT / scenario_id / "answer.json").get("injected") or {}).get(
                "flag"
            )
            == flags[0]
        ]
        assert matches
        # payment-1/payment-2 share a flag; like the two normals, the comparison
        # requires every candidate to carry the same stimulus.
        stimuli = {
            json.dumps(_json(V1_ROOT / m / "scenario.json")["stimulus"], sort_keys=True)
            for m in matches
        }
        assert len(stimuli) == 1
        return V1_ROOT / matches[0]
    normals = [scenario_id for scenario_id in V1_IDS if "normal" in scenario_id]
    return V1_ROOT / normals[0]


def _load_v2(root: Path):
    return _api("load_scenarios_v2")(root)


def _regression_source() -> Path:
    return next(
        path
        for path in sorted(REGRESSION_ROOT.iterdir())
        if not path.name.startswith("_")
    )


def test_v2_regression_loader_reads_six_sorted_scenarios_and_skips_examples():
    loaded = _load_v2(V2_ROOT)
    assert len(loaded) == 6
    assert [spec.scenario_id for spec, _answer in loaded] == sorted(
        spec.scenario_id for spec, _answer in loaded
    )
    assert all(spec.scenario_id.startswith("dv2-") for spec, _answer in loaded)
    assert all(
        len(spec.scenario_id.removeprefix("dv2-")) == 6
        and all(
            char in "0123456789abcdef" for char in spec.scenario_id.removeprefix("dv2-")
        )
        for spec, _answer in loaded
    )
    for spec, answer in loaded:
        assert spec.scenario_id == answer.scenario_id
        matching_dirs = [
            path
            for group in ("regression", "capability")
            for path in (V2_ROOT / group).iterdir()
            if path.is_dir() and path.name == spec.scenario_id
        ]
        assert len(matching_dirs) == 1

    example_root = V2_ROOT / "capability" / "_example"
    assert not any(spec.scenario_id == "_example" for spec, _answer in loaded)
    assert example_root.name.startswith("_")


def test_v2_regression_stimulus_is_byte_for_byte_the_v1_stimulus():
    for spec, answer in _load_v2(V2_ROOT):
        if not (REGRESSION_ROOT / spec.scenario_id).is_dir():
            continue
        v1_dir = _v1_by_v2_answer(
            _json(REGRESSION_ROOT / spec.scenario_id / "answer.json")
        )
        assert (
            _json(REGRESSION_ROOT / spec.scenario_id / "scenario.json")["stimulus"]
            == _json(v1_dir / "scenario.json")["stimulus"]
        )
        # Compare the stimulus object serialization-independent fields above;
        # the contract's "byte-for-byte" claim is about the stimulus payload.
        assert json.dumps(
            _json(REGRESSION_ROOT / spec.scenario_id / "scenario.json")["stimulus"],
            sort_keys=True,
            separators=(",", ":"),
        ) == json.dumps(
            _json(v1_dir / "scenario.json")["stimulus"],
            sort_keys=True,
            separators=(",", ":"),
        )


def test_v2_scenario_files_contain_no_answer_side_or_leaking_terms():
    for path in sorted(V2_ROOT.rglob("scenario.json")):
        payload = _json(path)
        assert not (set(_walk_keys(payload)) & ANSWER_SIDE_KEYS)
        text = path.read_text(encoding="utf-8").lower()
        forbidden = (
            V2_CATEGORIES | {"fault", "normal"} | V1_FLAG_NAMES | KNOWN_METRIC_NAMES
        )
        assert not [term for term in forbidden if term.lower() in text], path


@pytest.mark.parametrize("filename", ["scenario.json", "answer.json"])
@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_v2_rejects_extra_or_missing_keys(tmp_path: Path, filename: str, mutation: str):
    source = _regression_source()
    directory = _copy_tree(source, tmp_path / source.name)
    path = directory / filename
    payload = _json(path)
    if mutation == "extra":
        payload["__unexpected__"] = True
    else:
        payload.pop(next(iter(payload)))
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScenarioError):
        _api("load_scenario_v2")(directory)


@pytest.mark.parametrize("field", ["category", "polarity"])
def test_v2_rejects_unknown_answer_enums(tmp_path: Path, field: str):
    source = _regression_source()
    directory = _copy_tree(source, tmp_path / source.name)
    path = directory / "answer.json"
    payload = _json(path)
    payload[field] = "__unknown__"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScenarioError):
        _api("load_scenario_v2")(directory)


def test_v2_rejects_unknown_entry_and_injector_kind(tmp_path: Path):
    source = _regression_source()
    directory = _copy_tree(source, tmp_path / source.name)
    scenario = _json(directory / "scenario.json")
    scenario["entry"] = "__unknown__"
    (directory / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    with pytest.raises(ScenarioError):
        _api("load_scenario_v2")(directory)

    shutil.rmtree(directory)
    directory = _copy_tree(source, tmp_path / source.name)
    answer = _json(directory / "answer.json")
    answer["injectors"] = [{"kind": "__unknown__", "params": {}}]
    (directory / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    with pytest.raises(ScenarioError):
        _api("load_scenario_v2")(directory)


@pytest.mark.parametrize("filename", ["scenario.json", "answer.json"])
def test_v2_rejects_version_other_than_two(tmp_path: Path, filename: str):
    source = _regression_source()
    directory = _copy_tree(source, tmp_path / source.name)
    path = directory / filename
    payload = _json(path)
    payload["version"] = "1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScenarioError):
        _api("load_scenario_v2")(directory)


def test_v2_rejects_id_directory_mismatch(tmp_path: Path):
    source = _regression_source()
    directory = _copy_tree(source, tmp_path / source.name)
    path = directory / "scenario.json"
    payload = _json(path)
    payload["scenario_id"] = "dv2-000000"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScenarioError):
        _api("load_scenario_v2")(directory)


def test_v1_loader_and_tree_are_unchanged():
    loaded = load_scenarios(V1_ROOT)
    assert tuple(spec.scenario_id for spec, _answer in loaded) == V1_IDS
    for spec, answer in loaded:
        scenario = _json(V1_ROOT / spec.scenario_id / "scenario.json")
        expected = _json(V1_ROOT / spec.scenario_id / "answer.json")
        assert spec.version == scenario["version"]
        assert spec.kind == scenario["kind"]
        assert spec.stimulus.question == scenario["stimulus"]["question"]
        assert spec.stimulus.subject_id == scenario["stimulus"]["subject_id"]
        assert spec.stimulus.target_id == scenario["stimulus"]["target_id"]
        assert answer.version == expected["version"]
        assert answer.expected_output == tuple(expected["expected_output"])
        assert (answer.injected is None) == (expected["injected"] is None)
    head_paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "evals/scenarios"],
        text=True,
    ).splitlines()
    current_paths = sorted(
        str(path.relative_to(ROOT)) for path in V1_ROOT.rglob("*") if path.is_file()
    )
    assert current_paths == sorted(head_paths)
    for relative in head_paths:
        expected = subprocess.check_output(["git", "show", f"HEAD:{relative}"])
        assert (ROOT / relative).read_bytes() == expected


def test_dataset_tree_hash_is_deterministic_and_content_sensitive(tmp_path: Path):
    root = tmp_path / "dataset"
    (root / "b").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"a")
    (root / "b" / "z.txt").write_bytes(b"z")
    hasher = _api("dataset_tree_hash")
    assert hasher(root) == hasher(root)
    before = hasher(root)
    (root / "b" / "z.txt").write_bytes(b"Z")
    assert hasher(root) != before


def test_manifest_adds_runbook_and_dataset_and_missing_values_are_incomplete():
    assert len(REQUIRED_DIMENSIONS) == 30
    assert REQUIRED_DIMENSIONS[-2:] == ("runbook", "dataset")
    dimensions = {
        name: DimensionValue("version", "v1", None) for name in REQUIRED_DIMENSIONS
    }
    manifest = Manifest(round_id="offline-v2", dimensions=dimensions)
    validate_manifest(manifest)
    for missing in ("runbook", "dataset"):
        reduced = dict(dimensions)
        del reduced[missing]
        candidate = Manifest(round_id="offline-v2", dimensions=reduced)
        validate_manifest(candidate)
        assert manifest_unknowns(candidate) == (missing,)


def test_old_summary_without_new_dimensions_reads_them_as_unknown(tmp_path: Path):
    path = tmp_path / "old-summary.json"
    path.write_text(json.dumps({"run_id": "legacy", "trace": {}}), encoding="utf-8")
    summary = read_legacy_summary(path)
    dimensions = getattr(summary, "dimensions", {})
    assert dimensions["runbook"].kind == "unknown"
    assert dimensions["dataset"].kind == "unknown"
