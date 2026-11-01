"""Read back one product-mode Run from LangSmith (#428 evidence, OB-R13).

Run by the operator (``! .venv/bin/python -m scripts.outbox_trace_check ...``)
or by the evidence step; never by a reviewer prompt. Pattern of
``docs/evidence/m1-pg-roles-live/trace-review-2026-10-12.md``: the key is
loaded from the private env file into this process only and used solely by
``scripts.lab_evidence.langsmith_client`` (endpoint checked first). Nothing
printed or written contains the key: only ids, names, counts, fixed codes,
token numbers, the platform's cost estimate and links.

Inputs:

* ``--env-file``: the worker's private env file (``worker_env``). Only
  ``LANGSMITH_API_KEY``, ``LANGSMITH_PROJECT``, ``LANGSMITH_ENDPOINT`` and
  ``LANGSMITH_WORKSPACE_ID`` are read from it.
* ``--rows``: a JSON list exported from the outbox table for this Run
  (``[{"kind", "idempotency_key", "run_id", "state", "attempts"}...]``; no
  payload needed), from which the expected trace and span ids are derived
  with the same functions the worker uses.
* ``--incident-id``, ``--run-id``, optional ``--image-revision``.

Exit status 0 only when every check passes; otherwise 1, with the failed
check codes in ``failures`` (``ROOT_MISSING``, ``SPANS_MISSING``,
``SPANS_DUPLICATED`` unless ``--allow-duplicates``,
``INCIDENT_FILTER_EMPTY``, ``VERSION_FILTER_EMPTY``, ``FORBIDDEN_CONTENT``,
``CONTENT_PRESENT``, ``METADATA_OUTSIDE_ALLOWLIST``) and on stderr.

What it reports (JSON on stdout, or ``--out``):

* filter checks: runs found in the project by ``incident_id`` metadata, and
  by ``incident_id`` + ``image_revision``;
* the trace: run counts by ``langsmith.span.kind``, llm token sums,
  ``ls_provider`` / ``ls_model_name`` values, tool status classes and reason
  codes, run states, the root run's ``total_cost`` (platform estimate; the
  authority is the balance difference, Q17) and the root run link;
* identity: for every outbox row, whether a run with the derived id exists,
  and how many runs in the trace share its (name, step/state) identity --
  more than one means LangSmith did not deduplicate a redelivered span
  (contract "未确认" item 1);
* the root ``opspilot.run``: found or not, and how many runs carry its name
  (re-sent on every stop, item 2: a late root is acceptable if it is found
  with its children attached);
* a content scan of every returned run (inputs, outputs, metadata, extra):
  counts of forbidden markers and of the key's literal value, never values.

Assumption (未确认 until the first real read, from the 2026-10-12 review
where run ``00000000-0000-0000-edf0-819d41422b6d`` was the OTel span
``edf0819d41422b6d``): LangSmith names an OTLP-ingested run by its 8-byte
span id zero-padded into a UUID, and its trace by the root run's id. When a
derived id is not found the report says so instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from opspilot.outbox_delivery import root_span_id_for, span_id_for, trace_id_for
from opspilot.persistence.outbox import PAYLOAD_KEYS
from opspilot.worker_main import _read_env_file
from scripts.lab_evidence import langsmith_client

ENV_KEYS = (
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_WORKSPACE_ID",
)
# Lower-case markers that never belong in a product-mode run (Q14). Plain
# "prompt" / "query" are not markers: ``prompt_revision`` and
# ``prompt_tokens`` are allowlisted; content is caught by the non-empty
# inputs/outputs count and the metadata key check instead.
FORBIDDEN_MARKERS = (
    "reasoning_content",
    "x-api-key",
    "authorization",
    "bearer ",
    "gen_ai.prompt",
    "gen_ai.completion",
    "promql",
)
_LIST_LIMIT = 100
# What a product-mode run's metadata may hold: the Q14 payload keys, the two
# cost keys this project sets, and what LangSmith adds itself (``ls_*`` run
# keys and OTel/SDK bookkeeping are counted separately below, by prefix).
ALLOWED_METADATA = frozenset(PAYLOAD_KEYS) | {"ls_provider", "ls_model_name"}
_PLATFORM_PREFIXES = ("ls_", "LANGSMITH_", "revision_id", "otel.", "__")


def load_env(path: Path) -> dict[str, str]:
    """The four LangSmith settings from the private file; nothing else."""
    env = {}
    for name in ENV_KEYS:
        value = _read_env_file(path, name)
        if value:
            env[name] = value
    return env


def run_uuid(span_id: int) -> UUID:
    """The LangSmith run id assumed for an OTLP span id (see module doc)."""
    return UUID(int=span_id)


def expected_ids(rows: Sequence[Mapping[str, Any]], run_id: UUID) -> dict[str, Any]:
    spans = {
        row["idempotency_key"]: str(run_uuid(span_id_for(row["idempotency_key"])))
        for row in rows
    }
    otel_trace = {
        format(
            trace_id_for(row["kind"], _uuid(row.get("run_id")), row["idempotency_key"]),
            "032x",
        )
        for row in rows
    }
    return {
        "root_run_id": str(run_uuid(root_span_id_for(run_id))),
        "otel_trace_ids": sorted(otel_trace),
        "span_run_ids": spans,
    }


def _uuid(value: object) -> UUID | None:
    return None if value in (None, "") else UUID(str(value))


def _dump(run: Any) -> dict[str, Any]:
    if hasattr(run, "model_dump"):
        return dict(run.model_dump(mode="json"))
    return dict(run.dict())


def _metadata(run: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = run.get("extra") or {}
    return extra.get("metadata") or {}


def content_scan(runs: Iterable[Mapping[str, Any]], secret: str) -> dict[str, int]:
    """Marker hit counts over each run's inputs, outputs, metadata and extra."""
    hits: Counter[str] = Counter()
    for run in runs:
        blob = json.dumps(
            {k: run.get(k) for k in ("inputs", "outputs", "extra", "error", "events")},
            default=str,
        )
        lowered = blob.lower()
        for marker in FORBIDDEN_MARKERS:
            hits[marker] += lowered.count(marker)
        if secret:
            hits["api_key_literal"] += blob.count(secret)
        hits["runs_with_inputs"] += 1 if run.get("inputs") else 0
        hits["runs_with_outputs"] += 1 if run.get("outputs") else 0
        extra_keys = {
            key
            for key in _metadata(run)
            if key not in ALLOWED_METADATA and not key.startswith(_PLATFORM_PREFIXES)
        }
        hits["metadata_keys_outside_allowlist"] += len(extra_keys)
    return dict(hits)


