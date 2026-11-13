"""Direct tool sampling for the rental batch evidence (#580); no model, no Run.

Some #580 acceptance items are hard to get reliably from a model Run: the
column query's output shape on a real cluster, and a Pod log view that says
it is incomplete when more than 100 lines match. This script calls the tools
itself, once per item, through the ``otel-demo-ext`` profile's own executor
factory (``select_profile(env).executor_factory``): the same registrations,
transports, size and time ceilings, lab-target selector and evidence
projection a worker uses. What it does not use is the durable layer: the
Run row, control state and evidence store are in-memory stand-ins
(``_SampleStore`` / ``_MemorySink``), because no incident or Run exists.

Configuration is the product's: ``OPSPILOT_K8S_*``,
``OPSPILOT_OTEL_PROMETHEUS_*``, ``OPSPILOT_OTEL_JAEGER_URL`` and
``OPSPILOT_OTEL_TOKEN`` from the environment; no credential is accepted on
the command line. Every data-source address must be a lab host (the
``*_NOT_LAB`` codes of ``opspilot.tracing.check_lab_target``).

Output under ``--evidence-dir``: one ``<item>.json`` per item (tool, params,
status, reason, source contact, the model view without its ``content``, a
content summary, evidence metadata, the deterministic verdict), plus
``result.json`` and ``run.md``. Raw source bytes are never written; Pod log
text is never written (line counts and the first rows' timestamp and
container only). Before anything is written, the serialized output is
searched for every configured credential value; one hit writes nothing.

Development script, not product code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from opspilot.persistence import Lease
from opspilot.tools.executor import ToolRequest
from opspilot.tools.otel_demo import OBSERVATION_SECONDS, TARGET_ID
from opspilot.tools.outcomes import EvidenceRecord, ToolOutcome, Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from opspilot.tracing import check_lab_target

PROFILE = "otel-demo-ext"
STEP_ID = "tool-sample"

#: Item name -> tool name; the order is the call order.
ITEMS: dict[str, str] = {
    "tabular": "kubernetes_tabular_query",
    "logs": "fetch_pod_logs",
    "secret": "kubernetes_jq_query",
    "traces": "traces_search_by_tags",
    "prom_instant": "execute_prometheus_instant_query",
    "rules": "list_prometheus_rules",
}

DEFAULT_COLUMNS = (
    "NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName,"
    "IMAGE:.spec.containers[*].image"
)
#: The tabular view keys #580 checks ("header/rows/count/scope/notes"; the
#: rows are the view's ``content``).
TABULAR_KEYS = ("header", "content", "count", "scope", "notes")
LOG_VIEW_LIMIT = 100
#: Credential variables whose values must never appear in the output.
SECRET_ENV = (
    "OPSPILOT_K8S_TOKEN",
    "OPSPILOT_OTEL_TOKEN",
    "OPSPILOT_OTEL_PROMETHEUS_PASSWORD",
)
_LAB_CODES = frozenset({"PROMETHEUS_NOT_LAB", "JAEGER_NOT_LAB", "KUBERNETES_NOT_LAB"})
_CELL_CHARS = 120
_PREVIEW_ROWS = 3


class SampleError(Exception):
    """A fixed code that stops the sample before any tool call."""


# -- in-memory stand-ins for the durable layer ------------------------------


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class _SampleStore:
    """The three ``DurableStore`` calls the factory's wiring makes.

    The control state is fixed at the lease's generations (nobody pauses a
    sample). Charges follow ``DurableStore.charge_tool``: keyed by
    ``dispatch_id``, the first charge is the reservation and a later one
    settles it; ``charges`` keeps one entry per dispatch as the audit.
    """

    def __init__(self, lease: Lease, deadline: datetime) -> None:
        self.lease = lease
        self.deadline = deadline
        self._charges: dict[str, dict[str, Any]] = {}

    @property
    def charges(self) -> list[dict[str, Any]]:
        return list(self._charges.values())

    def rebuild(self, incident_id: UUID) -> dict[str, Any]:
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": len(self._charges),
                "tool_seconds_used": sum(
                    item["seconds"] for item in self._charges.values()
                ),
            }
        }

    def charge_tool(
        self,
        lease: Lease,
        operation_id: str,
        seconds: float,
        *,
        max_operations: int,
        max_tool_seconds: float,
        dispatch_id: UUID,
    ) -> None:
        key = str(dispatch_id)
        entry = self._charges.get(key)
        if entry is None:
            self._charges[key] = {
                "operation_id": operation_id,
                "dispatch_id": key,
                "reserved_seconds": round(float(seconds), 3),
                "seconds": round(float(seconds), 3),
                "settled": False,
            }
        else:
            entry["seconds"] = round(float(seconds), 3)
            entry["settled"] = True

    def control_state(self, incident_id: UUID) -> dict[str, Any]:
        return {
            "incident_generation": self.lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": self.lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": self.lease.target_suspension_generation,
        }


class _MemorySink:
    """Evidence sink that keeps records in memory; nothing raw leaves it."""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def register(self, record: EvidenceRecord) -> str:
        self.records.append(record)
        return record.evidence_id


# -- inputs -----------------------------------------------------------------


@dataclass(frozen=True)
class SampleSpec:
    item: str
    tool: str
    params: dict[str, Any]


@dataclass
class SampleRun:
    window: Window
    specs: list[SampleSpec]
    evidence_dir: Path
    extra: dict[str, Any] = field(default_factory=dict)


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_moment(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SampleError(f"BAD_TIME: {text!r}") from exc
    if value.tzinfo is None:
        raise SampleError(f"TIME_WITHOUT_TIMEZONE: {text!r}")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def default_params(
    item: str, *, window: Window, namespace: str, args: argparse.Namespace
) -> dict[str, Any]:
    if item == "tabular":
        return {
            "kind": args.tabular_kind,
            "columns": args.tabular_columns,
            "namespace": namespace,
        }
    if item == "logs":
        if not args.log_pod:
            raise SampleError("LOG_POD_REQUIRED: --log-pod names the Pod to read")
        params: dict[str, Any] = {
            "pod_name": args.log_pod,
            "namespace": namespace,
            "start_time": _rfc3339(window.start),
            "end_time": _rfc3339(window.end),
        }
        if args.log_limit is not None:
            params["limit"] = args.log_limit
        return params
    if item == "secret":
        return {
            "kind": "secrets",
            "jq_expr": ".items[] | .metadata.name",
            "namespace": namespace,
        }
    if item == "traces":
        params = {"service": args.trace_service, "limit": args.trace_limit}
        if args.trace_tags:
            params["tags"] = args.trace_tags
        return params
    if item == "prom_instant":
        return {"query": args.prom_query}
    if item == "rules":
        return {}
    raise SampleError(f"UNKNOWN_ITEM: {item}")


def _override(params: dict[str, Any], assignment: str) -> None:
    key, sep, raw = assignment.partition("=")
    if not sep or not key:
        raise SampleError(f"BAD_SET: {assignment!r}")
    try:
        params[key] = json.loads(raw)
    except json.JSONDecodeError:
        params[key] = raw


# -- verdicts ---------------------------------------------------------------


def _check(checks: list[dict[str, Any]], name: str, ok: bool, observed: Any) -> None:
    checks.append({"check": name, "ok": bool(ok), "observed": observed})


def judge(
    item: str,
    outcome: ToolOutcome,
    *,
    params: Mapping[str, Any],
    charges: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic checks for one item; the item passes when all hold."""
    view = dict(outcome.model_view)
    content = view.get("content")
    checks: list[dict[str, Any]] = []
    if item == "tabular":
        _check(checks, "status == ok", outcome.status == "ok", outcome.status)
        present = [key for key in TABULAR_KEYS if key in view]
        _check(
            checks,
            "view has header/content(rows)/count/scope/notes",
            present == list(TABULAR_KEYS),
            present,
        )
        rows = content if isinstance(content, list) else None
        _check(
            checks,
            "rows is a list of lists",
            rows is not None and all(isinstance(row, list) for row in rows),
            type(content).__name__,
        )
        count = view.get("count")
        _check(
            checks,
            "count == len(rows)",
            rows is not None and count == len(rows),
            {"count": count, "rows": None if rows is None else len(rows)},
        )
        expected = [
            part.split(":", 1)[0].strip()
            for part in str(params.get("columns", "")).split(",")
            if part.strip()
        ]
        _check(
            checks,
            "header == requested column headers",
            view.get("header") == expected,
            {"header": view.get("header"), "requested": expected},
        )
        width = len(expected)
        _check(
            checks,
            "every row has one cell per column",
            rows is not None
            and all(isinstance(row, list) and len(row) == width for row in rows),
            sorted({len(row) for row in rows if isinstance(row, list)})
            if rows is not None
            else None,
        )
        _check(
            checks,
            "scope is an object and notes a list",
            isinstance(view.get("scope"), dict) and isinstance(view.get("notes"), list),
            {
                "scope": type(view.get("scope")).__name__,
                "notes": type(view.get("notes")).__name__,
            },
        )
    elif item == "logs":
        _check(checks, "status == ok", outcome.status == "ok", outcome.status)
        _check(
            checks,
            "incomplete is True",
            view.get("incomplete") is True,
            view.get("incomplete"),
        )
        omitted = view.get("lines_omitted")
        _check(
            checks,
            "lines_omitted > 0",
            isinstance(omitted, int) and omitted > 0,
            omitted,
        )
        shown = view.get("lines_shown")
        rows = content if isinstance(content, list) else []
        _check(
            checks,
            "lines_shown == len(rows) <= limit",
            shown == len(rows)
            and len(rows) <= int(params.get("limit", LOG_VIEW_LIMIT)),
            {"lines_shown": shown, "rows": len(rows)},
        )
        read = view.get("backend_lines_returned")
        _check(
            checks,
            "matching lines > shown lines",
            isinstance(read, int) and isinstance(shown, int) and read > shown,
            {"backend_lines_returned": read, "lines_shown": shown},
        )
    elif item == "secret":
        _check(
            checks,
            "reason == RESOURCE_NOT_ALLOWED",
            outcome.reason == "RESOURCE_NOT_ALLOWED",
            {"status": outcome.status, "reason": outcome.reason},
        )
        _check(
            checks,
            "source_contact == none",
            outcome.source_contact == "none",
            outcome.source_contact,
        )
        _check(
            checks,
            "no read dispatched (every reservation settled at 0 s)",
            all(item["settled"] and item["seconds"] == 0 for item in charges),
            [
                {"settled": item["settled"], "seconds": item["seconds"]}
                for item in charges
            ],
        )
        _check(checks, "no evidence recorded", outcome.evidence is None, None)
    elif item in {"traces", "prom_instant", "rules"}:
        _check(checks, "status == ok", outcome.status == "ok", outcome.status)
        count = view.get("result_count")
        _check(
            checks,
            "result_count >= 1",
            isinstance(count, int) and count >= 1,
            count,
        )
        _check(
            checks,
            "source_contact == confirmed",
            outcome.source_contact == "confirmed",
            outcome.source_contact,
        )
        _check(checks, "evidence adopted", outcome.adopted, outcome.adopted)
    return checks


