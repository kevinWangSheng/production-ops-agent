"""Offline acceptance checks for F14 step 2 (#415).

This test is intentionally written against the mapping contract and repository
artifacts only.  It never imports or executes the implementation under review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MAPPING = ROOT / "docs/evidence/f14-upstream-mapping/mapping.json"
UPSTREAM_FILES = ROOT / "docs/evidence/f14-upstream-mapping/upstream-files.json"
DOC = ROOT / "docs/testing/f14-upstream-mapping.md"
M0_DOC = ROOT / "docs/testing/m0-upstream-mapping.md"
README = ROOT / "docs/README.md"
FEATURES = ROOT / "feature_list.json"
EVIDENCE_ROOT = ROOT / "docs/evidence"

CLASSES = {"reuse", "configuration", "confirmed_defect", "required_extension"}
STATES = {"implemented", "partial", "planned"}
CHOICES = {"ported", "own_impl", "upstream_config"}
REQUIRED_FIELDS = {
    "feature", "step", "mechanism", "class", "opspilot_state",
    "opspilot_choice", "requirement_ref", "upstream_evidence",
    "opspilot_evidence", "runtime_evidence", "note",
}
PATH_LINE = re.compile(r"(?P<path>[^\s:]+):L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")
SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _load(path: Path):
    assert path.is_file(), f"required contract artifact is missing: {path.relative_to(ROOT)}"
    return json.loads(path.read_text())


def _rows_and_header():
    data = _load(MAPPING)
    assert isinstance(data, dict), "mapping.json must be a JSON object"
    for key in ("schema", "upstream_commit", "upstream_tree", "opspilot_commit"):
        assert data.get(key), f"mapping header missing {key}"
    rows = data.get("rows", data.get("mapping"))
    assert isinstance(rows, list), "mapping.json must expose rows as a JSON array"
    return data, rows


def _features():
    features = json.loads(FEATURES.read_text())
    return {
        f["id"]: set(range(1, len(f.get("steps", [])) + 1))
        for f in features
        if f.get("id") != "F14" and not f.get("retired", False)
    }


def _text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _evidence_files(values):
    for value in values if isinstance(values, list) else []:
        text = _text(value)
        for token in re.findall(r"(?:docs/evidence|evidence)/[^\s,\]})]+", text):
            token = token.rstrip(".;`")
            path = ROOT / token if token.startswith("docs/") else ROOT / "docs" / token
            yield path


def test_coverage_is_derived_from_feature_list_and_rows_are_known():
    _, rows = _rows_and_header()
    expected = _features()
    seen = {(row.get("feature"), row.get("step")) for row in rows}
    assert seen <= {(feature, step) for feature, steps in expected.items() for step in steps}
    assert {(feature, step) for feature, steps in expected.items() for step in steps} <= seen


def test_rows_have_contract_fields_and_single_valid_class():
    _, rows = _rows_and_header()
    assert rows, "mapping must contain at least one row"
    for index, row in enumerate(rows):
        assert isinstance(row, dict), f"row {index} is not an object"
        assert REQUIRED_FIELDS <= row.keys(), f"row {index} is missing contract fields"
        assert row["class"] in CLASSES, f"row {index} has invalid class"
        assert row["opspilot_state"] in STATES
        assert row["opspilot_choice"] in CHOICES
        assert isinstance(row["step"], int) and row["step"] >= 1
        for field in ("upstream_evidence", "opspilot_evidence", "runtime_evidence"):
            assert isinstance(row[field], list), f"row {index} {field} must be an array"


def test_evidence_rules_and_planned_handling():
    _, rows = _rows_and_header()
    for index, row in enumerate(rows):
        upstream = _text(row["upstream_evidence"])
        opspilot = _text(row["opspilot_evidence"])
        runtime = _text(row["runtime_evidence"])
        requirement = _text(row["requirement_ref"])
        if row["class"] == "confirmed_defect":
            assert row["runtime_evidence"], f"confirmed defect row {index} needs run evidence"
            assert any(path.is_file() for path in _evidence_files(row["runtime_evidence"])), (
                f"confirmed defect row {index} must cite an existing docs/evidence file"
            )
        if "candidate_defect" in row:
            candidate = row["candidate_defect"]
            assert isinstance(candidate, dict) and candidate.get("step3_ref"), (
                f"candidate defect row {index} needs step3_ref"
            )
            assert row["class"] != "confirmed_defect"
        if row["class"] == "required_extension":
            assert requirement.strip(), f"required extension row {index} needs requirement source"
            assert row["upstream_evidence"], f"required extension row {index} needs upstream evidence"
            assert any(
                token in upstream.lower() for token in ("command", "grep", "ls-files", "0 hit", "0 hit(s)", "absent", "不够", "没有")
            ), f"required extension row {index} needs absence/insufficiency check record"
        if any(token in upstream.lower() for token in ("upstream absent", "上游没有", "不存在")):
            assert re.search(r"(?:command|cmd|命令)", upstream, re.I)
            assert re.search(r"(?:hit|match|命中)\D*0", upstream, re.I)
        if row["opspilot_state"] == "planned":
            assert row["class"] in CLASSES
            assert "suggest" in (opspilot + runtime + _text(row.get("note", ""))).lower() or row["note"]


def test_upstream_citations_are_allowlisted_and_within_recorded_line_counts():
    _, rows = _rows_and_header()
    files = _load(UPSTREAM_FILES)
    entries = files.get("files", files) if isinstance(files, dict) else files
    assert isinstance(entries, (list, dict))
    items = entries.items() if isinstance(entries, dict) else ((e.get("path"), e) for e in entries)
    allow = {}
    for path, entry in items:
        assert path and isinstance(entry, dict)
        assert SHA.fullmatch(str(entry.get("blob_sha", "")))
        assert isinstance(entry.get("line_count"), int) and entry["line_count"] >= 0
        allow[path] = entry["line_count"]
    for row in rows:
        for citation in row["upstream_evidence"]:
            match = PATH_LINE.search(_text(citation))
            if not match:
                continue
            assert match.group("path") in allow, f"upstream citation not in upstream-files.json: {citation}"
            end = int(match.group("end") or match.group("start"))
            assert end <= allow[match.group("path")], f"citation exceeds recorded line count: {citation}"


def test_human_readable_docs_and_legacy_pointer_exist():
    assert DOC.is_file()
    assert M0_DOC.is_file()
    assert "f14-upstream-mapping" in M0_DOC.read_text().splitlines()[0]
    assert "f14-upstream-mapping.md" in README.read_text()

