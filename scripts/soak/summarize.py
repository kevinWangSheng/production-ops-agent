"""``summary.json`` from a soak raw directory (S-R4–S-R13, S-R15).

    python3 -m scripts.soak.summarize RAW_DIR --out SUMMARY_JSON

Offline and deterministic: reads only the raw files (layout and every rule:
``tests/acceptance/test_soak_summarize.py`` module docstring and
``scripts/soak/README.md``), never calls a service and never writes into
the raw directory. No numeric threshold decides anything (E1, SPEC :123);
the step-4 verdict is the completeness rule S-R12. Exit 0 when the summary
was written (also when step 4 is not achieved), 2 without writing when
``plan.json``, ``events.jsonl`` or ``meta.json`` is missing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.soak.common import DURATION_S, extension_s, merge_intervals, parse_time

CORE = ("plan.json", "events.jsonl", "meta.json")
CLAIM = (
    "Lab soak on one rented shared-vCPU host (target side) and a shared "
    "control host. Not production evidence, not a load test, no numeric "
    "target; resource and latency numbers are descriptive only."
)
#: S-R5: cross-host events match within this many seconds.
TOLERANCE_S = 2
END_TO_END_S = 600
QUEUED_LIMIT_S = 600
OBSERVING_GRACE_S = 300
NON_TERMINAL = frozenset({"queued", "running"})
WAITING_ON_HUMAN = frozenset({"waiting_human", "paused", "blocked"})
KSM_SERIES = ["kube_pod_owner", "kube_replicaset_owner", "kube_pod_container_info"]
KSM_LIMITATION = (
    "Pod presence comes only from the kube-state-metrics owner mapping; a "
    "stale (zombie) series can outlive its pod, and presence is not proof "
    "the pod served traffic."
)
SECTIONS: dict[str, tuple[str, ...]] = {
    "availability": ("readyz.csv", "prom_up.csv", "alerts.jsonl", "deliveries.jsonl"),
    "missing_observations": ("sessions.jsonl", "samples.csv", "prom_up.csv", "ksm.csv"),
    "stuck_work": ("runs.jsonl", "sessions.jsonl", "alerts.jsonl", "deliveries.jsonl"),
    "resources": ("docker_stats.csv", "host_stats.csv", "sizes.csv"),
    "cost": ("runs.jsonl",),
}


# -- reading -------------------------------------------------------------------


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn last line from a killed writer
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _s(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    return parse_time(str(raw)).timestamp()


def _num(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clean(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _ids(values: Iterable[str]) -> dict[str, Any]:
    ids = sorted(set(values))
    return {"count": len(ids), "ids": ids}


def _stats(values: Iterable[float | None]) -> dict[str, Any]:
    """Nearest-rank percentiles."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return {"min": None, "p50": None, "p95": None, "max": None}

    def rank(q: float) -> float | int:
        return _clean(clean[max(1, math.ceil(q * len(clean))) - 1])

    return {
        "min": _clean(clean[0]),
        "p50": rank(0.50),
        "p95": rank(0.95),
        "max": _clean(clean[-1]),
    }


class Raw:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.plan_bytes = (root / "plan.json").read_bytes()
        self.plan: dict[str, Any] = json.loads(self.plan_bytes)
        self.meta: dict[str, Any] = json.loads((root / "meta.json").read_text("utf-8"))
        events = _jsonl(root / "events.jsonl")
        # Stable sort: the log is append-only, a late note may be out of order.
        self.events = sorted(events, key=lambda e: _s(e.get("ts")) or 0.0)
        self.alerts = _jsonl(root / "alerts.jsonl")
        self.deliveries = _jsonl(root / "deliveries.jsonl")
        self.runs = _jsonl(root / "runs.jsonl")
        self.sessions = _jsonl(root / "sessions.jsonl")
        self.samples = _csv(root / "samples.csv")
        self.readyz = _csv(root / "readyz.csv")
        self.prom_up = _csv(root / "prom_up.csv")
        self.ksm = _csv(root / "ksm.csv")
        self.docker = _csv(root / "docker_stats.csv")
        self.hosts = _csv(root / "host_stats.csv")
        self.sizes = _csv(root / "sizes.csv")

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == kind]

    @property
    def start(self) -> float | None:
        starts = self.of("soak_start")
        return _s(starts[0]["ts"]) if starts else None

    @property
    def end(self) -> float | None:
        ends = self.of("soak_end")
        return _s(ends[-1]["ts"]) if ends else None

    @property
    def snapshot(self) -> float:
        return _s(self.meta.get("snapshot_at")) or self.end or self.start or 0.0


