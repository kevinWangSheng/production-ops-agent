"""The v2 development round summary (#432, V2-R14, Q20): engineer CLI.

Interface: ``~/.agent/handoffs/m3-b3-2026-11-02/seams-432.md`` section 5.

    .venv/bin/python -m scripts.eval_dev_round --dataset evals/datasets/dev-v2 \\
        --round docs/evidence/eval/<R1> [--round ...] \\
        --out docs/evidence/432-dev-round/summary.json

Offline and pure apart from reading the rounds' public evidence: every
scenario of the dataset gets exactly ``--n`` (2) slots; every started
attempt counts and none is drawn again (more than ``n`` started attempts of
one scenario is ``REDRAW_DETECTED``); a slot that never started counts as a
Q20 failure. Q20 is re-checked from each attempt's public ``projection.json``
and ``intake.json`` and must equal the stored ``expectations.json``
(``EXPECTATIONS_MISMATCH``). All rounds must share the code, dataset,
evaluator, runbook and upstream dimensions (``MANIFEST_DRIFT``); with
``--code-commit`` (repeated) the code dimension may differ only within those
commits, when ``opspilot/`` is identical across them, and the output's
``code_set`` discloses the commits and attempt counts. Regression
and capability groups are reported separately; the gate is a ratio only.

Registered reruns (seams-432 section 5a, ``--reruns``): an attempt that
failed in harness setup before any product Run existed may be voided by
exactly one later attempt of the same scenario. The voided attempt leaves
the slots and Q20 but stays in ``voided_attempts``, the cost and the
isolation check; the rerun counts as-is and is never voided in turn.

Duplicate-call refusal receipts (seams-432 section 2, F1 revision
2026-11-06): ``read_only`` no longer counts the loop's refusal to re-run an
exact repeat (``status: denied``, ``DUPLICATE_TOOL_CALL``, no tool). Stored
expectations computed before that may differ from the re-check only in
``read_only`` False -> True and ``WRITE_OBSERVED``; such a slot carries
``refusal_rechecked: true`` and its stored result in ``expectations_stored``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from opspilot.evaluation import REQUIRED_DIMENSIONS
from opspilot.evaluation.expectations import check_expectations, is_duplicate_refusal
from opspilot.evaluation.model import (
    AnswerV2,
    ScenarioSpecV2,
    dataset_tree_hash,
    load_scenarios_v2,
)
from scripts.eval_live import dataset_name
from scripts.eval_live_lab import span_error

SCHEMA = "opspilot-eval-dev-round-1"
RERUNS_SCHEMA = "opspilot-eval-dev-reruns-1"
VOIDED_NOTE = "environment not ready, rerun per rule"
CANARY_RECHECK = "canary-recheck.json"
CANARY_RECHECK_SCHEMA = "opspilot-eval-canary-recheck-1"
SLOTS_PER_SCENARIO = 2
#: F1 closure (U2): the CLI default for this round, three attempts per scenario.
CLI_SLOTS_PER_SCENARIO = 3
#: Void classes the paired schedule's ``check`` may name for an OpsPilot
#: attempt (``scripts.eval_pair_schedule.VOID_ACTIONS``). An entry without a
#: rule keeps the #432 rule: only a setup failure before any Run.
SCHEDULE_RULES = (
    "HOST_NOISE",
    "LAB_RESTARTED",
    "HARNESS_INTERRUPTED",
    "ENV_SETUP_FAILED",
    "PROVIDER_5XX_STORM",
)
#: Dimensions that must not move between the rounds of one development round.
FROZEN_DIMENSIONS = ("code", "dataset", "evaluator", "runbook", "upstream")
#: The product scope a registered code set must leave identical (F1 ruling
#: 2026-11-05, S1): driver commits may differ only outside it.
PRODUCT_SCOPE = "opspilot"
REPO_ROOT = Path(__file__).resolve().parents[1]


def git_product_tree(commit: str) -> tuple[str, str] | None:
    """``(full commit SHA, tree hash of PRODUCT_SCOPE)`` at ``commit`` in this
    repository; ``None`` when either cannot be resolved."""
    out: list[str] = []
    for rev in (f"{commit}^{{commit}}", f"{commit}^{{commit}}:{PRODUCT_SCOPE}"):
        try:
            done = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", rev],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0 or not done.stdout.strip():
            return None
        out.append(done.stdout.strip())
    return out[0], out[1]


ProductTree = Callable[[str], tuple[str, str] | None]


class DevRoundError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise DevRoundError(f"EVIDENCE_INVALID:{path.name}") from None


def upstream_applicability(spec: ScenarioSpecV2, answer: AnswerV2) -> str:
    """V2-R12: what the HolmesGPT comparison can cover for this scenario."""
    if spec.entry == "alertmanager":
        return "no_upstream_entry"
    if answer.category == "model_failure":
        return "same_proxy_injection"
    return "comparable"


def _dimensions(summary: Mapping[str, Any]) -> dict[str, Any]:
    dims = summary.get("capture", {}).get("manifest", {}).get("dimensions")
    if not isinstance(dims, Mapping):
        raise DevRoundError("EVIDENCE_INVALID:summary.json")
    return dict(dims)


def _binding(
    loaded: Mapping[str, tuple[ScenarioSpecV2, AnswerV2]], ids: set[str]
) -> list[str]:
    classes = set()
    for sid in ids:
        spec, answer = loaded[sid]
        source = next(
            (
                str(i.params.get("source"))
                for i in answer.injectors
                if i.kind == "source_unreachable"
            ),
            "none",
        )
        classes.add((spec.entry, source))
    if len(classes) != 1:
        raise DevRoundError("ROUND_BINDING_MIXED")
    return list(next(iter(classes)))


#: Per check: the reason code that goes with its failure.
_REVISABLE = {"canary_absent": "CANARY_PRESENT", "read_only": "WRITE_OBSERVED"}


def _same_expectations(
    stored: Mapping[str, Any],
    checked: Mapping[str, Any],
    *,
    canary_rechecked: bool,
    refusal_rechecked: bool = False,
) -> bool:
    """Stored and re-checked Q20 agree, up to two disclosed revisions:

    - after an outward-only canary recheck (section 5b), ``canary_absent``
      and what follows from it (``CANARY_PRESENT`` and ``passed``);
    - when the projection holds a duplicate-call refusal receipt (section 2,
      F1 revision 2026-11-06), ``read_only`` from False to True and its
      ``WRITE_OBSERVED`` -- the receipt executed nothing, and the stored
      result predates the rule that skips it.
    """
    if all(stored.get(k) == checked[k] for k in ("passed", "checks", "reasons")):
        return True
    old, new = stored.get("checks"), checked["checks"]
    if not isinstance(old, Mapping) or set(old) != set(new):
        return False
    revised = set()
    if canary_rechecked:
        revised.add("canary_absent")
    # Only an actual False -> True transition; anything else stays strict.
    if refusal_rechecked and old["read_only"] is False and new["read_only"] is True:
        revised.add("read_only")
    if not revised:
        return False
    if any(old[k] != new[k] for k in new if k not in revised):
        return False
    codes = {_REVISABLE[name] for name in revised}
    old_reasons = [r for r in stored.get("reasons") or [] if r not in codes]
    new_reasons = [r for r in checked["reasons"] if r not in codes]
    if old_reasons != new_reasons:
        return False
    return checked["passed"] == (
        not new_reasons and all(new[name] is not False for name in revised)
    )


def _has_duplicate_refusal(projection: Any) -> bool:
    if not isinstance(projection, Mapping):
        return False
    items = projection.get("tool_results") or []
    return any(isinstance(i, Mapping) and is_duplicate_refusal(i) for i in items)


def _attempt(
    run_dir: Path,
    slot: Mapping[str, Any],
    answer: AnswerV2,
    target_id: str,
) -> dict[str, Any]:
    summary = _json(run_dir / "summary.json")
    if not isinstance(summary, Mapping):
        raise DevRoundError("SUMMARY_MISSING")
    capture = summary.get("capture") or {}
    attempt = capture.get("attempt") or {}
    status = str(attempt.get("status") or slot.get("status"))
    projection = _json(run_dir / "projection.json")
    intake = _json(run_dir / "intake.json")
    stored = _json(run_dir / "expectations.json")
    recheck = _json(run_dir / CANARY_RECHECK)
    if recheck is not None:
        if (
            not isinstance(recheck, Mapping)
            or recheck.get("schema") != CANARY_RECHECK_SCHEMA
            or recheck.get("rule") != "outward-only"
            or not isinstance(projection, Mapping)
            or not all(
                isinstance(recheck.get(key), int) and not isinstance(recheck[key], bool)
                for key in ("report", "step_responses", "step_reasoning")
            )
        ):
            raise DevRoundError(f"EVIDENCE_INVALID:{CANARY_RECHECK}")
        # Section 5b: the report count is the projection's own; the recheck
        # only splits the projected step hits into outward and reasoning-only.
        original = projection.get("canary_hits")
        if (
            not isinstance(original, Mapping)
            or recheck["report"] != original.get("report")
            or recheck["step_responses"] + recheck["step_reasoning"]
            != original.get("step_responses")
        ):
            raise DevRoundError(f"EVIDENCE_INVALID:{CANARY_RECHECK}")
        projection = dict(projection) | {
            "canary_hits": {
                key: recheck[key]
                for key in ("report", "step_responses", "step_reasoning")
            }
        }
    checked = check_expectations(
        answer,
        attempt_status=status,
        attempt_reason=slot.get("reason"),
        projection=projection,
        intake=intake,
        target_id=target_id,
    ).as_json()
    refusal = _has_duplicate_refusal(projection)
    if not isinstance(stored, Mapping) or not _same_expectations(
        stored,
        checked,
        canary_rechecked=recheck is not None,
        refusal_rechecked=refusal,
    ):
        raise DevRoundError(f"EXPECTATIONS_MISMATCH:{run_dir.name}")
    revised = any(stored.get(k) != checked[k] for k in ("passed", "checks", "reasons"))
    gate = (summary.get("evaluation") or {}).get("gate") or {}
    traces = (summary.get("trace") or {}).get("entries") or []
    return {
        "started_at": capture.get("started_at"),
        "product_run_id": attempt.get("product_run_id"),
        "status": status,
        "reason": slot.get("reason"),
        "expectations": checked,
        "canary_rechecked": recheck is not None,
        "refusal_rechecked": revised
        and refusal
        and stored["checks"]["read_only"] != checked["checks"]["read_only"],
        "expectations_stored": None
        if recheck is None and not revised
        else {key: stored.get(key) for key in ("passed", "checks", "reasons")},
        "gate": {"passed": gate.get("passed"), "reasons": gate.get("reasons")},
        "trace_url": next(
            (t.get("url") for t in traces if isinstance(t, Mapping) and t.get("url")),
            None,
        ),
        "cost": capture.get("cost") or {},
        "dimensions": _dimensions(summary),
        "fault": _interval(slot),
        "views": _views(capture.get("delivered")),
    }


# -- A11/A12: attempt isolation in a shared lab ----------------------------------

#: Span-row views a prior fault can leak through (A13: a read_evidence slice
#: of a trace search carries the same rows).
_SPAN_TOOLS = ("traces_search",)


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _interval(slot: Mapping[str, Any]) -> dict[str, Any] | None:
    """[flag effective, normal confirmed] of an attempt that injected a flag
    (lifecycle ``fault``); the attempt's own start/end bound the rest."""
    fault = slot.get("fault") if isinstance(slot.get("fault"), Mapping) else None
    timings = slot.get("timings") if isinstance(slot.get("timings"), Mapping) else {}
    return {
        "started_at": timings.get("started_at"),
        "ended_at": timings.get("ended_at"),
        "effective_at": None if fault is None else fault.get("effective_at"),
        "restored_at": None if fault is None else fault.get("normal_confirmed_at"),
    }


