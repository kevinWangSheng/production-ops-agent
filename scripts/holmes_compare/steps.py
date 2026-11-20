"""Shift and de-shift steps the upstream-side plugin calls (#668, R10-1).

Runs in OpsPilot's interpreter so Holmes gets exactly the rule OpsPilot's
driver applies (``scripts.holmes_evals.shift.apply_shift`` and
``scripts.holmes_evals.text.deshift``)::

    python -m scripts.holmes_compare.steps shift --qid Q --question-file F \\
        --out F2 --record R          # env: ELASTICSEARCH_URL, ELASTICSEARCH_API_KEY
    python -m scripts.holmes_compare.steps deshift --record R --in F --out F2

The last stdout line is one JSON object (``status``, and ``code`` on error,
``applied``/``deshift_count`` on success). Nothing else is printed; no key is
ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.holmes_evals.text import Shift, deshift


def shift_from_record(record: Mapping[str, Any] | None) -> Shift | None:
    """PR-A's de-shift input from a frozen ``ShiftRecord.as_json()``."""
    if not record or not record.get("applied"):
        return None
    low, high = record.get("shifted_min"), record.get("shifted_max")
    if not isinstance(low, str) or not isinstance(high, str):
        return None
    return Shift(
        int(record["seconds"]),
        datetime.fromisoformat(low),
        datetime.fromisoformat(high),
    )


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), sort_keys=True), flush=True)


def _shift(args: argparse.Namespace) -> int:
    from scripts.holmes_evals.shift import EsAdmin, ShiftError, apply_shift

    url = os.environ.get("ELASTICSEARCH_URL", "")
    key = os.environ.get("ELASTICSEARCH_API_KEY", "")
    if not url or not key:
        _emit({"status": "error", "code": "ES_ENV_MISSING"})
        return 3
    question = args.question_file.read_text(encoding="utf-8")
    try:
        delivered, record = apply_shift(
            EsAdmin(url, key), args.qid, question, datetime.now(UTC)
        )
    except ShiftError as exc:
        _emit({"status": "error", "code": exc.code})
        return 3
    args.out.write_text(delivered, encoding="utf-8")
    args.record.write_text(json.dumps(record.as_json(), sort_keys=True) + "\n")
    _emit({"status": "ok", "applied": record.applied, "reason": record.reason})
    return 0


def _deshift(args: argparse.Namespace) -> int:
    record = (
        json.loads(args.record.read_text(encoding="utf-8"))
        if args.record.is_file()
        else None
    )
    text, count = deshift(
        args.input.read_text(encoding="utf-8"), shift_from_record(record)
    )
    args.out.write_text(text, encoding="utf-8")
    _emit({"status": "ok", "deshift_count": count})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("shift")
    s.add_argument("--qid", required=True)
    s.add_argument("--question-file", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--record", type=Path, required=True)
    d = sub.add_parser("deshift")
    d.add_argument("--record", type=Path, required=True)
    d.add_argument("--in", dest="input", type=Path, required=True)
    d.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return _shift(args) if args.command == "shift" else _deshift(args)


if __name__ == "__main__":
    sys.exit(main())