def _iso(epoch: float | None, raw: Raw) -> str | None:
    if epoch is None:
        return None
    for event in raw.events:  # keep the writer's own spelling when it has one
        if _s(event.get("ts")) == epoch:
            return str(event["ts"])
    return (
        datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    )


# -- window, clock, environment (S-R4, S-R5, S-R13) -----------------------------


def _pairs(
    events: Sequence[Mapping[str, Any]], opener: str, closer: str, until: float
) -> list[tuple[float, float, Mapping[str, Any]]]:
    out: list[tuple[float, float, Mapping[str, Any]]] = []
    open_at: tuple[float, Mapping[str, Any]] | None = None
    for event in events:
        kind = event.get("type")
        at = _s(event.get("ts")) or 0.0
        if kind == opener and open_at is None:
            open_at = (at, event)
        elif kind == closer and open_at is not None:
            out.append((open_at[0], at, open_at[1]))
            open_at = None
    if open_at is not None:
        out.append((open_at[0], max(until, open_at[0]), open_at[1]))
    return out


def interruptions(raw: Raw) -> list[tuple[float, float, Mapping[str, Any]]]:
    until = raw.end or raw.snapshot
    return _pairs(raw.events, "env_interruption_start", "env_interruption_end", until)


def window(raw: Raw) -> dict[str, Any]:
    start, end = raw.start, raw.end
    spans = interruptions(raw)
    interrupted = sum(b - a for a, b in merge_intervals((a, b) for a, b, _ in spans))
    extension = extension_s(interrupted)
    required = DURATION_S + extension
    covered = None if start is None or end is None else end - start
    return {
        "start": raw.of("soak_start")[0]["ts"] if raw.of("soak_start") else None,
        "end": raw.of("soak_end")[-1]["ts"] if raw.of("soak_end") else None,
        "covered_s": None if covered is None else _clean(covered),
        "interruption_s": _clean(interrupted),
        "extension_s": _clean(extension),
        "required_s": _clean(required),
        "coverage_ok": covered is not None and covered >= required,
        "rental_lost": bool(raw.of("rental_lost")),
    }


def clock(raw: Raw) -> dict[str, Any]:
    checks = [e for e in raw.of("clock_check") if _num(e.get("offset_s")) is not None]
    offsets = [abs(float(e["offset_s"])) for e in checks]
    return {
        "tolerance_s": TOLERANCE_S,
        "max_abs_offset_s": max(offsets) if offsets else None,
        "over_1s": [
            {"ts": e["ts"], "host": e.get("host"), "offset_s": e["offset_s"]}
            for e in checks
            if abs(float(e["offset_s"])) > 1
        ],
        "checks": len(checks),
    }


def environment(raw: Raw) -> dict[str, Any]:
    return {
        "actions": [
            {"ts": e["ts"], "action": e.get("action"), "reason": e.get("reason")}
            for e in raw.of("env_action")
        ],
        "interruptions": [
            {
                "start": _iso(a, raw),
                "end": _iso(b, raw),
                "cause": e.get("cause"),
                "duration_s": _clean(b - a),
            }
            for a, b, e in interruptions(raw)
        ],
    }


# -- injections (S-R6, S-R9) ------------------------------------------------------


def _executed_episodes(raw: Raw) -> list[tuple[str, float]]:
    planned = {
        i["id"]
        for i in raw.plan.get("injections", [])
        if i.get("category") == "episode"
    }
    first_on: dict[str, float] = {}
    for event in raw.of("injection_on"):
        key = str(event.get("injection_id"))
        if key in planned and key not in first_on:
            first_on[key] = _s(event["ts"]) or 0.0
    return sorted(first_on.items(), key=lambda kv: (kv[1], kv[0]))


