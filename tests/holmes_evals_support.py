"""Shared helpers for the #666 PR-A contract tests (HolmesGPT public eval set).

Written from docs/tasks/2026-11-18-holmes-evals-666.md (PR-A public
interface) and the pre-review H-2, H-3, H-9, H-12, H-13, H-16, H-17, H-18,
without reading ``scripts/holmes_evals/``.

Answer isolation (R8-6, H-3): nothing here opens a real upstream
``test_case.yaml``. Question directories are synthetic, written under
``tmp_path`` with synthetic ``expected_output``. The real pinned checkout is
only used for ``git rev-parse``, a hash of ``classifiers.py``, the prompt
templates in ``classifiers.py`` and directory names.
"""

from __future__ import annotations

import dataclasses
import importlib
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

PINNED_COMMIT = "a0009195468ebc55a7dff7d602b61feabe3cda1b"
PINNED_CLASSIFIERS_SHA256 = (
    "66ebe528abf1d7507f1373aee055b9ba12a86de89bc79c016abc2ba8d2ced22c"
)
CLASSIFIERS = Path("tests/llm/utils/classifiers.py")
# ASSUMPTION: ``root`` is the upstream checkout root (the same root
# ``verify_upstream`` checks), and a question id is a directory name under
# upstream's own fixture folder.
FIXTURES = Path("tests/llm/fixtures/test_ask_holmes")

REPO = Path(__file__).resolve().parents[1]
_UPSTREAM_NAME = "holmesgpt-a0009195"


def mod(name: str) -> ModuleType:
    """Import lazily so collection stays green before the package exists."""
    return importlib.import_module(f"scripts.holmes_evals.{name}")


def upstream_root() -> Path | None:
    """The pinned upstream checkout, if this machine has it (CI does not)."""
    candidates = [REPO / "tmp" / "upstream" / _UPSTREAM_NAME]
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(common).parent / "tmp" / "upstream" / _UPSTREAM_NAME)
    except (OSError, subprocess.CalledProcessError):
        pass
    for c in candidates:
        if (c / CLASSIFIERS).is_file() and (c / ".git").exists():
            return c
    return None


def write_question(root: Path, qid: str, doc: Mapping[str, Any]) -> Path:
    qdir = root / FIXTURES / qid
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "test_case.yaml").write_text(
        yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True)
    )
    return qdir


def walk(obj: Any, _depth: int = 0) -> Iterator[Any]:
    """Every key and scalar reachable from ``obj`` (dataclasses, mappings,
    sequences, objects with ``__dict__``/``__slots__``)."""
    if _depth > 20:
        return
    yield obj
    if isinstance(obj, (str, bytes, int, float, bool)) or obj is None:
        return
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield f.name
            yield from walk(getattr(obj, f.name), _depth + 1)
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from walk(k, _depth + 1)
            yield from walk(v, _depth + 1)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            yield from walk(v, _depth + 1)
        return
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict):
        yield from walk(attrs, _depth + 1)
    for name in getattr(type(obj), "__slots__", ()) or ():
        if hasattr(obj, name):
            yield name
            yield from walk(getattr(obj, name), _depth + 1)


def strings_in(obj: Any) -> list[str]:
    return [x for x in walk(obj) if isinstance(x, str)]
