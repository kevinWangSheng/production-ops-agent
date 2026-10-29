"""Check that the product version has one source (#394).

pyproject.toml ``[project] version`` is the source. ``opspilot.__version__``
and the uv.lock ``opspilot`` entry must equal it, and a release tag must be
``v`` + it. Standard library only, so the release workflow (and anyone) can
run it without the project environment:

    python3 -I scripts/release/check_version.py [--tag vX.Y.Z]

Exit 0 when everything agrees, 1 with one line per problem on stderr.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

# MAJOR.MINOR.PATCH, no leading zeros, no pre-release or build suffix.
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def _package_version(init: Path) -> str | None:
    # Read, not imported: the check must not need the package's dependencies.
    for node in ast.parse(init.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(t, "id", None) for t in node.targets] == ["__version__"]
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def check(repo: Path, tag: str | None = None) -> list[str]:
    """Problems with the version of the checkout at ``repo`` (empty = ok)."""
    problems: list[str] = []
    version = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
    if not SEMVER.fullmatch(version):
        problems.append(
            f"pyproject.toml version {version!r} is not SemVer MAJOR.MINOR.PATCH"
        )

    package = _package_version(repo / "opspilot" / "__init__.py")
    if package != version:
        problems.append(
            f"opspilot/__init__.py __version__ is {package!r}, pyproject.toml has {version!r}"
        )

    lock = tomllib.loads((repo / "uv.lock").read_text())
    locked = sorted(
        str(entry.get("version"))
        for entry in lock.get("package", [])
        if entry.get("name") == "opspilot"
    )
    if locked != [version]:
        problems.append(
            f"uv.lock opspilot version is {locked!r}, pyproject.toml has {version!r} (run uv lock)"
        )

    if tag is not None and tag != f"v{version}":
        problems.append(
            f"tag {tag!r} does not match the version: expected 'v{version}'"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="release tag that must equal v<version>")
    args = parser.parse_args(argv)
    problems = check(Path(__file__).resolve().parents[2], tag=args.tag)
    for problem in problems:
        print(f"check_version: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
