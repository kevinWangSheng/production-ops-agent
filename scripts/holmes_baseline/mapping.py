"""F14 step 2 (#415): offline checks for the upstream mapping table.

``docs/evidence/f14-upstream-mapping/mapping.json`` cites upstream source as
``<path>:L<x>-L<y>`` at the pinned commit. ``upstream_files`` lists every cited
file with its blob SHA and line count, read from the local full checkout's
tree, so CI can check citations without the checkout (contract r2, R11).
``problems`` re-validates the table against that checkout: line ranges, the
recorded "upstream has none" searches (R4) and the OpsPilot-side paths and
symbols (R7). ``run1_tool_errors`` freezes the non-success tool results of the
step 1 Run from its raw output, after checking the raw file's sha256 against
``summary.json`` (R5: existing a0009195 run evidence, no new Run).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA = "opspilot.f14.upstream-mapping.v1"
FILES_SCHEMA = "opspilot.f14.upstream-files.v1"
COMMIT = "a0009195468ebc55a7dff7d602b61feabe3cda1b"
TREE = "e94a57b361a00cb2e793cf30d60fe2f4588e26e9"
CLASSES = frozenset(
    {"reuse", "configuration", "confirmed_defect", "required_extension"}
)
STATES = frozenset({"implemented", "partial", "planned"})
CHOICES = frozenset({"ported", "own_impl", "upstream_config"})
REF = re.compile(r"(?P<path>[^\s:]+):L(?P<start>[1-9][0-9]*)-L(?P<end>[1-9][0-9]*)")
BASELINE_EVIDENCE = "docs/evidence/f14-holmes-baseline/"
NOT_RUN = "源码证据，未运行"
ERROR_CHARS = 500


def parse_ref(ref: str) -> tuple[str, int, int] | None:
    match = REF.fullmatch(ref)
    if match is None:
        return None
    start, end = int(match["start"]), int(match["end"])
    return (match["path"], start, end) if start <= end else None


def cited_paths(mapping: Mapping[str, Any]) -> list[str]:
    paths = set()
    for row in mapping["rows"]:
        for ref in row.get("upstream_evidence") or []:
            parsed = parse_ref(ref)
            if parsed is not None:
                paths.add(parsed[0])
    return sorted(paths)


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def check_checkout(checkout: Path) -> None:
    """The checkout must be exactly the pinned, clean commit."""
    head = _git(checkout, "rev-parse", "HEAD", "HEAD^{tree}").split()
    if head != [COMMIT, TREE]:
        raise SystemExit(f"checkout is {head}, expected {[COMMIT, TREE]}")
    if _git(checkout, "status", "--porcelain").strip():
        raise SystemExit("checkout has local changes")


def upstream_files(checkout: Path, paths: Sequence[str]) -> dict[str, Any]:
    """Blob SHA (``git ls-tree -r`` of the pinned tree) and line count per path."""
    blobs = {}
    for line in _git(checkout, "ls-tree", "-r", TREE).splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, blob = meta.split()
        if kind == "blob":
            blobs[path] = blob
    files = {}
    for path in paths:
        if path not in blobs:
            raise SystemExit(f"{path} is not in tree {TREE}")
        data = subprocess.run(
            ["git", "-C", str(checkout), "cat-file", "blob", blobs[path]],
            check=True,
            capture_output=True,
        ).stdout
        files[path] = {"blob": blobs[path], "lines": len(data.splitlines())}
    return {"schema": FILES_SCHEMA, "commit": COMMIT, "tree": TREE, "files": files}


def render_check(check: Mapping[str, Any]) -> str:
    """The shell form recorded in the row (R4); ``<checkout>`` is the full
    local checkout of the pinned commit."""
    pattern = check["pattern"]
    if check["kind"] == "ls-files":
        return f"git -C <checkout> ls-files | grep -i -E '{pattern}'"
    pathspec = " ".join(check.get("pathspec") or [])
    tail = f" -- {pathspec}" if pathspec else ""
    return f"git -C <checkout> grep -n -I -i -E '{pattern}'{tail}"


def count_hits(checkout: Path, check: Mapping[str, Any]) -> int:
    pattern = re.compile(check["pattern"], re.IGNORECASE)
    if check["kind"] == "ls-files":
        files = _git(checkout, "ls-files").splitlines()
        return sum(1 for path in files if pattern.search(path))
    result = subprocess.run(
        ["git", "-C", str(checkout), "grep", "-n", "-I", "-i", "-E"]
        + [check["pattern"], "--", *(check.get("pathspec") or [])],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(f"git grep failed: {result.stderr.strip()}")
    return len(result.stdout.splitlines())


def _symbol_defined(text: str, symbol: str) -> bool:
    """Every dotted part appears as a whole word (class and method alike)."""
    return all(
        re.search(rf"(?<![\w]){re.escape(part)}(?![\w])", text)
        for part in symbol.split(".")
    )


def opspilot_problems(row_id: str, refs: Sequence[str], root: Path) -> list[str]:
    problems = []
    for ref in refs:
        path, _, symbol = ref.partition("::")
        target = root / path
        if not target.is_file():
            problems.append(f"{row_id}: OpsPilot path {path} does not exist")
        elif symbol and not _symbol_defined(target.read_text(), symbol):
            problems.append(f"{row_id}: {symbol} not found in {path}")
    return problems


def row_problems(
    row: Mapping[str, Any], manifest: Mapping[str, Any], root: Path
) -> list[str]:
    row_id = f"{row.get('feature')}#{row.get('step')} {row.get('mechanism')}"
    problems = []
    files = manifest["files"]
    if row.get("class") not in CLASSES:
        problems.append(f"{row_id}: class {row.get('class')!r}")
    if row.get("opspilot_state") not in STATES:
        problems.append(f"{row_id}: opspilot_state {row.get('opspilot_state')!r}")
    if row.get("opspilot_choice") not in CHOICES:
        problems.append(f"{row_id}: opspilot_choice {row.get('opspilot_choice')!r}")
    if not row.get("requirement_ref"):
        problems.append(f"{row_id}: no requirement_ref")
    refs = row.get("upstream_evidence") or []
    if not refs:
        problems.append(f"{row_id}: no upstream evidence")
    for ref in refs:
        parsed = parse_ref(ref)
        if parsed is None:
            problems.append(f"{row_id}: bad upstream ref {ref!r}")
            continue
        path, _, end = parsed
        if path not in files:
            problems.append(f"{row_id}: {path} missing from upstream-files.json")
        elif end > files[path]["lines"]:
            problems.append(f"{row_id}: {ref} beyond {files[path]['lines']} lines")
    runtime = row.get("runtime_evidence") or []
    for path in runtime:
        if not path.startswith("docs/evidence/") or not (root / path).is_file():
            problems.append(f"{row_id}: runtime evidence {path} missing")
    problems += opspilot_problems(row_id, row.get("opspilot_evidence") or [], root)
    kind = row.get("class")
    if kind == "confirmed_defect" and not any(
        path.startswith(BASELINE_EVIDENCE) for path in runtime
    ):
        problems.append(f"{row_id}: confirmed defect without a0009195 run evidence")
    if kind == "configuration" and not runtime and NOT_RUN not in row.get("note", ""):
        problems.append(f"{row_id}: configuration without run1 evidence or {NOT_RUN}")
    if kind == "reuse" and row.get("opspilot_state") != "planned":
        if not row.get("opspilot_evidence"):
            problems.append(f"{row_id}: reuse without the OpsPilot port site")
    candidate = row.get("candidate_defect")
    if candidate is not None and not (
        isinstance(candidate, dict) and candidate.get("step3_ref")
    ):
        problems.append(f"{row_id}: candidate_defect without step3_ref")
    if kind == "confirmed_defect" and candidate is not None:
        problems.append(f"{row_id}: confirmed defect still marked candidate")
    return problems


def problems(
    mapping: Mapping[str, Any],
    manifest: Mapping[str, Any],
    features: Sequence[Mapping[str, Any]],
    root: Path,
    checkout: Path | None = None,
) -> list[str]:
    """Everything wrong with the table; ``checkout`` also re-runs the
    recorded absence searches."""
    found = []
    if mapping.get("schema") != SCHEMA:
        found.append(f"schema {mapping.get('schema')!r}")
    upstream = mapping.get("upstream") or {}
    if (upstream.get("commit"), upstream.get("tree")) != (COMMIT, TREE):
        found.append("mapping upstream commit/tree")
    if (manifest.get("commit"), manifest.get("tree")) != (COMMIT, TREE):
        found.append("upstream-files commit/tree")
    if sorted(manifest.get("files") or {}) != cited_paths(mapping):
        found.append("upstream-files.json does not list exactly the cited files")
    covered = {(row.get("feature"), row.get("step")) for row in mapping["rows"]}
    for feature in features:
        if feature["id"] == "F14":
            continue
        for step in range(1, len(feature["steps"]) + 1):
            if (feature["id"], step) not in covered:
                found.append(f"{feature['id']} step {step} has no row")
    known = {
        (feature["id"], step)
        for feature in features
        for step in range(1, len(feature["steps"]) + 1)
        if feature["id"] != "F14"
    }
    for key in sorted(covered - known, key=str):
        found.append(f"row for unknown step {key}")
    for row in mapping["rows"]:
        found += row_problems(row, manifest, root)
        for check in row.get("absence_checks") or []:
            row_id = f"{row.get('feature')}#{row.get('step')} {row.get('mechanism')}"
            if check.get("command") != render_check(check):
                found.append(f"{row_id}: absence command differs from its spec")
            if checkout is not None and count_hits(checkout, check) != check["hits"]:
                found.append(f"{row_id}: {check['command']} hit count changed")
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run1_tool_errors(raw: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Tool, status, error text and target of each non-success tool call;
    no messages, no reasoning, no result data."""
    digest = sha256_file(raw)
    if digest != summary["raw_output_sha256"]:
        raise SystemExit(f"{raw} sha256 {digest} is not the recorded Run output")
    output = json.loads(raw.read_text())
    rows = []
    for index, call in enumerate(output.get("tool_calls") or []):
        result = call.get("result") or {}
        if result.get("status") == "success":
            continue
        params = result.get("params") or {}
        rows.append(
            {
                "index": index,
                "tool": call.get("tool_name"),
                "toolset": call.get("toolset_name"),
                "status": result.get("status"),
                "error": str(result.get("error") or "")[:ERROR_CHARS],
                "description": call.get("description"),
                "url": result.get("url"),
                "query": params.get("query") if isinstance(params, dict) else None,
            }
        )
    return {
        "run_id": summary["run_id"],
        "raw_output_sha256": digest,
        "source": "holmes --json-output-file of the Run, tool_calls[].result",
        "tool_calls": len(output.get("tool_calls") or []),
        "non_success": rows,
    }
