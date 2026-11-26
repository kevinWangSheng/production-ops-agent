"""Per-case snapshot view and case metadata for Cloud-OpsBench (#871 A2).

A benchmark case directory (``benchmark/<system>/<category>/<id>/``) holds the
tool snapshot (``tool_cache.json``, ``raw_data/``, ``code/`` for Boutique) next
to the answer (``metadata.json``'s ``result``). The benchmark's MCP server is
started on a *view*: a fresh directory holding copies of only the snapshot
files, so neither it nor anything it serves can reach an answer file
(``cloudopsbench_mcp/README.md`` "Benchmark data boundary"). OpsPilot's own
processes never get a dataset path at all; they see tool outputs only.

The driver reads ``metadata.json`` itself and keeps only ``query`` and
``namespace`` (:func:`read_question_fields`); the label fields are read only
by the subset selector (:mod:`scripts.cloudopsbench_eval.subset`), before any
Run, and by the benchmark's own scorer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

#: Top-level entries a view may hold: one file and two directories.
VIEW_FILES = ("tool_cache.json",)
VIEW_DIRS = ("raw_data", "code")
#: Names that carry answers or grading data anywhere in the benchmark tree.
ANSWER_NAMES = frozenset(
    {"metadata.json", "milestone.json", "process-label", "golden-trajectory"}
)
#: Benchmark system directory -> the backend's system setting.
SYSTEMS = {"boutique": "boutique", "trainticket": "train-ticket"}
_CASE_ID = re.compile(r"^(boutique|trainticket)/([a-z_-]+)/([0-9]+)$")


class ViewError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CaseRef:
    """``boutique/runtime/1``: system directory, fault category, case number."""

    system_dir: str
    category: str
    number: str

    @classmethod
    def parse(cls, case_id: str) -> CaseRef:
        match = _CASE_ID.fullmatch(case_id)
        if match is None:
            raise ViewError("CASE_ID_INVALID")
        return cls(*match.groups())

    @property
    def id(self) -> str:
        return f"{self.system_dir}/{self.category}/{self.number}"

    @property
    def system(self) -> str:
        """The backend's setting: ``boutique`` or ``train-ticket``."""
        return SYSTEMS[self.system_dir]

    def case_dir(self, dataset: Path) -> Path:
        return dataset / "benchmark" / self.system_dir / self.category / self.number

    @property
    def slug(self) -> str:
        return f"{self.system_dir}-{self.category}-{self.number}"


def _refuse_special(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ViewError("SNAPSHOT_HAS_LINK")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ViewError("SNAPSHOT_HAS_SPECIAL_FILE")
    if path.name in ANSWER_NAMES:
        raise ViewError("SNAPSHOT_HAS_ANSWER_NAME")
    return info


def _copy_tree(source: Path, target: Path) -> tuple[int, int]:
    """Regular files and directories only; names are checked before reading."""
    files = size = 0
    _refuse_special(source)
    target.mkdir()
    for root, dirs, names in os.walk(source, followlinks=False):
        here = Path(root)
        out = target / here.relative_to(source)
        for name in sorted(dirs):
            _refuse_special(here / name)
            (out / name).mkdir()
        for name in sorted(names):
            info = _refuse_special(here / name)
            if not stat.S_ISREG(info.st_mode):
                raise ViewError("SNAPSHOT_HAS_SPECIAL_FILE")
            shutil.copyfile(here / name, out / name, follow_symlinks=False)
            files += 1
            size += info.st_size
    return files, size


def build_view(case_dir: Path, view_dir: Path) -> dict[str, int]:
    """Copy the snapshot part of ``case_dir`` into the new ``view_dir``.

    ``view_dir`` must not exist. Returns ``{"files": n, "bytes": n}``; the
    result is re-checked with :func:`check_view`.
    """
    if view_dir.exists():
        raise ViewError("VIEW_EXISTS")
    cache = case_dir / VIEW_FILES[0]
    if not cache.exists():
        raise ViewError("SNAPSHOT_MISSING")
    view_dir.mkdir(parents=True)
    files = size = 0
    for name in VIEW_FILES:
        info = _refuse_special(case_dir / name)
        if not stat.S_ISREG(info.st_mode):
            raise ViewError("SNAPSHOT_HAS_SPECIAL_FILE")
        shutil.copyfile(case_dir / name, view_dir / name, follow_symlinks=False)
        files, size = files + 1, size + info.st_size
    for name in VIEW_DIRS:
        if (case_dir / name).exists() or (case_dir / name).is_symlink():
            n, b = _copy_tree(case_dir / name, view_dir / name)
            files, size = files + n, size + b
    check_view(view_dir)
    return {"files": files, "bytes": size}


def check_view(view_dir: Path) -> None:
    """A view holds the whitelisted entries only and no answer name below
    them; a link or special file anywhere refuses it."""
    allowed = set(VIEW_FILES) | set(VIEW_DIRS)
    for entry in view_dir.iterdir():
        if entry.name not in allowed:
            raise ViewError("VIEW_HAS_EXTRA_ENTRY")
    if not (view_dir / VIEW_FILES[0]).is_file():
        raise ViewError("SNAPSHOT_MISSING")
    for root, dirs, names in os.walk(view_dir, followlinks=False):
        for name in (*dirs, *names):
            _refuse_special(Path(root) / name)


def read_question_fields(case_dir: Path) -> tuple[str, str]:
    """``(query, namespace)`` from ``metadata.json``; nothing else is kept.

    The answer fields of the same file are dropped here, so no later step of
    the OpsPilot line can carry them (A2: the question is the driver's)."""
    data = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    query, namespace = data.get("query"), data.get("namespace")
    if not isinstance(query, str) or not query.strip():
        raise ViewError("QUERY_MISSING")
    if not isinstance(namespace, str) or not namespace.strip():
        raise ViewError("NAMESPACE_MISSING")
    return query.strip(), namespace.strip()