# -- summaries (what is written) --------------------------------------------


def _cell(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= _CELL_CHARS else text[:_CELL_CHARS] + "..."


def content_summary(item: str, content: Any) -> dict[str, Any]:
    """What the written record keeps of the view's ``content``."""
    if not isinstance(content, list):
        return {"type": type(content).__name__}
    summary: dict[str, Any] = {"type": "list", "length": len(content)}
    if item == "logs":
        # Never the log text: timestamp, container and instance only.
        summary["first_rows"] = [
            {
                "timestamp": row.get("timestamp"),
                "container": row.get("container"),
                "previous": row.get("previous"),
            }
            for row in content[:_PREVIEW_ROWS]
            if isinstance(row, dict)
        ]
    elif item == "tabular":
        summary["first_rows"] = [
            [_cell(cell) for cell in row] if isinstance(row, list) else _cell(row)
            for row in content[:_PREVIEW_ROWS]
        ]
    else:
        summary["first_row_keys"] = [
            sorted(row) if isinstance(row, dict) else type(row).__name__
            for row in content[:_PREVIEW_ROWS]
        ]
    return summary


def evidence_meta(record: EvidenceRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "evidence_id": record.evidence_id,
        "status": record.status,
        "adopted": record.adopted,
        "raw_bytes": len(record.raw),
        "raw_sha256": record.raw_sha256,
        "view_sha256": record.view_sha256,
        "projection_revision": record.projection_revision,
        "observed_at": record.observed_at,
        "data_as_of": record.data_as_of,
        "source_start_at": record.source_start_at,
        "source_end_at": record.source_end_at,
        "result_count": record.result_count,
        "incomplete": record.incomplete,
        "truncated": record.truncated,
        "omitted_rows": record.omitted_rows,
        "omitted_bytes": record.omitted_bytes,
    }


# -- the run ----------------------------------------------------------------


def _lab_problems(env: Mapping[str, str]) -> list[str]:
    return sorted(set(check_lab_target(env)) & _LAB_CODES)


def run_sample(
    env: Mapping[str, str],
    run: SampleRun,
    *,
    clock: Any = None,
) -> dict[str, Any]:
    """Call each spec once; returns ``{"result": ..., "files": {name: text}}``.

    Raises ``SampleError`` for a configuration problem before any call.
    """
    env = {**env, PROFILE_ENV: PROFILE}
    problems = _lab_problems(env)
    if problems:
        raise SampleError("NOT_LAB: " + ",".join(problems))
    clock = clock or SystemClock()
    now = clock.now().astimezone(timezone.utc).replace(microsecond=0)
    frame_start = now - timedelta(seconds=OBSERVATION_SECONDS)
    if run.window.end > now or run.window.start < frame_start:
        raise SampleError(
            "WINDOW_OUTSIDE_FRAME: the window must lie within the last "
            f"{OBSERVATION_SECONDS // 3600} h ending now ({_rfc3339(now)})"
        )
    profile = select_profile(env)
    registered = {item["function"]["name"] for item in profile.face(clock).tool_schemas}
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=1,
        global_suspension_generation=1,
        target_suspension_generation=1,
    )
    store = _SampleStore(lease, deadline=now + timedelta(minutes=10))
    sink = _MemorySink()
    try:
        factory = profile.executor_factory(store, sink, clock)  # type: ignore[arg-type]
    except SystemExit as exc:
        raise SampleError(f"STARTUP: {exc.code}") from None
    face_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="direct tool sample (#580)",
        target_id=TARGET_ID,
        deadline=store.deadline,
        model_requests=1,
    )
    executor = factory(lease, face_input)

    items: dict[str, Any] = {}
    files: dict[str, str] = {}
    for index, spec in enumerate(run.specs):
        if spec.tool not in registered:
            record: dict[str, Any] = {
                "item": spec.item,
                "tool": spec.tool,
                "params": spec.params,
                "verdict": "skipped",
                "skip_reason": "TOOL_NOT_REGISTERED in this build",
            }
        else:
            known = {item["dispatch_id"] for item in store.charges}
            outcome = executor.execute(
                ToolRequest(
                    step_id=STEP_ID,
                    tool_index=index,
                    tool_name=spec.tool,
                    target_ref=TARGET_ID,
                    params=dict(spec.params),
                    window=run.window.as_json(),
                )
            )
            charges = [
                item for item in store.charges if item["dispatch_id"] not in known
            ]
            checks = judge(spec.item, outcome, params=spec.params, charges=charges)
            view = dict(outcome.model_view)
            content = view.pop("content", None)
            record = {
                "item": spec.item,
                "tool": spec.tool,
                "params": spec.params,
                "window": run.window.as_json(),
                "status": outcome.status,
                "reason": outcome.reason,
                "source_contact": outcome.source_contact,
                "adopted": outcome.adopted,
                "model_view_keys": sorted([*view, "content"]),
                "model_view": view,
                "content_summary": content_summary(spec.item, content),
                "evidence": evidence_meta(outcome.evidence),
                "ledger_charges": charges,
                "checks": checks,
                "verdict": "pass" if all(item["ok"] for item in checks) else "fail",
            }
        items[spec.item] = record
        files[f"{spec.item}.json"] = _dump(record)

    executed = [item for item in items.values() if item["verdict"] != "skipped"]
    result = {
        "type": "opspilot-tool-sample-v1",
        "issue": 580,
        "profile": PROFILE,
        "versions": profile.versions(),
        "sampled_at": now,
        "window": run.window.as_json(),
        "frame": {"start": frame_start, "end": now},
        "endpoints": {
            "kubernetes": env.get("OPSPILOT_K8S_API_URL"),
            "application_namespace": env.get("OPSPILOT_K8S_NAMESPACE"),
            "prometheus": env.get("OPSPILOT_OTEL_PROMETHEUS_URL"),
            "jaeger": env.get("OPSPILOT_OTEL_JAEGER_URL"),
        },
        "items": {
            name: {
                "tool": item["tool"],
                "verdict": item["verdict"],
                "status": item.get("status"),
                "reason": item.get("reason"),
                "failed_checks": [
                    check["check"]
                    for check in item.get("checks", [])
                    if not check["ok"]
                ],
            }
            for name, item in items.items()
        },
        "all_executed_passed": bool(executed)
        and all(item["verdict"] == "pass" for item in executed),
        "operations_charged": len(store.charges),
        "credential_hits": 0,
        **run.extra,
    }
    files["run.md"] = render_markdown(result, items)
    files["result.json"] = _dump(result)
    hits = credential_hits(env, files.values())
    if hits:
        raise SampleError(f"CREDENTIAL_IN_OUTPUT: {hits} hit(s); nothing written")
    return {"result": result, "files": files}


