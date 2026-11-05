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

    .venv/bin/python -m scripts.eval_pair_schedule plan --dataset evals/datasets/dev-v2 \\
        --labs labs.json [--rules RULES] [--window-minutes 27] --out plan.json
    .venv/bin/python -m scripts.eval_pair_schedule check --plan plan.json --log log.json \\
        [--rules RULES]
    .venv/bin/python -m scripts.eval_pair_schedule rejoin --log log.json --labs labs.json \\
        --lab NAME --round-prefix P --recovery OUTPUT_ROOT/evidence/ROUND/recovery-*.json

``rejoin`` puts a lab that a running ``run`` stopped (unconfirmed restore,
exit 4, interrupted window) back into its rotation once ``eval_live
--recover`` confirmed it normal: it reads that recovery record on the lab
(``confirm_recovery``) and refuses without one. It writes
``<log>.rejoin/<lab>.json``, which ``run`` polls while another lab still
runs; the lab's next window records the rejoin. When every lab is stopped,
``run`` ends and the remaining windows stay MISSING, as before.

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
#: The void classes in precedence order and what each reruns; the rules
#: file must state exactly these (``load_rules``).
VOID_ACTIONS = {
    "HOST_NOISE": "rerun_pair",
    "LAB_RESTARTED": "rerun_pair",
    "HARNESS_INTERRUPTED": "rerun_pair",
    "ENV_SETUP_FAILED": "rerun_slot",
    "PROVIDER_5XX_STORM": "rerun_slot",
}
VOID_CLASSES = tuple(VOID_ACTIONS)
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
    #: per void class, the note an original keeps once it is rerun
    notes: Mapping[str, str] = field(default_factory=dict)


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
            [(c["code"], c["action"]) for c in void["classes"]]
            == list(VOID_ACTIONS.items()),
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
            notes={str(c["code"]): str(c["note"]) for c in void["classes"]},
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
    #: the voided window this one reruns (all of it, or only some systems)
    rerun_of: str | None = None

    def rerun(self, systems: Sequence[str] | None = None) -> Window:
        keep = (
            self.systems
            if systems is None
            else tuple(s for s in self.systems if s in systems)
        )
        return Window(
            f"{self.window_id}-rerun",
            self.scenario_id,
            self.repetition,
            keep,
            self.attempt + 1,
            self.window_id,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "scenario_id": self.scenario_id,
            "repetition": self.repetition,
            "systems": list(self.systems),
            "attempt": self.attempt,
            "rerun_of": self.rerun_of,
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
    i-th paired scenario (counting only scenarios HolmesGPT answers) puts
    OpsPilot first when ``i + r`` is even: each scenario alternates, and
    consecutive paired scenarios start on opposite sides, so the totals of
    first positions differ by at most one."""
    out = []
    i = -1
    for scenario in scenarios:
        paired = scenario.entry in rules.holmes_entries
        i += paired
        for r in range(1, rules.repetitions + 1):
            if paired:
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
    #: the lab's last restore is unconfirmed: it takes no further window
    stopped: str | None = None
    #: the window that stopped it: (window_id, scenario_id, ended_at)
    stopped_by: tuple[str, str, datetime | None] | None = None
    #: a rejoin after ``eval_live --recover``, booked on the next window
    rejoined: dict[str, Any] | None = None


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
            if state.stopped is not None:
                return None, None
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
            stop = restore_unconfirmed(record)
            restored = None if stop else restored_at(record)
            if stop:
                # never advance the wash or the gap from an unconfirmed
                # restore: the lab stops until an engineer ran --recover
                state.stopped = stop
                state.stopped_by = (
                    window.window_id,
                    window.scenario_id,
                    _moment(record.get("ended_at")),
                )
                record["lab_stopped"] = stop
            if state.rejoined is not None:
                record["rejoined"], state.rejoined = state.rejoined, None
            if restored is not None:
                state.last_restored = restored
                state.restored[window.scenario_id] = restored
                state.previous = {
                    "window_id": window.window_id,
                    "scenario_id": window.scenario_id,
                    "restored_at": restored.isoformat(),
                }
            decision = decide(record, self.rules)
            void = decision["void"]
            record["void"] = void
            record["decision"] = {k: decision[k] for k in ("action", "systems", "note")}
            if void is not None and window.attempt < self.rules.max_reruns:
                rerun = window.rerun(
                    None if decision["action"] == "rerun_pair" else decision["systems"]
                )
                self.rank[rerun.window_id] = self.rank.get(window.window_id, 0)
                self.pending.append(rerun)
                record["rerun_window_id"] = rerun.window_id
            self.records.append(record)
            state.busy = False
            self.in_flight -= 1
            return None if void is None else str(void)

    def done(self) -> bool:
        with self.lock:
            return not self.pending and self.in_flight == 0

    def stopped(self, lab: str) -> bool:
        with self.lock:
            return self.labs[lab].stopped is not None

    def all_stopped(self) -> bool:
        with self.lock:
            return all(state.stopped is not None for state in self.labs.values())

    def rejoin(self, lab: str, recovered_at: datetime, after: str) -> bool:
        """Put a stopped lab back after an engineer ran ``eval_live
        --recover`` (rules ``lab_state``: the lab stops *until* then).
        ``after`` names the window whose stop the recovery answers: a
        recovery of an earlier stop never ends a later one. ``recovered_at``
        (when the recovery confirmed normal) must be after the stopping
        window ended; the wash and the stopped scenario's gap count from it.
        The next window of the lab books it as ``rejoined`` (``check`` reads
        it). False when the lab is not stopped, was stopped by another
        window, or the recovery predates the stop."""
        with self.lock:
            state = self.labs[lab]
            if state.stopped is None or state.stopped_by is None:
                return False
            wid, sid, ended = state.stopped_by
            if wid != after:
                return False
            if ended is not None and recovered_at <= ended:
                return False
            state.rejoined = {
                "after": wid,
                "stop": state.stopped,
                "recovered_at": recovered_at.isoformat(),
            }
            state.stopped, state.stopped_by = None, None
            state.last_restored = recovered_at
            state.restored[sid] = recovered_at
            return True


def restore_unconfirmed(record: Mapping[str, Any]) -> str | None:
    """Why a finished window leaves its lab in an unknown state, or None:
    the driver's own restore/wash failed (exit 4), the window was
    interrupted, or it injected and no restore was confirmed."""
    if record.get("exit_code") == 4:
        return "EXIT_4_RETAINED_FAULT"
    if record.get("interrupted"):
        return "INTERRUPTED"
    if record.get("injected", True) and _moment(record.get("restored_at")) is None:
        return "RESTORE_UNCONFIRMED"
    return None


def restored_at(record: Mapping[str, Any]) -> datetime | None:
    """When the lab was back to normal after a window: its confirmed restore,
    or for a window that injected nothing (``injected`` False) its end."""
    if record.get("injected", True):
        return _moment(record.get("restored_at"))
    return _moment(record.get("restored_at")) or _moment(record.get("ended_at"))


# -- void classification --------------------------------------------------------


def _rules() -> Rules:
    return load_rules()


def decide(
    record: Mapping[str, Any],
    rules: Rules,
    *,
    shared_host: bool | None = None,
    probe_baseline_p95_ms: float | None = None,
) -> dict[str, Any]:
    """The preregistered rerun decision of a finished window (or pair):
    ``{"action": "keep" | "rerun_pair" | "rerun_slot", "systems": [...],
    "note": str | None, "void": class | None}``.

    Classes in precedence order (``VOID_ACTIONS``); a window that is itself
    a rerun (``rerun_of`` set) is always kept. Every slot fact comes from the
    window runner; nothing here judges an answer."""
    keep: dict[str, Any] = {"action": "keep", "systems": [], "note": None, "void": None}
    if record.get("rerun_of"):
        return keep
    slots = [s for s in record.get("slots") or [] if isinstance(s, Mapping)]
    systems = [str(s.get("system")) for s in slots]
    shared = record.get("shared_host") if shared_host is None else shared_host
    p95 = record.get("baseline_p95_ms")
    if probe_baseline_p95_ms is None:
        probe_baseline_p95_ms = record.get("probe_baseline_p95_ms")
    p95_noisy = (
        isinstance(p95, (int, float))
        and isinstance(probe_baseline_p95_ms, (int, float))
        and probe_baseline_p95_ms > 0
        and p95 > BASELINE_P95_LIMIT_RATIO * probe_baseline_p95_ms
    )
    found: dict[str, list[str]] = {}
    if shared and (p95_noisy or any(host_noisy(s.get("host_noise")) for s in slots)):
        found["HOST_NOISE"] = systems
    restarts = record.get("lab_restarts")
    if isinstance(restarts, Mapping) and restarts.get("changed") is True:
        found["LAB_RESTARTED"] = systems
    if record.get("interrupted") or any(
        s.get("status") == "not_started" for s in slots
    ):
        found["HARNESS_INTERRUPTED"] = systems
    failed = [
        str(s.get("system"))
        for s in slots
        if s.get("status") == "setup_failed" and not s.get("product_run_created")
    ]
    if failed:
        found["ENV_SETUP_FAILED"] = failed
    outage = [
        str(s.get("system"))
        for s in slots
        if s.get("provider_error") is True and not record.get("model_fault")
    ]
    if outage:
        found["PROVIDER_5XX_STORM"] = outage
    for code in rules.void_classes:
        if code in found:
            return {
                "action": VOID_ACTIONS[code],
                "systems": found[code],
                "note": rules.notes.get(code),
                "void": code,
            }
    return keep


def rerun_decision(
    pair: Mapping[str, Any], *, shared_host: bool, probe_baseline_p95_ms: float
) -> dict[str, Any]:
    """Public form of ``decide`` for one executed pair (acceptance entry)."""
    out = decide(
        pair,
        _rules(),
        shared_host=shared_host,
        probe_baseline_p95_ms=probe_baseline_p95_ms,
    )
    return {k: out[k] for k in ("action", "systems", "note")}


def classify_void(record: Mapping[str, Any], rules: Rules) -> str | None:
    """The void class ``decide`` gives a finished window, or None."""
    void = decide(record, rules)["void"]
    return None if void is None else str(void)


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
    if any(d < 0 for d in delta):
        return None
    if total == 0:
        return 0.0
    return round(100.0 * delta[7] / total, 3)


# -- planner ---------------------------------------------------------------------


def simulate(
    pending: Sequence[Window],
    labs: Sequence[str],
    rules: Rules,
    *,
    window_length: timedelta,
    start: datetime,
    per_system: bool = False,
) -> list[dict[str, Any]]:
    """The dispatcher run with every window taking ``window_length`` (times
    the number of its systems with ``per_system``) from injection to
    restore: the planned order, lab and estimated times."""
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
        end = begin + window_length * (len(window.systems) if per_system else 1)
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
    roots: Mapping[str, str] | None = None,
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
            s.scenario_id: {
                "entry": s.entry,
                "model_fault": s.model_fault,
                # the scenario root the runner passes as --scenario-root
                "root": (roots or {}).get(s.scenario_id),
            }
            for s in scenarios
        },
    }


# -- acceptance entry points (S7c) ---------------------------------------------------


def rules_sha256() -> str:
    """sha256 of the preregistered rules file as committed."""
    digest = load_rules().sha256
    assert digest is not None
    return digest


#: The preregistered void/rerun rules (``RULES_PATH``), as written.
RULES: Mapping[str, Any] = json.loads(RULES_PATH.read_text(encoding="utf-8"))


def plan_schedule(
    scenarios: Sequence[tuple[Any, Any]],
    *,
    k: int = 3,
    labs: int = 1,
    slot_seconds: float,
    wash_seconds: float = 600,
    same_scenario_gap_seconds: float = 3600,
    seed: str,
) -> dict[str, Any]:
    """The planned pairs over ``labs`` independent labs, each slot taking
    ``slot_seconds``; deterministic (``seed`` is recorded, the order is the
    fixed dispatcher order). The minimums of the rules cannot be lowered."""
    base = load_rules()
    if wash_seconds < base.wash_min.total_seconds():
        raise ValueError("wash_seconds below the registered minimum")
    if same_scenario_gap_seconds < base.same_scenario_gap.total_seconds():
        raise ValueError("same_scenario_gap_seconds below the registered minimum")
    if k < 1 or labs < 1 or slot_seconds <= 0:
        raise ValueError("k, labs and slot_seconds must be positive")
    from dataclasses import replace

    rules = replace(
        base,
        repetitions=k,
        wash_min=timedelta(seconds=wash_seconds),
        same_scenario_gap=timedelta(seconds=same_scenario_gap_seconds),
    )
    found = tuple(
        Scenario(
            spec.scenario_id,
            spec.entry,
            any(i.kind == "model_transport" for i in answer.injectors),
        )
        for spec, answer in scenarios
    )
    names = [str(n) for n in range(labs)]
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    records = simulate(
        windows(found, rules),
        names,
        rules,
        window_length=timedelta(seconds=slot_seconds),
        start=epoch,
        per_system=True,
    )

    def seconds(value: Any) -> float:
        moment = _moment(value)
        assert moment is not None
        return (moment - epoch).total_seconds()

    return {
        "k": k,
        "labs": labs,
        "seed": seed,
        "rules_sha256": rules.sha256,
        "pairs": [
            {
                "pair_id": r["window_id"],
                "lab": int(r["lab"]),
                "scenario_id": r["scenario_id"],
                "attempt": r["repetition"],
                "systems": list(r["systems"]),
                "start_s": seconds(r["injected_at"]),
                "end_s": seconds(r["restored_at"]),
            }
            for r in records
        ],
    }


def annotate_executed(slots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Executed slots in input order, each with ``predecessor`` (the latest
    earlier pair on the same lab: id, scenario, minutes from its end to this
    pair's start) and ``violations`` (``WASH_SHORT``, ``SAME_SCENARIO_GAP``
    against the registered minimums). Labs are independent; a pair's
    partner slot is never its predecessor."""
    rules = load_rules()
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for slot in slots:
        key = (str(slot["lab"]), str(slot["pair_id"]))
        began, ended = _moment(slot.get("started_at")), _moment(slot.get("ended_at"))
        item = pairs.setdefault(
            key, {"scenario_id": slot["scenario_id"], "start": began, "end": ended}
        )
        if began is not None and (item["start"] is None or began < item["start"]):
            item["start"] = began
        if ended is not None and (item["end"] is None or ended > item["end"]):
            item["end"] = ended
    notes: dict[tuple[str, str], tuple[dict[str, Any] | None, list[str]]] = {}
    for (lab, pid), item in pairs.items():
        earlier = [
            (other_pid, other)
            for (other_lab, other_pid), other in pairs.items()
            if other_lab == lab
            and other_pid != pid
            and other["end"] is not None
            and item["start"] is not None
            and other["end"] <= item["start"]
        ]
        predecessor = None
        violations: list[str] = []
        if earlier:
            last_pid, last = max(earlier, key=lambda e: e[1]["end"])
            gap = item["start"] - last["end"]
            predecessor = {
                "pair_id": last_pid,
                "scenario_id": last["scenario_id"],
                "gap_minutes": round(gap.total_seconds() / 60, 2),
            }
            if gap < rules.wash_min:
                violations.append("WASH_SHORT")
            if any(
                other["scenario_id"] == item["scenario_id"]
                and item["start"] - other["end"] < rules.same_scenario_gap
                for _, other in earlier
            ):
                violations.append("SAME_SCENARIO_GAP")
        notes[(lab, pid)] = (predecessor, violations)
    out = []
    for slot in slots:
        predecessor, violations = notes[(str(slot["lab"]), str(slot["pair_id"]))]
        out.append(
            {
                **slot,
                "predecessor": None if predecessor is None else dict(predecessor),
                "violations": list(violations),
            }
        )
    return out


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
        unconfirmed: tuple[str, str, datetime | None] | None = None
        for record in items:
            wid = str(record.get("window_id"))
            if unconfirmed is not None:
                back = record.get("rejoined")
                recovered = (
                    _moment(back.get("recovered_at"))
                    if isinstance(back, Mapping) and back.get("after") == unconfirmed[0]
                    else None
                )
                if recovered is not None and (
                    unconfirmed[2] is None or recovered > unconfirmed[2]
                ):
                    # rejoined after eval_live --recover: wash and gap count
                    # from the recovery
                    last_restored = recovered
                    restored_by_scenario[unconfirmed[1]] = recovered
                    unconfirmed = None
                else:
                    violations.append(f"AFTER_UNCONFIRMED_RESTORE:{wid}")
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
            stop = restore_unconfirmed(record)
            if stop is not None:
                violations.append(f"{stop}:{wid}")
                unconfirmed = (
                    wid,
                    str(record.get("scenario_id")),
                    _moment(record.get("ended_at")),
                )
            restored = None if stop else restored_at(record)
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
        expected = list(origin["systems"])
        if wid in seen:
            violations.append(f"RERUN_UNJUSTIFIED:{wid}")
        elif wid != base:
            original = seen.get(base)
            if original is None or original.get("void") is None:
                violations.append(f"RERUN_UNJUSTIFIED:{wid}")
            elif (original.get("decision") or {}).get("action") == "rerun_slot":
                chosen = set(original["decision"].get("systems") or ())
                expected = [s for s in expected if s in chosen]
        if order != expected:
            violations.append(f"ORDER_MISMATCH:{wid}")
        seen[wid] = record
    counted: dict[str, dict[str, int]] = {}
    missing: dict[str, dict[str, int]] = {}
    voided: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for wid, origin in planned.items():
        sid = origin["scenario_id"]
        counts = counted.setdefault(sid, {s: 0 for s in SYSTEMS})
        lost = missing.setdefault(sid, {s: 0 for s in SYSTEMS})
        found = seen.get(wid)
        if found is None:
            violations.append(f"MISSING:{wid}")
            for system in origin["systems"]:
                lost[system] += 1  # a planned slot that never ran: a failure
            continue
        final = {str(s.get("system")): s for s in found.get("slots") or []}
        rerun = seen.get(f"{wid}-rerun")
        if found.get("void") is not None and rerun is not None:
            replaced = {str(s.get("system")): s for s in rerun.get("slots") or []}
            voided.append(
                {
                    "window_id": wid,
                    "void": found["void"],
                    "rerun": f"{wid}-rerun",
                    "systems": sorted(replaced),
                }
            )
            note = (found.get("decision") or {}).get("note") or rules.notes.get(
                str(found["void"])
            )
            for system in sorted(replaced):
                old = final.get(system) or {}
                attempts.append(
                    {
                        "system": system,
                        "scenario_id": sid,
                        "window_id": wid,
                        "rule": found["void"],
                        "note": note,
                        "voided_attempt_id": old.get("attempt_id"),
                        "rerun_window_id": f"{wid}-rerun",
                        "rerun_attempt_id": replaced[system].get("attempt_id"),
                    }
                )
            final.update(replaced)
        for system in origin["systems"]:
            slot = final.get(system)
            if slot is None or slot.get("status") == "not_started":
                lost[system] += 1
            else:
                counts[system] += 1
    return {
        "rules_id": rules.rules_id,
        "violations": violations,
        "voided": voided,
        # X3: every voided attempt of either system, with its rule; the
        # judge drops these, eval_dev_round takes the OpsPilot ones
        "voided_attempts": attempts,
        "started_slots": counted,
        "missing_slots": missing,
    }


def dev_round_reruns(result: Mapping[str, Any]) -> dict[str, Any]:
    """The OpsPilot part of ``check``'s ``voided_attempts`` as an
    ``eval_dev_round --reruns`` registry (rule named per entry)."""
    from scripts.eval_dev_round import RERUNS_SCHEMA

    return {
        "schema": RERUNS_SCHEMA,
        "reruns": [
            {
                "voided_attempt_id": a["voided_attempt_id"],
                "rerun_attempt_id": a["rerun_attempt_id"],
                "note": a["note"],
                "rule": a["rule"],
            }
            for a in result.get("voided_attempts") or []
            if a["system"] == "opspilot"
            and a.get("voided_attempt_id")
            and a.get("rerun_attempt_id")
        ],
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
    rejoin: Callable[[str], tuple[str, datetime] | None] | None = None,
) -> list[dict[str, Any]]:
    """One thread per lab, each taking windows from the shared dispatcher
    until nothing is pending or in flight. Records are booked in finish
    order; ``on_record`` sees each (the CLI appends it to the log).

    A stopped lab ends its thread, unless ``rejoin`` is given: then it waits
    (while windows remain and another lab is not stopped) for ``rejoin(lab)`` to report when an engineer's
    ``eval_live --recover`` confirmed the lab normal, and takes windows
    again from then (``Dispatcher.rejoin``)."""
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
                if dispatcher.stopped(lab):
                    if rejoin is None:
                        return
                    marker = rejoin(lab)
                    if marker is not None and dispatcher.rejoin(
                        lab, marker[1], after=marker[0]
                    ):
                        continue
                    if dispatcher.all_stopped():
                        # no lab runs: end as without rejoin; the pending
                        # windows stay MISSING in ``check``
                        return
                    clock.sleep(poll_seconds)
                    continue
                wait = (
                    poll_seconds
                    if ready is None
                    else (ready - clock.now()).total_seconds()
                )
                clock.sleep(max(0.0, min(wait, poll_seconds)))
                continue
            dispatched = clock.now()
            try:
                record = runner.run(lab, window)
            except BaseException as exc:  # noqa: BLE001 - booked, then the lab stops
                record = {
                    "interrupted": True,
                    "error": type(exc).__name__,
                    # check() orders a lab's records by injected_at/started_at;
                    # the dispatch time keeps this stop before a rejoin's windows
                    "started_at": dispatched.isoformat(),
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
                continue  # the lab is stopped: ends, or waits for a rejoin
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


# -- the live window runner ---------------------------------------------------------

#: Names of environment variables a labs file may never carry (credentials
#: reach the host by the user's own command, never through this file).
SECRET_MARKERS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "AUTH")


@dataclass(frozen=True)
class LabSpec:
    """One lab: where the live driver runs (``host`` an ssh alias, or None
    for this machine), its checkout, the non-secret environment that picks
    the lab instance (``OPSPILOT_LAB_INSTANCE``, ``OPSPILOT_LAB_LEDGERS``,
    ``OPSPILOT_OTEL_*_URL``), its PG port, flagd URL and output root."""

    name: str
    repo: str
    pg_port: int
    flagd_url: str
    output_root: str
    host: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    shared_host: bool = False
    #: the S5 probe's baseline checkout p95 for this host (HOST_NOISE)
    probe_baseline_p95_ms: float | None = None


def load_lab_specs(path: Path) -> dict[str, LabSpec]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] != LABS_SCHEMA:
            raise ScheduleError("LABS_SCHEMA")
        specs = {
            str(item["name"]): LabSpec(
                name=str(item["name"]),
                repo=str(item["repo"]),
                pg_port=int(item["pg_port"]),
                flagd_url=str(item["flagd_url"]),
                output_root=str(item["output_root"]),
                host=item.get("host"),
                env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
                shared_host=bool(item.get("shared_host", False)),
                probe_baseline_p95_ms=item.get("probe_baseline_p95_ms"),
            )
            for item in data["labs"]
        }
    except (OSError, ValueError, KeyError, TypeError):
        raise ScheduleError("LABS_INVALID") from None
    if len(specs) != len(data["labs"]) or not specs:
        raise ScheduleError("LABS_INVALID")
    for spec in specs.values():
        if any(m in k.upper() for k in spec.env for m in SECRET_MARKERS):
            raise ScheduleError("LABS_SECRET_IN_ENV")
    ports = [(s.host, s.pg_port) for s in specs.values()]
    roots = [(s.host, s.output_root) for s in specs.values()]
    ledgers = [(s.host, s.env.get("OPSPILOT_LAB_LEDGERS")) for s in specs.values()]
    instances = [
        (s.host, s.env.get("OPSPILOT_LAB_INSTANCE", "0")) for s in specs.values()
    ]
    for seen in (ports, roots, ledgers, instances):
        if len(set(seen)) != len(seen):
            # two labs on one host sharing a port, output root, ledger root
            # or lab instance would be one lab
            raise ScheduleError("LABS_SHARED_RESOURCE")
    return specs


