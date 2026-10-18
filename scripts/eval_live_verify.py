"""Zero-model structural replay check of live recordings (eval PR-C, A11).

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md section 2
(``--verify-replay``, r9). Reads local files only: each summary's
``recording_ref`` is reloaded by its hash, re-bound to its attempt (Q1) and
replayed with ``mode="structural"`` and no investigator, so no model, network,
kubectl or PostgreSQL call can happen.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opspilot.evaluation import (
    DimensionValue,
    EvaluationCapture,
    check_recording_binding,
    load_recording,
    manifest_hash,
    replay,
)
from opspilot.evaluation.codec import StorageError, decode, parse

SCHEMA = "opspilot-eval-structural-replay-1"
KINDS = ("model", "http", "tool", "delivery", "refusal")


def _verify_one(
    round_id: str, capture: EvaluationCapture, recording_root: Path
) -> dict[str, Any]:
    attempt = capture.attempt
    ref = capture.recording_ref
    assert ref is not None
    item: dict[str, Any] = {
        "attempt_id": attempt.attempt_id,
        "recording_sha256": ref.sha256,
        "loaded": False,
        "binding_ok": False,
        "structural_verified": False,
        "error": None,
        "event_counts": {kind: 0 for kind in KINDS},
    }
    directory = recording_root / f"eval-live-{round_id}-{attempt.attempt_id}"
    try:
        recording = load_recording(directory / ref.path, expected_hash=ref.sha256)
        item["loaded"] = True
        counts: Counter[str] = Counter(str(event.kind) for event in recording.events)
        item["event_counts"] = {kind: counts.get(kind, 0) for kind in KINDS}
        epoch = (capture.usage or {}).get("epoch")
        if type(epoch) is not int:
            raise StorageError("IDENTITY_MISMATCH")
        check_recording_binding(
            recording,
            attempt,
            round_id=round_id,
            epoch=epoch,
            manifest=capture.manifest,
        )
        item["binding_ok"] = True
        manifest = replace(
            capture.manifest,
            dimensions={
                **capture.manifest.dimensions,
                "replay_fixture": DimensionValue("hash", ref.sha256, None),
            },
        )
        structural = replace(
            attempt,
            attempt_id=f"{attempt.attempt_id}-structural",
            manifest_hash=manifest_hash(manifest),
            mode="structural",
            product_run_id=None,
        )
        result = replay(
            recording,
            mode="structural",
            attempt=structural,
            manifest=manifest,
            investigator=None,
        )
        item["structural_verified"] = bool(result.structural_verified)
        item["error"] = result.error
    except StorageError as exc:
        item["error"] = exc.code
    except (OSError, ValueError):
        item["error"] = "EVIDENCE_UNAVAILABLE"
    return item


def verify_replay(
    round_id: str,
    *,
    evidence_root: Path,
    recording_root: Path,
    attempt_id: str | None = None,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    runs = evidence_root / round_id / "runs"
    results: list[dict[str, Any]] = []
    if runs.is_dir():
        for path in sorted(runs.glob("*/summary.json")):
            if attempt_id is not None and path.parent.name != attempt_id:
                continue
            try:
                capture = decode(EvaluationCapture, parse(path.read_bytes())["capture"])
            except (StorageError, OSError, KeyError):
                results.append(
                    {"attempt_id": path.parent.name, "error": "SCHEMA_INVALID"}
                )
                continue
            if capture.recording_ref is None:
                continue
            results.append(_verify_one(round_id, capture, recording_root))
    if not results:
        document = {
            "schema": SCHEMA,
            "round": round_id,
            "status": "refused",
            "exit_code": 3,
            "reason": "NOTHING_TO_VERIFY",
            "results": [],
        }
        return 3, document
    ok = all(item.get("structural_verified") is True for item in results)
    code = 0 if ok else 1
    document = {
        "schema": SCHEMA,
        "round": round_id,
        "status": "verified" if ok else "failed",
        "exit_code": code,
        "reason": None,
        "results": results,
    }
    if not _write_exclusive(evidence_root / round_id, document, now):
        # Never overwrite and never raise: the result is still reported (r10).
        document["write_error"] = "EVIDENCE_WRITE_FAILED"
        document["exit_code"] = 5  # the JSON states the process exit code
        return 5, document
    return code, document


MAX_NAME_ATTEMPTS = 100


def _write_exclusive(
    directory: Path, document: dict[str, Any], now: datetime | None
) -> bool:
    """Create the first free name: the plain file, then ``-<UTC>``, then
    ``-<UTC>-2``, ``-3`` ...; each attempt is an exclusive create."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    names = ["structural-replay.json", f"structural-replay-{stamp}.json"] + [
        f"structural-replay-{stamp}-{n}.json" for n in range(2, MAX_NAME_ATTEMPTS)
    ]
    raw = json.dumps(document, indent=2, sort_keys=True) + "\n"
    for name in names:
        try:
            with (directory / name).open("x") as stream:
                stream.write(raw)
            return True
        except FileExistsError:
            continue
        except OSError:
            return False
    return False