def links(raw: Raw) -> dict[str, dict[str, Any]]:
    """Episode -> its firing and delivery (module docstring "Linking")."""
    alertname = raw.plan.get("product_alertname")
    episodes = _executed_episodes(raw)
    close = raw.end if raw.end is not None else raw.snapshot
    out: dict[str, dict[str, Any]] = {}
    for index, (episode, on) in enumerate(episodes):
        w_end = episodes[index + 1][1] if index + 1 < len(episodes) else close
        firings = sorted(
            (
                (_s(a.get("starts_at")) or 0.0, str(a.get("fingerprint")))
                for a in raw.alerts
                if a.get("alertname") == alertname
                and on <= (_s(a.get("starts_at")) or -math.inf) < w_end
            ),
        )
        entry: dict[str, Any] = {"firing": None, "delivery": None}
        if firings:
            fired, fingerprint = firings[0]
            entry["firing"] = fired
            matches = sorted(
                (
                    d
                    for d in raw.deliveries
                    if str(d.get("fingerprint")) == fingerprint
                    and (_s(d.get("received_at")) or -math.inf) >= fired - TOLERANCE_S
                    and (_s(d.get("received_at")) or math.inf) < w_end
                ),
                key=lambda d: (
                    _s(d.get("received_at")) or 0.0,
                    str(d.get("delivery_id")),
                ),
            )
            entry["delivery"] = matches[0] if matches else None
        delivery = entry["delivery"]
        outcome = delivery.get("outcome") if delivery else None
        entry["disposition"] = (
            outcome if outcome in {"run", "handoff", "attached"} else "none"
        )
        out[episode] = entry
    return out


def _step_owner(raw: Raw) -> dict[str, str]:
    """Step id -> injection id from the plan's ``steps``; a step the plan
    does not list belongs to the id before its first ``:``."""
    owner: dict[str, str] = {}
    for item in raw.plan.get("injections", []):
        for step in item.get("steps") or []:
            owner[str(step.get("id"))] = str(item["id"])
    return owner


def unmet_conditions(raw: Raw) -> dict[str, list[dict[str, Any]]]:
    """#486 (S-R12): conditional steps (A1 ``run_running``, A2 ``idle``) that
    waited their maximum and then ran anyway. The runner's ``step`` records
    of an attempt carry ``condition`` and ``condition_met``; any record with
    ``condition_met: false`` counts, whatever its status, so a failed or
    retried attempt is reported too. One entry per step: its first such
    record's time and the status of its last record."""
    owner = _step_owner(raw)
    found: dict[str, dict[str, Any]] = {}
    for event in raw.of("step"):
        if event.get("condition_met") is not False:
            continue
        step = str(event.get("step"))
        entry = found.setdefault(
            step,
            {
                "step": step,
                "condition": event.get("condition"),
                "ts": event.get("ts"),
            },
        )
        entry["status"] = event.get("status")
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step, entry in found.items():
        out[owner.get(step, step.split(":", 1)[0])].append(entry)
    return dict(out)