def credential_hits(env: Mapping[str, str], texts: Sequence[str] | Any) -> int:
    values = [env[name] for name in SECRET_ENV if env.get(name)]
    return sum(text.count(value) for text in texts for value in values)


def _dump(value: Any) -> str:
    return json.dumps(value, default=_json_default, indent=2, ensure_ascii=False) + "\n"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    return str(value)


def render_markdown(result: Mapping[str, Any], items: Mapping[str, Any]) -> str:
    lines = [
        "### 直接调工具取样（#580）",
        "",
        f"- 取样时刻：{_json_default(result['sampled_at'])}；"
        f"窗口：{result['window']['start']} -> {result['window']['end']}",
        f"- 档位：`{result['profile']}`，"
        f"`tool_schema_revision={result['versions'].get('tool_schema_revision')}`",
        f"- 记账操作数：{result['operations_charged']}；凭据原文命中：0",
        "",
        "| 项 | 工具 | status / reason | 判定 | 关键观察 |",
        "|---|---|---|---|---|",
    ]
    for name, item in items.items():
        if item["verdict"] == "skipped":
            lines.append(
                f"| {name} | `{item['tool']}` | - | skipped | {item['skip_reason']} |"
            )
            continue
        observed = "; ".join(
            f"{check['check']}: {json.dumps(check['observed'], default=str)}"
            for check in item["checks"]
        ).replace("|", "\\|")
        lines.append(
            f"| {name} | `{item['tool']}` | {item['status']} / {item['reason']} | "
            f"{item['verdict']} | {observed} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_files(evidence_dir: Path, files: Mapping[str, str]) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if (evidence_dir / "result.json").exists():
        raise SampleError(f"EVIDENCE_EXISTS: {evidence_dir / 'result.json'}")
    for name, text in files.items():
        (evidence_dir / name).write_text(text)


# -- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call otel-demo-ext tools directly (no model) for #580 evidence."
    )
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--window-start", help="ISO-8601 with timezone; default window-end - 1h"
    )
    parser.add_argument("--window-end", help="ISO-8601 with timezone; default now")
    parser.add_argument(
        "--items",
        default=",".join(ITEMS),
        help=f"comma-separated subset of: {', '.join(ITEMS)}",
    )
    parser.add_argument(
        "--namespace", help="default OPSPILOT_K8S_NAMESPACE (the application one)"
    )
    parser.add_argument("--tabular-kind", default="pods")
    parser.add_argument("--tabular-columns", default=DEFAULT_COLUMNS)
    parser.add_argument("--log-pod", help="Pod with more than 100 lines in the window")
    parser.add_argument("--log-limit", type=int)
    parser.add_argument("--trace-service", default="frontend")
    parser.add_argument("--trace-tags", help="logfmt, e.g. error=true")
    parser.add_argument("--trace-limit", type=int, default=5)
    parser.add_argument("--prom-query", default="up")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="ITEM.KEY=VALUE",
        help="override one parameter (VALUE parsed as JSON when it is JSON)",
    )
    return parser


