"""CI structural replay of the frozen synthetic fixtures (eval PR-D1).

Interface: docs/tasks/2026-10-18-eval-pr-d-interface.md section 3. Each
fixture's recording is reloaded against the SHA its ``fixture.json`` declares
and replayed with ``mode="structural"`` and no investigator. Reads local files
only: no file is written, no environment variable or credential is read, and
no model client, executor or socket is created. Prints exactly one JSON object.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    # The entry writes no file, not even bytecode caches of what it imports.
    sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from collections import Counter  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from opspilot.evaluation import (  # noqa: E402
    AttemptRecord,
    Manifest,
    load_recording,
    replay,
)
from opspilot.evaluation.codec import StorageError, exact, parse  # noqa: E402
from opspilot.evaluation.recording import unwire  # noqa: E402

SCHEMA = "opspilot-eval-structural-ci-1"
FIXTURE_SCHEMA = "opspilot-eval-structural-fixture-1"
FIXTURE_KEYS = {
    "schema",
    "name",
    "recording_sha256",
    "attempt",
    "manifest",
    "expected_event_counts",
}
KINDS = ("model", "http", "tool", "delivery", "refusal")
MAX_FIXTURE_BYTES = 1024 * 1024
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "eval_structural"


class FixtureInvalid(Exception):
    pass


def _fixture(directory: Path) -> tuple[str, AttemptRecord, Manifest, dict[str, int]]:
    """Strictly parse ``fixture.json``; any deviation is FIXTURE_INVALID."""
    try:
        with (directory / "fixture.json").open("rb") as stream:
            raw = stream.read(MAX_FIXTURE_BYTES + 1)
        if len(raw) > MAX_FIXTURE_BYTES:
            raise FixtureInvalid
        obj = exact(parse(raw), FIXTURE_KEYS)
        name, digest, counts = (
            obj["name"],
            obj["recording_sha256"],
            obj["expected_event_counts"],
        )
        if (
            obj["schema"] != FIXTURE_SCHEMA
            or type(name) is not str
            or not re.fullmatch(r"[a-z0-9-]{1,64}", name)
            or name != directory.name
            or type(digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise FixtureInvalid
        exact(counts, set(KINDS))
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise FixtureInvalid
        attempt = unwire(AttemptRecord, obj["attempt"])
        manifest = unwire(Manifest, obj["manifest"])
        fixture = manifest.dimensions.get("replay_fixture")
        if (
            attempt.mode != "structural"
            or attempt.product_run_id is not None
            or fixture is None
            or fixture.kind != "hash"
            or fixture.value != digest
        ):
            raise FixtureInvalid
    except FixtureInvalid:
        raise
    except Exception:  # noqa: BLE001 - any parse failure is the same fixed code
        raise FixtureInvalid from None
    return digest, attempt, manifest, counts


def _verify(directory: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": directory.name,
        "recording_sha256": None,
        "loaded": False,
        "structural_verified": False,
        "counts_ok": False,
        "error": None,
    }
    try:
        digest, attempt, manifest, expected = _fixture(directory)
    except FixtureInvalid:
        item["error"] = "FIXTURE_INVALID"
        return item
    item["recording_sha256"] = digest
    try:
        recording = load_recording(
            directory / "recording" / "recording.json", expected_hash=digest
        )
        item["loaded"] = True
        result = replay(
            recording,
            mode="structural",
            attempt=attempt,
            manifest=manifest,
            investigator=None,
        )
        item["structural_verified"] = result.structural_verified is True
        if result.error is not None or not item["structural_verified"]:
            item["error"] = result.error or "VERIFY_FAILED"
            return item
        counts = Counter(str(event.kind) for event in recording.events)
        item["counts_ok"] = set(counts) <= set(KINDS) and all(
            counts.get(kind, 0) == expected[kind] for kind in KINDS
        )
        if not item["counts_ok"]:
            item["error"] = "EVENT_COUNTS_MISMATCH"
    except StorageError as exc:
        item["error"] = exc.code
    except Exception:  # noqa: BLE001 - every step failure is a fixed code
        item["error"] = "VERIFY_FAILED"
    return item


def verify(fixtures: Path) -> tuple[int, dict[str, Any]]:
    try:
        directories = sorted(
            (path for path in fixtures.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
    except OSError:
        directories = []
    if not directories:
        return 3, {
            "schema": SCHEMA,
            "status": "refused",
            "exit_code": 3,
            "reason": "NOTHING_TO_VERIFY",
            "results": [],
        }
    results = [_verify(directory) for directory in directories]
    ok = all(
        item["loaded"] and item["structural_verified"] and item["counts_ok"]
        for item in results
    )
    code = 0 if ok else 1
    return code, {
        "schema": SCHEMA,
        "status": "verified" if ok else "failed",
        "exit_code": code,
        "reason": None,
        "results": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args(argv)
    code, document = verify(args.fixtures)
    print(json.dumps(document, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