def summarize_trace(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    names: Counter[str] = Counter()
    tool_status: Counter[str] = Counter()
    tool_reason: Counter[str] = Counter()
    run_states: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    models: Counter[str] = Counter()
    tokens = Counter[str]()
    for run in runs:
        meta = _metadata(run)
        kinds[str(run.get("run_type"))] += 1
        names[str(run.get("name"))] += 1
        for key, counter in (
            ("tool_status_class", tool_status),
            ("tool_reason", tool_reason),
            ("run_state", run_states),
            ("ls_provider", providers),
            ("ls_model_name", models),
        ):
            if meta.get(key) is not None:
                counter[str(meta[key])] += 1
        if run.get("run_type") == "llm":
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                tokens[field] += int(run.get(field) or 0)
    return {
        "run_types": dict(kinds),
        "names": dict(names),
        "llm_tokens": dict(tokens),
        "ls_provider": dict(providers),
        "ls_model_name": dict(models),
        "tool_status_class": dict(tool_status),
        "tool_reason": dict(tool_reason),
        "run_state": dict(run_states),
    }


def identity_check(
    runs: Sequence[Mapping[str, Any]], expected: Mapping[str, str]
) -> dict[str, Any]:
    """Per outbox row: is its derived run id present, and how many runs share
    its identity (name + step_id/run_state/attempt_id metadata)."""
    by_id = {str(run.get("id")): run for run in runs}

    def identity(run: Mapping[str, Any]) -> tuple[str, ...]:
        meta = _metadata(run)
        return (
            str(run.get("name")),
            str(meta.get("step_id")),
            str(meta.get("run_state")),
            str(meta.get("attempt_id")),
            str(run.get("end_time")),
        )

    shared = Counter(identity(run) for run in runs)
    rows: dict[str, dict[str, Any]] = {}
    for key, run_id in expected.items():
        run = by_id.get(run_id)
        rows[key] = {
            "derived_run_id": run_id,
            "found": run is not None,
            "same_identity_count": shared[identity(run)] if run is not None else 0,
        }
    found = sum(1 for row in rows.values() if row["found"])
    return {
        "rows": rows,
        "found": found,
        "missing": len(rows) - found,
        "duplicated": sum(1 for row in rows.values() if row["same_identity_count"] > 1),
    }


def _metadata_filter(fields: Mapping[str, str]) -> str:
    clauses = [f"has(metadata, '{json.dumps({k: v})}')" for k, v in fields.items()]
    return clauses[0] if len(clauses) == 1 else f"and({', '.join(clauses)})"


def check(
    client: Any,
    *,
    project: str,
    incident_id: str,
    run_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    image_revision: str | None,
    secret: str,
) -> dict[str, Any]:
    expected = expected_ids(rows, run_id)
    by_incident = [
        _dump(run)
        for run in client.list_runs(
            project_name=project,
            filter=_metadata_filter({"incident_id": incident_id}),
            limit=_LIST_LIMIT,
        )
    ]
    by_version: list[dict[str, Any]] | None = None
    if image_revision:
        by_version = [
            _dump(run)
            for run in client.list_runs(
                project_name=project,
                filter=_metadata_filter(
                    {"incident_id": incident_id, "image_revision": image_revision}
                ),
                limit=_LIST_LIMIT,
            )
        ]
    root_id = expected["root_run_id"]
    root: dict[str, Any] | None
    root_error: str | None
    try:
        root = _dump(client.read_run(root_id))
    except Exception as exc:  # noqa: BLE001 - reported, never raised with a value
        root = None
        root_error = type(exc).__name__
    else:
        root_error = None
    trace = (
        [_dump(run) for run in client.list_runs(trace_id=root_id, limit=_LIST_LIMIT)]
        if root is not None
        else by_incident
    )
    link = None
    if root is not None:
        try:
            link = client.get_run_url(
                run=client.read_run(root_id), project_name=project
            )
            # a share-free project link; it carries ids only
        except Exception:  # noqa: BLE001
            link = None
    return {
        "project": project,
        "incident_id": incident_id,
        "run_id": str(run_id),
        "expected": {
            "root_run_id": root_id,
            "otel_trace_ids": expected["otel_trace_ids"],
            "outbox_rows": len(rows),
        },
        "filters": {
            "by_incident_id": len(by_incident),
            "by_incident_id_and_image_revision": None
            if by_version is None
            else len(by_version),
        },
        "root": {
            "found": root is not None,
            "error": root_error,
            "name": None if root is None else root.get("name"),
            "status": None if root is None else root.get("status"),
            "total_cost_estimate": None if root is None else root.get("total_cost"),
            "total_tokens": None if root is None else root.get("total_tokens"),
            "link": link,
        },
        "trace": {"runs": len(trace), **summarize_trace(trace)},
        "identity": identity_check(trace, expected["span_run_ids"]),
        "content_scan": content_scan(trace, secret),
        "cost_note": "LangSmith estimate; the authority is the supplier balance difference (Q17)",
    }


#: Exit status when any check fails (``failures`` lists which).
EXIT_FAILED = 1


def failures(report: Mapping[str, Any], *, allow_duplicates: bool = False) -> list[str]:
    """Fixed codes for every check the report fails; empty means pass.

    ``allow_duplicates`` is for the worker-restart readback (OB-R13 fault 2),
    where a duplicated span is the observation being recorded, not a defect.
    """
    found: list[str] = []
    if not report["root"]["found"]:
        found.append("ROOT_MISSING")
    if report["identity"]["missing"]:
        found.append("SPANS_MISSING")
    if report["identity"]["duplicated"] and not allow_duplicates:
        found.append("SPANS_DUPLICATED")
    if not report["filters"]["by_incident_id"]:
        found.append("INCIDENT_FILTER_EMPTY")
    if report["filters"]["by_incident_id_and_image_revision"] == 0:
        found.append("VERSION_FILTER_EMPTY")
    scan = report["content_scan"]
    if any(scan.get(marker, 0) for marker in (*FORBIDDEN_MARKERS, "api_key_literal")):
        found.append("FORBIDDEN_CONTENT")
    if scan.get("runs_with_inputs", 0) or scan.get("runs_with_outputs", 0):
        found.append("CONTENT_PRESENT")
    if scan.get("metadata_keys_outside_allowlist", 0):
        found.append("METADATA_OUTSIDE_ALLOWLIST")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--image-revision")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="record duplicated spans without failing (worker-restart readback)",
    )
    args = parser.parse_args(argv)
    env = load_env(args.env_file.expanduser())
    if not env.get("LANGSMITH_PROJECT", "").startswith("opspilot-lab-"):
        raise SystemExit(
            "LANGSMITH_PROJECT in the env file is not an opspilot-lab-* project"
        )
    client = langsmith_client(env)  # refuses an unlisted endpoint or a missing key
    rows = json.loads(args.rows.read_text())
    report = check(
        client,
        project=env["LANGSMITH_PROJECT"],
        incident_id=args.incident_id,
        run_id=args.run_id,
        rows=rows,
        image_revision=args.image_revision,
        secret=env.get("LANGSMITH_API_KEY", ""),
    )
    report["failures"] = failures(report, allow_duplicates=args.allow_duplicates)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if env.get("LANGSMITH_API_KEY") and env["LANGSMITH_API_KEY"] in text:
        raise SystemExit("refusing to print: the report contains the key")
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    if report["failures"]:
        print(
            "outbox trace check failed: " + ",".join(report["failures"]),
            file=sys.stderr,
        )
        return EXIT_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())
