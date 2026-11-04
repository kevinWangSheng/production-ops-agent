"""HolmesGPT on the v2 dataset's question-entry scenarios (F1 closure S7,
prereview 2026-11-04 section 2.2 S7, V2-R12, R15).

    python -m scripts.holmes_baseline.dataset run --dataset evals/datasets/dev-v2 \\
        --scenario dv2-c39f60 --attempt-id <id> --slot 1 [--round <name>]
    python -m scripts.holmes_baseline.dataset summarize --dataset <root> \\
        --attempts <raw dir> [--n 3] --out <summary.json> \\
        [--judge-inputs <holmes-inputs.json>]

The acceptance entry points (``select_scenarios``, ``holmes_plan``,
``result_row``) are re-exported by ``scripts.eval_holmes_dataset``; the
paired schedule (S7c) calls ``run_attempt`` in process.

``run_attempt`` is one attempt inside a window the caller already holds: the
paired schedule (R7) injects and restores the lab-side injectors (flagd
flags, OTLP spans) and waits for them to settle; this runner never touches
the lab. It applies the two Holmes-side injectors itself, the way OpsPilot's
harness does process-locally:

* ``source_unreachable``: the Holmes config points that source at the same
  loopback URL as OpsPilot (``eval_live_lab.UNREACHABLE_URLS``), with
  ``127.0.0.1`` in ``NO_PROXY`` so the refusal is the container's own (as in
  the F14 step 3 repro cases).
* ``model_transport``: the Holmes egress proxy answers the model API CONNECT
  with ``503 Service Unavailable`` (``relay.py --inject-503``): every CONNECT
  for ``always_503``, only the first for ``first_503``. The API is HTTPS
  through a CONNECT tunnel, so the 503 is the proxy's answer to CONNECT, not
  an HTTP response of the API; nothing reaches the provider while injected.

Same question and window as OpsPilot (``stimulus.question``, the last
``WINDOW_SECONDS`` before submission), same read-only proxy account and
tools (8 Prometheus tools + GET-only ``jaeger_request``, no new toolset), the
frozen OpsPilot budgets (``config.py``, R15). Alert-entry scenarios have no
upstream entry (V2-R12): ``holmes_plan`` refuses them and ``summarize``
lists them as not applicable, outside every denominator.

The attempt directory (git-ignored) keeps the raw Holmes output
(``out/holmes.json``, what the judge reads); ``summary.json`` there holds
counts, hashes, checks and the projected outcome, never report text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opspilot.evaluation.model import (
    AnswerV2,
    ScenarioSpecV2,
    dataset_tree_hash,
    load_scenarios_v2,
)
from scripts import kind_lab
from scripts.eval_dev_round import upstream_applicability
from scripts.eval_live_lab import (
    UNREACHABLE_URLS,
    WINDOW_SECONDS,
    LabError,
    validate_injectors,
)
from scripts.holmes_baseline import config as hconfig
from scripts.holmes_baseline.project import project_outcome

ATTEMPT_SCHEMA = "opspilot-holmes-attempt-2"
ROUND_SCHEMA = "opspilot-holmes-dataset-round-1"
JUDGE_INPUTS_SCHEMA = "opspilot-holmes-judge-inputs-1"
SYSTEM = "holmes"
NOT_APPLICABLE = "no_upstream_entry"
SUMMARY_FILE = "summary.json"
RAW_FILE = "out/holmes.json"
SLOTS_PER_SCENARIO = 3  # U2: three runs per scenario
#: The lab URLs the Holmes container reaches through this lab instance's
#: relay (same names as ``run.RELAY``/``run.PROMETHEUS_URL``/...).
SUFFIX = getattr(kind_lab, "SUFFIX", "")
RELAY = f"f14-lab-relay{SUFFIX}"
CAPTURE = f"f14-capture{SUFFIX}"
PROMETHEUS_URL = f"http://{RELAY}:9090"
JAEGER_URL = f"http://{RELAY}:16686/jaeger/ui"
#: Instance 0's URLs: the ``upstream`` dimension hashes these, so rounds
#: spread over parallel labs share one dimension.
CANONICAL_PROMETHEUS_URL = "http://f14-lab-relay:9090"
CANONICAL_JAEGER_URL = "http://f14-lab-relay:16686/jaeger/ui"
LOOPBACK = "127.0.0.1"
INJECT_503 = {"always_503": "always", "first_503": "first"}
#: Lab-side injectors the caller applies before ``run_attempt``.
LAB_SIDE_KINDS = frozenset({"flagd", "otlp_span"})


class DatasetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InjectionPlan:
    """The Holmes-side part of one scenario (pure, from the answer side;
    only the runner and the summary see it, never the model)."""

    scenario_id: str
    entry: str
    upstream: str
    source: str | None
    model_mode: str | None
    lab_side: tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return self.upstream != NOT_APPLICABLE

    def prometheus_url(self) -> str:
        return (
            UNREACHABLE_URLS["prometheus"]
            if self.source == "prometheus"
            else PROMETHEUS_URL
        )

    def jaeger_url(self) -> str:
        return UNREACHABLE_URLS["jaeger"] if self.source == "jaeger" else JAEGER_URL

    def no_proxy(self, base: Sequence[str]) -> str:
        hosts = list(base)
        if self.source is not None:
            # the refused connection must be the container's own, not the
            # egress proxy turning the loopback target away
            hosts.append(LOOPBACK)
        return ",".join(hosts)

    def egress_extra(self) -> list[str]:
        if self.model_mode is None:
            return []
        return ["--inject-503", INJECT_503[self.model_mode]]


def injection_plan(spec: ScenarioSpecV2, answer: AnswerV2) -> InjectionPlan:
    """The injector whitelist is OpsPilot's (``validate_injectors``), so a
    scenario the OpsPilot driver refuses is refused here too."""
    try:
        validated = validate_injectors(spec.entry, answer.injectors)
    except LabError as exc:
        raise DatasetError(f"INJECTOR_INVALID:{exc}") from None
    return InjectionPlan(
        scenario_id=spec.scenario_id,
        entry=spec.entry,
        upstream=upstream_applicability(spec, answer),
        source=validated.source,
        model_mode=validated.model_mode,
        lab_side=tuple(
            str(i["kind"]) for i in validated.injectors if i["kind"] in LAB_SIDE_KINDS
        ),
    )


# --- acceptance entry points (re-exported by scripts.eval_holmes_dataset)


def select_scenarios(
    loaded: Sequence[tuple[ScenarioSpecV2, AnswerV2]],
) -> list[tuple[ScenarioSpecV2, AnswerV2]]:
    """The scenarios Holmes runs: question entry only, input order kept."""
    return [(spec, answer) for spec, answer in loaded if spec.entry == "question"]


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def prompt(question: str, start: datetime, end: datetime) -> str:
    """The F14 run1 prompt shape with the scenario's own question."""
    return f"{question}\nInvestigation window (UTC): {utc(start)} to {utc(end)}."


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def base_config() -> dict[str, Any]:
    return hconfig.holmes_config(CANONICAL_PROMETHEUS_URL, CANONICAL_JAEGER_URL)


