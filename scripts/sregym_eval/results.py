"""Whitelisted reading of one SREGym attempt's results (#667 S-8).

SREGym's per-problem CSV also holds the judge's reasoning, checklist
evidence, the submitted text and (on failure) the expected root cause; its
``fault-spec.json`` names the root cause outright. Only the columns in
:data:`COLUMNS` and ``phase.<name>.duration_s`` for :data:`PHASES` are read
into the return value; nothing else is kept, printed or written.
``fault_injected_at`` comes from the phase ledger's ``inject_fault`` end
record, never from ``fault-spec.json``.
"""

from __future__ import annotations

import ast
import csv
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PHASES: tuple[str, ...] = (
    "deploy",
    "baseline",
    "inject_fault",
    "propagation",
    "stage:diagnosis",
    "evaluate:diagnosis",
    "cleanup",
    "fix_kubernetes",
    "undeploy_leftovers",
)
COLUMNS: tuple[str, ...] = (
    "attempt",
    "Diagnosis.success",
    "Diagnosis.accuracy",
    "Diagnosis.composite_score",
    "Diagnosis.dimensions",
    "Diagnosis.unscored_reason",
    "Diagnosis.oracles",
    "timed_out",
    "deploy_failed",
    "deploy_attempts",
    "run_status",
    "incomplete_reason",
    "TTL",
)
_SHORT_CODE = re.compile(r"^[a-z_]{1,64}$")
_DIMENSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:?\d{2})?$")
OTHER = "other"


class ResultError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AttemptOutcome:
    success: bool | None = None
    accuracy: float | None = None
    composite_score: float | None = None
    dimensions: dict[str, float] = field(default_factory=dict)
    unscored_reason: str | None = None
    multi_cause: bool = False
    timed_out: bool = False
    deploy_failed: bool = False
    deploy_attempts: int | None = None
    run_status: str | None = None
    incomplete_reason: str | None = None
    ttl_s: float | None = None
    phase_seconds: dict[str, float] = field(default_factory=dict)
    fault_injected_at: str | None = None


def find_result_csv(
    results_root: Path, problem_id: str, agent: str = "opspilot"
) -> Path | None:
    """Newest ``<root>/<run dir>/<agent>/<pid>/<pid>_<agent>_results.csv``."""
    name = f"{problem_id}_{agent}_results.csv"
    found = [
        p for p in results_root.glob(f"*/{agent}/{problem_id}/{name}") if p.is_file()
    ]
    if not found:
        return None
    return max(found, key=lambda p: (p.stat().st_mtime, str(p)))


def phases_path(csv_path: Path, attempt: int = 1) -> Path:
    return csv_path.with_name(f"phases_attempt{attempt}.jsonl")


# -- cell parsing (whitelisted cells only) ---------------------------------------------------------


def _bool(cell: str | None) -> bool | None:
    if cell is None:
        return None
    value = cell.strip()
    if value in ("True", "true", "1"):
        return True
    if value in ("False", "false", "0"):
        return False
    return None


def _float(cell: str | None) -> float | None:
    if cell is None or not cell.strip():
        return None
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _int(cell: str | None) -> int | None:
    value = _float(cell)
    return int(value) if value is not None and value.is_integer() else None


def _code(cell: str | None) -> str | None:
    if cell is None or not cell.strip():
        return None
    value = cell.strip()
    return value if _SHORT_CODE.match(value) else OTHER


def _dimensions(cell: str | None) -> dict[str, float]:
    if cell is None or not cell.strip():
        return {}
    try:
        parsed = ast.literal_eval(cell)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, float] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not _DIMENSION_ID.match(key):
            continue
        score = item.get("score") if isinstance(item, dict) else None
        if isinstance(score, int | float) and not isinstance(score, bool):
            if math.isfinite(float(score)):
                out[key] = float(score)
    return out


def _fault_injected_at(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    found: str | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("phase") == "inject_fault" and record.get("event") == "end":
            ts = record.get("ts")
            if isinstance(ts, str) and _TS.match(ts):
                found = ts
    return found


def _row(csv_path: Path, attempt: int) -> dict[str, str]:
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                if _int(raw.get("attempt")) == attempt:
                    wanted = {k: raw[k] for k in COLUMNS if raw.get(k) is not None}
                    for name in PHASES:
                        key = f"phase.{name}.duration_s"
                        if raw.get(key) is not None:
                            wanted[key] = raw[key]
                    return wanted
    except (OSError, csv.Error, UnicodeDecodeError):
        raise ResultError("RESULT_MISSING") from None
    raise ResultError("RESULT_MISSING")


def read_attempt(csv_path: Path, attempt: int = 1) -> AttemptOutcome:
    row = _row(csv_path, attempt)
    phase_seconds: dict[str, float] = {}
    for name in PHASES:
        seconds = _float(row.get(f"phase.{name}.duration_s"))
        if seconds is not None:
            phase_seconds[name] = seconds
    oracles = row.get("Diagnosis.oracles")
    return AttemptOutcome(
        success=_bool(row.get("Diagnosis.success")),
        accuracy=_float(row.get("Diagnosis.accuracy")),
        composite_score=_float(row.get("Diagnosis.composite_score")),
        dimensions=_dimensions(row.get("Diagnosis.dimensions")),
        unscored_reason=_code(row.get("Diagnosis.unscored_reason")),
        multi_cause=bool(oracles and oracles.strip() and oracles.strip() != "[]"),
        timed_out=_bool(row.get("timed_out")) is True,
        deploy_failed=_bool(row.get("deploy_failed")) is True,
        deploy_attempts=_int(row.get("deploy_attempts")),
        run_status=_code(row.get("run_status")),
        incomplete_reason=_code(row.get("incomplete_reason")),
        ttl_s=_float(row.get("TTL")),
        phase_seconds=phase_seconds,
        fault_injected_at=_fault_injected_at(phases_path(csv_path, attempt)),
    )


def as_dict(outcome: AttemptOutcome) -> dict[str, Any]:
    return asdict(outcome)
