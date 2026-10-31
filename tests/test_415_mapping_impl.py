"""F14 step 2 (#415): the offline mapping checks in scripts/holmes_baseline.

The committed table is validated against the committed file list without the
upstream checkout (what CI can do); the checkout-backed regeneration only runs
where the full local checkout exists.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_baseline import mapping
from scripts.holmes_baseline import run as hrun

ROOT = Path(__file__).resolve().parents[1]
MAPPING_DIR = ROOT / "docs/evidence/f14-upstream-mapping"
BASELINE = ROOT / "docs/evidence/f14-holmes-baseline"


def _load(name: str) -> Any:
    return json.loads((MAPPING_DIR / name).read_text())


def _features() -> list[dict[str, Any]]:
    return json.loads((ROOT / "feature_list.json").read_text())


def _row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "feature": "F2",
        "step": 3,
        "mechanism": "timeout",
        "class": "reuse",
        "opspilot_state": "implemented",
        "opspilot_choice": "ported",
        "requirement_ref": ["feature_list.json F2 steps[2]"],
        "upstream_evidence": ["a.py:L1-L2"],
        "opspilot_evidence": [
            "opspilot/tools/registry.py::MAX_REQUEST_TIMEOUT_SECONDS"
        ],
        "runtime_evidence": [],
        "note": "",
    }
    row.update(changes)
    return row


MANIFEST = {
    "commit": mapping.COMMIT,
    "tree": mapping.TREE,
    "files": {"a.py": {"blob": "0" * 40, "lines": 10}},
}


def _problems(row: dict[str, Any]) -> list[str]:
    return mapping.row_problems(row, MANIFEST, ROOT)


def test_ref_format() -> None:
    assert mapping.parse_ref("holmes/config.py:L113-L113") == (
        "holmes/config.py",
        113,
        113,
    )
    for bad in (
        "holmes/config.py:113",
        "a.py:L5-L4",
        "a.py:L0-L1",
        "a.py:L1-L2 ",
        "a.py",
    ):
        assert mapping.parse_ref(bad) is None


def test_rendered_absence_commands() -> None:
    grep = {"kind": "grep", "pattern": "suspend", "pathspec": ["holmes", "server.py"]}
    assert (
        mapping.render_check(grep)
        == "git -C <checkout> grep -n -I -i -E 'suspend' -- holmes server.py"
    )
    files = {"kind": "ls-files", "pattern": "jaeger"}
    assert (
        mapping.render_check(files)
        == "git -C <checkout> ls-files | grep -i -E 'jaeger'"
    )


def test_row_rules() -> None:
    assert _problems(_row()) == []
    assert any(
        "beyond 10 lines" in p
        for p in _problems(_row(upstream_evidence=["a.py:L9-L11"]))
    )
    assert any(
        "missing from" in p for p in _problems(_row(upstream_evidence=["b.py:L1-L1"]))
    )
    assert any(
        "no upstream evidence" in p for p in _problems(_row(upstream_evidence=[]))
    )
    assert any("class" in p for p in _problems(_row(**{"class": "extension"})))
    assert any(
        "not found" in p
        for p in _problems(
            _row(opspilot_evidence=["opspilot/tools/registry.py::NoSuchName"])
        )
    )
    defect = _row(**{"class": "confirmed_defect"})
    assert any("a0009195 run evidence" in p for p in _problems(defect))
    defect["runtime_evidence"] = ["docs/evidence/f14-holmes-baseline/summary.json"]
    assert _problems(defect) == []
    config = _row(**{"class": "configuration"})
    assert any(mapping.NOT_RUN in p for p in _problems(config))
    config["note"] = mapping.NOT_RUN
    assert _problems(config) == []
    assert any(
        "step3_ref" in p for p in _problems(_row(candidate_defect={"claim": "x"}))
    )


def test_coverage_comes_from_feature_list() -> None:
    table = _load("mapping.json")
    manifest = _load("upstream-files.json")
    dropped = copy.deepcopy(table)
    dropped["rows"] = [
        r for r in dropped["rows"] if (r["feature"], r["step"]) != ("F8", 4)
    ]
    found = mapping.problems(dropped, manifest, _features(), ROOT)
    assert "F8 step 4 has no row" in found


def test_committed_table_is_consistent_offline() -> None:
    table = _load("mapping.json")
    manifest = _load("upstream-files.json")
    assert mapping.problems(table, manifest, _features(), ROOT) == []
    for row in table["rows"]:
        for check in row.get("absence_checks") or []:
            assert check["hits"] == 0


@pytest.mark.skipif(not hrun.UPSTREAM_DIR.is_dir(), reason="no local upstream checkout")
def test_file_list_regenerates_from_checkout() -> None:
    table = _load("mapping.json")
    mapping.check_checkout(hrun.UPSTREAM_DIR)
    regenerated = mapping.upstream_files(hrun.UPSTREAM_DIR, mapping.cited_paths(table))
    assert regenerated == _load("upstream-files.json")
    found = mapping.problems(table, regenerated, _features(), ROOT, hrun.UPSTREAM_DIR)
    assert found == []


def test_run1_errors_refuse_another_file(tmp_path: Path) -> None:
    raw = tmp_path / "holmes.json"
    raw.write_text(json.dumps({"tool_calls": []}))
    with pytest.raises(SystemExit):
        mapping.run1_tool_errors(raw, {"run_id": "r", "raw_output_sha256": "0" * 64})


def test_run1_errors_keep_only_non_success_metadata(tmp_path: Path) -> None:
    output = {
        "messages": [{"role": "assistant", "content": "secret reasoning"}],
        "tool_calls": [
            {"tool_name": "a", "result": {"status": "success", "data": "rows"}},
            {
                "tool_name": "execute_prometheus_range_query",
                "toolset_name": "prometheus/metrics",
                "description": "Prometheus: Query (x)",
                "result": {
                    "status": "error",
                    "error": "The prometheus query returned no result. Is the query correct?",
                    "data": "should not be copied",
                    "params": {"query": "up"},
                },
            },
        ],
    }
    raw = tmp_path / "holmes.json"
    raw.write_text(json.dumps(output))
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    frozen = mapping.run1_tool_errors(raw, {"run_id": "r", "raw_output_sha256": digest})
    assert frozen["tool_calls"] == 2
    assert [row["index"] for row in frozen["non_success"]] == [1]
    assert frozen["non_success"][0]["query"] == "up"
    text = json.dumps(frozen)
    assert "secret reasoning" not in text and "should not be copied" not in text


def test_frozen_run1_errors_match_the_recorded_run() -> None:
    summary = json.loads((BASELINE / "summary.json").read_text())
    frozen = json.loads((BASELINE / "run1-tool-errors.json").read_text())
    assert frozen["raw_output_sha256"] == summary["raw_output_sha256"]
    assert frozen["tool_calls"] == summary["outcome"]["tool_calls"]
    failed = [row for row in summary["tool_calls"] if row["status"] != "success"]
    assert [(r["tool"], r["status"]) for r in frozen["non_success"]] == [
        (r["tool"], r["status"]) for r in failed
    ]