def upstream_dimension() -> dict[str, Any]:
    """V2-R7: the baseline round's ``upstream`` dimension: the pinned commit
    and the attempt-independent base config. A harness-side fault URL is an
    attempt override (recorded per attempt), not a new dimension; the image
    ID is recorded per attempt and must not change within a round."""
    return {
        "kind": "hash",
        "value": f"holmesgpt@{hconfig.UPSTREAM_COMMIT} "
        f"config={canonical_sha256(base_config())}",
        "reason": None,
    }


def holmes_plan(
    spec: ScenarioSpecV2, answer: AnswerV2, *, submitted_at: datetime
) -> dict[str, Any]:
    """One Holmes Run of one scenario submitted at ``submitted_at`` (aware
    UTC). ``ValueError`` for an alertmanager-entry scenario."""
    if spec.entry != "question":
        raise ValueError(f"{spec.scenario_id}: no upstream entry (V2-R12)")
    injection = injection_plan(spec, answer)
    start = submitted_at - timedelta(seconds=WINDOW_SECONDS)
    return {
        "scenario_id": spec.scenario_id,
        "prompt": prompt(spec.stimulus.question, start, submitted_at),
        "window": {"start": start, "end": submitted_at},
        "config": hconfig.holmes_config(
            injection.prometheus_url(), injection.jaeger_url()
        ),
        "model_env": dict(hconfig.MODEL_ENV),
        "wall_seconds": hconfig.RUN_WALL_SECONDS,
        "model_transport": injection.model_mode,
        "source_unreachable": injection.source,
        "egress_extra": injection.egress_extra(),
        "no_proxy": injection.no_proxy([RELAY]),
        "lab_side_by_caller": list(injection.lab_side),
        "upstream": upstream_dimension(),
    }