def build_run(
    args: argparse.Namespace, env: Mapping[str, str], clock: Any
) -> SampleRun:
    end = (
        _parse_moment(args.window_end)
        if args.window_end
        else clock.now().astimezone(timezone.utc).replace(microsecond=0)
    )
    start = (
        _parse_moment(args.window_start)
        if args.window_start
        else end - timedelta(hours=1)
    )
    if start >= end:
        raise SampleError("BAD_WINDOW: start must be before end")
    window = Window(start, end)
    names = [name.strip() for name in args.items.split(",") if name.strip()]
    unknown = [name for name in names if name not in ITEMS]
    if unknown or not names:
        raise SampleError(f"UNKNOWN_ITEM: {','.join(unknown) or '(none)'}")
    namespace = args.namespace or env.get("OPSPILOT_K8S_NAMESPACE") or ""
    specs: list[SampleSpec] = []
    for name in names:
        params = default_params(name, window=window, namespace=namespace, args=args)
        for assignment in args.set:
            target, sep, rest = assignment.partition(".")
            if not sep:
                raise SampleError(f"BAD_SET: {assignment!r}")
            if target == name:
                _override(params, rest)
        specs.append(SampleSpec(item=name, tool=ITEMS[name], params=params))
    return SampleRun(window=window, specs=specs, evidence_dir=args.evidence_dir)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ if env is None else env)
    clock = clock or SystemClock()
    try:
        run = build_run(args, env, clock)
        produced = run_sample(env, run, clock=clock)
        write_files(run.evidence_dir, produced["files"])
    except SampleError as exc:
        out(f"tool_sample: {exc}")
        return 2
    result = produced["result"]
    for name, item in result["items"].items():
        out(f"{name}: {item['verdict']} ({item['status']}/{item['reason']})")
    out(f"evidence: {run.evidence_dir}")
    return 0 if result["all_executed_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
