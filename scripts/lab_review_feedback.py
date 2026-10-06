"""Write an independent-review verdict to LangSmith as feedback, and read it back.

ADR-0006 decision 3: review verdicts of a lab Run live next to its trace as
feedback (``review_p1``, ``review_p2``, ...), with the comment linking the
review record (PR thread, task record). The frozen ``summary.json`` of the
Run supplies the LangSmith run id and project; ``--record`` appends the
feedback id to that summary so the repository keeps the pointer.

    .venv/bin/python scripts/lab_review_feedback.py \\
        --summary docs/evidence/<experiment>/live-runs/<run_id>/summary.json \\
        --key review_p1 --verdict pass --review-url https://github.com/.../pull/N \\
        [--comment "..."] [--record]

Credentials come from ``LANGSMITH_*`` in the environment (``--env-file``
loads the unset ones by name from a private file); nothing is printed but
the read-back fields. Development script, not product code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.lab_evidence import langsmith_client  # noqa: E402
from scripts.m1_live_flash_loop import load_langsmith_env  # noqa: E402

# ``review_<item>``: one feedback per review item, lower-case identifiers.
KEY = re.compile(r"^review_[a-z0-9_]{1,40}$")
# pass -> 1, fail -> 0; ``insufficient`` has no score, only the value.
VERDICT_SCORE: dict[str, int | None] = {"pass": 1, "fail": 0, "insufficient": None}


def write_and_read_back(
    client: Any,
    *,
    run_id: str,
    key: str,
    verdict: str,
    review_url: str,
    comment: str | None,
) -> dict[str, Any]:
    """Create the feedback, read it back by id and return the comparable view."""
    text = (
        f"review record: {review_url}"
        if not comment
        else f"{comment}\nreview record: {review_url}"
    )
    created = client.create_feedback(
        run_id,
        key,
        score=VERDICT_SCORE[verdict],
        value=verdict,
        comment=text,
        source_info={"review_record": review_url},
    )
    read = client.read_feedback(created.id)
    view = {
        "feedback_id": str(read.id),
        "run_id": str(read.run_id),
        "key": read.key,
        "score": read.score,
        "value": read.value,
        "comment": read.comment,
    }
    expected = {
        "feedback_id": str(created.id),
        "run_id": run_id,
        "key": key,
        "score": VERDICT_SCORE[verdict],
        "value": verdict,
        "comment": text,
    }
    view["read_back_matches"] = view == {**view, **expected}
    return view


def record_in_summary(summary_path: Path, view: dict[str, Any]) -> None:
    """Append the feedback pointer to the Run's frozen summary."""
    summary = json.loads(summary_path.read_text())
    entries = summary.setdefault("review_feedback", [])
    entries.append(
        {k: view[k] for k in ("feedback_id", "key", "score", "value", "comment")}
    )
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n"
    )


def main(
    argv: list[str] | None = None, *, client_factory: Any = langsmith_client
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, help="frozen summary.json of the Run")
    parser.add_argument("--run-id", help="LangSmith run id (overrides --summary)")
    parser.add_argument("--key", required=True)
    parser.add_argument("--verdict", required=True, choices=sorted(VERDICT_SCORE))
    parser.add_argument("--review-url", required=True)
    parser.add_argument("--comment")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--env-file", type=Path, default=os.environ.get("M0_ENV_FILE"))
    args = parser.parse_args(argv)
    if not KEY.match(args.key):
        raise SystemExit("--key must match review_[a-z0-9_]+")
    run_id = args.run_id
    if run_id is None:
        if args.summary is None:
            raise SystemExit("one of --summary or --run-id is required")
        run_id = (
            json.loads(args.summary.read_text())
            .get("trace", {})
            .get("langsmith_run_id")
        )
        if not run_id:
            raise SystemExit("summary has no trace.langsmith_run_id")
    if args.record and args.summary is None:
        raise SystemExit("--record requires --summary")
    if args.env_file:
        load_langsmith_env(Path(args.env_file).expanduser())
    view = write_and_read_back(
        client_factory(),
        run_id=run_id,
        key=args.key,
        verdict=args.verdict,
        review_url=args.review_url,
        comment=args.comment,
    )
    if args.record:
        record_in_summary(args.summary, view)
    print(json.dumps(view, ensure_ascii=False, default=str))
    return 0 if view["read_back_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