def read_only(output: Mapping[str, Any], proxy_log_lines: Sequence[str]) -> bool:
    """Q11 (Holmes): every request of the ``holmes`` account in the lab proxy
    log is a GET, and every Jaeger tool call Holmes records is an HTTP GET."""
    for line in proxy_log_lines:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "holmes" and fields[2] != "GET":
            return False
    for call in output.get("tool_calls") or []:
        description = call.get("description") if isinstance(call, Mapping) else None
        if isinstance(description, str) and description.startswith("HTTP "):
            if not description.startswith("HTTP GET "):
                return False
    return True


def report_text(output: Mapping[str, Any]) -> str | None:
    """Holmes' final answer (``result``); never messages or reasoning."""
    result = output.get("result")
    return result if isinstance(result, str) and result.strip() else None


def result_row(
    spec: ScenarioSpecV2,
    answer: AnswerV2,
    *,
    attempt_id: str,
    plan: Mapping[str, Any],
    holmes_output: Mapping[str, Any],
    proxy_log_lines: Sequence[str],
) -> dict[str, Any]:
    """The upstream row of one Holmes attempt (S7a summary)."""
    outcome = project_outcome(holmes_output, proxy_log_lines)
    return {
        "system": SYSTEM,
        "scenario_id": spec.scenario_id,
        "attempt_id": attempt_id,
        "final_state": outcome["final_state"],
        "report_text": report_text(holmes_output),
        "checks": {
            "read_only": read_only(holmes_output, proxy_log_lines),
            "completed": outcome["final_state"] == "completed",
        },
        "upstream": dict(plan["upstream"]),
        "outcome": outcome,
        # informational: Holmes' terminal state against the answer's allowed
        # set; the two judges are the main score (U3)
        "final_state_expected": outcome["final_state"]
        in answer.expected_outcome.final_states,
    }


# --- attempt summary (pure)


def budget() -> dict[str, Any]:
    """R15: the frozen OpsPilot values the config carries."""
    return {
        "max_steps": hconfig.MAX_STEPS,
        "run_wall_seconds": hconfig.RUN_WALL_SECONDS,
        "tool_timeout_seconds": hconfig.TOOL_TIMEOUT_SECONDS,
        "max_output_tokens": int(hconfig.MODEL_ENV["OVERRIDE_MAX_OUTPUT_TOKEN"]),
        "step_counting_note": (
            "Holmes does not count compaction or retries as steps; "
            "OpsPilot counts every model request"
        ),
    }


def egress_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    connects = [e for e in entries if e.get("mode") == "connect" and "client" in e]
    return {
        "allowed": sum(1 for e in connects if e.get("allowed")),
        "injected_503": sum(1 for e in connects if e.get("injected")),
        "denied": [
            e.get("reason")
            for e in connects
            if not e.get("allowed") and not e.get("injected")
        ],
        "targets": sorted({str(e["target"]) for e in connects if e.get("target")}),
    }


