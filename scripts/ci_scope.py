"""Decide whether a pull request may take the CI fast pass (#519).

Run inside the checkout of a ``pull_request`` event, where ``HEAD`` is the
merge commit GitHub built (first parent: the base branch, second parent: the PR
head). Prints ``full=true`` or ``full=false`` and appends it to
``$GITHUB_OUTPUT`` when set.

``full=false`` (fast pass) only when every path the merge commit changes is a
Markdown document no code reads:

* a regular file (mode 100644) added or modified in place: deletions, mode
  changes, symlinks and submodule gitlinks keep the full run, and a rename is
  seen as a deletion plus an addition;
* a root-level ``*.md`` or a ``*.md`` under ``docs/``, but not under
  ``docs/evidence/`` (tests read evidence records);
* its file name does not appear in any file under the code and data roots
  below, so a document a test, script or product module names (``README.md``,
  ``SPEC.md``, ``PRODUCT-CONSTRAINTS.md``, ``deploy/compose/UPGRADE.md``, a task
  record a test reads, ...) keeps the full run.

Everything else, including any doubt (no merge commit, an empty diff, an
unreadable file), means ``full=true``. Pushes and manual runs never call this.
The workflow calls it only after its own inline pre-check found nothing but
regular ``*.md`` files, so a PR that also edits this script never reaches it.
"""

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import NamedTuple

# Directories and files whose contents may read a document by name.
REFERENCE_ROOTS = (
    ".github",
    "deploy",
    "evals",
    "opspilot",
    "scripts",
    "tests",
    "third_party",
    "Makefile",
    "Dockerfile",
    "pyproject.toml",
)
EXCLUDED_PREFIXES = ("docs/evidence/",)
REGULAR = "100644"
ABSENT = "000000"


class Change(NamedTuple):
    status: str
    old_mode: str
    new_mode: str
    path: str


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def changed(root: Path) -> list[Change]:
    parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=root).split()
    if len(parents) != 3:
        raise ValueError(f"HEAD is not a two-parent merge commit: {parents}")
    listing = git("diff", "--raw", "--no-renames", "-z", "HEAD^1", "HEAD", cwd=root)
    fields = listing.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise ValueError("unexpected git diff --raw output")
    changes = []
    for meta, path in zip(fields[0::2], fields[1::2]):
        old_mode, new_mode, _old, _new, status = meta.lstrip(":").split()
        changes.append(Change(status, old_mode, new_mode, path))
    return changes


def referenced_text(root: Path) -> str:
    tracked = git("ls-files", "-z", "--", *REFERENCE_ROOTS, cwd=root)
    chunks = []
    for name in tracked.split("\0"):
        if not name:
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        chunks.append(path.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def blocking_reasons(changes: list[Change], references: str) -> list[str]:
    if not changes:
        return ["empty diff"]
    reasons = []
    for status, old_mode, new_mode, path in changes:
        pure = PurePosixPath(path)
        regular = (status == "A" and old_mode == ABSENT and new_mode == REGULAR) or (
            status == "M" and old_mode == new_mode == REGULAR
        )
        if not regular:
            reasons.append(
                f"{path}: {status} {old_mode}->{new_mode}, not a regular file edit"
            )
        elif pure.suffix != ".md":
            reasons.append(f"{path}: not Markdown")
        elif len(pure.parts) > 1 and pure.parts[0] != "docs":
            reasons.append(f"{path}: outside the root and docs/")
        elif path.startswith(EXCLUDED_PREFIXES):
            reasons.append(f"{path}: evidence records are read by tests")
        elif pure.name in references:
            reasons.append(f"{path}: file name appears in code or test data")
    return reasons


def main() -> int:
    root = Path.cwd()
    try:
        changes = changed(root)
        reasons = blocking_reasons(changes, referenced_text(root))
    except Exception as exc:  # any doubt keeps the full run
        changes, reasons = [], [f"scope check failed: {exc!r}"]
    full = bool(reasons)
    print(f"changed paths ({len(changes)}):")
    for change in changes:
        print(f"  {change.status} {change.old_mode}->{change.new_mode} {change.path}")
    for reason in reasons:
        print(f"full run: {reason}")
    if not full:
        print("fast pass: every change is a Markdown document no code reads")
    line = f"full={'true' if full else 'false'}"
    print(line)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
