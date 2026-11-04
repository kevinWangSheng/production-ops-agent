"""Re-count the canary of already-run attempts by the outward-only rule
(#432, seams-432 section 5b, user ruling 2026-11-03 (c)).

    .venv/bin/python -m scripts.eval_canary_recheck --dataset evals/datasets/dev-v2 \\
        --round docs/evidence/eval/<R1> [--round ...] [--recordings tmp/eval]

For every started attempt whose answer carries a canary and whose
``projection.json`` has ``canary_hits``, read the local recording
(``<recordings>/eval-live-<round>-<attempt>/recording.json``) and count, per
model response, whether the canary is in the outward output (``content``
and ``tool_calls``) or only in ``reasoning_content``. ``raw`` (the
provider's original body, which repeats the reasoning) is not read. Writes
``runs/<attempt>/canary-recheck.json`` next to the existing evidence, never
the canary itself, and never changes ``projection.json`` or
``expectations.json``. Refuses to overwrite an existing recheck file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opspilot.evaluation.model import load_scenarios_v2

SCHEMA = "opspilot-eval-canary-recheck-1"
RULE = "outward-only"
FILENAME = "canary-recheck.json"


class RecheckError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise RecheckError(f"EVIDENCE_INVALID:{path.name}") from None


def count_recording(recording_dir: Path, canary: str) -> dict[str, Any]:
    """Outward and reasoning-only canary counts over a recording's model
    responses, plus the recording's sha256."""
    path = recording_dir / "recording.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise RecheckError(f"RECORDING_MISSING:{recording_dir.name}") from None
    try:
        recording = json.loads(raw)
    except ValueError:
        raise RecheckError(f"EVIDENCE_INVALID:{path.name}") from None
    events = recording.get("events") if isinstance(recording, Mapping) else None
    if not isinstance(events, list):
        raise RecheckError(f"EVIDENCE_INVALID:{path.name}")
    outward = reasoning = 0
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") != "model":
            continue
        ref = event.get("response_ref")
        if not isinstance(ref, Mapping) or not isinstance(ref.get("path"), str):
            continue  # a transport error has no response body
        blob = _load(recording_dir / ref["path"])
        if not isinstance(blob, Mapping):
            raise RecheckError(f"RECORDING_BLOB_MISSING:{recording_dir.name}")
        visible = json.dumps(
            {"content": blob.get("content"), "tool_calls": blob.get("tool_calls")},
            ensure_ascii=False,
        )
        thought = json.dumps(blob.get("reasoning_content"), ensure_ascii=False)
        if canary in visible:
            outward += 1
        elif canary in thought:
            reasoning += 1
    return {
        "recording_sha256": hashlib.sha256(raw).hexdigest(),
        "step_responses": outward,
        "step_reasoning": reasoning,
    }


def recheck(
    round_dirs: Sequence[Path], dataset_root: Path, recordings: Path
) -> list[dict[str, Any]]:
    answers = {
        spec.scenario_id: answer for spec, answer in load_scenarios_v2(dataset_root)
    }
    written: list[dict[str, Any]] = []
    for round_dir in round_dirs:
        lifecycle = _load(round_dir / "lifecycle.json")
        if not isinstance(lifecycle, Mapping):
            raise RecheckError(f"LIFECYCLE_MISSING:{round_dir.name}")
        round_id = str(lifecycle.get("round"))
        for slot in lifecycle.get("slots") or []:
            if not isinstance(slot, Mapping) or not slot.get("attempt_id"):
                continue
            answer = answers.get(str(slot.get("scenario_id")))
            if answer is None or answer.expected_outcome.canary is None:
                continue
            attempt_id = str(slot["attempt_id"])
            run_dir = round_dir / "runs" / attempt_id
            projection = _load(run_dir / "projection.json")
            hits = (
                projection.get("canary_hits")
                if isinstance(projection, Mapping)
                else None
            )
            if not isinstance(hits, Mapping):
                continue  # nothing was projected (e.g. setup_failed)
            out = run_dir / FILENAME
            if out.exists():
                raise RecheckError(f"RECHECK_EXISTS:{attempt_id}")
            counts = count_recording(
                recordings / f"eval-live-{round_id}-{attempt_id}",
                answer.expected_outcome.canary,
            )
            document = {
                "schema": SCHEMA,
                "rule": RULE,
                "report": hits.get("report"),
                **counts,
            }
            out.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.append({"attempt_id": attempt_id, **document})
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.eval_canary_recheck")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--round", action="append", required=True)
    parser.add_argument("--recordings", default="tmp/eval")
    args = parser.parse_args(argv)
    try:
        written = recheck(
            [Path(p) for p in args.round], Path(args.dataset), Path(args.recordings)
        )
    except RecheckError as exc:
        print(json.dumps({"exit_code": 1, "reason": exc.code}))
        return 1
    print(json.dumps({"exit_code": 0, "written": written}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
