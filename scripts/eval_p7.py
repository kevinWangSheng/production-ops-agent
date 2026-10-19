"""P7 calibration driver (chore): private workspace, blind labels, debug, formal.

Interface: docs/tasks/2026-10-19-eval-p7-driver-interface.md (r2). Contract:
docs/tasks/2026-10-18-eval-p7-calibration.md (P7-D1..D14).

    .venv/bin/python -m scripts.eval_p7 <command> --workspace W [options]

Commands: init, import, rewrite, assemble, export-blind, import-annotation,
brief, adjudicate, debug, freeze, formal, balance, cost, summary.

Engineer script, not product code. It only calls the B3 judge/calibration
functions (``judge_once``, ``freeze_label_pack``, ``run_calibration``,
``load_label_pack``, ``load_verified_calibration``) and never changes their
semantics. Every artifact is written under ``<repo>/tmp/eval-p7`` after a
per-path Git ignore check; stdout carries only IDs, hashes, counts and codes.
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar

from opspilot.evaluation import (
    Manifest,
    check_recording_binding,
    evidence_from_recording,
    judge,
    load_recording,
    manifest_hash,
)
from opspilot.evaluation.calibration_flow import (
    COVERAGE,
    SCENARIOS,
    Adjudication,
    AnnotationSet,
    BlindLabel,
    CalibrationItem,
    LabelPack,
    SelectionRecord,
    check_blind_label,
    check_identifiers,
    final_labels,
    freeze_label_pack,
    is_candidate_model_identity,
    load_label_pack,
    load_verified_calibration,
    run_calibration,
)
from opspilot.evaluation.codec import (
    StorageError,
    canonical_bytes,
    decode,
    exact,
    parse,
)
from opspilot.evaluation.gate import parsed_report, run_gate
from opspilot.evaluation.judge import (
    HTTPJudgeTransport,
    JudgeConfig,
    JudgeError,
    JudgeInput,
    JudgeObservation,
    JudgeTransport,
    judge_config_hash,
    judge_once,
    load_judge_config,
    safe_input,
)
from opspilot.evaluation.model import JudgedDiagnosis, ReportCapture
from opspilot.evaluation.storage import load_summary_capture
from scripts import eval_live, eval_live_lab

ID_PATTERN = re.compile(eval_live.ROUND_PATTERN)
ROUND_PREFIX = "eval-p7-"
_ATTEMPT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ITEM = re.compile(r"p7-[0-9a-f]{24}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_LOCATION = re.compile(r"(claims|gaps|next_steps)\[(\d+)\]")
_FAMILY = re.compile(r"live/([^/]+)/([^/]+)")
PROVIDERS = ("zai", "openai", "deepseek")
SLOT_MODELS = {1: "claude-opus-5-5", 2: "gpt-6-astra"}
REWRITE_LABEL = "controlled-rewrite-of-real-run-evidence"
_MAX_FILE = 64 * 1024 * 1024
_PACK_FILES = (
    "items.json",
    "annotations-1.json",
    "annotations-2.json",
    "adjudications.json",
    "pack.json",
)
_WRITE_CODES = frozenset({"ARTIFACT_UNWRITABLE", "OUTPUT_EXISTS"})
_SCHEMAS = {
    "workspace": "opspilot-p7-workspace-1",
    "rewrite": "opspilot-p7-rewrite-1",
    "plan": "opspilot-p7-split-plan-1",
    "packet": "opspilot-p7-blind-packet-1",
    "response": "opspilot-p7-blind-response-1",
    "brief": "opspilot-p7-adjudication-brief-1",
    "decisions": "opspilot-p7-decisions-1",
}
# Field descriptions only (P7-D7): no coverage, split, source or intent.
OUTPUT_SCHEMA: Mapping[str, Any] = {
    "response": {
        "schema": _SCHEMAS["response"],
        "packet_sha256": "SHA-256 of the exact packet.json bytes received",
        "labels": "exactly one label per packet item",
    },
    "label": {
        "item_id": "packet item_id",
        "input_hash": "packet input_hash",
        "diagnoses": "every factual causal assertion (judge prompt rules)",
        "verdict": "boolean overall verdict (judge prompt rules)",
    },
    "diagnosis": {
        "location": "summary or claims[N]/gaps[N]/next_steps[N]",
        "quote": "exact quote from report_raw",
        "factual_causal": "boolean",
        "service": "string or null",
        "operation": "string or null",
        "cited_evidence_ids": "list of evidence IDs",
        "linked_claim": "claim index or null",
        "supported": "boolean",
        "reason": "short reason",
    },
}

T = TypeVar("T")
ItemKind = Literal["original", "controlled_rewrite"]
BalanceReader = Callable[[str], Decimal | None]


# -- errors ---------------------------------------------------------------------


class P7Error(ValueError):
    """Only a fixed code crosses the driver boundary (lower codes verbatim)."""

    def __init__(
        self,
        code: str,
        *,
        stopped: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        # None: derived from whether this call already wrote or sent (0.1).
        self.stopped = stopped
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(code)


_PROGRESS = {"started": False}


def _fail(code: str, **details: Any) -> NoReturn:
    raise P7Error(code, details=details)


def _public(fn: Callable[..., T]) -> Callable[..., T]:
    """Library boundary: P7Error only, fixed code, no exception chain."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        _PROGRESS["started"] = False
        code: str | None = None
        stopped: bool | None = None
        details: Mapping[str, Any] | None = None
        try:
            return fn(*args, **kwargs)
        except P7Error as exc:
            code, stopped, details = exc.code, exc.stopped, exc.details
        except (JudgeError, StorageError) as exc:
            code = exc.code if type(exc.code) is str else "SCHEMA_INVALID"
        except OSError:
            code = (
                "ARTIFACT_UNWRITABLE"
                if _PROGRESS["started"]
                else "EVIDENCE_UNAVAILABLE"
            )
        if stopped is None:
            stopped = _PROGRESS["started"]
        raise P7Error(code, stopped=stopped, details=details)

    return wrapper


# -- DTOs (section 2) ------------------------------------------------------------


@dataclass(frozen=True)
class SourceProvenance:
    round_id: str
    attempt_id: str
    product_run_id: str
    summary_sha256: str
    recording_sha256: str
    manifest_hash: str
    source_report_sha256: str


@dataclass(frozen=True)
class RewriteRecord:
    label: str
    source_item_id: str
    source_report_sha256: str
    derived_report_sha256: str
    change_description: str
    purpose: str
    author_identity: str
    author_model_identity: str


@dataclass(frozen=True)
class ItemRecord:
    item_id: str
    kind: ItemKind
    scenario_id: str
    source_family: str
    input_hash: str
    report_semantic_sha256: str
    request: JudgeInput
    provenance: SourceProvenance
    coverage: tuple[str, ...]
    coverage_note: str | None
    rewrite: RewriteRecord | None


@dataclass(frozen=True)
class SplitPlan:
    families: Mapping[str, Literal["debug", "validation"]]
    items: tuple[str, ...]


@dataclass(frozen=True)
class BlindPacketItem:
    item_id: str
    input_hash: str
    p6_input: Mapping[str, Any]


@dataclass(frozen=True)
class BlindPacket:
    schema: str
    items_sha256: str
    judge_prompt: str
    judge_prompt_sha256: str
    convention: str
    convention_sha256: str
    output_schema: Mapping[str, Any]
    items: tuple[BlindPacketItem, ...]


@dataclass(frozen=True)
class BlindResponse:
    schema: str
    packet_sha256: str
    labels: tuple[BlindLabel, ...]


@dataclass(frozen=True)
class AnnotationProvenance:
    slot: Literal[1, 2]
    annotator_identity: str
    model_identity: str
    packet_sha256: str
    response_sha256: str
    annotation_sha256: str
    evidence_note: str | None


@dataclass(frozen=True)
class BriefEntry:
    item_id: str
    input_hash: str
    p6_input: Mapping[str, Any]
    label_a: BlindLabel
    label_b: BlindLabel
    verdict_differs: bool
    causal_keys_a: tuple[tuple[str, str], ...]
    causal_keys_b: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AdjudicationBrief:
    schema: str
    items_sha256: str
    packet_sha256: str
    annotation_hashes: tuple[str, str]
    agreed: int
    differences: tuple[BriefEntry, ...]


@dataclass(frozen=True)
class RevisedLabel:
    diagnoses: tuple[JudgedDiagnosis, ...]
    verdict: bool


@dataclass(frozen=True)
class Decision:
    item_id: str
    choice: Literal["A", "B", "revised"]
    label: RevisedLabel | None
    decision_record: str


@dataclass(frozen=True)
class Decisions:
    schema: str
    brief_sha256: str
    decisions: tuple[Decision, ...]


@dataclass(frozen=True)
class DebugRecord:
    session_id: str
    item_id: str
    input_hash: str
    provider: str
    config_hash: str
    observation: JudgeObservation


@dataclass(frozen=True)
class BalanceRecord:
    session_id: str
    provider: str
    phase: Literal["before", "after"]
    amount: str | None
    currency: str | None
    source: Literal["manual", "deepseek_balance_api", "unknown"]
    shared_account_possible: bool


@dataclass(frozen=True)
class FormalResult:
    run_id: str
    pack_hash: str
    config_hashes: Mapping[str, str]
    selection: SelectionRecord
    readback: Literal["verified", "unresolved_as_expected", "failed"]
    readback_code: str | None


@dataclass(frozen=True)
class SourceRoots:
    evidence_root: Path
    ledger_root: Path
    recording_root: Path


@dataclass(frozen=True)
class Workspace:
    """``repo`` is the main worktree root; ``ignore_check`` is 1.2 step 2."""

    repo: Path
    workspace_id: str
    ignore_check: Callable[[Path], bool | None]

    @property
    def private_root(self) -> Path:
        return self.repo / "tmp" / "eval-p7"

    @property
    def root(self) -> Path:
        return self.private_root / self.workspace_id