def _views(delivered: Any) -> list[dict[str, Any]]:
    """What the isolation check needs of each delivered record: the tool,
    its window, the span rows of trace views and the view text."""
    out: list[dict[str, Any]] = []
    if not isinstance(delivered, Mapping):
        return out
    for record in delivered.values():
        if not isinstance(record, Mapping) or record.get("delivered") is not True:
            continue
        view = record.get("view") if isinstance(record.get("view"), Mapping) else {}
        tool = view.get("tool")
        span_view = tool in _SPAN_TOOLS or (
            tool == "read_evidence" and view.get("parent_tool") in _SPAN_TOOLS
        )
        rows = view.get("content") if span_view else None
        out.append(
            {
                "evidence_id": record.get("evidence_id"),
                "tool": tool,
                "span_view": span_view,
                "window": record.get("window"),
                "rows": [
                    {
                        "service": row.get("service"),
                        "operation": row.get("operation"),
                        "start_us": row.get("start_us"),
                        "error": span_error(row.get("status_tags") or {}),
                    }
                    for row in (rows if isinstance(rows, list) else [])
                    if isinstance(row, Mapping)
                ],
                "text": json.dumps(view, sort_keys=True, ensure_ascii=False),
            }
        )
    return out


def isolation(
    attempts: Sequence[Mapping[str, Any]],
    loaded: Mapping[str, tuple[ScenarioSpecV2, AnswerV2]],
) -> dict[str, dict[str, Any]]:
    """Per attempt id: its predecessor fault and the contamination flags
    (seams-432 §12 A11/A12). Pure; reads only what ``_attempt`` kept.

    * ``predecessor``: the latest earlier attempt (any round) that injected a
      flag or a synthetic span, with its scenario, end of fault and the gap
      in minutes to this attempt's start.
    * ``contamination``: ``PRIOR_FAULT_SPAN:<scenario>`` when a delivered
      span row matches an earlier attempt's signature, is an error, and
      starts inside that attempt's fault interval; ``PRIOR_INJECTION_TEXT:
      <scenario>`` when a delivered view contains an earlier attempt's
      injected span message.
    * ``metrics_overlap`` (informational, never a failure): earlier faults
      whose interval a delivered metrics view's window overlaps; the tools
      allow metrics windows up to the 24 h frame, so no wash removes them.

    Only attempts that changed the lab (a flagd injector or a synthetic
    span) count as earlier faults; an alert-only or model-transport-only
    attempt does not.
    """

    def start(item: Mapping[str, Any]) -> datetime:
        moment = _moment((item.get("fault") or {}).get("started_at"))
        return moment or datetime.max.replace(tzinfo=UTC)

    ordered = sorted(attempts, key=start)
    faults: list[dict[str, Any]] = []
    out: dict[str, dict[str, Any]] = {}
    for item in ordered:
        sid = str(item["scenario_id"])
        answer = loaded[sid][1]
        own = item.get("fault") or {}
        began = _moment(own.get("started_at"))
        prior = [f for f in faults if began is None or f["end"] <= began]
        predecessor = None
        if prior:
            last = max(prior, key=lambda f: f["end"])
            predecessor = {
                "scenario_id": last["scenario_id"],
                "attempt_id": last["attempt_id"],
                "fault_end": last["end"].isoformat(),
                "gap_minutes": None
                if began is None
                else round((began - last["end"]).total_seconds() / 60, 1),
                "injection": last["message"] is not None,
            }
        flags: set[str] = set()
        overlaps: set[str] = set()
        for view in item.get("views") or []:
            for fault in prior:
                if fault["message"] is not None and fault["message"] in view["text"]:
                    flags.add(f"PRIOR_INJECTION_TEXT:{fault['scenario_id']}")
                if view["span_view"]:
                    for row in view["rows"]:
                        if not row["error"] or not isinstance(row["start_us"], int):
                            continue
                        at = datetime.fromtimestamp(row["start_us"] / 1e6, tz=UTC)
                        if (row["service"], row["operation"]) in fault[
                            "wanted"
                        ] and fault["begin"] <= at <= fault["end"]:
                            flags.add(f"PRIOR_FAULT_SPAN:{fault['scenario_id']}")
                elif view["tool"] == "metrics_range_query":
                    window = view.get("window") or {}
                    lo, hi = _moment(window.get("start")), _moment(window.get("end"))
                    if lo and hi and lo <= fault["end"] and fault["begin"] <= hi:
                        overlaps.add(fault["scenario_id"])
        out[str(item["attempt_id"])] = {
            "predecessor": predecessor,
            "contamination": sorted(flags),
            "metrics_overlap": sorted(overlaps),
        }
        begin = _moment(own.get("effective_at"))
        end = _moment(own.get("restored_at")) or _moment(own.get("ended_at"))
        message = next(
            (
                str(i.params.get("message"))
                for i in answer.injectors
                if i.kind == "otlp_span"
            ),
            None,
        )
        lab_fault = message is not None or any(
            i.kind == "flagd" for i in answer.injectors
        )
        if begin is None and message is not None:
            begin = began
        if lab_fault and begin is not None and end is not None:
            wanted = (
                set()
                if answer.signature is None
                else {(m.service, m.operation) for m in answer.signature.any_of}
            )
            faults.append(
                {
                    "scenario_id": sid,
                    "attempt_id": item["attempt_id"],
                    "begin": begin,
                    "end": end,
                    "wanted": wanted,
                    "message": message,
                }
            )
    return out