def tool_rows(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for call in output.get("tool_calls") or []:
        result = call.get("result") or {}
        rows.append({"tool": call.get("tool_name"), "status": result.get("status")})
    return rows


def attempt_summary(
    *,
    row: Mapping[str, Any],
    plan: Mapping[str, Any],
    slot: int | None,
    round_id: str | None,
    mode: str,
    output: Mapping[str, Any],
    proxy_lines: Sequence[str],
    egress: Sequence[Mapping[str, Any]],
    started: float,
    finished: float,
    image_id: str | None,
    raw_sha256: str | None,
    pre_run: Mapping[str, Any] | None,
    balance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The attempt's ``summary.json``: counts, hashes, checks and the
    projected outcome; the report only by hash."""
    report = row["report_text"]
    proxy_counts = Counter(
        " ".join(line.split()[2:5]) for line in proxy_lines if len(line.split()) >= 5
    )
    rows = tool_rows(output)
    return {
        "schema": ATTEMPT_SCHEMA,
        "system": SYSTEM,
        "mode": mode,
        "attempt_id": row["attempt_id"],
        "slot": slot,
        "round": round_id,
        "scenario_id": row["scenario_id"],
        "injection": {
            "source_unreachable": plan["source_unreachable"],
            "model_transport": plan["model_transport"],
            "lab_side_by_caller": list(plan["lab_side_by_caller"]),
        },
        "final_state": row["final_state"],
        "checks": dict(row["checks"]),
        "final_state_expected": row["final_state_expected"],
        "outcome": dict(row["outcome"]),
        "started_at": utc(datetime.fromtimestamp(started, UTC)),
        "finished_at": utc(datetime.fromtimestamp(finished, UTC)),
        "started_ts": started,
        "wall_seconds": round(finished - started, 1),
        "window": {k: utc(v) for k, v in plan["window"].items()},
        "prompt_sha256": hashlib.sha256(plan["prompt"].encode()).hexdigest(),
        "exit_code": output.get("exit_code"),
        "timed_out": output.get("timed_out"),
        "finish_reason": output.get("finish_reason"),
        "usage": {
            k: output.get(k)
            for k in (
                "num_llm_calls",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
                "num_compactions",
            )
        },
        "tool_calls": rows,
        "tool_errors": sum(1 for r in rows if r["status"] != "success"),
        "proxy_requests_holmes": dict(sorted(proxy_counts.items())),
        "egress": egress_summary(egress),
        "pre_run": dict(pre_run) if pre_run is not None else None,
        "budget": budget(),
        "versions": {
            "upstream": dict(plan["upstream"]),
            "image_id": image_id,
            "attempt_config_sha256": canonical_sha256(plan["config"]),
        },
        "raw": {"file": RAW_FILE if raw_sha256 else None, "sha256": raw_sha256},
        "report_sha256": hashlib.sha256(report.encode()).hexdigest()
        if report is not None
        else None,
        "balance": dict(balance) if balance is not None else None,
    }


# --- summarize (pure apart from reading the attempt directories)


def _load_attempts(attempts_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    if not attempts_root.is_dir():
        raise DatasetError("ATTEMPTS_ROOT_MISSING")
    for path in sorted(attempts_root.glob(f"*/{SUMMARY_FILE}")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DatasetError(f"EVIDENCE_INVALID:{path.parent.name}") from None
        if not isinstance(data, dict) or data.get("schema") != ATTEMPT_SCHEMA:
            continue  # other raw directories (F14 runs, checks) share the root
        found.append((path.parent, data))
    return found


def _group(answer: AnswerV2) -> str:
    return "regression" if answer.category == "regression" else "capability"


def _raw_path(directory: Path, data: Mapping[str, Any]) -> Path | None:
    raw = data["raw"]
    if raw["sha256"] is None:
        return None
    path = directory / str(raw["file"])
    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != (raw["sha256"])
    ):
        raise DatasetError(f"RAW_MISMATCH:{directory.name}")
    return path


def summarize(
    dataset_root: Path,
    attempts_root: Path,
    *,
    n: int = SLOTS_PER_SCENARIO,
    rounds: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The Holmes rows in the dev-round slot shape (``eval_dev_round``):
    every scenario gets ``n`` slots; alert-entry scenarios are
    ``not_applicable`` and outside every denominator; every started attempt
    counts, more than ``n`` is ``REDRAW_DETECTED``; a slot that never started
    counts as not started (in the denominator). Slots are numbered by start
    time. ``rounds`` limits the attempts read to those rounds."""
    loaded = {s.scenario_id: (s, a) for s, a in load_scenarios_v2(dataset_root)}
    plans = {sid: injection_plan(s, a) for sid, (s, a) in loaded.items()}
    by_scenario: dict[str, list[tuple[Path, dict[str, Any]]]] = {
        sid: [] for sid in loaded
    }
    upstream_seen: set[str] = set()
    images_seen: set[str] = set()
    for directory, data in _load_attempts(attempts_root):
        if rounds is not None and data.get("round") not in rounds:
            continue
        sid = str(data.get("scenario_id"))
        if sid not in loaded:
            raise DatasetError(f"UNKNOWN_SCENARIO:{sid}")
        if not plans[sid].applicable:
            raise DatasetError(f"NOT_APPLICABLE_ATTEMPT:{sid}")
        if data.get("mode") != "real_model":
            raise DatasetError(f"ATTEMPT_NOT_REAL:{data.get('attempt_id')}")
        if data.get("attempt_id") != directory.name:
            raise DatasetError(f"EVIDENCE_INVALID:{directory.name}")
        upstream_seen.add(json.dumps(data["versions"]["upstream"], sort_keys=True))
        images_seen.add(str(data["versions"]["image_id"]))
        by_scenario[sid].append((directory, data))
    if len(upstream_seen) > 1:
        raise DatasetError("MANIFEST_DRIFT:upstream")
    if len(images_seen) > 1:
        raise DatasetError("MANIFEST_DRIFT:image")
    for sid, items in by_scenario.items():
        if len(items) > n:
            raise DatasetError(f"REDRAW_DETECTED:{sid}")
    slots: list[dict[str, Any]] = []
    for sid in sorted(loaded):
        spec, answer = loaded[sid]
        applicable = plans[sid].applicable
        items = sorted(by_scenario[sid], key=lambda item: float(item[1]["started_ts"]))
        for index in range(n):
            base: dict[str, Any] = {
                "system": SYSTEM,
                "scenario_id": sid,
                "group": _group(answer),
                "category": answer.category,
                "polarity": answer.polarity,
                "entry": spec.entry,
                "slot": index + 1,
                "upstream": plans[sid].upstream,
                "in_denominator": applicable,
            }
            if not applicable:
                slots.append(
                    {
                        **base,
                        "status": "not_applicable",
                        "reason": "NO_UPSTREAM_ENTRY",
                        "attempt_id": None,
                    }
                )
                continue
            if index >= len(items):
                slots.append(
                    {
                        **base,
                        "status": "not_started",
                        "reason": "NOT_STARTED",
                        "attempt_id": None,
                        "round": None,
                        "final_state": None,
                        "checks": {"read_only": None, "completed": False},
                        "final_state_expected": False,
                        "raw": None,
                    }
                )
                continue
            directory, data = items[index]
            raw = _raw_path(directory, data)
            slots.append(
                {
                    **base,
                    "status": "ran",
                    "reason": None,
                    "attempt_id": data["attempt_id"],
                    "round": data.get("round"),
                    "started_at": data["started_at"],
                    "final_state": data["final_state"],
                    "checks": data["checks"],
                    # S7a reads this per started slot (Q11, Holmes side)
                    "read_only": bool(data["checks"]["read_only"]),
                    "provider_error": bool(data.get("provider_error")),
                    "final_state_expected": data["final_state_expected"],
                    "raw": None if raw is None else f"{directory.name}/{RAW_FILE}",
                    "report_available": data["report_sha256"] is not None,
                    "tool_calls": data["outcome"]["tool_calls"],
                    "tool_errors": data["tool_errors"],
                    "model_calls_ok": data["outcome"]["model_calls_ok"],
                    "egress_injected_503": data["egress"]["injected_503"],
                    "wall_seconds": data["wall_seconds"],
                }
            )
    totals: dict[str, dict[str, int]] = {}
    for group in ("regression", "capability"):
        mine = [s for s in slots if s["group"] == group]
        counted = [s for s in mine if s["in_denominator"]]
        totals[group] = {
            "slots": len(counted),
            "not_applicable": len(mine) - len(counted),
            "started": sum(1 for s in counted if s["attempt_id"] is not None),
            "completed": sum(1 for s in counted if s["checks"]["completed"] is True),
            "read_only": sum(1 for s in counted if s["checks"]["read_only"] is True),
            "final_state_expected": sum(
                1 for s in counted if s["final_state_expected"]
            ),
        }
    return {
        "schema": ROUND_SCHEMA,
        "system": SYSTEM,
        "dataset": f"{dataset_root.name}:{dataset_tree_hash(dataset_root)}",
        "upstream": json.loads(next(iter(upstream_seen))) if upstream_seen else None,
        "image_id": next(iter(images_seen)) if images_seen else None,
        "n": n,
        "budget": budget(),
        "slots": slots,
        "totals": totals,
        "disclosure": [
            "Holmes gets raw Jaeger JSON (25k-token tool result cap) through a "
            "GET-only http toolset; OpsPilot's trace tool is a projected view "
            "(prereview section 1 C)",
            "model_transport 503 is the egress proxy's answer to CONNECT, "
            "not an HTTP 503 of the API (TLS tunnel); LiteLLM reports it as "
            "'DeepseekException - 503 Service Unavailable'",
            "an unreachable Prometheus fails Holmes' toolset check at startup, "
            "so its 8 tools are not offered at all (upstream behaviour); an "
            "unreachable Jaeger keeps jaeger_request, whose calls fail",
            "unconfirmed: whether Holmes/LiteLLM would retry a real HTTP 503 "
            "from the API; the CONNECT 503 was not retried (first_503 not "
            "run live)",
        ],
    }


def judge_inputs(
    document: Mapping[str, Any], attempts_root: Path, out: Path
) -> dict[str, Any]:
    """S7a's ``--holmes`` input: one entry per started attempt, the raw
    Holmes output path relative to ``out``'s directory (``None`` when the
    attempt produced none)."""
    entries = []
    for slot in document["slots"]:
        if slot["status"] != "ran":
            continue
        raw = slot["raw"]
        entries.append(
            {
                "scenario_id": slot["scenario_id"],
                "attempt_id": slot["attempt_id"],
                "slot": slot["slot"],
                "holmes_json": None
                if raw is None
                else os.path.relpath(
                    (attempts_root / raw).resolve(), out.parent.resolve()
                ),
                "read_only": bool(slot["checks"]["read_only"]),
            }
        )
    return {"schema": JUDGE_INPUTS_SCHEMA, "attempts": entries}


# --- live attempt (docker, lab relay, model API)

#: Model provider failures in Holmes' error text (LiteLLM exception names,
#: HTTP 5xx, transport breaks); a tool's own error never ends ``holmes ask``.
PROVIDER_ERROR_MARKERS = (
    "InternalServerError",
    "ServiceUnavailableError",
    "APIConnectionError",
    "Timeout",
    "Service Unavailable",
    "Bad Gateway",
    "Gateway Timeout",
    "Connection error",
    "RemoteProtocolError",
)


def provider_error(
    final_state: str,
    output: Mapping[str, Any],
    egress: Sequence[Mapping[str, Any]],
) -> bool:
    """The Run ended on a model provider 5xx or transport failure that was
    not injected (PROVIDER_5XX_STORM, R2): Holmes failed, its error names a
    provider failure, and the last model CONNECT through the egress proxy was
    not the injected 503 (an injected 503 Holmes does not retry is the
    scenario, not the provider). Completed or budget-exhausted Runs never
    count."""
    if final_state != "failed":
        return False
    error = output.get("error")
    if not isinstance(error, str):
        return False
    # only the fatal exception at the end of the log, not earlier tool noise
    # (a refused Jaeger call also says "Connection error")
    tail = "\n".join([line for line in error.splitlines() if line.strip()][-3:])
    if not any(marker in tail for marker in PROVIDER_ERROR_MARKERS):
        return False
    connects = [
        e
        for e in egress
        if e.get("mode") == "connect"
        and "client" in e
        and e.get("target") == f"{hconfig.MODEL_HOST}:443"
    ]
    return not (connects and connects[-1].get("injected"))


ProxyLog = Callable[[float], list[str]]


def run_attempt(
    spec: ScenarioSpecV2,
    answer: AnswerV2,
    *,
    attempt_id: str,
    slot: int | None,
    raw_root: Path,
    deadline: float | None = None,
    round_id: str | None = None,
    capture_model: bool = False,
    kind_node: str | None = None,
    proxy_log: ProxyLog | None = None,
    lab_checks: bool = True,
    submit_not_after: float | None = None,
    auth_file: Path | None = None,
) -> dict[str, Any]:
    """One Holmes attempt for the paired schedule (S7c), in process.

    The caller holds the lab window and has applied the lab-side injectors.
    ``deadline`` (epoch seconds) caps the Run below the frozen wall budget;
    ``kind_node`` names the kind control-plane container (default: the
    ``kind_lab`` cluster); ``proxy_log(since)`` returns the lab proxy's log
    lines for the ``holmes`` account (default ``run.proxy_log_lines``, the
    ``kind_lab`` kubeconfig). ``submit_not_after`` (epoch seconds) is checked
    once setup is done, right before Holmes starts: later is
    ``setup_failed``/``SUBMIT_LATE``; it never caps the Run. ``auth_file``
    overrides the lab proxy's account file (default: this process's lab
    instance, ``kind_lab.AUTH_FILE`` read at call time). Returns ``status``
    ``ran`` | ``setup_failed`` | ``aborted`` with ``reason``, times, the attempt directory and the raw
    output path/sha256; ``setup_failed`` means no Holmes container started.
    """
    import yaml

    from scripts.holmes_baseline import run as hrun

    directory = raw_root / attempt_id

    def result(status: str, reason: str | None, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "attempt_id": attempt_id,
            "slot": slot,
            "attempt_dir": str(directory),
            "started_at": None,
            "ended_at": None,
            "holmes_json": None,
            "holmes_json_sha256": None,
            "summary": None,
            "provider_error": False,
            **extra,
        }

    if spec.entry != "question":
        return result("setup_failed", "NOT_APPLICABLE")
    if directory.exists():
        return result("setup_failed", "ATTEMPT_EXISTS")
    wall = float(hconfig.RUN_WALL_SECONDS)
    capped = False
    if deadline is not None:
        remaining = deadline - time.time()
        if remaining <= 0:
            return result("aborted", "DEADLINE_PASSED")
        if remaining < wall:
            wall, capped = remaining, True
    pre_run: dict[str, Any] | None = None
    try:
        if lab_checks and not capture_model:
            rate = hrun.frontend_span_rate()
            skew = hrun.clock_skew()
            pre_run = {"frontend_span_rate": rate, "clock_skew": skew}
            if rate <= 0:
                return result("setup_failed", "SPAN_RATE_ZERO", pre_run=pre_run)
            if max(abs(skew["host_vm"]), abs(skew["host_container"])) > (
                hrun.MAX_CLOCK_SKEW_SECONDS
            ):
                return result("setup_failed", "CLOCK_SKEW", pre_run=pre_run)
        key = "sk-capture-dummy" if capture_model else hrun.model_key()
        balance_before = None if capture_model else hrun.balance(key)
        image_id = json.loads(hrun.docker("image", "inspect", hrun.IMAGE))[0]["Id"]
        injection = injection_plan(spec, answer)
        hrun.setup(
            capture=capture_model,
            egress_extra=injection.egress_extra(),
            kind_node=kind_node or hrun.KIND_NODE,
        )
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - reported as setup
        hrun.teardown()
        return result("setup_failed", f"SETUP:{type(exc).__name__}")
    if submit_not_after is not None and time.time() > submit_not_after:
        hrun.teardown()
        return result("setup_failed", "SUBMIT_LATE", pre_run=pre_run)
    plan = holmes_plan(spec, answer, submitted_at=datetime.now(UTC))
    content = dict(plan["config"])
    no_proxy = injection.no_proxy([hrun.RELAY])
    if capture_model:
        # offline smoke: the fake endpoint answers once; the egress proxy and
        # its 503 injection are not on this path
        content["api_base"] = f"http://{hrun.CAPTURE}:8000"
        no_proxy = injection.no_proxy([hrun.RELAY, hrun.CAPTURE])
    directory.mkdir(parents=True)
    config_path = directory / "config.yaml"
    config_path.write_text(yaml.safe_dump(content, sort_keys=False))
    config_path.chmod(0o644)
    try:
        run = hrun.run_holmes(
            directory,
            config_path,
            plan["prompt"],
            hrun.holmes_secret_env(key, auth_file),
            no_proxy=no_proxy,
            wall_seconds=wall,
        )
        egress = hrun.egress_log(hrun.EGRESS)
    finally:
        hrun.teardown()
    reader = proxy_log or hrun.proxy_log_lines
    proxy_lines = [] if capture_model else reader(run["started"])
    output = run["output"]
    row = result_row(
        spec,
        answer,
        attempt_id=attempt_id,
        plan=plan,
        holmes_output=output,
        proxy_log_lines=proxy_lines,
    )
    summary = attempt_summary(
        row=row,
        plan=plan,
        slot=slot,
        round_id=round_id,
        mode="capture_model" if capture_model else "real_model",
        output=output,
        proxy_lines=proxy_lines,
        egress=egress,
        started=run["started"],
        finished=run["finished"],
        image_id=image_id,
        raw_sha256=run["raw_sha256"],
        pre_run=pre_run,
        balance=None
        if capture_model
        else {
            "source": f"GET https://{hconfig.MODEL_HOST}/user/balance",
            "before": balance_before,
            "after": hrun.balance(key),
        },
    )
    summary["provider_error"] = provider_error(row["final_state"], output, egress)
    summary_path = directory / SUMMARY_FILE
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    killed_by_deadline = capped and bool(output.get("timed_out"))
    return result(
        "aborted" if killed_by_deadline else "ran",
        "DEADLINE" if killed_by_deadline else None,
        started_at=summary["started_at"],
        ended_at=summary["finished_at"],
        holmes_json=str(directory / RAW_FILE) if run["raw_sha256"] else None,
        holmes_json_sha256=run["raw_sha256"],
        summary=str(summary_path),
        final_state=row["final_state"],
        checks=row["checks"],
        provider_error=summary["provider_error"],
    )


def _scenario(dataset_root: Path, scenario_id: str) -> tuple[ScenarioSpecV2, AnswerV2]:
    for spec, answer in load_scenarios_v2(dataset_root):
        if spec.scenario_id == scenario_id:
            return spec, answer
    raise DatasetError(f"UNKNOWN_SCENARIO:{scenario_id}")


def cmd_run(args: argparse.Namespace) -> int:
    from scripts.holmes_baseline import run as hrun

    spec, answer = _scenario(Path(args.dataset), args.scenario)
    outcome = run_attempt(
        spec,
        answer,
        attempt_id=args.attempt_id,
        slot=args.slot,
        raw_root=Path(args.raw_root) if args.raw_root else hrun.RAW_ROOT,
        deadline=args.deadline,
        round_id=args.round,
        capture_model=args.capture_model,
        kind_node=args.kind_node,
        proxy_log=None if args.proxy_log else (lambda _since: []),
        lab_checks=not args.capture_model,
    )
    print(json.dumps(outcome))
    return 0 if outcome["status"] == "ran" else 1


def cmd_summarize(args: argparse.Namespace) -> int:
    attempts = Path(args.attempts)
    try:
        document = summarize(
            Path(args.dataset), attempts, n=args.n, rounds=args.round or None
        )
    except DatasetError as exc:
        print(json.dumps({"exit_code": 1, "reason": exc.code}))
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    if args.judge_inputs:
        target = Path(args.judge_inputs)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as stream:
            doc = judge_inputs(document, attempts, target)
            stream.write(json.dumps(doc, indent=2) + "\n")
    print(json.dumps({"exit_code": 0, "out": str(out), "totals": document["totals"]}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.holmes_baseline.dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dataset", required=True)
    run.add_argument("--scenario", required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--slot", type=int, default=None)
    run.add_argument("--round", default=None)
    run.add_argument("--raw-root", default=None)
    run.add_argument("--deadline", type=float, default=None, help="epoch seconds")
    run.add_argument("--kind-node", default=None)
    run.add_argument(
        "--capture-model",
        action="store_true",
        help="offline smoke: fake model endpoint, dummy key, no lab checks",
    )
    run.add_argument(
        "--no-proxy-log",
        dest="proxy_log",
        action="store_false",
        help="skip reading the lab proxy log (smoke without kind)",
    )
    run.set_defaults(func=cmd_run)
    summ = sub.add_parser("summarize")
    summ.add_argument("--dataset", required=True)
    summ.add_argument("--attempts", required=True)
    summ.add_argument("--n", type=int, default=SLOTS_PER_SCENARIO)
    summ.add_argument("--round", action="append")
    summ.add_argument("--out", required=True)
    summ.add_argument("--judge-inputs", default=None)
    summ.set_defaults(func=cmd_summarize)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
