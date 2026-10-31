"""Offline acceptance checks for F14 step 2 (#415).

This test is intentionally written against the mapping contract and repository
artifacts only.  It never imports or executes the implementation under review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAPPING = ROOT / "docs/evidence/f14-upstream-mapping/mapping.json"
UPSTREAM_FILES = ROOT / "docs/evidence/f14-upstream-mapping/upstream-files.json"
DOC = ROOT / "docs/testing/f14-upstream-mapping.md"
M0_DOC = ROOT / "docs/testing/m0-upstream-mapping.md"
README = ROOT / "docs/README.md"
FEATURES = ROOT / "feature_list.json"

CLASSES = {"reuse", "configuration", "confirmed_defect", "required_extension"}
STATES = {"implemented", "partial", "planned"}
CHOICES = {"ported", "own_impl", "upstream_config"}
REQUIRED_FIELDS = {
    "feature",
    "step",
    "mechanism",
    "class",
    "opspilot_state",
    "opspilot_choice",
    "requirement_ref",
    "upstream_evidence",
    "opspilot_evidence",
    "runtime_evidence",
    "note",
}
PATH_LINE = re.compile(r"(?P<path>[^\s:]+):L(?P<start>\d+)-L(?P<end>\d+)$")
SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _load(path: Path):
    assert path.is_file(), (
        f"required contract artifact is missing: {path.relative_to(ROOT)}"
    )
    return json.loads(path.read_text())


def _rows_and_header():
    data = _load(MAPPING)
    assert isinstance(data, dict), "mapping.json must be a JSON object"
    assert data.get("schema") == "opspilot.f14.upstream-mapping.v1"
    assert isinstance(data.get("upstream"), dict)
    for key in ("repository", "commit", "tree"):
        assert data["upstream"].get(key), f"mapping header missing upstream.{key}"
    assert SHA.fullmatch(str(data.get("upstream", {}).get("commit")))
    assert SHA.fullmatch(str(data.get("upstream", {}).get("tree")))
    assert SHA.fullmatch(str(data.get("opspilot_commit")))
    rows = data.get("rows")
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


def test_coverage_is_derived_from_feature_list_and_rows_are_known():
    _, rows = _rows_and_header()
    expected = _features()
    seen = {(row.get("feature"), row.get("step")) for row in rows}
    assert seen <= {
        (feature, step) for feature, steps in expected.items() for step in steps
    }
    assert {
        (feature, step) for feature, steps in expected.items() for step in steps
    } <= seen


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
        assert row["upstream_evidence"], (
            f"row {index} upstream_evidence must not be empty"
        )
        assert row["opspilot_evidence"], (
            f"row {index} opspilot_evidence must not be empty"
        )
        assert isinstance(row["requirement_ref"], list) and row["requirement_ref"]


def test_evidence_rules_and_planned_handling():
    _, rows = _rows_and_header()
    for index, row in enumerate(rows):
        opspilot = _text(row["opspilot_evidence"])
        runtime = _text(row["runtime_evidence"])
        for citation in row["upstream_evidence"]:
            assert PATH_LINE.fullmatch(citation), (
                f"row {index} has malformed upstream citation"
            )
        for citation in row["opspilot_evidence"]:
            path = citation.split("::", 1)[0]
            assert (ROOT / path).is_file(), f"row {index} cites missing OpsPilot file"
            if "::" in citation:
                assert citation.split("::", 1)[1]
        for citation in row["runtime_evidence"]:
            assert citation.startswith("docs/evidence/")
            assert (ROOT / citation).is_file(), (
                f"row {index} cites missing runtime evidence"
            )
        if row["class"] == "confirmed_defect":
            assert any(
                path.startswith("docs/evidence/f14-holmes-baseline/")
                for path in row["runtime_evidence"]
            )
        if row["class"] == "configuration" and "源码证据，未运行" in row["note"]:
            assert row["runtime_evidence"] == []
        if "candidate_defect" in row:
            candidate = row["candidate_defect"]
            assert (
                isinstance(candidate, dict)
                and candidate.get("step3_ref")
                and isinstance(candidate.get("claim"), str)
            ), f"candidate defect row {index} needs step3_ref"
            assert row["class"] != "confirmed_defect"
        if row["class"] == "required_extension":
            assert all(
                isinstance(value, str) and value.strip()
                for value in row["requirement_ref"]
            ), f"required extension row {index} needs requirement source"
            assert row["upstream_evidence"], (
                f"required extension row {index} needs upstream evidence"
            )
            checks = row.get("absence_checks", [])
            assert checks or (row["upstream_evidence"] and row["note"].strip())
            for check in checks:
                assert isinstance(check, dict) and isinstance(check.get("command"), str)
                assert check["command"].startswith("git -C <checkout>")
                assert check.get("hits") == 0
        if row["opspilot_state"] == "planned":
            assert row["class"] in CLASSES
            assert (
                "suggest" in (opspilot + runtime + _text(row.get("note", ""))).lower()
                or row["note"]
            )


def test_upstream_citations_are_allowlisted_and_within_recorded_line_counts():
    _, rows = _rows_and_header()
    files = _load(UPSTREAM_FILES)
    entries = files.get("files", files) if isinstance(files, dict) else files
    assert isinstance(entries, (list, dict))
    items = (
        entries.items()
        if isinstance(entries, dict)
        else ((e.get("path"), e) for e in entries)
    )
    allow = {}
    assert SHA.fullmatch(str(files.get("commit")))
    assert SHA.fullmatch(str(files.get("tree")))
    for path, entry in items:
        assert path and isinstance(entry, dict)
        assert SHA.fullmatch(str(entry.get("blob", "")))
        assert isinstance(entry.get("lines"), int) and entry["lines"] >= 0
        allow[path] = entry["lines"]
    for row in rows:
        for citation in row["upstream_evidence"]:
            match = PATH_LINE.search(_text(citation))
            if not match:
                continue
            assert match.group("path") in allow, (
                f"upstream citation not in upstream-files.json: {citation}"
            )
            end = int(match.group("end"))
            assert end <= allow[match.group("path")], (
                f"citation exceeds recorded line count: {citation}"
            )


def test_human_readable_docs_and_legacy_pointer_exist():
    assert DOC.is_file()
    assert M0_DOC.is_file()
    lines = M0_DOC.read_text().splitlines()
    assert lines and lines[0] == "# M0 上游与 OTel 固定源码映射草案"
    pointer_lines = [
        index for index, line in enumerate(lines) if "f14-upstream-mapping" in line
    ]
    assert len(pointer_lines) == 1
    pointer_index = pointer_lines[0]
    assert 0 < pointer_index < 3
    first_body = next(
        index
        for index, line in enumerate(lines[1:], start=1)
        if line.strip()
        and "f14-upstream-mapping" not in line
        and not line.lstrip().startswith("#")
    )
    assert pointer_index < first_body
    assert "f14-upstream-mapping.md" in README.read_text()
