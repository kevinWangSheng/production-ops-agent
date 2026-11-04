"""HolmesGPT on the v2 dataset's question-entry scenarios (F1 closure S7,
prereview 2026-11-04 section 2.2 S7, V2-R12, R15).

    python -m scripts.holmes_baseline.dataset run --dataset evals/datasets/dev-v2 \\
        --scenario dv2-c39f60 --attempt-id <id> [--round <name>]
    python -m scripts.holmes_baseline.dataset summarize --dataset <root> \\
        --attempts <raw dir> [--n 3] --out <summary.json>

``run`` is one attempt inside a window the caller already holds: the paired
schedule (R7) injects and restores the lab-side injectors (flagd flags, OTLP
spans) and waits for them to settle; this runner never touches the lab. It
applies the two Holmes-side injectors itself, the way OpsPilot's harness
does process-locally:

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
upstream entry (V2-R12): ``run`` refuses them and ``summarize`` lists them
as not applicable, outside every denominator.

The attempt directory (git-ignored ``RAW_ROOT``) keeps the raw Holmes output
and ``report.md`` (the final answer only, for the judge); ``summary.json``
there holds counts, hashes and the projected outcome, never report text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from scripts.eval_dev_round import upstream_applicability
from scripts.eval_live_lab import UNREACHABLE_URLS, LabError, validate_injectors
from scripts.holmes_baseline import config as hconfig
from scripts.holmes_baseline.project import project_outcome

ATTEMPT_SCHEMA = "opspilot-holmes-attempt-1"
ROUND_SCHEMA = "opspilot-holmes-dataset-round-1"
SYSTEM = "holmesgpt"
NOT_APPLICABLE = "no_upstream_entry"
REPORT_FILE = "report.md"
SUMMARY_FILE = "summary.json"
SLOTS_PER_SCENARIO = 3  # U2: three runs per scenario
#: Container-side loopback; ``eval_live_lab.UNREACHABLE_URLS`` is the same
#: text OpsPilot's investigator gets.
LOOPBACK = "127.0.0.1"
INJECT_503 = {"always_503": "always", "first_503": "first"}
#: Lab-side injectors the caller applies before ``run``.
LAB_SIDE_KINDS = frozenset({"flagd", "otlp_span"})


class DatasetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HolmesPlan:
    """What this runner does for one scenario (pure, from the answer side;
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

    def prometheus_url(self, default: str) -> str:
        return (
            UNREACHABLE_URLS["prometheus"] if self.source == "prometheus" else default
        )

    def jaeger_url(self, default: str) -> str:
        return UNREACHABLE_URLS["jaeger"] if self.source == "jaeger" else default

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


def plan(spec: ScenarioSpecV2, answer: AnswerV2) -> HolmesPlan:
    """The scenario's Holmes plan; the injector whitelist is OpsPilot's
    (``validate_injectors``), so a scenario the OpsPilot driver refuses is
    refused here too (``INJECTOR_INVALID:<code>``)."""
    try:
        validated = validate_injectors(spec.entry, answer.injectors)
    except LabError as exc:
        raise DatasetError(f"INJECTOR_INVALID:{exc}") from None
    return HolmesPlan(
        scenario_id=spec.scenario_id,
        entry=spec.entry,
        upstream=upstream_applicability(spec, answer),
        source=validated.source,
        model_mode=validated.model_mode,
        lab_side=tuple(
            str(i["kind"]) for i in validated.injectors if i["kind"] in LAB_SIDE_KINDS
        ),
    )


def utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def prompt(question: str, end: float, window_seconds: int) -> str:
    """The F14 run1 prompt shape with the scenario's own question."""
    return (
        f"{question}\nInvestigation window (UTC): {utc(end - window_seconds)} "
        f"to {utc(end)}."
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def upstream_dimension(image_id: str, base_config: Mapping[str, Any]) -> dict[str, Any]:
    """V2-R7: the baseline round's ``upstream`` dimension. The config hash is
    the attempt-independent base config (lab URLs); an unreachable-source
    attempt records its own override separately."""
    return {
        "kind": "hash",
        "value": f"holmesgpt@{hconfig.UPSTREAM_COMMIT} image={image_id} "
        f"config={canonical_sha256(base_config)}",
        "reason": None,
    }


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
    injected = [e for e in connects if e.get("injected")]
    return {
        "allowed": sum(1 for e in connects if e.get("allowed")),
        "injected_503": len(injected),
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
        rows.append(
            {
                "tool": call.get("tool_name"),
                "toolset": call.get("toolset_name"),
                "status": result.get("status"),
            }
        )
    return rows


def report_text(output: Mapping[str, Any]) -> str | None:
    """Holmes' final answer (``result``); never messages or reasoning."""
    result = output.get("result")
    return result if isinstance(result, str) and result.strip() else None


def attempt_summary(
    *,
    holmes_plan: HolmesPlan,
    attempt_id: str,
    round_id: str | None,
    mode: str,
    output: Mapping[str, Any],
    proxy_lines: Sequence[str],
    egress: Sequence[Mapping[str, Any]],
    started: float,
    finished: float,
    prompt_text: str,
    window_seconds: int,
    upstream: Mapping[str, Any],
    attempt_config: Mapping[str, Any],
    raw_sha256: str | None,
    pre_run: Mapping[str, Any] | None,
    balance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The attempt's ``summary.json`` (pure): counts, hashes, the projected
    outcome; the report itself is only referenced by hash and file name."""
    report = report_text(output)
    proxy_counts = Counter(
        " ".join(line.split()[2:5]) for line in proxy_lines if len(line.split()) >= 5
    )
    rows = tool_rows(output)
    return {
        "schema": ATTEMPT_SCHEMA,
        "system": SYSTEM,
        "mode": mode,
        "attempt_id": attempt_id,
        "round": round_id,
        "scenario_id": holmes_plan.scenario_id,
        "entry": holmes_plan.entry,
        "upstream": holmes_plan.upstream,
        "injection": {
            "source_unreachable": holmes_plan.source,
            "model_transport": holmes_plan.model_mode,
            "lab_side_by_caller": list(holmes_plan.lab_side),
        },
        "outcome": project_outcome(output, proxy_lines),
        "started_at": utc(started),
        "finished_at": utc(finished),
        "started_ts": started,
        "wall_seconds": round(finished - started, 1),
        "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
        "prompt_window_seconds": window_seconds,
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
            "upstream": dict(upstream),
            "attempt_config_sha256": canonical_sha256(attempt_config),
        },
        "raw_output_sha256": raw_sha256,
        "report": {
            "available": report is not None,
            "file": REPORT_FILE if report is not None else None,
            "sha256": hashlib.sha256(report.encode()).hexdigest()
            if report is not None
            else None,
        },
        "balance": dict(balance) if balance is not None else None,
    }


# --- summarize (pure apart from reading the attempt summaries)


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
            continue  # other raw directories (F14 runs, checks) share RAW_ROOT
        found.append((path.parent, data))
    return found


def _group(answer: AnswerV2) -> str:
    return "regression" if answer.category == "regression" else "capability"


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
    counts as not started (in the denominator). ``rounds`` limits the
    attempts read to those rounds."""
    loaded = {
        spec.scenario_id: (spec, answer)
        for spec, answer in load_scenarios_v2(dataset_root)
    }
    plans = {sid: plan(spec, answer) for sid, (spec, answer) in loaded.items()}
    by_scenario: dict[str, list[tuple[Path, dict[str, Any]]]] = {
        sid: [] for sid in loaded
    }
    upstream_seen: set[str] = set()
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
        by_scenario[sid].append((directory, data))
    if len(upstream_seen) > 1:
        raise DatasetError("MANIFEST_DRIFT:upstream")
    for sid, items in by_scenario.items():
        if len(items) > n:
            raise DatasetError(f"REDRAW_DETECTED:{sid}")
    slots: list[dict[str, Any]] = []
    for sid in sorted(loaded):
        spec, answer = loaded[sid]
        holmes_plan = plans[sid]
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
                "upstream": holmes_plan.upstream,
                "in_denominator": holmes_plan.applicable,
            }
            if not holmes_plan.applicable:
                slots.append(
                    {
                        **base,
                        "status": "not_applicable",
                        "reason": "NO_UPSTREAM_ENTRY",
                        "attempt_id": None,
                        "round": None,
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
                        "final_state_expected": False,
                        "report": {"available": False, "path": None, "sha256": None},
                    }
                )
                continue
            directory, data = items[index]
            outcome = data["outcome"]
            report = data["report"]
            report_path = None
            if report["available"]:
                path = directory / str(report["file"])
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != report["sha256"]
                ):
                    raise DatasetError(f"REPORT_MISMATCH:{directory.name}")
                report_path = f"{directory.name}/{report['file']}"
            slots.append(
                {
                    **base,
                    "status": "ran",
                    "reason": None,
                    "attempt_id": data["attempt_id"],
                    "round": data.get("round"),
                    "started_at": data["started_at"],
                    "final_state": outcome["final_state"],
                    # informational: Holmes' terminal state against the
                    # answer's allowed set; the judge is the main score (U3)
                    "final_state_expected": outcome["final_state"]
                    in answer.expected_outcome.final_states,
                    "report": {
                        "available": bool(report["available"]),
                        "path": report_path,
                        "sha256": report["sha256"],
                    },
                    "tool_calls": outcome["tool_calls"],
                    "tool_errors": data["tool_errors"],
                    "model_calls_ok": outcome["model_calls_ok"],
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
            "completed": sum(1 for s in counted if s.get("final_state") == "completed"),
            "report_available": sum(1 for s in counted if s["report"]["available"]),
            "final_state_expected": sum(
                1 for s in counted if s["final_state_expected"]
            ),
        }
    upstream = json.loads(next(iter(upstream_seen))) if upstream_seen else None
    return {
        "schema": ROUND_SCHEMA,
        "system": SYSTEM,
        "dataset": f"{dataset_root.name}:{dataset_tree_hash(dataset_root)}",
        "upstream": upstream,
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
        ],
    }


# --- live attempt (docker, lab relay, model API)


def _scenario(dataset_root: Path, scenario_id: str) -> tuple[ScenarioSpecV2, AnswerV2]:
    for spec, answer in load_scenarios_v2(dataset_root):
        if spec.scenario_id == scenario_id:
            return spec, answer
    raise DatasetError(f"UNKNOWN_SCENARIO:{scenario_id}")


def cmd_run(args: argparse.Namespace) -> int:
    import yaml

    from scripts.holmes_baseline import run as hrun

    spec, answer = _scenario(Path(args.dataset), args.scenario)
    holmes_plan = plan(spec, answer)
    if not holmes_plan.applicable:
        print(json.dumps({"exit_code": 2, "reason": "NOT_APPLICABLE"}))
        return 2
    directory = hrun.RAW_ROOT / args.attempt_id
    if directory.exists():
        raise SystemExit(f"{directory} exists; attempt ids are never reused")
    capture = args.capture_model
    pre_run: dict[str, Any] | None = None
    if not capture:
        rate = hrun.frontend_span_rate()
        skew = hrun.clock_skew()
        pre_run = {"frontend_span_rate": rate, "clock_skew": skew}
        if rate <= 0:
            raise SystemExit("frontend span rate is 0; restart otel-collector first")
        if max(abs(skew["host_vm"]), abs(skew["host_container"])) > (
            hrun.MAX_CLOCK_SKEW_SECONDS
        ):
            raise SystemExit("clock skew above 2 s; refusing")
    base_config = hconfig.holmes_config(hrun.PROMETHEUS_URL, hrun.JAEGER_URL)
    content: dict[str, Any] = hconfig.holmes_config(
        holmes_plan.prometheus_url(hrun.PROMETHEUS_URL),
        holmes_plan.jaeger_url(hrun.JAEGER_URL),
    )
    if capture:
        # offline smoke: the fake endpoint answers once; the egress proxy and
        # its 503 injection are not on this path
        content = {**content, "api_base": f"http://{hrun.CAPTURE}:8000"}
    directory.mkdir(parents=True)
    config_path = directory / "config.yaml"
    config_path.write_text(yaml.safe_dump(content, sort_keys=False))
    config_path.chmod(0o644)
    key = "sk-capture-dummy" if capture else hrun.model_key()
    balance_before = None if capture else hrun.balance(key)
    image_id = json.loads(hrun.docker("image", "inspect", hrun.IMAGE))[0]["Id"]
    hrun.setup(capture=capture, egress_extra=holmes_plan.egress_extra())
    end = time.time()
    prompt_text = prompt(spec.stimulus.question, end, hrun.WINDOW_SECONDS)
    no_proxy = [hrun.RELAY] + ([hrun.CAPTURE] if capture else [])
    try:
        result = hrun.run_holmes(
            directory,
            config_path,
            prompt_text,
            hrun.holmes_secret_env(key),
            no_proxy=holmes_plan.no_proxy(no_proxy),
            wall_seconds=args.wall_seconds or hconfig.RUN_WALL_SECONDS,
        )
        egress = hrun.egress_log(hrun.EGRESS)
    finally:
        hrun.teardown()
    proxy_lines = hrun.proxy_log_lines(result["started"]) if args.proxy_log else []
    output = result["output"]
    report = report_text(output)
    if report is not None:
        (directory / REPORT_FILE).write_text(report)
    summary = attempt_summary(
        holmes_plan=holmes_plan,
        attempt_id=args.attempt_id,
        round_id=args.round,
        mode="capture_model" if capture else "real_model",
        output=output,
        proxy_lines=proxy_lines,
        egress=egress,
        started=result["started"],
        finished=result["finished"],
        prompt_text=prompt_text,
        window_seconds=hrun.WINDOW_SECONDS,
        upstream=upstream_dimension(image_id, base_config),
        attempt_config=content,
        raw_sha256=result["raw_sha256"],
        pre_run=pre_run,
        balance=None
        if capture
        else {
            "source": f"GET https://{hconfig.MODEL_HOST}/user/balance",
            "before": balance_before,
            "after": hrun.balance(key),
        },
    )
    (directory / SUMMARY_FILE).write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "attempt_id": args.attempt_id,
                **summary["outcome"],
                "egress": summary["egress"],
            }
        )
    )
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    try:
        document = summarize(
            Path(args.dataset),
            Path(args.attempts),
            n=args.n,
            rounds=args.round or None,
        )
    except DatasetError as exc:
        print(json.dumps({"exit_code": 1, "reason": exc.code}))
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"exit_code": 0, "out": str(out), "totals": document["totals"]}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.holmes_baseline.dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dataset", required=True)
    run.add_argument("--scenario", required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--round", default=None)
    run.add_argument("--wall-seconds", type=float, default=None)
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
    summ.set_defaults(func=cmd_summarize)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
