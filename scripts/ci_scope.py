"""Decide whether a pull request may take the CI fast pass (#519).

Run inside the checkout of a ``pull_request`` event, where ``HEAD`` is the
merge commit GitHub built (first parent: the base branch, second parent: the PR
head). Prints ``full=true`` or ``full=false`` and appends it to
``$GITHUB_OUTPUT`` when set.

``full=false`` (fast pass) only when every path the merge commit changes is a
Markdown document no code reads:

* a root-level ``*.md`` or a ``*.md`` under ``docs/``, but not under
  ``docs/evidence/`` (tests read evidence records);
* its file name does not appear in any file under the code and data roots
  below, so a document a test, script or product module names (``README.md``,
  ``SPEC.md``, ``PRODUCT-CONSTRAINTS.md``, ``deploy/compose/UPGRADE.md``, a task
  record a test reads, ...) keeps the full run.

Everything else, including any doubt (no merge commit, an empty diff, an
unreadable file), means ``full=true``. Pushes and manual runs never call this.
"""

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

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


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def changed_paths(root: Path) -> list[str]:
    parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=root).split()
    if len(parents) != 3:
        raise ValueError(f"HEAD is not a two-parent merge commit: {parents}")
    listing = git(
        "diff", "--name-only", "--no-renames", "-z", "HEAD^1", "HEAD", cwd=root
    )
    return [path for path in listing.split("\0") if path]


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


def blocking_reasons(paths: list[str], references: str) -> list[str]:
    if not paths:
        return ["empty diff"]
    reasons = []
    for path in paths:
        pure = PurePosixPath(path)
        if pure.suffix != ".md":
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
        paths = changed_paths(root)
        reasons = blocking_reasons(paths, referenced_text(root))
    except Exception as exc:  # any doubt keeps the full run
        paths, reasons = [], [f"scope check failed: {exc!r}"]
    full = bool(reasons)
    print(f"changed paths ({len(paths)}):")
    for path in paths:
        print(f"  {path}")
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