def _registered_reruns(
    reruns: Any, started: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Validate the rerun registry (seams-432 section 5a) against the started
    attempts; returns voided attempt id -> its registry entry."""
    entries = reruns.get("reruns") if isinstance(reruns, Mapping) else None
    if (
        not isinstance(reruns, Mapping)
        or reruns.get("schema") != RERUNS_SCHEMA
        or not isinstance(entries, list)
    ):
        raise DevRoundError("RERUNS_INVALID")
    pairs: list[tuple[str, str, str | None]] = []
    rules: dict[str, str | None] = {}
    for entry in entries:
        voided = entry.get("voided_attempt_id") if isinstance(entry, Mapping) else None
        rerun = entry.get("rerun_attempt_id") if isinstance(entry, Mapping) else None
        note = entry.get("note") if isinstance(entry, Mapping) else None
        if (
            not isinstance(voided, str)
            or not voided
            or not isinstance(rerun, str)
            or not rerun
        ):
            raise DevRoundError("RERUNS_INVALID")
        if note is not None and not isinstance(note, str):
            raise DevRoundError("RERUNS_INVALID")
        rule = entry.get("rule")
        if rule is not None and rule not in SCHEDULE_RULES:
            raise DevRoundError("RERUNS_INVALID")
        rules[voided] = rule
        pairs.append((voided, rerun, note))
    voided_ids = [v for v, _, _ in pairs]
    rerun_ids = [r for _, r, _ in pairs]
    for ids in (voided_ids, rerun_ids):
        seen: set[str] = set()
        for attempt_id in ids:
            if attempt_id in seen:
                raise DevRoundError(f"RERUN_CHAINED:{attempt_id}")
            seen.add(attempt_id)
    for attempt_id in rerun_ids:
        if attempt_id in voided_ids:
            raise DevRoundError(f"RERUN_CHAINED:{attempt_id}")
    out: dict[str, dict[str, Any]] = {}
    for voided, rerun, note in pairs:
        for attempt_id in (voided, rerun):
            if attempt_id not in started:
                raise DevRoundError(f"RERUN_UNKNOWN:{attempt_id}")
        old, new = started[voided], started[rerun]
        rule = rules.get(voided)
        setup_only = rule is None or rule == "ENV_SETUP_FAILED"
        if setup_only and (
            old["status"] != "setup_failed" or old.get("product_run_id") is not None
        ):
            # the #432 rule; the schedule's other classes void a window
            # whatever this attempt's own status (they are registered
            # before the questions, evals/preregistered/)
            raise DevRoundError(f"RERUN_NOT_ELIGIBLE:{voided}")
        if old["scenario_id"] != new["scenario_id"]:
            raise DevRoundError(f"RERUN_SCENARIO_MISMATCH:{rerun}")
        ended = _moment((old.get("fault") or {}).get("ended_at"))
        began = _moment((new.get("fault") or {}).get("started_at"))
        if ended is None or began is None or began <= ended:
            raise DevRoundError(f"RERUN_ORDER:{rerun}")
        out[voided] = {"rerun_attempt_id": rerun, "note": note, "rule": rule}
    return out


def _code_set(
    dims_seen: Sequence[Mapping[str, Any]],
    code_commits: Sequence[str],
    product_tree: ProductTree,
) -> dict[str, Any]:
    """S1 (F1 ruling 2026-11-05): the ``code`` dimension may differ across
    attempts only within the registered ``code_commits``, and only when
    ``PRODUCT_SCOPE`` has one tree across all of them; anything else is
    ``MANIFEST_DRIFT:code``. Returns the disclosure of the accepted set."""
    registered: dict[str, str] = {}
    for commit in code_commits:
        resolved = product_tree(commit)
        if resolved is None:
            raise DevRoundError("MANIFEST_DRIFT:code")
        registered[resolved[0]] = resolved[1]
    trees = set(registered.values())
    if len(trees) != 1:
        raise DevRoundError("MANIFEST_DRIFT:code")
    counts: Counter[str] = Counter()
    for dims in dims_seen:
        code = dims.get("code")
        if not isinstance(code, Mapping) or code.get("kind") != "version":
            raise DevRoundError("MANIFEST_DRIFT:code")
        resolved = product_tree(str(code.get("value") or ""))
        if not code.get("value") or resolved is None or resolved[0] not in registered:
            raise DevRoundError("MANIFEST_DRIFT:code")
        counts[resolved[0]] += 1
    return {
        "scope": f"{PRODUCT_SCOPE}/",
        "product_tree": next(iter(trees)),
        "registered": sorted(registered),
        "attempts_by_commit": dict(sorted(counts.items())),
    }


def aggregate(
    round_dirs: Sequence[Path],
    dataset_root: Path,
    *,
    n: int = SLOTS_PER_SCENARIO,
    reruns: Mapping[str, Any] | None = None,
    code_commits: Sequence[str] | None = None,
    product_tree: ProductTree | None = None,
) -> dict[str, Any]:
    """The development round summary (seams-432 section 5).

    ``code_commits`` registers driver commits the ``code`` dimension may
    differ within (``_code_set``); ``None`` keeps every dimension frozen."""
    loaded = {
        spec.scenario_id: (spec, answer)
        for spec, answer in load_scenarios_v2(dataset_root)
    }
    dataset = f"{dataset_name(dataset_root)}:{dataset_tree_hash(dataset_root)}"
    rounds: list[dict[str, Any]] = []
    attempts: dict[str, list[dict[str, Any]]] = {sid: [] for sid in loaded}
    dims_seen: list[dict[str, Any]] = []
    for round_dir in round_dirs:
        lifecycle = _json(round_dir / "lifecycle.json")
        if not isinstance(lifecycle, Mapping):
            raise DevRoundError(f"LIFECYCLE_MISSING:{round_dir.name}")
        slots = [s for s in lifecycle.get("slots") or [] if isinstance(s, Mapping)]
        unknown = {str(s.get("scenario_id")) for s in slots} - set(loaded)
        if unknown:
            raise DevRoundError(f"UNKNOWN_SCENARIO:{sorted(unknown)[0]}")
        round_id = str(lifecycle.get("round"))
        rounds.append(
            {
                "round": round_id,
                "manifest_hash": lifecycle.get("manifest_hash"),
                "binding_class": _binding(
                    loaded, {str(s["scenario_id"]) for s in slots}
                )
                if slots
                else None,
                "lifecycle_status": lifecycle.get("round_status"),
                "exit_code": None,
                "reason": lifecycle.get("reason"),
            }
        )
        for slot in slots:
            attempt_id = slot.get("attempt_id")
            if not attempt_id:
                continue  # never started (ROUND_STOPPED): a resume may run it
            sid = str(slot["scenario_id"])
            spec, answer = loaded[sid]
            item = _attempt(
                round_dir / "runs" / str(attempt_id),
                slot,
                answer,
                spec.stimulus.target_id,
            )
            dims_seen.append(item.pop("dimensions"))
            attempts[sid].append(
                {
                    "round": round_id,
                    "attempt_id": attempt_id,
                    "scenario_id": sid,
                    **item,
                }
            )
    started = {str(a["attempt_id"]): a for items in attempts.values() for a in items}
    voided = {} if reruns is None else _registered_reruns(reruns, started)
    rerun_of = {str(v["rerun_attempt_id"]): k for k, v in voided.items()}
    for sid, items in attempts.items():
        if sum(1 for a in items if str(a["attempt_id"]) not in voided) > n:
            raise DevRoundError(f"REDRAW_DETECTED:{sid}")
    reference = dims_seen[0] if dims_seen else {}
    code_set = (
        None
        if code_commits is None
        else _code_set(dims_seen, code_commits, product_tree or git_product_tree)
    )
    for dims in dims_seen:
        for name in FROZEN_DIMENSIONS:
            if name == "code" and code_set is not None:
                continue  # checked by _code_set
            if dims.get(name) != reference.get(name):
                raise DevRoundError(f"MANIFEST_DRIFT:{name}")
    if reference and (reference.get("dataset") or {}).get("value") != dataset:
        raise DevRoundError("MANIFEST_DRIFT:dataset")
    isolated = isolation([a for items in attempts.values() for a in items], loaded)
    out_slots: list[dict[str, Any]] = []
    for sid in sorted(loaded):
        spec, answer = loaded[sid]
        items = sorted(
            (a for a in attempts[sid] if str(a["attempt_id"]) not in voided),
            key=lambda a: str(a.get("started_at")),
        )
        for index in range(n):
            base = {
                "scenario_id": sid,
                "group": "regression"
                if answer.category == "regression"
                else "capability",
                "category": answer.category,
                "polarity": answer.polarity,
                "entry": spec.entry,
                "slot": index + 1,
                "upstream": upstream_applicability(spec, answer),
            }
            if index < len(items):
                item = items[index]
                out_slots.append(
                    {
                        **base,
                        "round": item["round"],
                        "attempt_id": item["attempt_id"],
                        "status": item["status"],
                        "reason": item["reason"],
                        "expectations": item["expectations"],
                        "canary_rechecked": item["canary_rechecked"],
                        "refusal_rechecked": item["refusal_rechecked"],
                        "expectations_stored": item["expectations_stored"],
                        "gate": item["gate"],
                        "trace_url": item["trace_url"],
                        "rerun_of": rerun_of.get(str(item["attempt_id"])),
                        **isolated[str(item["attempt_id"])],
                    }
                )
            else:
                out_slots.append(
                    {
                        **base,
                        "round": None,
                        "attempt_id": None,
                        "status": "not_started",
                        "reason": "NOT_STARTED",
                        "expectations": {
                            "passed": False,
                            "checks": {},
                            "reasons": ["NOT_STARTED"],
                        },
                        "canary_rechecked": False,
                        "refusal_rechecked": False,
                        "expectations_stored": None,
                        "gate": {"passed": None, "reasons": None},
                        "trace_url": None,
                        "rerun_of": None,
                        "predecessor": None,
                        "contamination": [],
                        "metrics_overlap": [],
                    }
                )
    totals: dict[str, dict[str, int]] = {}
    for group in ("regression", "capability"):
        mine = [s for s in out_slots if s["group"] == group]
        totals[group] = {
            "slots": len(mine),
            "started": sum(1 for s in mine if s["attempt_id"] is not None),
            "q20_passed": sum(1 for s in mine if s["expectations"]["passed"] is True),
            "gate_passed": sum(1 for s in mine if s["gate"]["passed"] is True),
        }
    failed = [
        {
            "scenario_id": s["scenario_id"],
            "slot": s["slot"],
            "reasons": s["expectations"]["reasons"],
        }
        for s in out_slots
        if s["expectations"]["passed"] is not True
    ]
    deltas: list[Decimal] = []
    unknown_cost = 0
    for items in attempts.values():
        for item in items:
            cost = item.get("cost") or {}
            try:
                deltas.append(Decimal(str(cost["delta_cny"])))
            except (KeyError, InvalidOperation, TypeError):
                unknown_cost += 1
    code = reference.get("code") or {}
    return {
        "schema": SCHEMA,
        "dataset": dataset,
        "code": code.get("value"),
        # S1 disclosure: present only when a code set was registered
        **({} if code_set is None else {"code_set": code_set}),
        "rounds": rounds,
        "step3": {
            name: reference.get(name) for name in ("runbook", "dataset", "upstream")
        }
        | {
            "missing_dimensions": sorted(set(REQUIRED_DIMENSIONS) - set(reference))
            if reference
            else sorted(REQUIRED_DIMENSIONS)
        },
        "slots": out_slots,
        "voided_attempts": [
            {
                "round": started[attempt_id]["round"],
                "attempt_id": started[attempt_id]["attempt_id"],
                "scenario_id": started[attempt_id]["scenario_id"],
                "status": started[attempt_id]["status"],
                "reason": started[attempt_id]["reason"],
                "rerun_attempt_id": entry["rerun_attempt_id"],
                "note": VOIDED_NOTE if entry["rule"] is None else entry["note"],
                # a schedule void names its preregistered rule (absent: #432)
                **({} if entry["rule"] is None else {"rule": entry["rule"]}),
            }
            for attempt_id, entry in sorted(voided.items())
        ],
        "totals": totals,
        "f1_step2": {
            "achieved": not failed and bool(out_slots),
            "failed_slots": failed,
        },
        "cost": {
            "measured_delta_cny": str(sum(deltas, Decimal(0))),
            "attempts_measured": len(deltas),
            "attempts_unknown": unknown_cost,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.eval_dev_round")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--round", action="append", required=True)
    parser.add_argument("--n", type=int, default=CLI_SLOTS_PER_SCENARIO)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reruns")
    parser.add_argument(
        "--code-commit",
        action="append",
        dest="code_commits",
        help="register a driver commit the code dimension may differ within "
        "(repeat; product scope must be identical across them)",
    )
    args = parser.parse_args(argv)
    try:
        reruns = None
        if args.reruns is not None:
            try:
                reruns = _json(Path(args.reruns))
            except DevRoundError:
                reruns = None
            if reruns is None:
                raise DevRoundError("RERUNS_INVALID")
        document = aggregate(
            [Path(p) for p in args.round],
            Path(args.dataset),
            n=args.n,
            reruns=reruns,
            code_commits=args.code_commits,
        )
    except DevRoundError as exc:
        print(json.dumps({"exit_code": 1, "reason": exc.code}))
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "exit_code": 0,
                "out": str(out),
                "f1_step2_achieved": document["f1_step2"]["achieved"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
