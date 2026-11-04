"""``summary.json`` from a soak data directory (S-R7–S-R12, S-R15).

Offline and deterministic: reads only the files the runner and ``finish``
wrote (formats in ``scripts/soak/README.md``) and never calls a service. No
numeric threshold decides anything (E1, SPEC :123); the step-4 verdict is
the completeness rule S-R12. Every number names the file it came from, and
``inputs`` lists the SHA-256 of every file read.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.soak.common import (
    DURATION_S,
    extension_s,
    interval_total,
    iso,
    merge_intervals,
    parse_time,
    read_jsonl,
)

SCHEMA = "opspilot-soak-summary/1"
CLAIM = (
    "Lab soak on a rented host and a shared control host. Not production "
    "evidence, not a load test, no numeric target."
)
ALERTNAME = "CheckoutPlaceOrderErrorRatioHigh"
#: A delivery belongs to an episode when it arrives between the flag going on
#: and this long after it went off.
EPISODE_SLACK_S = 15 * 60
#: S-R7: a Run or a handoff within this long of the alert firing.
END_TO_END_S = 10 * 60
QUEUED_LIMIT_S = 10 * 60
OBSERVING_GRACE_S = 5 * 60
#: S-R9: the export must be taken at least this long after the end for the
#: "not terminal 30 min after the end" column to be meaningful.
SETTLE_S = 30 * 60
TERMINAL = frozenset({"completed", "failed", "cancelled", "budget_exhausted"})
DESTINATIONS = {
    "created": "run",
    "handoff_created": "handoff",
    "attached": "attached",
    "replayed": "replayed",
    "handoff_replayed": "handoff_replayed",
}
#: The reverse-tunnel ports of the three health listeners (S-R1).
INSTANCE_ROLES = {"29464": "web", "29465": "worker", "29466": "observer"}
OPSPILOT_PREFIX = "opspilot-"


# -- inputs -------------------------------------------------------------------


class Inputs:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.read: dict[str, str] = {}

    def _mark(self, path: Path) -> None:
        if path.exists():
            rel = str(path.relative_to(self.root))
            self.read[rel] = hashlib.sha256(path.read_bytes()).hexdigest()

    def jsonl(self, name: str) -> list[dict[str, Any]]:
        path = self.root / name
        self._mark(path)
        return list(read_jsonl(path))

    def json(self, name: str) -> Any:
        path = self.root / name
        if not path.exists():
            return None
        self._mark(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def glob(self, pattern: str) -> list[Path]:
        return sorted(self.root.glob(pattern))


def _t(raw: Any) -> datetime | None:
    if raw is None:
        return None
    return parse_time(str(raw))


def _s(raw: Any) -> float | None:
    moment = _t(raw)
    return None if moment is None else moment.timestamp()


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _stats(values: Iterable[float | int | None]) -> dict[str, Any]:
    clean = [float(v) for v in values if v is not None]
    return {
        "n": len(clean),
        "min": min(clean) if clean else None,
        "p50": _pct(clean, 0.50),
        "p95": _pct(clean, 0.95),
        "max": max(clean) if clean else None,
    }


# -- window and faults (S-R4, S-R6) --------------------------------------------


def _step_records(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    final: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") == "step" and event.get("status") in {
            "ok",
            "failed",
            "skipped",
        }:
            final[str(event["step"])] = dict(event)
    return final


def tunnel_down(probes: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
    """Intervals the tunnel probe failed: from the first failing tick to the
    next good one (or the last tick)."""
    ticks = sorted(
        (_s(p["at"]) or 0.0, p.get("status") == 200)
        for p in probes
        if p.get("target") == "tunnel"
    )
    intervals = []
    start: float | None = None
    for at, ok in ticks:
        if not ok and start is None:
            start = at
        elif ok and start is not None:
            intervals.append((start, at))
            start = None
    if start is not None and ticks:
        intervals.append((start, ticks[-1][0]))
    return intervals


def planned_tunnel_stop(
    steps: Mapping[str, Mapping[str, Any]],
) -> list[tuple[float, float]]:
    stop, start = steps.get("A4:stop"), steps.get("A4:start")
    if stop is None or stop.get("status") != "ok":
        return []
    begin = _s(stop["at"]) or 0.0
    # The tunnel needs a moment to reconnect after the start step.
    end = (_s(start["at"]) or begin) + 120 if start is not None else math.inf
    return [(begin, end)]


def interruptions(
    events: Sequence[Mapping[str, Any]], probes: Sequence[Mapping[str, Any]]
) -> list[tuple[float, float, str]]:
    """S-R4 environment-only interruptions: tunnel outages outside the planned
    A4 stop, plus ``interruption`` notes (rental reboot, provider maintenance)."""
    steps = _step_records(events)
    planned = planned_tunnel_stop(steps)
    out: list[tuple[float, float, str]] = []
    for start, end in tunnel_down(probes):
        pieces = [(start, end)]
        for p_start, p_end in planned:
            pieces = [
                piece
                for a, b in pieces
                for piece in ((a, min(b, p_start)), (max(a, p_end), b))
                if piece[1] > piece[0]
            ]
        out += [(a, b, "tunnel_down") for a, b in pieces]
    for event in events:
        if event.get("kind") == "interruption":
            out.append(
                (
                    _s(event["start"]) or 0.0,
                    _s(event["end"]) or 0.0,
                    str(event.get("reason", "noted")),
                )
            )
    return out


def interruption_seconds(
    events: Sequence[Mapping[str, Any]], probes: Sequence[Mapping[str, Any]]
) -> float:
    return interval_total((a, b) for a, b, _ in interruptions(events, probes))


def window(
    events: Sequence[Mapping[str, Any]], probes: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    start = next((e for e in events if e.get("kind") == "start"), None)
    end = next((e for e in reversed(events) if e.get("kind") == "end"), None)
    if start is None:
        return {"t0": None, "coverage_ok": False, "reason": "no start event"}
    t0 = parse_time(str(start["t0"]))
    interrupted = interruption_seconds(events, probes)
    extension = extension_s(interrupted)
    required = t0 + timedelta(seconds=DURATION_S + extension)
    ended = _t(end["at"]) if end is not None else None
    return {
        "t0": iso(t0),
        "ended_at": None if ended is None else iso(ended),
        "planned_s": DURATION_S,
        "interruption_s": round(interrupted, 1),
        "interruptions": [
            {"start": iso(_dt(a)), "end": iso(_dt(b)), "reason": r}
            for a, b, r in interruptions(events, probes)
        ],
        "extension_s": extension,
        "required_end": iso(required),
        "covered_s": None if ended is None else round((ended - t0).total_seconds(), 1),
        "coverage_ok": ended is not None and ended >= required,
    }


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def faults(
    plan: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each planned item: executed (every step ok), or not executed with the
    recorded reason of each step that was not; a step without any record is
    ``missing`` (undocumented)."""
    steps = _step_records(events)
    out = []
    for item in plan["items"]:
        records = []
        for step in item["steps"]:
            record = steps.get(step["id"])
            entry: dict[str, Any] = {"step": step["id"], "action": step["action"]}
            if record is None:
                entry["status"] = "missing"
            else:
                entry["status"] = record["status"]
                entry["at"] = record.get("at")
                for key in ("reason", "exit", "stderr", "condition", "condition_met"):
                    if key in record:
                        entry[key] = record[key]
            records.append(entry)
        statuses = {r["status"] for r in records}
        status = "executed" if statuses == {"ok"} else "not_executed"
        out.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "planned_at_s": item["at_s"],
                "status": status,
                "documented": "missing" not in statuses,
                "steps": records,
            }
        )
    return out