def repo_root(cwd: Path | None = None) -> Path:
    """Main worktree root: the Git common directory's parent (1.1)."""
    result = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if result is None or result.returncode or not result.stdout.strip():
        _fail("PRIVATE_PATH_UNVERIFIED")
    return Path(result.stdout.decode().strip()).resolve().parent


def git_ignore_check(repo: Path) -> Callable[[Path], bool | None]:
    """``git check-ignore -q`` without ``--no-index``: tracked = not ignored."""

    def check(path: Path) -> bool | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", "--", str(path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return {0: True, 1: False}.get(result.returncode)

    return check


def open_workspace(workspace_id: str, *, cwd: Path | None = None) -> Workspace:
    repo = repo_root(cwd)
    return Workspace(repo, workspace_id, git_ignore_check(repo))


# -- paths, reads and writes (section 1.2) -----------------------------------------


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _fail("PRIVATE_PATH_UNVERIFIED")
    return True


def _inside_repo(ws: Workspace, path: Path) -> None:
    """1.2 step 1 (first half): inside ``<repo>``, no symlink component."""
    try:
        relative = path.relative_to(ws.repo)
    except ValueError:
        relative = None
    if (
        relative is None
        or not path.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        _fail("PRIVATE_PATH_OUTSIDE_REPO")
    current = ws.repo
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError:
            mode = None
        if mode is None:
            _fail("PRIVATE_PATH_UNVERIFIED")
        if stat.S_ISLNK(mode):
            _fail("PRIVATE_PATH_OUTSIDE_REPO")


def _check_private(ws: Workspace, path: Path) -> None:
    """1.2 step 1: workspace artifacts under ``<private_root>/<W>/`` only."""
    _inside_repo(ws, path)
    allowed = {
        ws.private_root / "exposure.lock",
        ws.private_root / "exposure.jsonl",
        ws.private_root,
        *ws.private_root.parents,
    }
    if path not in allowed and path != ws.root and ws.root not in path.parents:
        _fail("PRIVATE_PATH_OUTSIDE_WORKSPACE")


def _ignored(ws: Workspace, path: Path) -> None:
    try:
        decision = ws.ignore_check(path)
    except Exception:
        decision = None
    if decision is True:
        return
    _fail(
        "PRIVATE_PATH_NOT_IGNORED" if decision is False else "PRIVATE_PATH_UNVERIFIED"
    )


def _preflight(
    ws: Workspace, *, files: Sequence[Path] = (), dirs: Sequence[Path] = ()
) -> None:
    """Every target checked before the first write; nothing is left behind.

    Git applies directory-only rules (``!dir/``) only to existing directories,
    so a missing directory is created, checked and removed again.
    """
    for path in (*dirs, *files):
        _check_private(ws, path)
    for path in (*dirs, *files):
        if _exists(path):
            _fail("OUTPUT_EXISTS")
    needed: list[Path] = []
    for path in (*dirs, *files):
        chain = [path] if path in dirs else []
        for parent in path.parents:
            if parent == ws.repo or ws.repo not in parent.parents:
                break
            chain.append(parent)
        for directory in chain:
            if directory not in needed and not _exists(directory):
                _check_private(ws, directory)
                needed.append(directory)
    needed.sort(key=lambda p: len(p.parts))
    created: list[Path] = []
    try:
        for directory in needed:
            failed = False
            try:
                os.mkdir(directory, 0o700)
            except OSError:
                failed = True
            if failed:
                _fail("ARTIFACT_UNWRITABLE")
            created.append(directory)
            _ignored(ws, directory)
        for path in files:
            _ignored(ws, path)
    finally:
        for directory in reversed(created):
            try:
                os.rmdir(directory)
            except OSError:
                pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dirs(ws: Workspace, path: Path) -> None:
    missing = []
    for directory in (path, *path.parents):
        if directory == ws.repo or _exists(directory):
            break
        missing.append(directory)
    for directory in reversed(missing):
        _check_private(ws, directory)
        _PROGRESS["started"] = True
        code = None
        try:
            os.mkdir(directory, 0o700)
            _fsync_dir(directory.parent)
        except FileExistsError:
            code = "OUTPUT_EXISTS"
        except OSError:
            code = "ARTIFACT_UNWRITABLE"
        if code is not None:
            _fail(code)


def _write_new(ws: Workspace, path: Path, raw: bytes) -> None:
    _check_private(ws, path)
    if len(raw) > _MAX_FILE:
        _fail("ARTIFACT_UNWRITABLE")
    _ensure_dirs(ws, path.parent)
    _PROGRESS["started"] = True
    code = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_dir(path.parent)
    except FileExistsError:
        code = "OUTPUT_EXISTS"
    except OSError:
        code = "ARTIFACT_UNWRITABLE"
    if code is not None:
        _fail(code)


def _read_file(path: Path, *, missing: str = "EVIDENCE_UNAVAILABLE") -> bytes:
    raw: bytes | None = None
    oversize = False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size <= _MAX_FILE:
                raw = stream.read(_MAX_FILE + 1)
                oversize = len(raw) > _MAX_FILE
            else:
                oversize = True
    except OSError:
        pass
    if oversize:
        _fail("SCHEMA_INVALID")
    if raw is None:
        _fail(missing)
    return raw


def _read_ws(
    ws: Workspace, path: Path, *, missing: str = "EVIDENCE_UNAVAILABLE"
) -> bytes:
    _check_private(ws, path)
    return _read_file(path, missing=missing)


def _decode(annotation: Any, raw: bytes) -> Any:
    return decode(annotation, parse(raw))


def _loads(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        pass
    _fail("SCHEMA_INVALID")


# -- workspace state ---------------------------------------------------------------


def _check_id(value: Any) -> None:
    if type(value) is not str or ID_PATTERN.fullmatch(value) is None:
        _fail("SCHEMA_INVALID")


def _check_workspace(ws: Workspace) -> None:
    if type(ws) is not Workspace or ID_PATTERN.fullmatch(ws.workspace_id) is None:
        _fail("WORKSPACE_INVALID")
    try:
        directory = stat.S_ISDIR(ws.root.lstat().st_mode)
    except OSError:
        directory = False
    if not directory:
        _fail("WORKSPACE_INVALID")
    raw = _read_ws(ws, ws.root / "workspace.json", missing="WORKSPACE_INVALID")
    value = _loads(raw)
    if value != {"schema": _SCHEMAS["workspace"], "workspace": ws.workspace_id}:
        _fail("WORKSPACE_INVALID")


def _records(ws: Workspace) -> dict[str, ItemRecord]:
    directory = ws.root / "items"
    if not _exists(directory):
        return {}
    _check_private(ws, directory)
    records: dict[str, ItemRecord] = {}
    for path in sorted(directory.iterdir()):
        record = _decode(ItemRecord, _read_ws(ws, path))
        if path.name != f"{record.item_id}.json":
            _fail("SCHEMA_INVALID")
        records[record.item_id] = record
    return records


def _assembled(ws: Workspace) -> tuple[bytes, tuple[CalibrationItem, ...]]:
    raw = _read_ws(ws, ws.root / "assembled/items.json")
    items = decode(tuple[CalibrationItem, ...], exact(parse(raw), {"items"})["items"])
    return raw, items


def _annotation(ws: Workspace, slot: int) -> tuple[bytes, AnnotationSet] | None:
    path = ws.root / f"annotations/annotations-{slot}.json"
    if not _exists(path):
        return None
    raw = _read_ws(ws, path)
    annotation = _decode(AnnotationSet, raw)
    if canonical_bytes(annotation) != raw:
        _fail("SCHEMA_INVALID")
    return raw, annotation


def _adjudications(ws: Workspace) -> tuple[bytes, tuple[Adjudication, ...]]:
    raw = _read_ws(ws, ws.root / "adjudication/adjudications.json")
    return raw, decode(
        tuple[Adjudication, ...], exact(parse(raw), {"adjudications"})["adjudications"]
    )


def _label_pack(ws: Workspace) -> tuple[LabelPack, Mapping[str, BlindLabel], bytes]:
    _, items = _assembled(ws)
    annotations = []
    for slot in (1, 2):
        loaded = _annotation(ws, slot)
        if loaded is None:
            _fail("CALIBRATION_UNRESOLVED")
        annotations.append(loaded[1])
    adjudication_raw, adjudications = _adjudications(ws)
    pack = LabelPack(items, tuple(annotations), adjudications)
    return pack, final_labels(pack), adjudication_raw


# -- item checks (sections 3, 4, 9.3) --------------------------------------------------


def _input_hash(request: JudgeInput) -> str:
    return _sha(canonical_bytes(safe_input(request)))


def _semantic_hash(report: ReportCapture) -> str:
    parsed = parsed_report(report)
    if parsed is None:
        _fail("SCHEMA_INVALID")
    return _sha(canonical_bytes(parsed.model_dump(mode="json")))


def _gate(request: JudgeInput) -> None:
    if not run_gate(
        request.spec,
        request.answer,
        request.outcome,
        request.report,
        request.delivered,
        request.authorized,
    ).passed:
        _fail("GATE_NOT_PASSED")


def _same_non_report(source: JudgeInput, derived: JudgeInput) -> bool:
    return all(
        canonical_bytes(getattr(source, name))
        == canonical_bytes(getattr(derived, name))
        for name in ("spec", "answer", "delivered", "authorized", "outcome")
    ) and (source.report.finish_reason == derived.report.finish_reason)


def _citations_delivered(request: JudgeInput) -> bool:
    report = parsed_report(request.report)
    if report is None:
        return False
    for claim in report.claims:
        for evidence_id in claim.evidence_ids:
            record = request.delivered.get(evidence_id)
            if (
                record is None
                or record.delivered is not True
                or evidence_id not in request.outcome.evidence_ids
            ):
                return False
    return True


def _check_coverage(coverage: Any) -> tuple[str, ...]:
    if (
        not isinstance(coverage, (list, tuple))
        or any(type(name) is not str or name not in COVERAGE for name in coverage)
        or len(set(coverage)) != len(coverage)
    ):
        _fail("SCHEMA_INVALID")
    return tuple(coverage)


def _check_record(record: ItemRecord, records: Mapping[str, ItemRecord]) -> None:
    """9.3 I1-I6 for one record; global uniqueness is ``_check_unique``."""
    if (
        record.input_hash != _input_hash(record.request)
        or record.item_id != "p7-" + record.input_hash[:24]
    ):
        _fail("HASH_MISMATCH")
    if record.report_semantic_sha256 != _semantic_hash(record.request.report):
        _fail("HASH_MISMATCH")
    provenance = record.provenance
    if record.kind == "original":
        if record.rewrite is not None or record.source_family != (
            f"live/{provenance.round_id}/{provenance.attempt_id}"
        ):
            _fail("SCHEMA_INVALID")
    else:
        rewrite = record.rewrite
        if rewrite is None or rewrite.label != REWRITE_LABEL:
            _fail("SCHEMA_INVALID")
        source = records.get(rewrite.source_item_id)
        if source is None:
            _fail("REWRITE_SOURCE_UNKNOWN")
        if source.kind != "original":
            _fail("REWRITE_SOURCE_NOT_ORIGINAL")
        source_raw = source.request.report.raw or ""
        if rewrite.source_report_sha256 != _sha(
            source_raw.encode("utf-8")
        ) or rewrite.derived_report_sha256 != _sha(
            (record.request.report.raw or "").encode("utf-8")
        ):
            _fail("REWRITE_SOURCE_HASH_MISMATCH")
        if (
            record.source_family != source.source_family
            or record.provenance != source.provenance
            or not _same_non_report(source.request, record.request)
        ):
            _fail("REWRITE_NON_REPORT_CHANGE")
        if not _citations_delivered(record.request):
            _fail("REWRITE_CITATION_NOT_DELIVERED")
    _gate(record.request)
    check_identifiers(record.request)
    _check_coverage(record.coverage)
    if record.kind == "controlled_rewrite" and not (record.coverage_note or "").strip():
        _fail("SCHEMA_INVALID")
    if record.scenario_id != record.request.spec.scenario_id:
        _fail("IDENTITY_MISMATCH")


def _check_unique(records: Mapping[str, ItemRecord]) -> None:
    values = records.values()
    if max(Counter(r.input_hash for r in values).values(), default=0) > 1:
        _fail("DUPLICATE_INPUT_HASH")
    if max(Counter(r.report_semantic_sha256 for r in values).values(), default=0) > 1:
        _fail("DUPLICATE_REPORT")


def _rewrite_authors(records: Mapping[str, ItemRecord]) -> set[str]:
    return {
        r.rewrite.author_identity.casefold()
        for r in records.values()
        if r.rewrite is not None
    }


# -- section 1.3: init ------------------------------------------------------------------


@_public
def init_workspace(ws: Workspace) -> None:
    if ID_PATTERN.fullmatch(ws.workspace_id) is None:
        _fail("SCHEMA_INVALID")
    if _exists(ws.root):
        _fail("OUTPUT_EXISTS")
    shared = [
        p
        for p in (ws.private_root / "exposure.lock", ws.private_root / "exposure.jsonl")
        if not _exists(p)
    ]
    document = ws.root / "workspace.json"
    _preflight(ws, files=(*shared, document), dirs=(ws.root,))
    for path in shared:
        _write_new(ws, path, b"")
    _ensure_dirs(ws, ws.root)
    _write_new(
        ws,
        document,
        canonical_bytes(
            {"schema": _SCHEMAS["workspace"], "workspace": ws.workspace_id}
        ),
    )


# -- section 3: import ------------------------------------------------------------------


def _check_source(ws: Workspace, path: Path) -> None:
    """Q7: a tracked or unignored source report is already public."""
    path = Path(os.path.abspath(path))
    try:
        path.relative_to(ws.repo)
    except ValueError:
        _fail("PRIVATE_PATH_UNVERIFIED")  # Git cannot judge a path outside.
    _inside_repo(ws, path)
    _ignored(ws, path)


def _check_annotator_identity(identity: Any, model: Any) -> None:
    check_identifiers(AnnotationSet(identity, model, ()))


@_public
def import_capture(
    ws: Workspace,
    *,
    round_id: str,
    attempt_id: str,
    roots: SourceRoots,
    coverage: tuple[str, ...] = (),
    coverage_note: str | None = None,
    author_identity: str | None = None,
) -> ItemRecord:
    _check_workspace(ws)
    if (
        type(round_id) is not str
        or ID_PATTERN.fullmatch(round_id) is None
        or not round_id.startswith(ROUND_PREFIX)
    ):
        _fail("SOURCE_ROUND_INVALID")
    if type(attempt_id) is not str or _ATTEMPT.fullmatch(attempt_id) is None:
        _fail("SCHEMA_INVALID")
    round_dir = Path(os.path.abspath(roots.evidence_root)) / round_id
    aggregate_path = round_dir / "aggregate.json"
    summary_path = round_dir / "runs" / attempt_id / "summary.json"
    for path in (round_dir, aggregate_path, summary_path):
        _check_source(ws, path)
    # 1. The round manifest.
    aggregate = parse(_read_file(aggregate_path))
    capture_obj = aggregate.get("capture")
    if type(capture_obj) is not dict or "manifest" not in capture_obj:
        _fail("SCHEMA_INVALID")
    manifest = decode(Manifest, capture_obj["manifest"])
    # 2. Storage revalidates everything; only its capture is used.
    summary_raw = _read_file(summary_path)
    _, capture = load_summary_capture(
        summary_path,
        manifest=manifest,
        resolve=eval_live.attempt_resolver(
            round_id,
            attempt_id,
            ledger_root=roots.ledger_root,
            recording_root=roots.recording_root,
        ),
    )
    attempt = capture.attempt
    if (attempt.round_id, attempt.attempt_id) != (round_id, attempt_id):
        _fail("IDENTITY_MISMATCH")
    # 3. Source eligibility.
    if attempt.mode != "live":
        _fail("SOURCE_NOT_LIVE")
    if attempt.status != "ran":
        _fail("SOURCE_NOT_RAN")
    if (
        capture.report is None
        or capture.outcome is None
        or capture.recording_ref is None
    ):
        _fail("SOURCE_INCOMPLETE")
    if capture.judge is not None:
        _fail("SOURCE_ALREADY_JUDGED")
    if attempt.product_run_id is None:
        _fail("SOURCE_INCOMPLETE")
    # 4. The recording, as eval_live._verified_evidence.
    recording_dir = Path(roots.recording_root) / f"eval-live-{round_id}-{attempt_id}"
    recording = load_recording(
        recording_dir / capture.recording_ref.path,
        expected_hash=capture.recording_ref.sha256,
    )
    epoch = (capture.usage or {}).get("epoch")
    if recording.context.synthetic or type(epoch) is not int:
        _fail("IDENTITY_MISMATCH")
    check_recording_binding(
        recording, attempt, round_id=round_id, epoch=epoch, manifest=manifest
    )
    if recording.outcome != capture.outcome:
        _fail("IDENTITY_MISMATCH")
    evidence = evidence_from_recording(recording, outcome=capture.outcome)
    if canonical_bytes(evidence.delivered) != canonical_bytes(
        capture.delivered
    ) or canonical_bytes(evidence.authorized) != canonical_bytes(capture.authorized):
        _fail("DERIVED_RESULT_MISMATCH")
    # 5. The P6 request and its projection hash.
    request = JudgeInput(
        capture.spec,
        capture.answer,
        capture.report,
        capture.delivered,
        capture.authorized,
        capture.outcome,
    )
    check_identifiers(request)
    input_hash = _input_hash(request)
    semantic = _semantic_hash(request.report)
    # 6. Gate (Q4 A).
    _gate(request)
    # 7. Uniqueness.
    records = _records(ws)
    if any(
        (r.provenance.round_id, r.provenance.attempt_id) == (round_id, attempt_id)
        and r.kind == "original"
        for r in records.values()
    ):
        _fail("SOURCE_ALREADY_IMPORTED")
    if any(r.input_hash == input_hash for r in records.values()):
        _fail("DUPLICATE_INPUT_HASH")
    if any(r.report_semantic_sha256 == semantic for r in records.values()):
        _fail("DUPLICATE_REPORT")
    # 9. Coverage.
    coverage = _check_coverage(coverage)
    if coverage:
        if type(coverage_note) is not str or not coverage_note.strip():
            _fail("SCHEMA_INVALID")
        _check_annotator_identity(author_identity, "unspecified")
    elif coverage_note is not None and type(coverage_note) is not str:
        _fail("SCHEMA_INVALID")
    record = ItemRecord(
        item_id="p7-" + input_hash[:24],
        kind="original",
        scenario_id=capture.spec.scenario_id,
        source_family=f"live/{round_id}/{attempt_id}",
        input_hash=input_hash,
        report_semantic_sha256=semantic,
        request=request,
        provenance=SourceProvenance(
            round_id,
            attempt_id,
            attempt.product_run_id,
            _sha(summary_raw),
            capture.recording_ref.sha256,
            manifest_hash(manifest),
            _sha((capture.report.raw or "").encode("utf-8")),
        ),
        coverage=coverage,
        coverage_note=coverage_note,
        rewrite=None,
    )
    path = ws.root / "items" / f"{record.item_id}.json"
    _preflight(ws, files=(path,))
    _write_new(ws, path, canonical_bytes(record))
    return record


# -- section 4: rewrite ------------------------------------------------------------------


_REWRITE_KEYS = {
    "schema",
    "source_item_id",
    "source_report_sha256",
    "report_raw",
    "change_description",
    "purpose",
    "coverage",
    "coverage_note",
    "author_identity",
    "author_model_identity",
}


@_public
def register_rewrite(ws: Workspace, rewrite: Path) -> ItemRecord:
    _check_workspace(ws)
    obj = exact(parse(_read_file(Path(rewrite))), _REWRITE_KEYS)
    if obj["schema"] != _SCHEMAS["rewrite"] or any(
        type(obj[k]) is not str for k in _REWRITE_KEYS - {"coverage"}
    ):
        _fail("SCHEMA_INVALID")
    coverage = _check_coverage(obj["coverage"])
    records = _records(ws)
    source = records.get(obj["source_item_id"])
    if source is None:  # R1
        _fail("REWRITE_SOURCE_UNKNOWN")
    if source.kind != "original":  # R2
        _fail("REWRITE_SOURCE_NOT_ORIGINAL")
    source_raw = source.request.report.raw or ""
    if obj["source_report_sha256"] != _sha(source_raw.encode("utf-8")):  # R3
        _fail("REWRITE_SOURCE_HASH_MISMATCH")
    request = replace(
        source.request,
        report=ReportCapture(obj["report_raw"], source.request.report.finish_reason),
    )
    if not _same_non_report(source.request, request):  # R4
        _fail("REWRITE_NON_REPORT_CHANGE")
    parsed = parsed_report(request.report)  # R5
    if parsed is None or parsed.assessment_status != "completed":
        _fail("SCHEMA_INVALID")
    if not _citations_delivered(request):  # R6
        _fail("REWRITE_CITATION_NOT_DELIVERED")
    _gate(request)  # R7
    check_identifiers(request)  # R8
    input_hash = _input_hash(request)
    if any(r.input_hash == input_hash for r in records.values()):  # R9
        _fail("DUPLICATE_INPUT_HASH")
    semantic = _semantic_hash(request.report)
    if any(r.report_semantic_sha256 == semantic for r in records.values()):  # R10
        _fail("DUPLICATE_REPORT")
    if not all(  # R11
        obj[k].strip() for k in ("change_description", "purpose", "coverage_note")
    ):
        _fail("SCHEMA_INVALID")
    record_rewrite = RewriteRecord(
        REWRITE_LABEL,
        source.item_id,
        obj["source_report_sha256"],
        _sha(obj["report_raw"].encode("utf-8")),
        obj["change_description"],
        obj["purpose"],
        obj["author_identity"],
        obj["author_model_identity"],
    )
    check_identifiers(record_rewrite)  # R12
    annotators = {
        a.annotator_identity.casefold()
        for slot in (1, 2)
        if (loaded := _annotation(ws, slot)) is not None
        for a in (loaded[1],)
    }
    if obj["author_identity"].casefold() in annotators:
        _fail("CALIBRATION_UNRESOLVED")
    record = ItemRecord(
        item_id="p7-" + input_hash[:24],
        kind="controlled_rewrite",
        scenario_id=source.scenario_id,
        source_family=source.source_family,
        input_hash=input_hash,
        report_semantic_sha256=semantic,
        request=request,
        provenance=source.provenance,
        coverage=coverage,
        coverage_note=obj["coverage_note"],
        rewrite=record_rewrite,
    )
    path = ws.root / "items" / f"{record.item_id}.json"
    _preflight(ws, files=(path,))
    _write_new(ws, path, canonical_bytes(record))
    return record


# -- section 5: assemble -----------------------------------------------------------------


@_public
def assemble(ws: Workspace, plan: Path) -> str:
    _check_workspace(ws)
    plan_raw = _read_file(Path(plan))
    obj = exact(parse(plan_raw), {"schema", "families", "items"})
    if obj["schema"] != _SCHEMAS["plan"]:
        _fail("SCHEMA_INVALID")
    split_plan = SplitPlan(
        decode(Mapping[str, Literal["debug", "validation"]], obj["families"]),
        decode(tuple[str, ...], obj["items"]),
    )
    records = _records(ws)
    ids = split_plan.items
    if (
        len(ids) != 36
        or len(set(ids)) != 36
        or any(i not in records for i in ids)
        or any(records[i].source_family not in split_plan.families for i in ids)
        or {records[i].source_family for i in ids} != set(split_plan.families)
    ):
        _fail("SPLIT_PLAN_INVALID")
    chosen = [records[i] for i in sorted(ids)]

    def split_of(record: ItemRecord) -> Literal["debug", "validation"]:
        return split_plan.families[record.source_family]

    wanted = Counter(
        {
            (s, split): n
            for s in SCENARIOS
            for split, n in (("debug", 2), ("validation", 4))
        }
    )
    if Counter((r.scenario_id, split_of(r)) for r in chosen) != wanted:
        _fail("SCHEMA_INVALID")
    family_scenarios: dict[str, set[str]] = {}
    for record in chosen:
        family_scenarios.setdefault(record.source_family, set()).add(record.scenario_id)
    if any(len(s) != 1 for s in family_scenarios.values()):
        _fail("SPLIT_PLAN_INVALID")
    family_splits: dict[str, set[str]] = {}
    for record in chosen:
        family_splits.setdefault(record.source_family, set()).add(split_of(record))
    if any(len(s) != 1 for s in family_splits.values()):
        _fail("CALIBRATION_SET_OVERLAP")
    for scenario in SCENARIOS:
        families = {
            split: {
                r.source_family
                for r in chosen
                if r.scenario_id == scenario and split_of(r) == split
            }
            for split in ("debug", "validation")
        }
        if len(families["debug"]) != 1 or len(families["validation"]) != 1:
            _fail("SPLIT_PLAN_INVALID")
        if families["debug"] & families["validation"]:
            _fail("CALIBRATION_SET_OVERLAP")
    coverage = {c for r in chosen if split_of(r) == "validation" for c in r.coverage}
    if coverage != COVERAGE:
        _fail("CALIBRATION_COVERAGE_INCOMPLETE")
    for record in chosen:
        _check_record(record, records)
    _check_unique(records)
    items = tuple(
        CalibrationItem(
            r.item_id,
            r.scenario_id,
            split_of(r),
            r.source_family,
            r.input_hash,
            r.request,
            r.coverage,
        )
        for r in chosen
    )
    items_raw = canonical_bytes({"items": items})
    directory = ws.root / "assembled"
    targets = (directory / "plan.json", directory / "items.json")
    _preflight(ws, files=targets, dirs=(directory,))
    _write_new(ws, targets[0], plan_raw)
    _write_new(ws, targets[1], items_raw)
    return _sha(items_raw)


# -- section 6: blind packets and annotations ------------------------------------------------


@_public
def export_blind_packets(ws: Workspace, *, convention: Path) -> str:
    _check_workspace(ws)
    items_raw, items = _assembled(ws)
    text = None
    try:
        text = _read_file(Path(convention)).decode("utf-8").strip()
    except UnicodeError:
        pass
    if not text:
        _fail("SCHEMA_INVALID")
    if text not in judge.PROMPT:
        _fail("CONVENTION_MISMATCH")
    packet = BlindPacket(
        schema=_SCHEMAS["packet"],
        items_sha256=_sha(items_raw),
        judge_prompt=judge.PROMPT,
        judge_prompt_sha256=_sha(judge.PROMPT.encode("utf-8")),
        convention=text,
        convention_sha256=_sha(text.encode("utf-8")),
        output_schema=OUTPUT_SCHEMA,
        items=tuple(
            BlindPacketItem(i.item_id, i.input_hash, safe_input(i.request))
            for i in sorted(items, key=lambda i: i.item_id)
        ),
    )
    raw = canonical_bytes(packet)
    blind = ws.root / "blind"
    copies = tuple(blind / f"annotator-{slot}" / "packet.json" for slot in (1, 2))
    _preflight(
        ws,
        files=(blind / "packet.json", *copies),
        dirs=(blind, *(c.parent for c in copies)),
    )
    for path in (blind / "packet.json", *copies):
        _write_new(ws, path, raw)
    return _sha(raw)


def _diagnosis_key(diagnosis: JudgedDiagnosis) -> tuple[int, int, str]:
    if diagnosis.location == "summary":
        return (0, 0, diagnosis.quote)
    match = _LOCATION.fullmatch(diagnosis.location)
    if match is None:
        return (4, 0, diagnosis.quote)
    section = {"claims": 1, "gaps": 2, "next_steps": 3}[match[1]]
    return (section, int(match[2]), diagnosis.quote)


@_public
def import_annotation(
    ws: Workspace,
    *,
    slot: Literal[1, 2],
    response: Path,
    annotator_identity: str,
    model_identity: str,
    evidence_note: str | None = None,
) -> str:
    _check_workspace(ws)
    if slot not in SLOT_MODELS or type(slot) is not int:
        _fail("SCHEMA_INVALID")
    if evidence_note is not None and type(evidence_note) is not str:
        _fail("SCHEMA_INVALID")
    _, items = _assembled(ws)
    packet_raw = _read_ws(ws, ws.root / "blind/packet.json")
    response_raw = _read_file(Path(response))
    answer = _decode(BlindResponse, response_raw)
    if answer.schema != _SCHEMAS["response"]:
        _fail("SCHEMA_INVALID")
    if answer.packet_sha256 != _sha(packet_raw):
        _fail("PACKET_STALE")
    by_id = {i.item_id: i for i in items}
    labels = {label.item_id: label for label in answer.labels}
    if len(labels) != len(answer.labels) or set(labels) != set(by_id):
        _fail("SCHEMA_INVALID")
    if any(label.input_hash != by_id[i].input_hash for i, label in labels.items()):
        _fail("HASH_MISMATCH")
    _check_annotator_identity(annotator_identity, model_identity)
    if model_identity != SLOT_MODELS[slot]:
        _fail("ANNOTATOR_NOT_APPROVED")
    if is_candidate_model_identity(model_identity):
        _fail("CALIBRATION_UNRESOLVED")
    other = _annotation(ws, 3 - slot)
    taken = _rewrite_authors(_records(ws))
    if other is not None:
        taken.add(other[1].annotator_identity.casefold())
    if annotator_identity.casefold() in taken:
        _fail("CALIBRATION_UNRESOLVED")
    invalid: list[str] = []
    for item_id in sorted(labels):
        code = None
        try:
            check_blind_label(by_id[item_id], labels[item_id])
        except (JudgeError, StorageError) as exc:
            code = exc.code
        if code is not None:
            invalid.append(item_id)
    if invalid:
        _fail("CALIBRATION_LABEL_INVALID", invalid_items=invalid)
    annotation = AnnotationSet(
        annotator_identity,
        model_identity,
        tuple(
            replace(
                labels[i],
                diagnoses=tuple(sorted(labels[i].diagnoses, key=_diagnosis_key)),
            )
            for i in sorted(labels)
        ),
    )
    raw = canonical_bytes(annotation)
    provenance = AnnotationProvenance(
        slot,
        annotator_identity,
        model_identity,
        _sha(packet_raw),
        _sha(response_raw),
        _sha(raw),
        evidence_note,
    )
    directory = ws.root / "annotations"
    targets = (
        directory / f"annotations-{slot}.json",
        directory / f"annotations-{slot}.provenance.json",
    )
    _preflight(ws, files=targets)
    _write_new(ws, targets[0], raw)
    _write_new(ws, targets[1], canonical_bytes(provenance))
    return _sha(raw)


# -- section 7: brief and adjudication ------------------------------------------------------


def _causal_keys(label: BlindLabel) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted({(d.location, d.quote) for d in label.diagnoses if d.factual_causal})
    )


@_public
def write_adjudication_brief(ws: Workspace) -> str:
    _check_workspace(ws)
    loaded = [_annotation(ws, slot) for slot in (1, 2)]
    if loaded[0] is None or loaded[1] is None:
        _fail("CALIBRATION_UNRESOLVED")
    (raw_a, first), (raw_b, second) = loaded[0], loaded[1]
    items_raw, items = _assembled(ws)
    packet_raw = _read_ws(ws, ws.root / "blind/packet.json")
    a = {label.item_id: label for label in first.labels}
    b = {label.item_id: label for label in second.labels}
    by_id = {i.item_id: i for i in items}
    if set(a) != set(by_id) or set(b) != set(by_id):
        _fail("SCHEMA_INVALID")
    differences = tuple(
        BriefEntry(
            item_id,
            by_id[item_id].input_hash,
            safe_input(by_id[item_id].request),
            a[item_id],
            b[item_id],
            a[item_id].verdict != b[item_id].verdict,
            _causal_keys(a[item_id]),
            _causal_keys(b[item_id]),
        )
        for item_id in sorted(by_id)
        if a[item_id] != b[item_id]
    )
    brief = AdjudicationBrief(
        _SCHEMAS["brief"],
        _sha(items_raw),
        _sha(packet_raw),
        (_sha(raw_a), _sha(raw_b)),
        len(by_id) - len(differences),
        differences,
    )
    raw = canonical_bytes(brief)
    path = ws.root / "adjudication/brief.json"
    _preflight(ws, files=(path,))
    _write_new(ws, path, raw)
    return _sha(raw)


@_public
def apply_decisions(ws: Workspace, decisions: Path) -> tuple[Adjudication, ...]:
    _check_workspace(ws)
    decisions_raw = _read_file(Path(decisions))
    value = _decode(Decisions, decisions_raw)
    if value.schema != _SCHEMAS["decisions"]:
        _fail("SCHEMA_INVALID")
    brief_raw = _read_ws(ws, ws.root / "adjudication/brief.json")
    if value.brief_sha256 != _sha(brief_raw):
        _fail("BRIEF_STALE")
    brief = _decode(AdjudicationBrief, brief_raw)
    entries = {e.item_id: e for e in brief.differences}
    chosen = Counter(d.item_id for d in value.decisions)
    if set(chosen) != set(entries) or any(n != 1 for n in chosen.values()):
        _fail("CALIBRATION_UNRESOLVED")
    if any(not d.decision_record.strip() for d in value.decisions):
        _fail("CALIBRATION_UNRESOLVED")
    _, items = _assembled(ws)
    by_id = {i.item_id: i for i in items}
    adjudications = []
    for decision in sorted(value.decisions, key=lambda d: d.item_id):
        entry = entries[decision.item_id]
        if decision.choice in ("A", "B"):
            if decision.label is not None:
                _fail("SCHEMA_INVALID")
            label = entry.label_a if decision.choice == "A" else entry.label_b
        else:
            if decision.label is None:
                _fail("SCHEMA_INVALID")
            item = by_id.get(decision.item_id)
            if item is None:
                _fail("SCHEMA_INVALID")
            label = BlindLabel(
                item.item_id,
                item.input_hash,
                decision.label.diagnoses,
                decision.label.verdict,
            )
            check_blind_label(item, label)
        adjudications.append(
            Adjudication(
                decision.item_id,
                brief.annotation_hashes,
                decision.decision_record,
                label,
            )
        )
    annotations = []
    for slot in (1, 2):
        loaded = _annotation(ws, slot)
        if loaded is None:
            _fail("CALIBRATION_UNRESOLVED")
        annotations.append(loaded[1])
    result = tuple(adjudications)
    final_labels(LabelPack(items, tuple(annotations), result))
    directory = ws.root / "adjudication"
    targets = (directory / "decisions.json", directory / "adjudications.json")
    _preflight(ws, files=targets)
    _write_new(ws, targets[0], decisions_raw)
    _write_new(ws, targets[1], canonical_bytes({"adjudications": result}))
    return result


def _distribution(
    items: Sequence[CalibrationItem], labels: Mapping[str, BlindLabel], split: str
) -> dict[str, dict[str, int]]:
    counts = {s: {"positive": 0, "negative": 0} for s in sorted(SCENARIOS)}
    for item in items:
        if item.split == split:
            key = "positive" if labels[item.item_id].verdict else "negative"
            counts[item.scenario_id][key] += 1
    return counts


# -- section 10: balances ------------------------------------------------------------------


def _balance_path(ws: Workspace, session_id: str, provider: str, phase: str) -> Path:
    return ws.root / "costs" / session_id / f"{provider}-{phase}.json"


def _balance_record(
    session_id: str,
    provider: str,
    phase: str,
    amount: str | None,
    currency: str | None,
    source: str,
) -> BalanceRecord:
    _check_id(session_id)
    if provider not in PROVIDERS or phase not in ("before", "after"):
        _fail("SCHEMA_INVALID")
    if source == "unknown":
        if amount is not None or currency is not None:
            _fail("SCHEMA_INVALID")
    elif source in ("manual", "deepseek_balance_api"):
        if source == "deepseek_balance_api" and provider != "deepseek":
            _fail("SCHEMA_INVALID")
        value = None
        if type(amount) is str:
            try:
                value = Decimal(amount)
            except InvalidOperation:
                value = None
        if value is None or not value.is_finite():
            _fail("SCHEMA_INVALID")
        if type(currency) is not str or _CURRENCY.fullmatch(currency) is None:
            _fail("SCHEMA_INVALID")
        amount = str(value)
    else:
        _fail("SCHEMA_INVALID")
    return BalanceRecord(
        session_id,
        provider,
        phase,  # type: ignore[arg-type]
        amount,
        currency,
        source,  # type: ignore[arg-type]
        provider == "deepseek",
    )


@_public
def record_balance(
    ws: Workspace,
    *,
    session_id: str,
    provider: str,
    phase: Literal["before", "after"],
    amount: str | None,
    currency: str | None,
    source: Literal["manual", "deepseek_balance_api", "unknown"],
) -> None:
    _check_workspace(ws)
    if provider == "deepseek":
        _fail("USAGE")  # Only debug/formal write DeepSeek balances (section 10).
    record = _balance_record(session_id, provider, phase, amount, currency, source)
    path = _balance_path(ws, session_id, provider, phase)
    _preflight(ws, files=(path,))
    _write_new(ws, path, canonical_bytes(record))


def _write_deepseek_balance(
    ws: Workspace, session_id: str, phase: str, balance: BalanceReader | None
) -> None:
    value: Decimal | None = None
    if balance is not None:
        try:
            value = balance("deepseek")
        except Exception:
            value = None
    if type(value) is not Decimal or not value.is_finite():
        record = _balance_record(session_id, "deepseek", phase, None, None, "unknown")
    else:
        record = _balance_record(
            session_id, "deepseek", phase, str(value), "CNY", "deepseek_balance_api"
        )
    _write_new(
        ws, _balance_path(ws, session_id, "deepseek", phase), canonical_bytes(record)
    )


def _usage_files(ws: Workspace, session_id: str) -> list[Path]:
    files: list[Path] = []
    for directory in (
        ws.root / "debug" / session_id,
        ws.root / "calibration" / session_id,
    ):
        if _exists(directory):
            _check_private(ws, directory)
            files.extend(
                p
                for p in sorted(directory.glob("*.json"))
                if p.name != "calibration.json"
            )
    return files


@_public
def cost_report(ws: Workspace, session_id: str) -> Mapping[str, Any]:
    _check_workspace(ws)
    _check_id(session_id)
    report: dict[str, Any] = {}
    observations: dict[str, list[Mapping[str, Any]]] = {p: [] for p in PROVIDERS}
    for path in _usage_files(ws, session_id):
        obj = _loads(_read_ws(ws, path))
        observation = obj.get("observation") if isinstance(obj, dict) else None
        if (
            not isinstance(observation, dict)
            or observation.get("provider") not in PROVIDERS
        ):
            _fail("SCHEMA_INVALID")
        observations[observation["provider"]].append(observation)
    for provider in PROVIDERS:
        records: dict[str, BalanceRecord | None] = {}
        for phase in ("before", "after"):
            path = _balance_path(ws, session_id, provider, phase)
            records[phase] = (
                _decode(BalanceRecord, _read_ws(ws, path)) if _exists(path) else None
            )
        before, after = records["before"], records["after"]
        delta = None
        if (
            before is not None
            and after is not None
            and before.amount is not None
            and after.amount is not None
            and before.currency == after.currency
        ):
            delta = str(Decimal(before.amount) - Decimal(after.amount))
        tokens: dict[str, int] = {}
        missing: dict[str, int] = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [
                o.get("usage", {}).get(name)
                if isinstance(o.get("usage"), dict)
                else None
                for o in observations[provider]
            ]
            tokens[name] = sum(v for v in values if type(v) is int)
            missing[name] = sum(type(v) is not int for v in values)
        report[provider] = {
            "before": None if before is None else before.amount,
            "after": None if after is None else after.amount,
            "currency": None if before is None else before.currency,
            "delta": delta,
            "status": "measured" if delta is not None else "unknown",
            "shared_account_possible": provider == "deepseek",
            "calls": len(observations[provider]),
            "tokens": tokens,
            "missing_usage": missing,
        }
    return {"session": session_id, "providers": report}


# -- section 8: debug -----------------------------------------------------------------------


def _packet_binding(ws: Workspace, *, require_annotations: bool) -> None:
    """9.2 step 4 (also debug 2a when a packet exists)."""
    packet_raw = _read_ws(ws, ws.root / "blind/packet.json")
    packet = _decode(BlindPacket, packet_raw)
    if not packet.convention or packet.convention not in judge.PROMPT:
        _fail("JUDGE_PROMPT_CHANGED")
    items_raw = _read_ws(ws, ws.root / "assembled/items.json")
    if packet.items_sha256 != _sha(items_raw):
        _fail("PACKET_STALE")
    for slot in (1, 2):
        path = ws.root / f"annotations/annotations-{slot}.provenance.json"
        if not _exists(path):
            if require_annotations:
                _fail("PACKET_STALE")
            continue
        provenance = _decode(AnnotationProvenance, _read_ws(ws, path))
        if provenance.packet_sha256 != _sha(packet_raw):
            _fail("PACKET_STALE")


def _ordered_providers(providers: Sequence[str]) -> tuple[str, ...]:
    if (
        not providers
        or len(set(providers)) != len(providers)
        or any(p not in PROVIDERS for p in providers)
    ):
        _fail("SCHEMA_INVALID")
    return tuple(p for p in PROVIDERS if p in providers)


def _check_bindings(
    providers: Sequence[str],
    configs: Mapping[str, JudgeConfig],
    transports: Mapping[str, JudgeTransport],
    *,
    exact_configs: bool,
) -> None:
    keys = set(providers)
    if (
        set(transports) != keys
        or (set(configs) != keys if exact_configs else not keys <= set(configs))
        or any(
            type(configs[p]) is not JudgeConfig or configs[p].provider != p
            for p in keys
        )
    ):
        _fail("CONFIG_INVALID")


@_public
def debug_judge(
    ws: Workspace,
    *,
    session_id: str,
    providers: tuple[str, ...],
    item_ids: tuple[str, ...] | None,
    configs: Mapping[str, JudgeConfig],
    transports: Mapping[str, JudgeTransport],
    balance: BalanceReader | None = None,
) -> tuple[DebugRecord, ...]:
    _check_workspace(ws)
    _check_id(session_id)
    ordered = _ordered_providers(providers)
    _, items = _assembled(ws)
    by_id = {i.item_id: i for i in items}
    if item_ids is None:
        selected = sorted(
            (i for i in items if i.split == "debug"), key=lambda i: i.item_id
        )
    else:
        if len(set(item_ids)) != len(item_ids) or not item_ids:
            _fail("SCHEMA_INVALID")
        if any(i in by_id and by_id[i].split != "debug" for i in item_ids):
            _fail("VALIDATION_EXPOSURE_FORBIDDEN")
        if any(i not in by_id for i in item_ids):
            _fail("SCHEMA_INVALID")
        selected = sorted((by_id[i] for i in item_ids), key=lambda i: i.item_id)
    # 2a. Provider binding and criterion binding, no side effects.
    _check_bindings(ordered, configs, transports, exact_configs=False)
    if _exists(ws.root / "blind/packet.json"):
        _packet_binding(ws, require_annotations=False)
    # 3. Costs.
    for provider in ordered:
        if provider != "deepseek" and not _exists(
            _balance_path(ws, session_id, provider, "before")
        ):
            _fail("COST_BEFORE_MISSING")
    if "deepseek" in ordered and _exists(
        _balance_path(ws, session_id, "deepseek", "before")
    ):
        _fail("OUTPUT_EXISTS")
    # 4. Every target, then the first write.
    directory = ws.root / "debug" / session_id
    targets = [
        directory / f"{p}-{n:03d}.json"
        for p in ordered
        for n in range(1, len(selected) + 1)
    ]
    if "deepseek" in ordered:
        targets += [
            _balance_path(ws, session_id, "deepseek", phase)
            for phase in ("before", "after")
        ]
    _preflight(ws, files=targets)
    if "deepseek" in ordered:
        _write_deepseek_balance(ws, session_id, "before", balance)
    records: list[DebugRecord] = []
    for provider in ordered:
        config = configs[provider]
        for n, item in enumerate(selected, 1):
            if item.split != "debug":  # Mechanical guard (section 8 step 2).
                _fail("VALIDATION_EXPOSURE_FORBIDDEN")
            _PROGRESS["started"] = True
            observation = judge_once(
                item.request,
                config=config,
                calibration=None,
                transport=transports[provider],
            )
            record = DebugRecord(
                session_id,
                item.item_id,
                item.input_hash,
                provider,
                judge_config_hash(config),
                observation,
            )
            _write_new(
                ws, directory / f"{provider}-{n:03d}.json", canonical_bytes(record)
            )
            records.append(record)
    if "deepseek" in ordered:
        _write_deepseek_balance(ws, session_id, "after", balance)
    return tuple(records)


def _debug_comparison(
    ws: Workspace, records: Sequence[DebugRecord]
) -> dict[str, dict[str, int]] | None:
    """Counts only; omitted unless adjudication has completed."""
    if not _exists(ws.root / "adjudication/adjudications.json"):
        return None
    try:
        _, labels, _ = _label_pack(ws)
    except (P7Error, JudgeError, StorageError):
        return None
    result: dict[str, dict[str, int]] = {}
    for record in records:
        counts = result.setdefault(record.provider, {"verdict_match": 0, "key_hit": 0})
        label = labels[record.item_id]
        observed = record.observation.result
        if observed.status == "ok" and observed.verdict is label.verdict:
            counts["verdict_match"] += 1
        expected = {(d.location, d.quote) for d in label.diagnoses if d.factual_causal}
        actual = {(d.location, d.quote) for d in observed.diagnoses if d.factual_causal}
        if observed.status == "ok" and expected <= actual:
            counts["key_hit"] += 1
    return result


# -- section 9.1: freeze ---------------------------------------------------------------------


def _check_acceptance(
    ws: Workspace,
    accept: str,
    distribution: Mapping[str, Mapping[str, int]],
    adjudications_raw: bytes,
) -> None:
    brief_raw = _read_ws(ws, ws.root / "adjudication/brief.json")
    if accept != _sha(brief_raw):
        _fail("ACCEPTANCE_MISMATCH")
    path = ws.root / "adjudication/distribution-acceptance.json"
    _check_private(ws, path)
    _ignored(ws, path)
    raw = _read_ws(ws, path, missing="ACCEPTANCE_MISMATCH")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        value = None
    if (
        type(value) is not dict
        or set(value)
        != {"brief_sha256", "adjudications_sha256", "distribution", "decision_record"}
        or value["brief_sha256"] != accept
        or value["adjudications_sha256"] != _sha(adjudications_raw)
        or value["distribution"] != distribution
        or type(value["decision_record"]) is not str
        or not value["decision_record"].strip()
    ):
        _fail("ACCEPTANCE_MISMATCH")


@_public
def freeze(ws: Workspace, *, accept_distribution: str | None = None) -> str:
    _check_workspace(ws)
    pack, labels, adjudications_raw = _label_pack(ws)
    debug = _distribution(pack.items, labels, "debug")
    if any(c != {"positive": 1, "negative": 1} for c in debug.values()):
        _fail("LABEL_DISTRIBUTION_MISMATCH", debug_distribution=debug)
    validation = _distribution(pack.items, labels, "validation")
    if all(c == {"positive": 1, "negative": 3} for c in validation.values()):
        if accept_distribution is not None:
            _fail("USAGE")
    elif accept_distribution is None:
        _fail(
            "LABEL_DISTRIBUTION_MISMATCH",
            distribution=validation,
            adjudications_sha256=_sha(adjudications_raw),
        )
    else:
        _check_acceptance(ws, accept_distribution, validation, adjudications_raw)
    records = _records(ws)
    for item in pack.items:
        record = records.get(item.item_id)
        if record is None:
            _fail("SCHEMA_INVALID")
        if (
            canonical_bytes(record.request) != canonical_bytes(item.request)
            or (record.scenario_id, record.source_family, record.input_hash)
            != (item.scenario_id, item.source_family, item.input_hash)
            or tuple(record.coverage) != tuple(item.coverage)
        ):
            _fail("HASH_MISMATCH")
        _check_record(record, records)
    _check_unique(records)
    directory = ws.root / "label-pack"
    _preflight(ws, files=tuple(directory / n for n in _PACK_FILES), dirs=(directory,))
    pack_hash = freeze_label_pack(directory, pack)
    if load_label_pack(directory, expected_pack_hash=pack_hash) != pack:
        _fail("HASH_MISMATCH")
    return pack_hash


# -- section 9.2: formal ---------------------------------------------------------------------


class _CountingTransport:
    def __init__(self, inner: JudgeTransport) -> None:
        self.inner = inner
        self.sent = 0

    def send(self, **kwargs: Any) -> Any:
        self.sent += 1
        return self.inner.send(**kwargs)


def _debug_precheck(ws: Workspace, hashes: Mapping[str, str]) -> None:
    root = ws.root / "debug"
    if _exists(root):
        _check_private(ws, root)
        for session in sorted(root.iterdir()):
            ok: set[str] = set()
            for path in sorted(session.glob("*.json")):
                record = _decode(DebugRecord, _read_ws(ws, path))
                if (
                    record.observation.result.status == "ok"
                    and record.provider in hashes
                    and record.config_hash == hashes[record.provider]
                ):
                    ok.add(record.provider)
            if ok == set(hashes):
                return
    _fail("DEBUG_PRECHECK_MISSING")


def _sources(families: set[str]) -> set[tuple[str, str]]:
    result = set()
    for family in families:
        match = _FAMILY.fullmatch(family)
        if match is not None:
            result.add((match[1], match[2]))
    return result


def _register(ws: Workspace, run_id: str, pack_hash: str, pack: LabelPack) -> None:
    """9.2 step 7: one cross-workspace check-and-append under ``flock``."""
    lock = ws.private_root / "exposure.lock"
    registry = ws.private_root / "exposure.jsonl"
    for path in (lock, registry):
        _inside_repo(ws, path)
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            regular = False
        if not regular:
            _fail("EXPOSURE_REGISTRY_MISSING")
    records = _records(ws)
    families = {i.source_family for i in pack.items if i.split == "validation"}
    sources = _sources(families) | {
        (r.provenance.round_id, r.provenance.attempt_id)
        for i in pack.items
        if i.split == "validation" and (r := records.get(i.item_id)) is not None
    }
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        seen_families: set[str] = set()
        seen_sources: set[tuple[str, str]] = set()
        for line in _read_file(registry).splitlines():
            if not line.strip():
                continue
            row = _loads(line)
            if (
                type(row) is not dict
                or type(row.get("validation_families")) is not list
                or type(row.get("sources")) is not list
            ):
                _fail("SCHEMA_INVALID")
            seen_families.update(map(str, row["validation_families"]))
            for source in row["sources"]:
                if type(source) is not dict:
                    _fail("SCHEMA_INVALID")
                seen_sources.add(
                    (str(source.get("round_id")), str(source.get("attempt_id")))
                )
        for path in sorted(
            ws.private_root.glob("*/calibration/*/label-pack/items.json")
        ):
            obj = _loads(_read_file(path))
            entries = obj.get("items") if type(obj) is dict else None
            if type(entries) is not list or any(type(e) is not dict for e in entries):
                _fail("SCHEMA_INVALID")
            seen_families.update(
                str(e.get("source_family"))
                for e in entries
                if e.get("split") == "validation"
            )
        seen_sources |= _sources(seen_families)
        if families & seen_families or sources & seen_sources:
            _fail("VALIDATION_EXPOSED")
        row = {
            "run_id": run_id,
            "workspace": ws.workspace_id,
            "pack_hash": pack_hash,
            "validation_families": sorted(families),
            "sources": [{"round_id": r, "attempt_id": a} for r, a in sorted(sources)],
            "at": datetime.now(UTC).isoformat(),
        }
        _PROGRESS["started"] = True
        code = None
        try:
            out = os.open(registry, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            try:
                os.write(out, json.dumps(row, sort_keys=True).encode("utf-8") + b"\n")
                os.fsync(out)
            finally:
                os.close(out)
        except OSError:
            code = "ARTIFACT_UNWRITABLE"
        if code is not None:
            _fail(code)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@_public
def formal(
    ws: Workspace,
    *,
    run_id: str,
    pack_hash: str,
    configs: Mapping[str, JudgeConfig],
    transports: Mapping[str, JudgeTransport],
    balance: BalanceReader | None = None,
) -> FormalResult:
    _check_workspace(ws)
    _check_id(run_id)
    # 1.
    pack = load_label_pack(ws.root / "label-pack", expected_pack_hash=pack_hash)
    # 2.
    _check_bindings(PROVIDERS, configs, transports, exact_configs=True)
    hashes = {p: judge_config_hash(configs[p]) for p in PROVIDERS}
    # 3.
    _debug_precheck(ws, hashes)
    # 4.
    _packet_binding(ws, require_annotations=True)
    # 5.
    for provider in ("zai", "openai"):
        if not _exists(_balance_path(ws, run_id, provider, "before")):
            _fail("COST_BEFORE_MISSING")
    if _exists(_balance_path(ws, run_id, "deepseek", "before")):
        _fail("OUTPUT_EXISTS")
    # 6.
    destination = ws.root / "calibration" / run_id
    if _exists(destination):
        _fail("OUTPUT_EXISTS")
    per_provider = sum(2 if i.split == "validation" else 1 for i in pack.items)
    files = [
        *(destination / "label-pack" / n for n in _PACK_FILES),
        *(
            destination / f"{p}-{n:03d}.json"
            for p in PROVIDERS
            for n in range(1, per_provider + 1)
        ),
        destination / "calibration.json",
        *(
            _balance_path(ws, run_id, "deepseek", phase)
            for phase in ("before", "after")
        ),
    ]
    _preflight(ws, files=files, dirs=(destination, destination / "label-pack"))
    # 7. First write: the exposure registration.
    _register(ws, run_id, pack_hash, pack)
    _ensure_dirs(ws, destination.parent)
    _write_deepseek_balance(ws, run_id, "before", balance)
    counting = {p: _CountingTransport(transports[p]) for p in PROVIDERS}
    selection: SelectionRecord | None = None
    code: str | None = None
    try:
        selection = run_calibration(
            pack, configs=configs, transports=counting, destination=destination
        )
    except (JudgeError, StorageError) as exc:
        code = exc.code
    if selection is None:
        sent = any(t.sent for t in counting.values())
        raise P7Error(code or "SCHEMA_INVALID", stopped=sent or code in _WRITE_CODES)
    _write_deepseek_balance(ws, run_id, "after", balance)
    readback: Literal["verified", "unresolved_as_expected", "failed"] = "failed"
    readback_code: str | None = None
    provider = selection.selected_provider if selection.status == "selected" else "zai"
    verified = None
    try:
        verified = load_verified_calibration(
            destination, pack=pack, config=configs[provider or "zai"]
        )
    except (JudgeError, StorageError) as exc:
        readback_code = exc.code
    if selection.status == "selected":
        if verified is not None and verified.result.passed:
            readback = "verified"
    elif verified is None and readback_code == "CALIBRATION_UNRESOLVED":
        readback, readback_code = "unresolved_as_expected", None
    return FormalResult(run_id, pack_hash, hashes, selection, readback, readback_code)


# -- section 11: summary --------------------------------------------------------------------


@_public
def summary(ws: Workspace) -> Mapping[str, Any]:
    _check_workspace(ws)
    out: dict[str, Any] = {}
    records = _records(ws)
    out["item_kinds"] = dict(Counter(r.kind for r in records.values()))
    if _exists(ws.root / "assembled/items.json"):
        items_raw, items = _assembled(ws)
        out["items_sha256"] = _sha(items_raw)
        out["items"] = {
            f"{s}/{split}": n
            for (s, split), n in sorted(
                Counter((i.scenario_id, i.split) for i in items).items()
            )
        }
        out["validation_coverage"] = sorted(
            {c for i in items if i.split == "validation" for c in i.coverage}
        )
    if _exists(ws.root / "blind/packet.json"):
        packet_raw = _read_ws(ws, ws.root / "blind/packet.json")
        packet = _decode(BlindPacket, packet_raw)
        out["packet_sha256"] = _sha(packet_raw)
        out["judge_prompt_sha256"] = {
            "packet": packet.judge_prompt_sha256,
            "current": _sha(judge.PROMPT.encode("utf-8")),
        }
    annotations = {}
    for slot in (1, 2):
        loaded = _annotation(ws, slot)
        if loaded is not None:
            annotations[str(slot)] = {
                "sha256": _sha(loaded[0]),
                "model_identity": loaded[1].model_identity,
            }
    out["annotations"] = annotations
    if _exists(ws.root / "adjudication/brief.json"):
        brief = _decode(
            AdjudicationBrief, _read_ws(ws, ws.root / "adjudication/brief.json")
        )
        out["differences"] = len(brief.differences)
    if _exists(ws.root / "adjudication/adjudications.json"):
        out["adjudications"] = len(_adjudications(ws)[1])
    index = ws.root / "label-pack/pack.json"
    if _exists(index):
        pack_hash = _sha(_read_ws(ws, index))
        load_label_pack(ws.root / "label-pack", expected_pack_hash=pack_hash)
        out["pack_hash"] = pack_hash
        acceptance = ws.root / "adjudication/distribution-acceptance.json"
        if _exists(acceptance):
            value = _loads(_read_ws(ws, acceptance))
            if type(value) is dict:
                out["distribution_acceptance"] = {
                    k: value.get(k)
                    for k in ("brief_sha256", "adjudications_sha256", "distribution")
                }
    runs = {}
    calibration = ws.root / "calibration"
    if _exists(calibration):
        for path in sorted(calibration.glob("*/calibration.json")):
            selection = _loads(_read_ws(ws, path)).get("selection", {})
            runs[path.parent.name] = {
                "status": selection.get("status"),
                "selected_provider": selection.get("selected_provider"),
                "eligible": selection.get("eligible"),
                "results": {
                    p: {
                        k: r.get(k)
                        for k in (
                            "passed",
                            "items",
                            "comparisons",
                            "agreement",
                            "negatives",
                            "false_passes",
                            "reasons",
                        )
                    }
                    for p, r in (selection.get("results") or {}).items()
                },
            }
    out["formal_runs"] = runs
    costs = {}
    if _exists(ws.root / "costs"):
        for session in sorted(p.name for p in (ws.root / "costs").iterdir()):
            report = cost_report.__wrapped__(ws, session)  # type: ignore[attr-defined]
            costs[session] = {
                p: {
                    "delta": v["delta"],
                    "status": v["status"],
                    "currency": v["currency"],
                }
                for p, v in report["providers"].items()
            }
    out["costs"] = costs
    return out


# -- CLI ------------------------------------------------------------------------------------


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _Usage(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="scripts.eval_p7", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, add_help=False)
        p.add_argument("--workspace", required=True)
        return p

    command("init")
    p = command("import")
    p.add_argument("--round", required=True)
    p.add_argument("--attempt", required=True)
    for name in ("--evidence-root", "--ledger-root", "--recording-root"):
        p.add_argument(name)
    p.add_argument("--coverage", action="extend", nargs="+", default=[])
    p.add_argument("--coverage-note")
    p.add_argument("--author-identity")
    command("rewrite").add_argument("--file", required=True)
    command("assemble").add_argument("--plan", required=True)
    command("export-blind").add_argument("--convention", required=True)
    p = command("import-annotation")
    p.add_argument("--slot", required=True, choices=["1", "2"])
    p.add_argument("--response", required=True)
    p.add_argument("--annotator-identity", required=True)
    p.add_argument("--model-identity", required=True)
    p.add_argument("--evidence-note")
    command("brief")
    command("adjudicate").add_argument("--decisions", required=True)
    p = command("debug")
    p.add_argument("--session", required=True)
    p.add_argument("--judge-env", required=True)
    p.add_argument("--provider", action="extend", nargs="+", choices=PROVIDERS)
    p.add_argument("--item", action="extend", nargs="+")
    command("freeze").add_argument("--accept-distribution")
    p = command("formal")
    p.add_argument("--run", required=True)
    p.add_argument("--pack-hash", required=True)
    p.add_argument("--judge-env", required=True)
    p = command("balance")
    p.add_argument("--session", required=True)
    p.add_argument("--provider", required=True, choices=PROVIDERS)
    p.add_argument("--phase", required=True, choices=["before", "after"])
    p.add_argument("--amount")
    p.add_argument("--currency")
    p.add_argument("--unknown", action="store_true")
    command("cost").add_argument("--session", required=True)
    command("summary")
    return parser


def _validate(args: argparse.Namespace) -> None:
    """Section 0: every ID is checked before any path is built."""

    def matches(pattern: re.Pattern[str], value: Any) -> bool:
        return value is None or pattern.fullmatch(value) is not None

    checks = [
        matches(ID_PATTERN, args.workspace),
        matches(ID_PATTERN, getattr(args, "session", None)),
        matches(ID_PATTERN, getattr(args, "run", None)),
        matches(_ATTEMPT, getattr(args, "attempt", None)),
        matches(_SHA256, getattr(args, "pack_hash", None)),
        matches(_SHA256, getattr(args, "accept_distribution", None)),
        all(_ITEM.fullmatch(i) for i in getattr(args, "item", None) or ()),
    ]
    round_id = getattr(args, "round", None)
    if round_id is not None:
        checks.append(
            ID_PATTERN.fullmatch(round_id) is not None
            and round_id.startswith(ROUND_PREFIX)
        )
    if args.command == "balance":
        manual = args.amount is not None and args.currency is not None
        none = args.amount is None and args.currency is None
        checks.append(args.provider != "deepseek")
        checks.append((manual and not args.unknown) or (none and args.unknown))
    if not all(checks):
        raise _Usage("invalid argument")


def _emit(
    command: str, status: str, exit_code: int, reason: str | None, **extra: Any
) -> int:
    print(
        json.dumps(
            {
                **extra,
                "command": command,
                "status": status,
                "exit_code": exit_code,
                "reason": reason,
            },
            sort_keys=True,
        )
    )
    return exit_code


def _configs(env: str, providers: Sequence[str]) -> dict[str, JudgeConfig]:
    return {
        p: load_judge_config(Path(env), environ=os.environ, provider=p)  # type: ignore[arg-type]
        for p in providers
    }


def _deepseek_reader(configs: Mapping[str, JudgeConfig]) -> BalanceReader | None:
    config = configs.get("deepseek")
    if config is None:
        return None
    return lambda _provider: eval_live_lab.DeepSeekBalance(config.api_key).balance()


def _run(
    args: argparse.Namespace, ws: Workspace
) -> tuple[int, str, str, dict[str, Any]]:
    """Returns (exit_code, status, reason, extra stdout fields)."""
    command = args.command
    if command == "init":
        init_workspace(ws)
        return 0, "ok", "OK", {"workspace": ws.workspace_id}
    if command == "import":
        from scripts.f11_u2_harness import resolve_ledger_root

        ledger = args.ledger_root
        if ledger is None:
            try:
                ledger = resolve_ledger_root(ws.repo)
            except ValueError:
                raise P7Error("EVIDENCE_UNAVAILABLE", stopped=False) from None
        roots = SourceRoots(
            Path(args.evidence_root or ws.repo / "docs/evidence/eval").absolute(),
            Path(ledger).absolute(),
            Path(args.recording_root or ws.repo / "tmp/eval").absolute(),
        )
        record = import_capture(
            ws,
            round_id=args.round,
            attempt_id=args.attempt,
            roots=roots,
            coverage=tuple(args.coverage),
            coverage_note=args.coverage_note,
            author_identity=args.author_identity,
        )
        return 0, "ok", "OK", _item_fields(record)
    if command == "rewrite":
        return 0, "ok", "OK", _item_fields(register_rewrite(ws, Path(args.file)))
    if command == "assemble":
        return 0, "ok", "OK", {"items_sha256": assemble(ws, Path(args.plan))}
    if command == "export-blind":
        digest = export_blind_packets(ws, convention=Path(args.convention))
        return 0, "ok", "OK", {"packet_sha256": digest}
    if command == "import-annotation":
        digest = import_annotation(
            ws,
            slot=int(args.slot),  # type: ignore[arg-type]
            response=Path(args.response),
            annotator_identity=args.annotator_identity,
            model_identity=args.model_identity,
            evidence_note=args.evidence_note,
        )
        return 0, "ok", "OK", {"annotation_sha256": digest, "slot": int(args.slot)}
    if command == "brief":
        digest = write_adjudication_brief(ws)
        brief = _decode(
            AdjudicationBrief, _read_file(ws.root / "adjudication/brief.json")
        )
        return (
            0,
            "ok",
            "OK",
            {
                "brief_sha256": digest,
                "agreed": brief.agreed,
                "differences": len(brief.differences),
            },
        )
    if command == "adjudicate":
        adjudications = apply_decisions(ws, Path(args.decisions))
        pack, labels, _ = _public(_label_pack)(ws)
        return (
            0,
            "ok",
            "OK",
            {
                "adjudications": len(adjudications),
                "debug_distribution": _distribution(pack.items, labels, "debug"),
                "validation_distribution": _distribution(
                    pack.items, labels, "validation"
                ),
            },
        )
    if command == "debug":
        providers = tuple(args.provider or PROVIDERS)
        configs = _configs(args.judge_env, providers)
        records = debug_judge(
            ws,
            session_id=args.session,
            providers=providers,
            item_ids=tuple(args.item) if args.item else None,
            configs=configs,
            transports={p: HTTPJudgeTransport() for p in providers},
            balance=_deepseek_reader(configs),
        )
        counts: dict[str, dict[str, int]] = {}
        for record in records:
            per = counts.setdefault(record.provider, {})
            status = record.observation.result.status
            per[status] = per.get(status, 0) + 1
        extra: dict[str, Any] = {"session": args.session, "results": counts}
        comparison = _debug_comparison(ws, records)
        if comparison is not None:
            extra["label_comparison"] = comparison
        return 0, "ok", "OK", extra
    if command == "freeze":
        return (
            0,
            "ok",
            "OK",
            {"pack_hash": freeze(ws, accept_distribution=args.accept_distribution)},
        )
    if command == "formal":
        configs = _configs(args.judge_env, PROVIDERS)
        result = formal(
            ws,
            run_id=args.run,
            pack_hash=args.pack_hash,
            configs=configs,
            transports={p: HTTPJudgeTransport() for p in PROVIDERS},
            balance=_deepseek_reader(configs),
        )
        selection = result.selection
        extra = {
            "run": result.run_id,
            "pack_hash": result.pack_hash,
            "config_hashes": dict(result.config_hashes),
            "selection": {
                "status": selection.status,
                "selected_provider": selection.selected_provider,
                "eligible": list(selection.eligible),
            },
            "results": {
                p: {
                    "passed": r.passed,
                    "items": r.items,
                    "comparisons": r.comparisons,
                    "agreement": r.agreement,
                    "negatives": r.negatives,
                    "false_passes": r.false_passes,
                    "reasons": list(r.reasons),
                }
                for p, r in selection.results.items()
            },
            "readback": result.readback,
            "readback_code": result.readback_code,
        }
        if result.readback == "failed":
            return 1, "failed", "READBACK_FAILED", extra
        if selection.status == "selected":
            return 0, "selected", "OK", extra
        if selection.status == "pending_user_decision":
            return 6, "pending_user_decision", "PENDING_USER_DECISION", extra
        return 1, "no_baseline", "NO_BASELINE", extra
    if command == "balance":
        record_balance(
            ws,
            session_id=args.session,
            provider=args.provider,
            phase=args.phase,
            amount=args.amount,
            currency=args.currency,
            source="unknown" if args.unknown else "manual",
        )
        return (
            0,
            "ok",
            "OK",
            {
                "session": args.session,
                "provider": args.provider,
                "phase": args.phase,
            },
        )
    if command == "cost":
        return 0, "ok", "OK", {"cost": dict(cost_report(ws, args.session))}
    return 0, "ok", "OK", dict(summary(ws))


def _item_fields(record: ItemRecord) -> dict[str, Any]:
    return {
        "item_id": record.item_id,
        "input_hash": record.input_hash,
        "scenario_id": record.scenario_id,
        "source_family": record.source_family,
    }


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    try:
        return _main(command, argv)
    except KeyboardInterrupt:
        return _emit(command, "interrupted", 130, "INTERRUPTED")


def _main(command: str, argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
        _validate(args)
    except _Usage:
        return _emit(command, "usage", 2, "USAGE")
    error: P7Error | None = None
    try:
        ws = open_workspace(args.workspace)
        exit_code, status, reason, extra = _run(args, ws)
        return _emit(command, status, exit_code, reason, **extra)
    except P7Error as exc:
        error = P7Error(exc.code, stopped=exc.stopped, details=exc.details)
    except JudgeError as exc:  # Credential loading before any write.
        error = P7Error(exc.code, stopped=False)
    if error.code == "USAGE":
        return _emit(command, "usage", 2, "USAGE")
    if error.stopped:
        return _emit(command, "stopped", 5, error.code, **error.details)
    return _emit(command, "refused", 3, error.code, **error.details)


if __name__ == "__main__":
    sys.exit(main())