Execute = Callable[[LabSpec, Sequence[str]], tuple[int, str]]


def _shell(spec: LabSpec, argv: Sequence[str]) -> list[str]:
    import shlex

    env = [f"{k}={shlex.quote(v)}" for k, v in sorted(spec.env.items())]
    command = " ".join(["env", *env, shlex.join(argv)]) if env else shlex.join(argv)
    line = f"cd {shlex.quote(spec.repo)} && {command}"
    if spec.host is None:
        return ["bash", "-c", line]
    return ["ssh", "-o", "BatchMode=yes", spec.host, line]


def subprocess_execute(spec: LabSpec, argv: Sequence[str]) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(
        _shell(spec, argv), capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


#: ``scripts.eval_live`` exit code of a usage error (``_main``: argument
#: checks, before ``real_deps`` builds any lab adapter).
USAGE_EXIT = 2


class CommandWindowRunner:
    """Runs a window as one ``scripts.eval_live`` round on its lab and reads
    the round's ``lifecycle.json`` and ``pair.json`` back."""

    def __init__(
        self,
        labs: Mapping[str, LabSpec],
        roots: Mapping[str, str],
        *,
        round_prefix: str,
        holder: str,
        batch: str,
        execute: Execute = subprocess_execute,
        python: str = ".venv/bin/python",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.labs, self.roots = labs, roots
        self.prefix, self.holder, self.batch = round_prefix, holder, batch
        self.execute, self.python = execute, python
        self.now = now or SystemClock().now

    def round_id(self, window: Window) -> str:
        return f"{self.prefix}-{window.window_id}"

    def argv(self, lab: str, window: Window) -> list[str]:
        spec = self.labs[lab]
        if window.systems == ("opspilot",):
            pair = "opspilot-only"
        else:
            pair = f"{window.systems[0]}-first"
        return [
            self.python, "-m", "scripts.eval_live", "--mode", "live",
            "--round", self.round_id(window),
            "--scenario", window.scenario_id, "--n", "1", "--parallel", "1",
            "--holder", self.holder, "--batch", self.batch,
            "--flagd-url", spec.flagd_url, "--pg-port", str(spec.pg_port),
            "--scenario-root", self.roots[window.scenario_id],
            "--output-root", spec.output_root,
            "--pair", pair,
        ]  # fmt: skip

    def _read(self, spec: LabSpec, relative: str) -> Any:
        code, out = self.execute(spec, ["cat", relative])
        if code != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def run(self, lab: str, window: Window) -> dict[str, Any]:
        spec = self.labs[lab]
        launched = self.now()
        code, _ = self.execute(spec, self.argv(lab, window))
        evidence = f"{spec.output_root}/evidence/{self.round_id(window)}"
        lifecycle = self._read(spec, f"{evidence}/lifecycle.json") or {}
        slots = lifecycle.get("slots") or []
        slot = slots[0] if slots and isinstance(slots[0], Mapping) else {}
        pair = None
        if slot.get("attempt_id"):
            pair = self._read(spec, f"{evidence}/runs/{slot['attempt_id']}/pair.json")
        timings = slot.get("timings") or {}
        fault = slot.get("fault") or {}
        refused = code == USAGE_EXIT and not slot
        record: dict[str, Any] = {
            "round_id": self.round_id(window),
            "exit_code": code,
            "attempt_id": slot.get("attempt_id"),
            "started_at": timings.get("started_at"),
            "injected_at": fault.get("effective_at") or timings.get("started_at"),
            "restored_at": fault.get("normal_confirmed_at"),
            "ended_at": timings.get("ended_at"),
            "shared_host": spec.shared_host,
            "probe_baseline_p95_ms": spec.probe_baseline_p95_ms,
            "interrupted": (code == 130 or not slot) and not refused,
            "injected": slot.get("fault") is not None if slot else not refused,
        }
        if refused:
            # eval_live exits 2 only on a usage error, before any lab
            # adapter exists: nothing was injected, nothing to --recover.
            # Its slots never started (HARNESS_INTERRUPTED, rerun_pair);
            # the lab is not stopped. It has no lifecycle timings: its
            # dispatch time orders it among the lab's windows in ``check``
            # (no ended_at: it restored nothing, so it advances no wash or
            # gap).
            record["refused"] = "USAGE"
            record["started_at"] = launched.isoformat()
        if isinstance(pair, Mapping):
            record["baseline_p95_ms"] = pair.get("baseline_p95_ms")
            record["lab_restarts"] = pair.get("lab_restarts")
        if isinstance(pair, Mapping) and isinstance(pair.get("slots"), list):
            record["slots"] = [dict(s) for s in pair["slots"]]
        else:
            record["slots"] = [
                {"system": s, "position": n + 1, "status": "not_started"}
                for n, s in enumerate(window.systems)
            ]
        return record


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
    parser = argparse.ArgumentParser(prog="scripts.eval_pair_schedule")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--dataset", required=True, action="append")
    p.add_argument("--labs", required=True)
    p.add_argument("--rules", default=str(RULES_PATH))
    p.add_argument("--window-minutes", type=float, default=27.0)
    p.add_argument("--start")
    p.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--plan", required=True)
    r.add_argument("--labs", required=True)
    r.add_argument("--rules", default=str(RULES_PATH))
    r.add_argument("--round-prefix", required=True)
    r.add_argument("--holder", required=True)
    r.add_argument("--batch", required=True)
    r.add_argument("--log", required=True)
    c = sub.add_parser("check")
    c.add_argument("--plan", required=True)
    c.add_argument("--log", required=True)
    c.add_argument("--rules", default=str(RULES_PATH))
    # X3: the OpsPilot voided attempts as an eval_dev_round --reruns file
    c.add_argument("--reruns-out")
    j = sub.add_parser("rejoin")
    j.add_argument("--log", required=True)
    j.add_argument("--labs", required=True)
    j.add_argument("--lab", required=True)
    j.add_argument("--round-prefix", required=True)
    j.add_argument("--recovery", required=True)
    return parser


def rejoin_dir(log_path: Path) -> Path:
    """Where ``rejoin`` leaves a recovered lab for a running ``run``."""
    return log_path.with_name(log_path.name + ".rejoin")


def _lab_file(log_path: Path, lab: str) -> Path:
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", lab):
        raise ScheduleError("LAB_NAME_INVALID")
    return rejoin_dir(log_path) / f"{lab}.json"


def write_rejoin(log_path: Path, lab: str, after: str, recovered_at: datetime) -> Path:
    """The marker answers the stop of window ``after`` only."""
    path = _lab_file(log_path, lab)
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    document = {"lab": lab, "after": after, "recovered_at": recovered_at.isoformat()}
    tmp.write_text(json.dumps(document))
    tmp.replace(path)
    return path


#: ``scripts.eval_live --recover`` writes this record into the round's
#: evidence only after its restore and wash confirmed the lab normal
#: (``_recover_round``); nothing else writes it.
RECOVERY_SCHEMA = "opspilot-eval-live-recovery-1"
#: Run on the lab, in its checkout and environment: the driver's coordination
#: state in its ledger root now, through the driver's own helpers (the ledger
#: root ``eval_live`` uses, its restore intent ``STATE_FILE``, F11's
#: ``.active-*.json`` run markers).
LEDGER_STATE_PROBE = (
    "import json\n"
    "from scripts.f11_u2_harness import resolve_ledger_root\n"
    "from scripts.eval_live_lab import STATE_FILE\n"
    "root = resolve_ledger_root()\n"
    "state = root / STATE_FILE\n"
    "print(json.dumps({'retained': state.exists() or state.is_symlink(),\n"
    "    'active': sorted(p.name for p in root.glob('.active-*.json'))}))\n"
)


def confirm_recovery(
    log_path: Path,
    lab: str,
    spec: LabSpec,
    round_prefix: str,
    recovery: str,
    *,
    now: datetime,
    execute: Execute | None = None,
    python: str = ".venv/bin/python",
) -> tuple[str, datetime]:
    """The stopping window and when ``eval_live --recover`` confirmed ``lab``
    normal after it: the ``recovered_at`` of the recovery record ``recovery`` (read on the
    lab), which must be the stopping window's round (and attempt), later
    than that window's end and not later than ``now``; and the lab's ledger
    root holds no restore intent and no active run marker now (``--recover``
    can write the record and still exit 1 with its marker left). Raises
    otherwise."""
    import posixpath
    import re

    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
        windows = [w for w in log["windows"] if isinstance(w, Mapping)]
    except (OSError, ValueError, KeyError, TypeError):
        raise ScheduleError("LOG_INVALID") from None
    stops = [w for w in windows if w.get("lab") == lab and w.get("lab_stopped")]
    if not stops:
        raise ScheduleError("LAB_NOT_STOPPED")
    stop = stops[-1]
    round_id = stop.get("round_id") or f"{round_prefix}-{stop.get('window_id')}"
    # the paths live on the lab, so normalize lexically (no local resolve)
    folder = posixpath.normpath(f"{spec.output_root}/evidence/{round_id}")
    recovery = posixpath.normpath(recovery)
    name = recovery.rsplit("/", 1)[-1]
    if recovery != f"{folder}/{name}" or not re.fullmatch(
        r"recovery-\d{8}T\d{6}Z(-\d{1,2})?\.json", name
    ):
        raise ScheduleError("RECOVERY_PATH_INVALID")
    run = execute or subprocess_execute
    code, out = run(spec, ["cat", recovery])
    if code != 0:
        raise ScheduleError("RECOVERY_UNREADABLE")
    try:
        document = json.loads(out)
    except ValueError:
        raise ScheduleError("RECOVERY_INVALID") from None
    if (
        not isinstance(document, dict)
        or document.get("schema") != RECOVERY_SCHEMA
        or document.get("round") != round_id
        or (
            stop.get("attempt_id") is not None
            and document.get("attempt_id") != stop.get("attempt_id")
        )
    ):
        raise ScheduleError("RECOVERY_INVALID")
    recovered = _moment(document.get("recovered_at"))
    if recovered is None:
        raise ScheduleError("RECOVERY_INVALID")
    if recovered > now:
        raise ScheduleError("RECOVERED_AT_IN_FUTURE")
    ended = _moment(stop.get("ended_at"))
    if ended is not None and recovered <= ended:
        raise ScheduleError("RECOVERY_BEFORE_STOP")
    code, out = run(spec, [python, "-c", LEDGER_STATE_PROBE])
    try:
        state = json.loads(out.strip().splitlines()[-1]) if code == 0 else None
    except (ValueError, IndexError):
        state = None
    if (
        not isinstance(state, dict)
        or type(state.get("retained")) is not bool
        or not isinstance(state.get("active"), list)
    ):
        raise ScheduleError("LAB_STATE_UNCONFIRMED")
    if state["retained"]:
        raise ScheduleError("LAB_FAULT_RETAINED")
    if state["active"]:
        raise ScheduleError("LAB_DRIVER_ACTIVE")
    return str(stop.get("window_id")), recovered


def read_rejoin(log_path: Path) -> Callable[[str], tuple[str, datetime] | None]:
    """``run_schedule``'s ``rejoin``: (the stopping window, the recovery
    time) a ``rejoin`` command left for a lab, or None."""

    def read(lab: str) -> tuple[str, datetime] | None:
        try:
            data = json.loads(_lab_file(log_path, lab).read_text(encoding="utf-8"))
        except (OSError, ValueError, ScheduleError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("after"), str):
            return None
        recovered = _moment(data.get("recovered_at"))
        return None if recovered is None else (data["after"], recovered)

    return read


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "rejoin":
            specs = load_lab_specs(Path(args.labs))
            if args.lab not in specs:
                raise ScheduleError("LAB_UNKNOWN")
            after, recovered = confirm_recovery(
                Path(args.log),
                args.lab,
                specs[args.lab],
                args.round_prefix,
                args.recovery,
                now=datetime.now(UTC),
            )
            path = write_rejoin(Path(args.log), args.lab, after, recovered)
            print(json.dumps({"lab": args.lab, "rejoin": str(path)}))
            return 0
        rules = load_rules(Path(args.rules))
        if args.command == "plan":
            from scripts.eval_live import dataset_dimension

            scenarios: list[Scenario] = []
            names = []
            roots: dict[str, str] = {}
            for root in args.dataset:
                found = scenarios_from_dataset(Path(root))
                scenarios += found
                roots.update((s.scenario_id, root) for s in found)
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
                roots=roots,
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
        if args.command == "run":
            return _run(args, rules, plan_json)
        log = json.loads(Path(args.log).read_text(encoding="utf-8"))
        result = check(plan_json, log, rules)
        if args.reruns_out:
            Path(args.reruns_out).write_text(
                json.dumps(dev_round_reruns(result), indent=2, sort_keys=True) + "\n"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if not result["violations"] else 1
    except ScheduleError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, rules: Rules, plan_json: Mapping[str, Any]) -> int:
    specs = load_lab_specs(Path(args.labs))
    result = execute_plan(
        plan_json,
        specs,
        rules,
        Path(args.log),
        round_prefix=args.round_prefix,
        holder=args.holder,
        batch=args.batch,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if not result["violations"] else 1


def execute_plan(
    plan_json: Mapping[str, Any],
    specs: Mapping[str, LabSpec],
    rules: Rules,
    log_path: Path,
    *,
    round_prefix: str,
    holder: str,
    batch: str,
    runner: WindowRunner | None = None,
    poll_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute a plan over ``specs``; the log is rewritten after every
    finished window; returns ``check`` of the finished log."""
    if plan_json.get("rules_sha256") != rules.sha256:
        raise ScheduleError("PLAN_RULES_MISMATCH")
    if log_path.exists():
        raise ScheduleError("LOG_EXISTS")
    meta = plan_json["scenarios"]
    if any(not v.get("root") for v in meta.values()):
        raise ScheduleError("PLAN_ROOT_MISSING")
    scenarios = {
        sid: Scenario(sid, v["entry"], bool(v["model_fault"]))
        for sid, v in meta.items()
    }
    pending = [
        Window(w["window_id"], w["scenario_id"], w["repetition"], tuple(w["systems"]))
        for w in plan_json["windows"]
    ]
    if runner is None:
        runner = CommandWindowRunner(
            specs,
            {sid: str(v["root"]) for sid, v in meta.items()},
            round_prefix=round_prefix,
            holder=holder,
            batch=batch,
        )
    lock = threading.Lock()
    booked: list[dict[str, Any]] = []

    def write(record: dict[str, Any]) -> None:
        with lock:
            booked.append(record)
            document = {
                "schema": LOG_SCHEMA,
                "rules_id": rules.rules_id,
                "rules_sha256": rules.sha256,
                "windows": booked,
            }
            tmp = log_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
            tmp.replace(log_path)

    run_schedule(
        pending,
        tuple(specs),
        rules,
        runner,
        scenarios=scenarios,
        on_record=write,
        poll_seconds=poll_seconds,
        rejoin=read_rejoin(log_path),
    )
    if not booked:
        write_empty = {"schema": LOG_SCHEMA, "rules_id": rules.rules_id, "windows": []}
        log_path.write_text(json.dumps(write_empty) + "\n")
    return check(plan_json, json.loads(log_path.read_text()), rules)


if __name__ == "__main__":
    sys.exit(main())