def _item_window(
    item: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]]
) -> tuple[float, float] | None:
    on = steps.get(f"{item['id']}:on")
    if on is None or on.get("status") != "ok":
        return None
    off = steps.get(f"{item['id']}:off")
    start = _s(on["at"]) or 0.0
    end = (_s(off["at"]) if off is not None else None) or start + float(
        item["duration_s"]
    )
    return start, end


# -- availability (S-R7) --------------------------------------------------------


def availability(
    probes: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    deliveries: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    by_target: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for probe in probes:
        by_target[str(probe.get("target"))].append(probe)
    readyz = {}
    for target, rows in sorted(by_target.items()):
        if target == "tunnel":
            continue
        ok = sum(1 for r in rows if r.get("status") == 200)
        readyz[target] = {
            "ticks": len(rows),
            "ok": ok,
            "ratio": round(ok / len(rows), 6) if rows else None,
        }
    tunnel_rows = by_target.get("tunnel", [])
    tunnel_ok = sum(1 for r in tunnel_rows if r.get("status") == 200)
    # Secondary: up{job="opspilot"} as the lab Prometheus saw it, per role.
    readyz_ok_at = {
        (str(p.get("target")), str(p.get("at"))): p.get("status") == 200 for p in probes
    }
    prom: dict[str, dict[str, int]] = {}
    for row in tunnel_rows:
        up = row.get("opspilot_up")
        for role in INSTANCE_ROLES.values():
            counts = prom.setdefault(
                role, {"ticks": 0, "up": 0, "tunnel_down": 0, "readyz_ok_prom_down": 0}
            )
            counts["ticks"] += 1
            if row.get("status") != 200 or not isinstance(up, dict):
                counts["tunnel_down"] += 1
                continue
            value = next(
                (
                    v
                    for inst, v in up.items()
                    if INSTANCE_ROLES.get(inst.rsplit(":", 1)[-1]) == role
                ),
                None,
            )
            if value == "1":
                counts["up"] += 1
            elif readyz_ok_at.get((role, str(row.get("at")))):
                counts["readyz_ok_prom_down"] += 1
    return {
        "source": "probe.jsonl",
        "readyz": readyz,
        "tunnel": {
            "ticks": len(tunnel_rows),
            "ok": tunnel_ok,
            "ratio": round(tunnel_ok / len(tunnel_rows), 6) if tunnel_rows else None,
            "down_intervals": [
                {"start": iso(_dt(a)), "end": iso(_dt(b))}
                for a, b in merge_intervals(tunnel_down(probes))
            ],
        },
        "prometheus_up": prom,
        "end_to_end": end_to_end(plan, events, deliveries),
    }


def _episode_deliveries(
    item: Mapping[str, Any],
    steps: Mapping[str, Mapping[str, Any]],
    deliveries: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    span = _item_window(item, steps)
    if span is None:
        return []
    start, end = span
    return sorted(
        (
            d
            for d in deliveries
            if d.get("status") == "firing"
            and d.get("alertname") == ALERTNAME
            and start <= (_s(d.get("received_at")) or 0.0) <= end + EPISODE_SLACK_S
        ),
        key=lambda d: str(d.get("received_at")),
    )


def end_to_end(
    plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    deliveries: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if deliveries is None:
        return []
    steps = _step_records(events)
    out = []
    for item in plan["items"]:
        if item["kind"] != "episode":
            continue
        found = _episode_deliveries(item, steps, deliveries)
        first = found[0] if found else None
        entry: dict[str, Any] = {
            "episode": item["id"],
            "injected": _item_window(item, steps) is not None,
        }
        if first is None:
            entry.update(firing_at=None, outcome=None, within_10_min=False)
        else:
            latency = (_s(first["received_at"]) or 0.0) - (
                _s(first["starts_at"]) or 0.0
            )
            entry.update(
                firing_at=first.get("starts_at"),
                received_at=first.get("received_at"),
                outcome=first.get("outcome"),
                incident_id=first.get("incident_id"),
                latency_s=round(latency, 1),
                within_10_min=first.get("outcome") in {"created", "handoff_created"}
                and latency <= END_TO_END_S,
            )
        out.append(entry)
    return out


# -- missing observations (S-R8) ------------------------------------------------


def missing_observations(
    pg: Mapping[str, Any] | None,
    exports: Sequence[Mapping[str, Any]],
    lab: Sequence[Mapping[str, Any]],
    expected_deployments: Sequence[str],
) -> dict[str, Any]:
    sessions = []
    totals = {"expected": 0, "recorded": 0, "missing": 0, "signal_missing_or_stale": 0}
    exported_at = _s((pg or {}).get("exported_at"))
    for row in (pg or {}).get("sessions", []) or []:
        authorized = _s(row["authorized_at"]) or 0.0
        end = _s(row.get("ended_at")) or exported_at or authorized
        interval = max(1, int(row["sample_interval_seconds"]))
        expected = min(
            int(row["max_samples"]), max(0, int((end - authorized) // interval))
        )
        recorded = int(row.get("adopted_samples") or 0)
        bad = int(row.get("signal_missing_or_stale") or 0)
        entry = {
            "session_id": row["session_id"],
            "incident_id": row["incident_id"],
            "expected": expected,
            "recorded": recorded,
            "missing": max(0, expected - recorded),
            "signal_missing_or_stale": bad,
            "ended_reason": row.get("ended_reason"),
        }
        for key in totals:
            totals[key] += entry[key]
        sessions.append(entry)
    return {
        "recovery_samples": {
            "source": "pg.json sessions",
            "sessions": sessions,
            "totals": totals,
        },
        "scrape_gaps": scrape_gaps(exports),
        "lab_instances": lab_instances(lab, expected_deployments),
    }


def _series_points(
    exports: Sequence[Mapping[str, Any]], name: str
) -> tuple[int, dict[str, dict[float, str]]]:
    step = 60
    points: dict[str, dict[float, str]] = defaultdict(dict)
    for export in exports:
        step = int(export.get("step_s", step))
        series = (export.get("series") or {}).get(name) or {}
        for row in series.get("result") or []:
            key = str(row["metric"].get("instance", ""))
            for at, value in row.get("values", []):
                points[key][float(at)] = str(value)
    return step, points


def scrape_gaps(exports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """S-R8(b): runs of >= 2 consecutive export steps where an opspilot
    target is absent or 0, from the 60 s ``query_range`` exports. A span no
    export covers is reported as ``uncovered``, not as a gap."""
    step, points = _series_points(exports, "opspilot_up")
    covered = merge_intervals(
        ((_s(e["start"]) or 0.0), (_s(e["end"]) or 0.0)) for e in exports
    )
    gaps: dict[str, list[dict[str, Any]]] = {}
    for instance, values in sorted(points.items()):
        role = INSTANCE_ROLES.get(instance.rsplit(":", 1)[-1], instance)
        found = []
        for start, end in covered:
            first = math.ceil(start / step) * step
            run_start: float | None = None
            missed = 0
            at = first
            while at <= end + 1e-6:
                bad = values.get(at) != "1"
                if bad:
                    run_start = at if run_start is None else run_start
                    missed += 1
                if not bad or at + step > end + 1e-6:
                    if run_start is not None and missed >= 2:
                        found.append(
                            {"start": iso(_dt(run_start)), "missed_steps": missed}
                        )
                    if not bad:
                        run_start, missed = None, 0
                at += step
        gaps[role] = found
    return {
        "source": "prom/*.json opspilot_up",
        "step_s": step,
        "covered": [{"start": iso(_dt(a)), "end": iso(_dt(b))} for a, b in covered],
        "gaps": gaps,
    }


def lab_instances(
    lab: Sequence[Mapping[str, Any]], expected: Sequence[str]
) -> dict[str, Any]:
    """S-R8(c): ticks where an expected Deployment had no pod (KSM owner
    mapping only), and ticks the query failed."""
    names = list(expected)
    if not names:
        first: Mapping[str, Any] = next(
            (r["deployments"] for r in lab if isinstance(r.get("deployments"), dict)),
            {},
        )
        names = sorted(first)
    failed = sum(1 for r in lab if not isinstance(r.get("deployments"), dict))
    empty: dict[str, int] = {}
    for name in names:
        empty[name] = sum(
            1
            for r in lab
            if isinstance(r.get("deployments"), dict)
            and int(r["deployments"].get(name, 0)) == 0
        )
    return {
        "source": "lab.jsonl",
        "ticks": len(lab),
        "query_failed_ticks": failed,
        "expected_deployments": names,
        "no_pod_ticks": empty,
        "limit": "kube-state-metrics series can outlive their pod (zombie series); "
        "pod presence is not proof the pod served traffic",
    }


# -- stuck work (S-R9) -----------------------------------------------------------


def stuck_work(
    pg: Mapping[str, Any] | None,
    run_log: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    exports: Sequence[Mapping[str, Any]],
    ended_at: str | None,
) -> dict[str, Any]:
    pg = pg or {}
    exported_at = _s(pg.get("exported_at"))
    ended = _s(ended_at)
    runs = pg.get("runs") or []
    not_terminal = [r["run_id"] for r in runs if r.get("state") not in TERMINAL]
    over_wall = []
    for r in runs:
        deadline = _s(r.get("deadline"))
        last = _s(r.get("last_step_at"))
        if deadline is None:
            continue
        if (last is not None and last > deadline) or (
            r.get("state") not in TERMINAL
            and exported_at is not None
            and exported_at > deadline
        ):
            over_wall.append(r["run_id"])
    queued = queued_too_long(run_log, exported_at)
    observing = []
    for s in pg.get("sessions") or []:
        deadline = _s(s.get("deadline_at"))
        end = _s(s.get("ended_at")) or exported_at
        if (
            deadline is not None
            and end is not None
            and end > deadline + OBSERVING_GRACE_S
        ):
            observing.append(s["incident_id"])
    return {
        "source": "pg.json, runs.jsonl, prom/*.json",
        "export_after_end_s": None
        if exported_at is None or ended is None
        else round(exported_at - ended, 1),
        "export_settled": exported_at is not None
        and ended is not None
        and exported_at - ended >= SETTLE_S,
        "runs_not_terminal": sorted(not_terminal),
        "runs_over_wall_clock": sorted(over_wall),
        "runs_queued_over_10_min": queued,
        "incidents_observing_past_deadline": sorted(set(observing)),
        "alerts_without_delivery": alerts_without_delivery(
            exports, pg.get("deliveries")
        ),
        "injections": injections(plan, events, pg.get("deliveries")),
    }


def queued_too_long(
    run_log: Sequence[Mapping[str, Any]], until: float | None
) -> list[dict[str, Any]]:
    """Continuous ``queued`` spans longer than 10 min in the runner's 60 s
    state log (a resolution of one closer tick)."""
    by_run: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for row in run_log:
        by_run[str(row["run_id"])].append((_s(row["at"]) or 0.0, str(row["state"])))
    out = []
    for run_id, changes in sorted(by_run.items()):
        changes.sort()
        for index, (at, state) in enumerate(changes):
            if state != "queued":
                continue
            nxt = changes[index + 1][0] if index + 1 < len(changes) else until
            if nxt is not None and nxt - at > QUEUED_LIMIT_S:
                out.append(
                    {
                        "run_id": run_id,
                        "queued_at": iso(_dt(at)),
                        "queued_s": round(nxt - at, 1),
                    }
                )
    return out


def alerts_without_delivery(
    exports: Sequence[Mapping[str, Any]], deliveries: Sequence[Mapping[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """Firing spans of the checkout alert in ``ALERTS`` with no firing
    delivery received from their start to 10 min after their end."""
    if deliveries is None:
        return None
    spans: list[tuple[float, float]] = []
    for export in exports:
        step = int(export.get("step_s", 60))
        series = (export.get("series") or {}).get("alerts") or {}
        for row in series.get("result") or []:
            metric = row.get("metric", {})
            if (
                metric.get("alertname") != ALERTNAME
                or metric.get("alertstate") != "firing"
            ):
                continue
            spans += [(float(at), float(at) + step) for at, _ in row.get("values", [])]
    received = [
        _s(d.get("received_at")) or 0.0
        for d in deliveries
        if d.get("status") == "firing" and d.get("alertname") == ALERTNAME
    ]
    out = []
    for start, end in merge_intervals(spans):
        if not any(start <= at <= end + END_TO_END_S for at in received):
            out.append({"start": iso(_dt(start)), "end": iso(_dt(end))})
    return out


def injections(
    plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    deliveries: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """S-R9: where every injection went. An episode: run, handoff, attached
    (or replayed) from its first firing delivery, ``none`` without one. A
    noise toggle: the deliveries that opened anything in its window (U2:
    it should open nothing)."""
    steps = _step_records(events)
    out = []
    for item in plan["items"]:
        if item["kind"] not in {"episode", "noise"}:
            continue
        span = _item_window(item, steps)
        entry: dict[str, Any] = {
            "id": item["id"],
            "kind": item["kind"],
            "injected": span is not None,
        }
        if span is None or deliveries is None:
            entry["destination"] = None
        elif item["kind"] == "episode":
            found = _episode_deliveries(item, steps, deliveries)
            entry["destination"] = (
                DESTINATIONS.get(str(found[0].get("outcome")), "other")
                if found
                else "none"
            )
        else:
            start, end = span
            opened = [
                {"alertname": d.get("alertname"), "outcome": d.get("outcome")}
                for d in deliveries
                if d.get("status") == "firing"
                and start <= (_s(d.get("received_at")) or 0.0) <= end + EPISODE_SLACK_S
                and d.get("outcome") in {"created", "handoff_created"}
            ]
            entry["destination"] = "nothing_opened" if not opened else "opened"
            entry["opened"] = opened
        out.append(entry)
    return out


# -- resources (S-R10) and cost (S-R11) -----------------------------------------


def resources(inputs: Inputs, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    hosts = {}
    for path in inputs.glob("resources-*.jsonl"):
        rows = inputs.jsonl(path.name)
        host = path.stem.removeprefix("resources-")
        containers: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            for c in row.get("containers") or []:
                containers[str(c.get("name"))].append(c)
        groups: dict[str, dict[str, Any]] = {"opspilot": {}, "other": {}}
        for name, samples in sorted(containers.items()):
            group = "opspilot" if name.startswith(OPSPILOT_PREFIX) else "other"
            groups[group][name] = {
                "cpu_pct": _stats(c.get("cpu_pct") for c in samples),
                "mem_bytes": _stats(c.get("mem_bytes") for c in samples),
            }
        hosts[host] = {
            "source": path.name,
            "samples": len(rows),
            "mem_available_kib": _stats(r.get("mem_available_kib") for r in rows),
            "swap_free_kib": _stats(r.get("swap_free_kib") for r in rows),
            "load1": _stats(r.get("load1") for r in rows),
            "disk_free_bytes": _stats(r.get("disk_free_bytes") for r in rows),
            "containers": groups,
        }
    sizes = [e for e in events if e.get("kind") == "sizes"]
    return {
        "hosts": hosts,
        "sizes": [
            {
                "at": e.get("at"),
                "phase": e.get("phase"),
                "db_bytes": e.get("db_bytes"),
                "docker_df": e.get("docker_df"),
            }
            for e in sizes
        ],
    }


def cost(
    pg: Mapping[str, Any] | None, prices: Mapping[str, Any] | None
) -> dict[str, Any]:
    pg = pg or {}
    tokens = {
        "prompt": 0,
        "completion": 0,
        "cache_hit": 0,
        "cache_miss": 0,
        "runs": 0,
        "runs_without_cache_split": 0,
        "model_steps": 0,
    }
    for run in pg.get("runs") or []:
        tokens["runs"] += 1
        tokens["model_steps"] += int(run.get("model_steps") or 0)
        prompt = int(float(run.get("prompt_tokens") or 0))
        hit, miss = (
            run.get("prompt_cache_hit_tokens"),
            run.get("prompt_cache_miss_tokens"),
        )
        tokens["prompt"] += prompt
        tokens["completion"] += int(float(run.get("completion_tokens") or 0))
        if hit is None or miss is None:
            if prompt:
                tokens["runs_without_cache_split"] += 1
            tokens["cache_miss"] += prompt
        else:
            tokens["cache_hit"] += int(float(hit))
            tokens["cache_miss"] += int(float(miss))
    postmortem = (
        (pg.get("postmortem_tokens") or [{}])[0] if pg.get("postmortem_tokens") else {}
    )
    pm_prompt = int(postmortem.get("prompt_tokens") or 0)
    pm_completion = int(postmortem.get("completion_tokens") or 0)
    out: dict[str, Any] = {
        "source": "pg.json runs (opspilot_steps usage), cost.json",
        "tokens": tokens,
        "postmortem_tokens": {"prompt": pm_prompt, "completion": pm_completion},
        "note": "a lower bound: model calls whose step was never committed "
        "(retries, a crash mid-request) are not in opspilot_steps",
        "langsmith": {"usd": 0, "reason": "trace outbox off (S-R18)"},
    }
    table = (prices or {}).get("deepseek_cny_per_mtok")
    if isinstance(table, Mapping):
        estimate = (
            tokens["cache_hit"] * float(table["input_cache_hit"])
            + (tokens["cache_miss"] + pm_prompt) * float(table["input_cache_miss"])
            + (tokens["completion"] + pm_completion) * float(table["output"])
        ) / 1_000_000
        out["deepseek_cny_estimate"] = round(estimate, 4)
        out["price_source"] = (prices or {}).get("price_source")
    else:
        out["deepseek_cny_estimate"] = None
        out["price_source"] = "missing cost.json deepseek_cny_per_mtok"
    balance = (prices or {}).get("balance_cny") or {}
    if balance.get("start") is not None and balance.get("end") is not None:
        out["balance_delta_cny"] = round(
            float(balance["start"]) - float(balance["end"]), 4
        )
    else:
        out["balance_delta_cny"] = None
    rental = (prices or {}).get("rental") or {}
    if (
        rental.get("usd_per_hour") is not None
        and rental.get("started_at")
        and rental.get("ended_at")
    ):
        hours = (
            (_s(rental["ended_at"]) or 0.0) - (_s(rental["started_at"]) or 0.0)
        ) / 3600
        out["rental"] = {
            "hours": round(hours, 2),
            "usd_estimate": round(hours * float(rental["usd_per_hour"]), 2),
            "source": rental.get("source"),
            "billing_is_authoritative": True,
        }
    else:
        out["rental"] = None
    return out


# -- S-R12 and the whole -----------------------------------------------------------


def verdict(summary: Mapping[str, Any]) -> dict[str, Any]:
    faults_list = summary["faults"]
    injected = [
        i for i in summary["stuck_work"]["injections"] if i["kind"] == "episode"
    ]
    checks = {
        "coverage_ok": bool(summary["window"].get("coverage_ok")),
        "faults_accounted": all(
            f["status"] == "executed" or f["documented"] for f in faults_list
        ),
        "availability_published": bool(summary["availability"]["readyz"]),
        "missing_observations_published": summary["inputs"].get("pg.json") is not None
        and bool(summary["missing_observations"]["lab_instances"]["ticks"]),
        "stuck_work_published": summary["inputs"].get("pg.json") is not None,
        "resources_published": bool(summary["resources"]["hosts"]),
        "cost_published": summary["inputs"].get("pg.json") is not None,
        "injections_accounted": bool(injected)
        and all(
            i["destination"] not in {None, "none"} for i in injected if i["injected"]
        ),
    }
    return {
        "achieved": all(checks.values()),
        "checks": checks,
        "passes_flipped": False,
        "note": "F8 passes flips only once steps 1-4 all pass (S-R17)",
    }


def summarize(root: Path) -> dict[str, Any]:
    inputs = Inputs(root)
    plan_doc = inputs.json("plan.json")
    if plan_doc is None:
        raise SystemExit(f"{root}: no plan.json")
    plan_sha = hashlib.sha256((root / "plan.json").read_bytes()).hexdigest()
    events = inputs.jsonl("events.jsonl")
    probes = inputs.jsonl("probe.jsonl")
    run_log = inputs.jsonl("runs.jsonl")
    lab = inputs.jsonl("lab.jsonl")
    inputs.jsonl("clock.jsonl")
    pg = inputs.json("pg.json")
    prices = inputs.json("cost.json")
    config = inputs.json("soak.json") or {}
    exports = []
    for path in inputs.glob("prom/*.json"):
        exports.append(inputs.json(str(path.relative_to(root))))
    win = window(events, probes)
    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "claim": CLAIM,
        "plan_sha256": plan_sha,
        "plan_matches_start": any(
            e.get("kind") == "start" and e.get("plan_sha256") == plan_sha
            for e in events
        ),
        "window": win,
        "faults": faults(plan_doc, events),
        "availability": availability(
            probes, plan_doc, events, (pg or {}).get("deliveries")
        ),
        "missing_observations": missing_observations(
            pg, exports, lab, config.get("expected_deployments", ())
        ),
        "stuck_work": stuck_work(
            pg, run_log, plan_doc, events, exports, win.get("ended_at")
        ),
        "resources": resources(inputs, events),
        "cost": cost(pg, prices),
        "operator": operator_actions(events),
        "clock": clock(inputs),
        "environment_actions": [e for e in events if e.get("kind") == "env"],
        "errors": sum(1 for e in events if e.get("kind") == "error"),
    }
    summary["inputs"] = dict(sorted(inputs.read.items()))
    summary["step4"] = verdict(summary)
    return summary


def operator_actions(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closes = [e for e in events if e.get("kind") == "operator"]
    return {
        "actor_note": "scripted on behalf of a human (S-R3); no human interaction claimed",
        "closes": len(closes),
        "accepted": sum(1 for e in closes if e.get("status") == 200),
        "by_reason": dict(
            sorted(
                (reason, sum(1 for e in closes if e.get("reason") == reason))
                for reason in {str(e.get("reason")) for e in closes}
            )
        ),
        "open_at_episode_start": [
            e
            for e in events
            if e.get("kind") == "open_incidents" and e.get("incidents")
        ],
    }


def clock(inputs: Inputs) -> dict[str, Any]:
    rows = list(read_jsonl(inputs.root / "clock.jsonl"))
    offsets = [abs(r["offset_s"]) for r in rows if r.get("offset_s") is not None]
    return {
        "source": "clock.jsonl",
        "samples": len(rows),
        "max_abs_offset_s": max(offsets) if offsets else None,
        "over_1s": [
            r.get("at")
            for r in rows
            if r.get("offset_s") is not None and abs(r["offset_s"]) > 1
        ],
        "match_tolerance_s": 2,
    }


def write(root: Path, out: Path) -> dict[str, Any]:
    summary = summarize(root)
    out.write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary
