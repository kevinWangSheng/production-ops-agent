"""Paired schedule for the F1 closure comparison runs (S7, R7): engineer CLI.

One *window* is one injection on one lab: OpsPilot and HolmesGPT answer the
same scenario one after the other under that injection, in the order the
preregistered rules give (``evals/preregistered/f1-closure-paired-rules.json``).
Alert-entry scenarios have no HolmesGPT entry (V2-R12), so their windows run
OpsPilot alone.

Windows can be spread over several independent labs (separate kind clusters
on one host, or separate hosts); each lab still runs one window at a time.
The spacing rules hold per lab: between two windows of the same scenario at
least ``same_scenario_min_gap_seconds`` (fault restored to next injection),
and between any two windows at least ``wash_min_seconds``. Two labs share no
Prometheus or Jaeger, so a fault on one cannot be read on the other.

    .venv/bin/python -m scripts.eval_schedule plan --dataset evals/datasets/dev-v2 \\
        --labs labs.json [--rules RULES] [--window-minutes 27] --out plan.json
    .venv/bin/python -m scripts.eval_schedule check --plan plan.json --log log.json \\
        [--rules RULES]

``plan`` is offline and deterministic: it simulates the same dispatcher the
runner uses with an estimated window length, so the plan shows the order and
the expected length; the real order follows the real window lengths.
``check`` reads a schedule log (one record per finished window) and reports
every rule the log breaks, the voided and rerun windows and the per-scenario
counts; it never drops a record.

Engineer script, not product code: it never touches a lab itself; the window
runner (``WindowRunner``) is the only thing that does, through the live
driver.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "evals/preregistered/f1-closure-paired-rules.json"
RULES_SCHEMA = "opspilot-eval-preregistered-rules-1"
PLAN_SCHEMA = "opspilot-eval-paired-plan-1"
LOG_SCHEMA = "opspilot-eval-paired-log-1"
LABS_SCHEMA = "opspilot-eval-labs-1"
SYSTEMS = ("opspilot", "holmes")
VOID_CLASSES = (
    "ENV_SETUP_FAILED",
    "PROVIDER_5XX_STORM",
    "HOST_NOISE",
    "HARNESS_INTERRUPTED",
)
#: r0.1-3: the shared-vCPU thresholds of the HOST_NOISE class.
STEAL_LIMIT_PERCENT = 5.0
BASELINE_P95_LIMIT_RATIO = 2.0

SlotStatus = Literal["ran", "setup_failed", "aborted", "not_started"]


class ScheduleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- rules --------------------------------------------------------------------


@dataclass(frozen=True)
class Rules:
    rules_id: str
    repetitions: int
    holmes_entries: frozenset[str]
    same_scenario_gap: timedelta
    wash_min: timedelta
    max_reruns: int
    void_classes: tuple[str, ...]
    sha256: str | None = None


def load_rules(path: Path = RULES_PATH) -> Rules:
    """The preregistered rules; anything this module does not implement is
    refused, so a changed rule file cannot be silently half-applied."""
    import hashlib

    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        raise ScheduleError("RULES_UNREADABLE") from None
    try:
        if data["schema"] != RULES_SCHEMA:
            raise ScheduleError("RULES_SCHEMA")
        spacing = data["spacing"]
        void = data["void_rerun"]
        pairing = data["pairing"]
        checks = (
            tuple(data["systems"]) == SYSTEMS,
            pairing["same_injection"] is True,
            pairing["order"] == "alternate",
            spacing["same_scenario_gap_scope"] == "lab",
            spacing["one_window_per_lab"] is True,
            void["rerun_unit"] == "window",
            all(c["action"] == "rerun_window" for c in void["classes"]),
            {c["code"] for c in void["classes"]} <= set(VOID_CLASSES),
        )
        if not all(checks):
            raise ScheduleError("RULES_UNSUPPORTED")
        rules = Rules(
            rules_id=str(data["rules_id"]),
            repetitions=int(data["repetitions"]),
            holmes_entries=frozenset(data["holmes_entries"]),
            same_scenario_gap=timedelta(
                seconds=int(spacing["same_scenario_min_gap_seconds"])
            ),
            wash_min=timedelta(seconds=int(spacing["wash_min_seconds"])),
            max_reruns=int(void["max_reruns_per_window"]),
            void_classes=tuple(c["code"] for c in void["classes"]),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (KeyError, TypeError, ValueError):
        raise ScheduleError("RULES_INVALID") from None
    if rules.repetitions < 1 or rules.max_reruns < 0:
        raise ScheduleError("RULES_INVALID")
    return rules


# -- windows ------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    entry: str
    #: the scenario injects a model transport fault (a 5xx there is the task)
    model_fault: bool = False


@dataclass(frozen=True)
class Window:
    window_id: str
    scenario_id: str
    repetition: int
    systems: tuple[str, ...]
    #: 0 the first run of this window, 1 its registered rerun
    attempt: int = 0

    def rerun(self) -> Window:
        return Window(
            f"{self.window_id}-rerun",
            self.scenario_id,
            self.repetition,
            self.systems,
            self.attempt + 1,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "scenario_id": self.scenario_id,
            "repetition": self.repetition,
            "systems": list(self.systems),
            "attempt": self.attempt,
        }


def scenarios_from_dataset(root: Path) -> tuple[Scenario, ...]:
    """Dataset order (``load_scenarios_v2``): the order the rotation uses."""
    from opspilot.evaluation.model import load_scenarios_v2

    return tuple(
        Scenario(
            spec.scenario_id,
            spec.entry,
            any(i.kind == "model_transport" for i in answer.injectors),
        )
        for spec, answer in load_scenarios_v2(root)
    )


def windows(scenarios: Sequence[Scenario], rules: Rules) -> tuple[Window, ...]:
    """Every scenario ``repetitions`` times. Repetition r (1-based) of the
    i-th scenario puts OpsPilot first when ``i + r`` is even: each scenario
    alternates, and consecutive scenarios start on opposite sides, so the
    totals of first positions differ by at most one per two scenarios."""
    out = []
    for i, scenario in enumerate(scenarios):
        for r in range(1, rules.repetitions + 1):
            if scenario.entry in rules.holmes_entries:
                order: tuple[str, ...] = (
                    SYSTEMS if (i + r) % 2 == 0 else tuple(reversed(SYSTEMS))
                )
            else:
                order = ("opspilot",)
            out.append(
                Window(f"{scenario.scenario_id}-r{r}", scenario.scenario_id, r, order)
            )
    return tuple(out)


# -- dispatcher ---------------------------------------------------------------


@dataclass
class _LabState:
    #: when the lab's last window restored its fault; None before the first
    last_restored: datetime | None = None
    #: per scenario: when this lab last restored a window of it
    restored: dict[str, datetime] = field(default_factory=dict)
    busy: bool = False
    previous: dict[str, Any] | None = None


class Dispatcher:
    """Which window a free lab takes next. Pure state, guarded by one lock;
    the planner and the runner share it so the plan is what the runner does.

    A lab takes, among the pending windows it may start now, the one with the
    lowest repetition, then the earliest in dataset order. A window may start
    on a lab once ``wash_min`` passed since the lab's last restore and
    ``same_scenario_gap`` since the lab last restored the same scenario."""

    def __init__(self, pending: Sequence[Window], labs: Sequence[str], rules: Rules):
        if not labs or len(set(labs)) != len(labs):
            raise ScheduleError("LABS_INVALID")
        self.rules = rules
        self.pending: list[Window] = list(pending)
        self.labs = {lab: _LabState() for lab in labs}
        self.rank = {w.window_id: n for n, w in enumerate(pending)}
        self.lock = threading.Lock()
        self.records: list[dict[str, Any]] = []
        self.in_flight = 0

    def _ready_at(self, lab: str, window: Window) -> datetime | None:
        state = self.labs[lab]
        moments = []
        if state.last_restored is not None:
            moments.append(state.last_restored + self.rules.wash_min)
        same = state.restored.get(window.scenario_id)
        if same is not None:
            moments.append(same + self.rules.same_scenario_gap)
        return max(moments) if moments else None

    def take(self, lab: str, now: datetime) -> tuple[Window | None, datetime | None]:
        """(window, None) to start now; (None, ready_at) to wait until then
        (None when nothing is pending: the lab may stop once nothing is in
        flight either, since only a finished window can add a rerun)."""
        with self.lock:
            state = self.labs[lab]
            if state.busy:
                raise ScheduleError("LAB_BUSY")
            best: tuple[Any, ...] | None = None
            soonest: datetime | None = None
            for window in self.pending:
                ready = self._ready_at(lab, window)
                if ready is not None and ready > now:
                    soonest = ready if soonest is None else min(soonest, ready)
                    continue
                key = (
                    window.attempt == 0,
                    window.repetition,
                    self.rank.get(window.window_id, 0),
                )
                if best is None or key < best[0]:
                    best = (key, window)
            if best is None:
                return None, soonest
            window = best[1]
            self.pending.remove(window)
            state.busy = True
            self.in_flight += 1
            return window, None

    def finish(self, lab: str, window: Window, record: dict[str, Any]) -> str | None:
        """Book a finished window; returns its void class (and queues the
        rerun) when the rules void it. The record gets ``lab``,
        ``predecessor`` and ``void``."""
        with self.lock:
            state = self.labs[lab]
            record = dict(record)
            record.update(window.as_json())
            record["lab"] = lab
            record["predecessor"] = state.previous
            for slot in record.get("slots") or []:
                slot.setdefault("predecessor", state.previous)
            restored = _moment(record.get("restored_at")) or _moment(
                record.get("ended_at")
            )
            if restored is not None:
                state.last_restored = restored
                state.restored[window.scenario_id] = restored
                state.previous = {
                    "window_id": window.window_id,
                    "scenario_id": window.scenario_id,
                    "restored_at": restored.isoformat(),
                }
            void = classify_void(record, self.rules)
            record["void"] = void
            if void is not None and window.attempt < self.rules.max_reruns:
                rerun = window.rerun()
                self.rank[rerun.window_id] = self.rank.get(window.window_id, 0)
                self.pending.append(rerun)
                record["rerun_window_id"] = rerun.window_id
            self.records.append(record)
            state.busy = False
            self.in_flight -= 1
            return void

    def done(self) -> bool:
        with self.lock:
            return not self.pending and self.in_flight == 0


# -- void classification --------------------------------------------------------


def classify_void(record: Mapping[str, Any], rules: Rules) -> str | None:
    """The preregistered void class of a finished window, or None.

    The classes are checked in the rule file's order. Every slot's facts come
    from the window runner; nothing here judges an answer."""
    slots = [s for s in record.get("slots") or [] if isinstance(s, Mapping)]
    found: list[str] = []
    if record.get("interrupted") or any(
        s.get("status") == "not_started" for s in slots
    ):
        found.append("HARNESS_INTERRUPTED")
    if any(
        s.get("status") == "setup_failed" and not s.get("run_created") for s in slots
    ):
        found.append("ENV_SETUP_FAILED")
    if not record.get("model_fault") and any(
        s.get("provider_error") is True for s in slots
    ):
        found.append("PROVIDER_5XX_STORM")
    if record.get("shared_host") and any(
        host_noisy(s.get("host_noise")) for s in slots
    ):
        found.append("HOST_NOISE")
    for code in rules.void_classes:
        if code in found:
            return code
    return None


def host_noisy(noise: Any) -> bool:
    if not isinstance(noise, Mapping):
        return False
    steal = noise.get("steal_percent")
    ratio = noise.get("baseline_p95_ratio")
    return (isinstance(steal, (int, float)) and steal > STEAL_LIMIT_PERCENT) or (
        isinstance(ratio, (int, float)) and ratio > BASELINE_P95_LIMIT_RATIO
    )


def steal_percent(before: str, after: str) -> float | None:
    """CPU steal share between two ``/proc/stat`` reads (the ``cpu`` line:
    user nice system idle iowait irq softirq steal ...)."""

    def line(text: str) -> list[int] | None:
        for row in text.splitlines():
            parts = row.split()
            if parts and parts[0] == "cpu" and len(parts) >= 9:
                try:
                    return [int(v) for v in parts[1:9]]
                except ValueError:
                    return None
        return None

    a, b = line(before), line(after)
    if a is None or b is None:
        return None
    delta = [y - x for x, y in zip(a, b, strict=True)]
    total = sum(delta)
    if total <= 0 or any(d < 0 for d in delta):
        return None
    return round(100.0 * delta[7] / total, 3)


# -- planner ---------------------------------------------------------------------


def simulate(
    pending: Sequence[Window],
    labs: Sequence[str],
    rules: Rules,
    *,
    window_length: timedelta,
    start: datetime,
) -> list[dict[str, Any]]:
    """The dispatcher run with every window taking ``window_length`` from
    injection to restore: the planned order, lab and estimated times."""
    dispatcher = Dispatcher(pending, labs, rules)
    free = dict.fromkeys(labs, start)
    while not dispatcher.done():
        lab = min(free, key=lambda name: (free[name], list(labs).index(name)))
        window, ready = dispatcher.take(lab, free[lab])
        if window is None:
            if ready is None:
                del free[lab]
                if not free:
                    raise ScheduleError("PLAN_STUCK")
                continue
            free[lab] = ready
            continue
        begin = free[lab]
        end = begin + window_length
        dispatcher.finish(
            lab,
            window,
            {
                "injected_at": begin.isoformat(),
                "restored_at": end.isoformat(),
                "ended_at": end.isoformat(),
                "slots": [
                    {"system": s, "position": n + 1, "status": "ran"}
                    for n, s in enumerate(window.systems)
                ],
                "estimated": True,
            },
        )
        free[lab] = end
    return dispatcher.records


def plan(
    scenarios: Sequence[Scenario],
    labs: Sequence[str],
    rules: Rules,
    *,
    window_length: timedelta,
    start: datetime,
    dataset: str | None = None,
) -> dict[str, Any]:
    planned = windows(scenarios, rules)
    records = simulate(planned, labs, rules, window_length=window_length, start=start)
    end = max(
        (m for r in records if (m := _moment(r["restored_at"])) is not None),
        default=start,
    )
    first = {s: 0 for s in SYSTEMS}
    for w in planned:
        if len(w.systems) == 2:
            first[w.systems[0]] += 1
    return {
        "schema": PLAN_SCHEMA,
        "rules_id": rules.rules_id,
        "rules_sha256": rules.sha256,
        "dataset": dataset,
        "labs": list(labs),
        "window_minutes": window_length.total_seconds() / 60,
        "estimated_hours": round((end - start).total_seconds() / 3600, 2),
        "first_position": first,
        "windows": [w.as_json() for w in planned],
        "simulated": [
            {
                "window_id": r["window_id"],
                "lab": r["lab"],
                "injected_at": r["injected_at"],
                "restored_at": r["restored_at"],
                "predecessor": None
                if r["predecessor"] is None
                else r["predecessor"]["scenario_id"],
            }
            for r in records
        ],
        "scenarios": {
            s.scenario_id: {"entry": s.entry, "model_fault": s.model_fault}
            for s in scenarios
        },
    }


# -- check -----------------------------------------------------------------------


def check(
    plan_json: Mapping[str, Any], log: Mapping[str, Any], rules: Rules
) -> dict[str, Any]:
    """Every rule a schedule log breaks, the counts and the voids.

    * ``OVERLAP:<lab>``: two windows of one lab overlapped in time;
    * ``WASH_SHORT:<window>``: injection less than ``wash_min`` after the lab's
      previous restore;
    * ``GAP_SHORT:<window>``: same scenario on the same lab again within
      ``same_scenario_gap``;
    * ``ORDER_MISMATCH:<window>``: the slots ran in another order than planned;
    * ``UNPLANNED:<window>``: a window that is neither planned nor a rerun;
    * ``RERUN_UNJUSTIFIED:<window>``: a rerun without a voided original, or a
      second rerun;
    * ``MISSING:<window>``: a planned window that never ran (its slots count
      as failures)."""
    if log.get("schema") != LOG_SCHEMA:
        raise ScheduleError("LOG_SCHEMA")
    planned = {w["window_id"]: w for w in plan_json["windows"]}
    records = [r for r in log.get("windows") or [] if isinstance(r, Mapping)]
    violations: list[str] = []
    by_lab: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_lab.setdefault(str(record.get("lab")), []).append(record)
    for lab, items in sorted(by_lab.items()):
        items = sorted(
            items,
            key=lambda r: (
                _moment(r.get("injected_at"))
                or _moment(r.get("started_at"))
                or datetime.max.replace(tzinfo=UTC)
            ),
        )
        last_end: datetime | None = None
        last_restored: datetime | None = None
        restored_by_scenario: dict[str, datetime] = {}
        for record in items:
            wid = str(record.get("window_id"))
            began = _moment(record.get("started_at")) or _moment(
                record.get("injected_at")
            )
            injected = _moment(record.get("injected_at")) or began
            if began is not None and last_end is not None and began < last_end:
                violations.append(f"OVERLAP:{lab}")
            if (
                injected is not None
                and last_restored is not None
                and injected - last_restored < rules.wash_min
            ):
                violations.append(f"WASH_SHORT:{wid}")
            sid = str(record.get("scenario_id"))
            same = restored_by_scenario.get(sid)
            if (
                injected is not None
                and same is not None
                and injected - same < rules.same_scenario_gap
            ):
                violations.append(f"GAP_SHORT:{wid}")
            restored = _moment(record.get("restored_at")) or _moment(
                record.get("ended_at")
            )
            if restored is not None:
                last_restored = restored
                restored_by_scenario[sid] = restored
            last_end = _moment(record.get("ended_at")) or restored or last_end
    seen: dict[str, Mapping[str, Any]] = {}
    for record in records:
        wid = str(record.get("window_id"))
        base = wid.removesuffix("-rerun")
        origin = planned.get(base)
        if origin is None:
            violations.append(f"UNPLANNED:{wid}")
            continue
        order = [
            s.get("system")
            for s in sorted(
                record.get("slots") or [], key=lambda s: s.get("position", 0)
            )
        ]
        if order != list(origin["systems"]):
            violations.append(f"ORDER_MISMATCH:{wid}")
        if wid != base:
            original = seen.get(base)
            if original is None or original.get("void") is None or wid in seen:
                violations.append(f"RERUN_UNJUSTIFIED:{wid}")
        elif wid in seen:
            violations.append(f"RERUN_UNJUSTIFIED:{wid}")
        seen[wid] = record
    counted: dict[str, dict[str, int]] = {}
    voided: list[dict[str, Any]] = []
    for wid, origin in planned.items():
        sid = origin["scenario_id"]
        counts = counted.setdefault(sid, {s: 0 for s in SYSTEMS})
        found = seen.get(wid)
        if found is None:
            violations.append(f"MISSING:{wid}")
            continue
        record = final = found
        if record.get("void") is not None and f"{wid}-rerun" in seen:
            voided.append(
                {"window_id": wid, "void": record["void"], "rerun": f"{wid}-rerun"}
            )
            final = seen[f"{wid}-rerun"]
        for slot in final.get("slots") or []:
            if slot.get("status") != "not_started":
                counts[str(slot.get("system"))] += 1
    return {
        "rules_id": rules.rules_id,
        "violations": violations,
        "voided": voided,
        "started_slots": counted,
    }


# -- runner ----------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class WindowRunner(Protocol):
    """Runs one window on one lab and returns its record: ``started_at``,
    ``injected_at``, ``restored_at``, ``ended_at``, ``interrupted``,
    ``shared_host`` and per slot ``system``, ``position``, ``status``,
    ``reason``, ``run_created``, ``provider_error``, ``host_noise``,
    ``attempt_id``, ``started_at``, ``ended_at``. It must not raise for a
    failed window; an exception stops the lab (the window is booked as
    interrupted)."""

    def run(self, lab: str, window: Window) -> dict[str, Any]: ...


def run_schedule(
    pending: Sequence[Window],
    labs: Sequence[str],
    rules: Rules,
    runner: WindowRunner,
    *,
    clock: Clock | None = None,
    scenarios: Mapping[str, Scenario] | None = None,
    on_record: Callable[[dict[str, Any]], None] | None = None,
    poll_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """One thread per lab, each taking windows from the shared dispatcher
    until nothing is pending or in flight. Records are booked in finish
    order; ``on_record`` sees each (the CLI appends it to the log)."""
    clock = clock or SystemClock()
    dispatcher = Dispatcher(pending, labs, rules)
    model_fault = {k: v.model_fault for k, v in (scenarios or {}).items()}
    errors: list[BaseException] = []

    def worker(lab: str) -> None:
        while True:
            window, ready = dispatcher.take(lab, clock.now())
            if window is None:
                if dispatcher.done():
                    return
                wait = (
                    poll_seconds
                    if ready is None
                    else (ready - clock.now()).total_seconds()
                )
                clock.sleep(max(0.0, min(wait, poll_seconds)))
                continue
            try:
                record = runner.run(lab, window)
            except BaseException as exc:  # noqa: BLE001 - booked, then the lab stops
                record = {
                    "interrupted": True,
                    "error": type(exc).__name__,
                    "ended_at": clock.now().isoformat(),
                    "slots": [
                        {"system": s, "position": n + 1, "status": "not_started"}
                        for n, s in enumerate(window.systems)
                    ],
                }
                record["model_fault"] = model_fault.get(window.scenario_id, False)
                dispatcher.finish(lab, window, record)
                if on_record is not None:
                    on_record(dispatcher.records[-1])
                errors.append(exc)
                return
            record.setdefault("model_fault", model_fault.get(window.scenario_id, False))
            dispatcher.finish(lab, window, record)
            if on_record is not None:
                on_record(dispatcher.records[-1])

    threads = [
        threading.Thread(target=worker, args=(lab,), name=f"lab-{lab}") for lab in labs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors and not dispatcher.done():
        # A lab stopped on a runner error; its pending windows stay MISSING
        # in ``check`` (never silently skipped).
        pass
    return dispatcher.records


# -- helpers & CLI ----------------------------------------------------------------


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def load_labs(path: Path) -> tuple[str, ...]:
    """``{"schema": LABS_SCHEMA, "labs": [{"name": ...}, ...]}``; the runner
    reads the rest of each entry (host, instance, ports)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] != LABS_SCHEMA:
            raise ScheduleError("LABS_SCHEMA")
        names = tuple(str(item["name"]) for item in data["labs"])
    except (OSError, ValueError, KeyError, TypeError):
        raise ScheduleError("LABS_INVALID") from None
    if not names or len(set(names)) != len(names):
        raise ScheduleError("LABS_INVALID")
    return names


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.eval_schedule")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--dataset", required=True, action="append")
    p.add_argument("--labs", required=True)
    p.add_argument("--rules", default=str(RULES_PATH))
    p.add_argument("--window-minutes", type=float, default=27.0)
    p.add_argument("--start")
    p.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--plan", required=True)
    c.add_argument("--log", required=True)
    c.add_argument("--rules", default=str(RULES_PATH))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rules = load_rules(Path(args.rules))
        if args.command == "plan":
            from scripts.eval_live import dataset_dimension

            scenarios: list[Scenario] = []
            names = []
            for root in args.dataset:
                scenarios += scenarios_from_dataset(Path(root))
                names.append(str(dataset_dimension(Path(root)).value))
            if len({s.scenario_id for s in scenarios}) != len(scenarios):
                raise ScheduleError("SCENARIO_DUPLICATE")
            start = (
                _moment(args.start) if args.start else datetime(2026, 1, 1, tzinfo=UTC)
            )
            if start is None:
                raise ScheduleError("START_INVALID")
            out = plan(
                scenarios,
                load_labs(Path(args.labs)),
                rules,
                window_length=timedelta(minutes=args.window_minutes),
                start=start,
                dataset=",".join(names),
            )
            Path(args.out).write_text(
                json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(
                json.dumps(
                    {k: out[k] for k in ("estimated_hours", "first_position", "labs")}
                )
            )
            return 0
        plan_json = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        log = json.loads(Path(args.log).read_text(encoding="utf-8"))
        result = check(plan_json, log, rules)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if not result["violations"] else 1
    except ScheduleError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