def injections(
    raw: Raw, linked: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    executed = {str(e.get("injection_id")) for e in raw.of("injection_on")}
    unmet = unmet_conditions(raw)
    skipped: dict[str, str] = {}
    for event in raw.of("injection_skipped"):
        skipped[str(event.get("injection_id"))] = str(event.get("reason") or "")
    out = []
    for item in raw.plan.get("injections", []):
        key = str(item["id"])
        is_executed = key in executed
        out.append(
            {
                "id": key,
                "category": item.get("category"),
                "name": item.get("name"),
                "executed": is_executed,
                "skipped_reason": None if is_executed else skipped.get(key) or None,
                "disposition": linked[key]["disposition"]
                if is_executed and item.get("category") == "episode" and key in linked
                else None,
                "unmet_conditions": unmet.get(key, []),
            }
        )
    return out


# -- sections -----------------------------------------------------------------------


def _frame(raw: Raw, section: str) -> dict[str, Any]:
    required = SECTIONS[section]
    missing = [name for name in required if not (raw.root / name).exists()]
    frame: dict[str, Any] = {
        "published": not missing,
        "sources": [name for name in required if name not in missing],
    }
    if missing:
        frame["missing_sources"] = missing
    return frame


def _ratio(
    rows: Sequence[Mapping[str, str]], key: str, good: float
) -> dict[str, dict[str, Any]]:
    by_role: dict[str, list[float | None]] = defaultdict(list)
    for row in rows:
        by_role[str(row.get("role"))].append(_num(row.get(key)))
    out = {}
    for role, values in sorted(by_role.items()):
        ok = sum(1 for v in values if v == good)
        out[role] = {
            "ticks": len(values),
            "ok": ok,
            "ratio": _clean(ok / len(values)) if values else None,
        }
    return out


def availability(raw: Raw, linked: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    tunnel = _pairs(raw.events, "tunnel_down", "tunnel_up", raw.end or raw.snapshot)
    end_to_end = []
    for episode, entry in linked.items():
        delivery, fired = entry["delivery"], entry["firing"]
        outcome_at = _s(delivery.get("outcome_at")) if delivery else None
        after = None if outcome_at is None or fired is None else outcome_at - fired
        disposition = entry["disposition"]
        end_to_end.append(
            {
                "injection_id": episode,
                "outcome": disposition,
                "outcome_after_s": None if after is None else _clean(round(after, 3)),
                "within_10min": disposition in {"run", "handoff"}
                and after is not None
                and after <= END_TO_END_S,
            }
        )
    return {
        **_frame(raw, "availability"),
        "readyz": _ratio(raw.readyz, "status", 200),
        "prometheus_up": _ratio(raw.prom_up, "up", 1),
        "tunnel": {
            "disconnects": len(tunnel),
            "disconnected_s": _clean(sum(b - a for a, b, _ in tunnel)),
        },
        "end_to_end": end_to_end,
    }


def missing_observations(raw: Raw) -> dict[str, Any]:
    per_session: dict[str, list[str]] = defaultdict(list)
    for row in raw.samples:
        per_session[str(row.get("session_id"))].append(str(row.get("signal_status")))
    totals = {
        "expected": 0,
        "recorded": 0,
        "missing": 0,
        "signal_missing": 0,
        "stale": 0,
    }
    sessions = []
    for session in raw.sessions:
        started = _s(session.get("started_at")) or 0.0
        ended = _s(session.get("ended_at"))
        until = ended if ended is not None else raw.snapshot
        interval = max(1.0, float(session.get("interval_s") or 60))
        expected = max(0, math.floor((until - started) / interval))
        statuses = per_session.get(str(session.get("session_id")), [])
        entry = {
            "expected": expected,
            "recorded": len(statuses),
            "missing": max(0, expected - len(statuses)),
            "signal_missing": sum(1 for s in statuses if s == "missing"),
            "stale": sum(1 for s in statuses if s == "stale"),
        }
        for key in totals:
            totals[key] += entry[key]
        sessions.append({"session_id": session.get("session_id"), **entry})
    return {
        **_frame(raw, "missing_observations"),
        "recovery": totals,
        "recovery_sessions": sessions,
        "scrape_gaps": scrape_gaps(raw.prom_up),
        "lab_liveness": lab_liveness(raw),
    }


def scrape_gaps(rows: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    """Runs of >= 2 consecutive ``up=0`` rows per role."""
    by_role: dict[str, list[tuple[float, str, float | None]]] = defaultdict(list)
    for row in rows:
        by_role[str(row.get("role"))].append(
            (_s(row.get("ts")) or 0.0, str(row.get("ts")), _num(row.get("up")))
        )
    gaps = []
    for role, values in sorted(by_role.items()):
        values.sort()
        current: list[str] = []
        for _, stamp, up in [*values, (math.inf, "", 1.0)]:
            if up == 0:
                current.append(stamp)
                continue
            if len(current) >= 2:
                gaps.append(
                    {
                        "role": role,
                        "start": current[0],
                        "end": current[-1],
                        "missed_scrapes": len(current),
                    }
                )
            current = []
    return gaps


def lab_liveness(raw: Raw) -> dict[str, Any]:
    expected = [str(d) for d in raw.plan.get("expected_deployments", [])]
    ticks: dict[str, dict[str, float | None]] = {}
    for row in raw.ksm:
        ticks.setdefault(str(row.get("ts")), {})[str(row.get("deployment"))] = _num(
            row.get("pods")
        )
    absent = []
    for stamp in sorted(ticks, key=lambda t: _s(t) or 0.0):
        for deployment in expected:
            pods = ticks[stamp].get(deployment)
            if not pods:
                absent.append({"ts": stamp, "deployment": deployment})
    return {
        "source": "kube_state_metrics_owner_mapping",
        "series": list(KSM_SERIES),
        "limitation": KSM_LIMITATION,
        "expected_deployments": expected,
        "ticks": len(ticks),
        "absent": absent,
    }


def stuck_work(raw: Raw, linked: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    snapshot = raw.snapshot
    over_wall, long_queued = [], []
    for run in raw.runs:
        run_id = str(run.get("run_id"))
        created, started = _s(run.get("created_at")), _s(run.get("started_at"))
        finished = _s(run.get("finished_at"))
        limit = _num(run.get("wall_clock_limit_s"))
        if started is not None and limit is not None:
            if (finished if finished is not None else snapshot) - started > limit:
                over_wall.append(run_id)
        if created is not None:
            if (
                started if started is not None else snapshot
            ) - created > QUEUED_LIMIT_S:
                long_queued.append(run_id)
    overdue = []
    for session in raw.sessions:
        started = _s(session.get("started_at")) or 0.0
        ended = _s(session.get("ended_at"))
        limit = float(session.get("max_duration_s") or 0) + OBSERVING_GRACE_S
        if (ended if ended is not None else snapshot) - started > limit:
            overdue.append(str(session.get("session_id")))
    return {
        **_frame(raw, "stuck_work"),
        "non_terminal_runs": _ids(
            str(r.get("run_id")) for r in raw.runs if r.get("state") in NON_TERMINAL
        ),
        "waiting_on_human_runs": _ids(
            str(r.get("run_id")) for r in raw.runs if r.get("state") in WAITING_ON_HUMAN
        ),
        "over_wall_clock_runs": _ids(over_wall),
        "long_queued_runs": _ids(long_queued),
        "overdue_recovery_sessions": _ids(overdue),
        "deliveries_without_outcome": _ids(
            str(d.get("delivery_id"))
            for d in raw.deliveries
            if d.get("outcome") is None
        ),
        "unaccounted_injections": _ids(
            key for key, entry in linked.items() if entry["disposition"] == "none"
        ),
        "injections_left_on": _ids(left_on(raw)),
    }


def left_on(raw: Raw) -> list[str]:
    """#477 B2: injections whose plan has a restoring step (more than one
    step) that were turned on and never got their ``injection_off``. Plans
    without ``steps`` (hand-written raw directories) have none."""
    on = {str(e.get("injection_id")) for e in raw.of("injection_on")}
    off = {str(e.get("injection_id")) for e in raw.of("injection_off")}
    return [
        str(item["id"])
        for item in raw.plan.get("injections", [])
        if len(item.get("steps") or []) > 1 and str(item["id"]) in on - off
    ]


def resources(raw: Raw) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in raw.docker:
        groups[
            (str(row.get("host")), str(row.get("group")), str(row.get("container")))
        ].append(row)
    containers, shared = [], []
    for (host, group, name), rows in sorted(groups.items()):
        entry = {
            "host": host,
            "group": group,
            "container": name,
            "cpu_pct": _stats(_num(r.get("cpu_pct")) for r in rows),
            "mem_bytes": _stats(_num(r.get("mem_bytes")) for r in rows),
        }
        (shared if group == "shared" else containers).append(entry)
    hosts: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in raw.hosts:
        hosts[str(row.get("host"))].append(row)
    sizes: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for row in raw.sizes:
        value = _num(row.get("bytes"))
        if value is not None:
            sizes[(str(row.get("host")), str(row.get("kind")))].append(
                (_s(row.get("ts")) or 0.0, value)
            )
    size_rows = []
    for (host, kind), readings in sorted(sizes.items()):
        readings.sort()
        start, end = readings[0][1], readings[-1][1]
        size_rows.append(
            {
                "host": host,
                "kind": kind,
                "start_bytes": _clean(start),
                "end_bytes": _clean(end),
                "delta_bytes": _clean(end - start),
            }
        )
    return {
        **_frame(raw, "resources"),
        "containers": containers,
        "shared_services": shared,
        "hosts": {
            host: {
                key: _stats(_num(r.get(key)) for r in rows)
                for key in ("mem_available_bytes", "load1", "disk_free_bytes")
            }
            for host, rows in sorted(hosts.items())
        },
        "sizes": size_rows,
    }


def cost(raw: Raw) -> dict[str, Any]:
    prices = raw.plan.get("model_price_cny_per_mtok") or {}
    tokens_in = sum(int(_num(r.get("input_tokens")) or 0) for r in raw.runs)
    tokens_out = sum(int(_num(r.get("output_tokens")) or 0) for r in raw.runs)
    token_cost = None
    if _num(prices.get("input")) is not None and _num(prices.get("output")) is not None:
        token_cost = round(
            tokens_in / 1e6 * float(prices["input"])
            + tokens_out / 1e6 * float(prices["output"]),
            6,
        )
    readings = sorted(
        (
            r
            for r in raw.meta.get("balance_readings") or []
            if _num(r.get("balance_cny")) is not None
        ),
        key=lambda r: _s(r.get("ts")) or 0.0,
    )
    balance = (
        round(float(readings[0]["balance_cny"]) - float(readings[-1]["balance_cny"]), 6)
        if len(readings) >= 2
        else None
    )
    rental = raw.meta.get("rental") or {}
    on, off = _s(rental.get("powered_on_at")), _s(rental.get("powered_off_at"))
    hourly = _num(rental.get("hourly_usd"))
    hours = None if on is None or off is None else (off - on) / 3600
    return {
        **_frame(raw, "cost"),
        "model": {
            "runs": len(raw.runs),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "token_cost_cny": token_cost,
            "note": "lower bound: only committed steps carry usage",
        },
        "balance_delta_cny": balance,
        "rental": {
            "hours": None if hours is None else round(hours, 6),
            "hourly_usd": hourly,
            "estimated_usd": None
            if hours is None or hourly is None
            else round(hours * hourly, 6),
            "billing_is_authoritative": True,
        },
        "langsmith_usd": 0,
    }


def step4(summary: Mapping[str, Any]) -> dict[str, Any]:
    unmet: list[str] = []
    if not summary["window"]["coverage_ok"]:
        unmet.append("coverage")
    if summary["window"]["rental_lost"]:
        unmet.append("rental_lost")
    for item in summary["injections"]:
        if not item["executed"] and not item["skipped_reason"]:
            unmet.append(f"injection_unexplained:{item['id']}")
    for key in summary["stuck_work"]["unaccounted_injections"]["ids"]:
        unmet.append(f"injection_unaccounted:{key}")
    for key in summary["stuck_work"]["injections_left_on"]["ids"]:
        unmet.append(f"injection_left_on:{key}")
    for section in SECTIONS:
        if not summary[section]["published"]:
            unmet.append(f"section_unpublished:{section}")
    return {"achieved": not unmet, "unmet": unmet}


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw = Raw(Path(raw_dir))
    linked = links(raw)
    summary: dict[str, Any] = {
        "production_proof": False,
        "claim": CLAIM,
        "plan_sha256": hashlib.sha256(raw.plan_bytes).hexdigest(),
        "window": window(raw),
        "clock": clock(raw),
        "injections": injections(raw, linked),
        "availability": availability(raw, linked),
        "missing_observations": missing_observations(raw),
        "stuck_work": stuck_work(raw, linked),
        "resources": resources(raw),
        "cost": cost(raw),
        "environment": environment(raw),
        "operator": operator(raw, linked),
        "errors": len(raw.of("error")),
    }
    summary["step4"] = step4(summary)
    return summary


def operator(raw: Raw, linked: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The scripted closes (S-R3 and its B1 extension, by reason), the
    episodes that attached to an earlier incident instead of opening one,
    and what was still open when each injection started."""
    closes = raw.of("operator")
    reasons: dict[str, int] = defaultdict(int)
    for event in closes:
        reasons[str(event.get("reason"))] += 1
    return {
        "note": "closes scripted on behalf of a human (S-R3); no human interaction claimed",
        "closes": len(closes),
        "accepted": sum(1 for e in closes if e.get("status") == 200),
        "by_reason": dict(sorted(reasons.items())),
        "attached_injections": [
            {
                "injection_id": key,
                "incident_id": (entry["delivery"] or {}).get("incident_id"),
            }
            for key, entry in linked.items()
            if entry["disposition"] == "attached"
        ],
        "open_incidents_at_injection": [
            {
                "ts": e["ts"],
                "before_step": e.get("before_step"),
                "incidents": e.get("incidents") or [],
            }
            for e in raw.of("open_incidents")
            if e.get("incidents")
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.soak.summarize")
    parser.add_argument("raw_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    missing = [name for name in CORE if not (args.raw_dir / name).exists()]
    if missing:
        print(f"missing core input: {', '.join(missing)}", file=sys.stderr)
        return 2
    summary = summarize(args.raw_dir)
    args.out.write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
