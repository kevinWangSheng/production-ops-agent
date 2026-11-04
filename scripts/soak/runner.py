"""The soak runner on da-server: executes the frozen plan, closes incidents
as the scripted operator (S-R3) and samples availability, alerts, lab
liveness, resources and clocks (S-R5, S-R7, S-R8, S-R10).

It writes the raw directory the summarizer reads (``summarize`` module
docstring, ``scripts/soak/README.md``) directly: ``events.jsonl`` (``ts`` +
``type``), ``readyz.csv``, ``prom_up.csv``, ``ksm.csv``, ``alerts.jsonl``,
``docker_stats.csv``, ``host_stats.csv``, ``sizes.csv``; plus its own
``run_states.jsonl`` (for ``finish``), ``frontend_rate.jsonl`` and ``prom/``.

One process, one thread per loop; a loop that raises logs an ``error`` event
and goes on. Restartable: T0 is frozen in the plan, and finished steps are
read back from ``events.jsonl``, so a restart never repeats a step. It never
patches or restarts the product outside the planned A1–A5 steps (S-R13); the
only environment action it takes by itself is the OTel collector restart
after 10 min without frontend spans.
"""

from __future__ import annotations

import csv
import json
import shutil
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.soak import pg
from scripts.soak import plan as soak_plan
from scripts.soak.common import (
    DURATION_S,
    append_jsonl,
    extension_s,
    iso,
    merge_intervals,
    parse_time,
    read_jsonl,
    utcnow,
)
from scripts.soak.lab_io import Database, Prometheus, basic_auth, http, run

PROBE_INTERVAL_S = 30
SAMPLE_INTERVAL_S = 60
CLOSER_INTERVAL_S = 60
CLOCK_INTERVAL_S = 6 * 3600
EXPORT_INTERVAL_S = 6 * 3600
EXPORT_STEP_S = 60
#: The runner must be started before T0; later than this it refuses.
LATE_START_S = 300
#: A step whose time passed this long ago while the runner was down is
#: recorded as missed (``injection_skipped``) instead of run late; restoring
#: steps are never skipped.
MISSED_AFTER_S = 30 * 60
STEP_ATTEMPTS = 3
STEP_RETRY_S = 30
#: S-R13: frontend span rate 0 for this long -> restart the OTel collector.
COLLECTOR_STALL_S = 10 * 60
#: After the planned tunnel start, the reconnect is still part of A4.
TUNNEL_RECONNECT_GRACE_S = 120
RESTORING_ACTIONS = frozenset(
    {"flag_off", "worker_up", "egress_restore", "tunnel_start"}
)
#: Reverse-tunnel port of each health listener (S-R1) -> role.
INSTANCE_ROLES = {"29464": "web", "29465": "worker", "29466": "observer"}
FRONTEND_RATE = (
    'sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",'
    'service_name="frontend"}[2m]))'
)
OPSPILOT_UP = 'up{job="opspilot"}'
#: S-R15: the series exported every 6 h at 60 s resolution.
EXPORT_SERIES = {
    "opspilot_up": OPSPILOT_UP,
    "alerts": "ALERTS",
    "frontend_span_rate": FRONTEND_RATE,
}
KSM_QUERIES = {
    "pod_owner": 'kube_pod_owner{namespace="otel-demo",owner_kind="ReplicaSet"}',
    "replicaset_owner": (
        'kube_replicaset_owner{namespace="otel-demo",owner_kind="Deployment"}'
    ),
    "container_info": 'kube_pod_container_info{namespace="otel-demo"}',
}
CSV_HEADERS = {
    "readyz.csv": ["ts", "role", "status"],
    "prom_up.csv": ["ts", "role", "up"],
    "ksm.csv": ["ts", "deployment", "pods"],
    "docker_stats.csv": ["ts", "host", "group", "container", "cpu_pct", "mem_bytes"],
    "host_stats.csv": ["ts", "host", "mem_available_bytes", "load1", "disk_free_bytes"],
    "sizes.csv": ["ts", "host", "kind", "bytes"],
    "samples.csv": ["ts", "session_id", "signal_status"],
}
OPSPILOT_PREFIX = "opspilot-"


def append_csv(path: Path, rows: Sequence[Sequence[Any]]) -> None:
    """Append rows, writing the header first for a new file."""
    if not rows:
        return
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(CSV_HEADERS[path.name])
        writer.writerows(rows)
        handle.flush()


@dataclass(frozen=True)
class Config:
    """``soak.json`` in the data directory (example in the README)."""

    compose: tuple[str, ...]
    compose_egress: tuple[str, ...]
    workbench: str
    operator_credentials_file: Path
    health: Mapping[str, str]
    prometheus: str
    prometheus_env_file: Path | None
    fault_ssh: tuple[str, ...]
    host_label: str = "da-server"
    #: Non-secret variables compose.soak.yaml interpolates (SOAK_RENTAL_HOST,
    #: SOAK_SECRETS_DIR, SOAK_LOG_DIR, SOAK_KIND_GATEWAY).
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = json.loads(path.read_text(encoding="utf-8"))
        env_file = raw.get("prometheus_env_file")
        return cls(
            compose=tuple(raw["compose"]),
            compose_egress=tuple(raw["compose_egress"]),
            workbench=str(raw["workbench"]).rstrip("/"),
            operator_credentials_file=Path(raw["operator_credentials_file"]),
            health=dict(raw["health"]),
            prometheus=str(raw["prometheus"]),
            prometheus_env_file=Path(env_file) if env_file else None,
            fault_ssh=tuple(raw["fault_ssh"]),
            host_label=str(raw.get("host_label", "da-server")),
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        )


# -- bookkeeping over events (pure) ---------------------------------------------


def finished_steps(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The final record of each step that finished (ok, failed or skipped)."""
    done: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") == "step" and event.get("status") in {
            "ok",
            "failed",
            "skipped",
        }:
            done[str(event["step"])] = dict(event)
    return done


def due_at(
    step: Mapping[str, Any], t0: datetime, done: Mapping[str, Mapping[str, Any]]
) -> datetime | None:
    """When the step is due; ``None`` while the step it follows has not
    finished. A follower is timed from its predecessor's actual time."""
    if "at_s" in step:
        return t0 + timedelta(seconds=int(step["at_s"]))
    before = done.get(str(step["after"]))
    if before is None:
        return None
    return parse_time(str(before["ts"])) + timedelta(seconds=int(step["delay_s"]))


def flags_on(
    steps: Sequence[Mapping[str, Any]], done: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Injections whose flag was turned on and not yet turned off."""
    active = []
    for step in steps:
        if step["action"] != "flag_on":
            continue
        on = done.get(str(step["id"]))
        if (
            on is not None
            and on.get("status") == "ok"
            and f"{step['item']}:off" not in done
        ):
            active.append(str(step["item"]))
    return active


def interruption_seconds(events: Sequence[Mapping[str, Any]], now: datetime) -> float:
    """S-R4: total ``env_interruption_start``/``_end`` time (an open one runs
    until ``now``), the same pairing the summarizer uses."""
    spans: list[tuple[float, float]] = []
    open_at: float | None = None
    ordered = sorted(events, key=lambda e: parse_time(str(e["ts"])).timestamp())
    for event in ordered:
        at = parse_time(str(event["ts"])).timestamp()
        if event.get("type") == "env_interruption_start" and open_at is None:
            open_at = at
        elif event.get("type") == "env_interruption_end" and open_at is not None:
            spans.append((open_at, at))
            open_at = None
    if open_at is not None:
        spans.append((open_at, max(open_at, now.timestamp())))
    return sum(b - a for a, b in merge_intervals(spans))


def soak_end(t0: datetime, interruption_s: float) -> datetime:
    return t0 + timedelta(seconds=DURATION_S + extension_s(interruption_s))


# -- the runner -------------------------------------------------------------------


@dataclass
class Runner:
    config: Config
    data: Path
    plan: dict[str, Any]
    plan_sha256: str
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.events_path = self.data / "events.jsonl"
        self.t0 = parse_time(str(self.plan["t0"]))
        self.steps = soak_plan.steps(self.plan)
        self.injections = {str(i["id"]): i for i in self.plan["injections"]}
        self.db = Database(self.config.compose, env=self.config.env)
        self.prom = Prometheus(self.config.prometheus, self.config.prometheus_env_file)
        self._lock = threading.Lock()
        self._zero_rate_since: datetime | None = None
        self._last_run_states: dict[str, str] = {}
        self._tunnel_ok: bool | None = None
        self._seen_alerts: set[tuple[str, str]] = {
            (str(a.get("fingerprint")), str(a.get("starts_at")))
            for a in read_jsonl(self.data / "alerts.jsonl")
        }

    # events ---------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            append_jsonl(
                self.events_path, {"ts": iso(self.now()), "type": kind, **fields}
            )

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(read_jsonl(self.events_path))

    def started(self) -> bool:
        return any(e.get("type") == "soak_start" for e in self.events())

    def begin(self) -> None:
        """Wait for T0 and write ``soak_start``; a restart after T0 goes on."""
        if self.started():
            for event in self.events():
                if (
                    event.get("type") == "soak_start"
                    and event.get("plan_sha256") != self.plan_sha256
                ):
                    raise SystemExit("events.jsonl was started with another plan")
            self.event("runner_restarted")
            return
        if (self.now() - self.t0).total_seconds() > LATE_START_S:
            raise SystemExit(f"T0 {iso(self.t0)} has passed; freeze a new plan")
        while self.now() < self.t0 and not self.stop.is_set():
            self.sleep(min(5.0, (self.t0 - self.now()).total_seconds()))
        self.event("soak_start", plan_sha256=self.plan_sha256, t0=iso(self.t0))

    # commands -------------------------------------------------------------

    def compose(self, *args: str, egress: bool = False, timeout: float = 300) -> Any:
        base = [*self.config.compose, *(self.config.compose_egress if egress else ())]
        return run([*base, *args], timeout=timeout, env=self.config.env)

    def fault(self, *args: str, timeout: float = 180) -> Any:
        return run([*self.config.fault_ssh, " ".join(args)], timeout=timeout)

    def action(self, step: Mapping[str, Any]) -> Any:
        kind = step["action"]
        if kind in {"flag_on", "flag_off"}:
            state = "on" if kind == "flag_on" else "off"
            return self.fault("flag", str(step["flag"]), state, str(step["item"]))
        commands: dict[str, Callable[[], Any]] = {
            "worker_kill": lambda: self.compose("kill", "worker"),
            "worker_up": lambda: self.compose("up", "-d", "--no-deps", "worker"),
            "pg_restart": lambda: self.compose("restart", "postgres"),
            "egress_cut": lambda: self.compose(
                "up", "-d", "--no-deps", "worker", egress=True
            ),
            "egress_restore": lambda: self.compose(
                "up", "-d", "--no-deps", "--force-recreate", "worker"
            ),
            "tunnel_stop": lambda: self.compose("stop", "soak-tunnel"),
            "tunnel_start": lambda: self.compose("start", "soak-tunnel"),
            "web_restart": lambda: self.compose("restart", "web"),
        }
        return commands[kind]()

    def condition_met(self, condition: str) -> bool:
        states = self.run_states()
        if condition == "run_running":
            return any(state == "running" for state in states.values())
        if condition == "idle":
            busy = any(state in pg.ACTIVE_RUN_STATES for state in states.values())
            return not busy and not flags_on(self.steps, finished_steps(self.events()))
        raise ValueError(condition)

    def run_states(self) -> dict[str, str]:
        rows = self.db.rows(pg.run_states_sql(self.t0))
        return {str(row["run_id"]): str(row["state"]) for row in rows}

    # the plan loop --------------------------------------------------------

    def plan_tick(self) -> None:
        now = self.now()
        events = self.events()
        done = finished_steps(events)
        waiting = {
            str(e["step"]): parse_time(str(e["ts"]))
            for e in events
            if e.get("type") == "step" and e.get("status") == "waiting"
        }
        for step in self.steps:
            step_id = str(step["id"])
            if step_id in done:
                continue
            due = due_at(step, self.t0, done)
            if due is None or now < due:
                continue
            restoring = step["action"] in RESTORING_ACTIONS
            late = (now - due).total_seconds()
            if not restoring and late > MISSED_AFTER_S + int(step.get("max_wait_s", 0)):
                self.event(
                    "step",
                    step=step_id,
                    status="skipped",
                    reason="missed",
                    due=iso(due),
                )
                self.mark(
                    step, "skipped", reason=f"runner was down; {int(late)} s late"
                )
                done = finished_steps(self.events())
                continue
            condition = step.get("condition")
            condition_met: bool | None = None
            if condition is not None:
                condition_met = self.condition_met(str(condition))
                if not condition_met:
                    started = waiting.get(step_id)
                    if started is None:
                        self.event(
                            "step",
                            step=step_id,
                            status="waiting",
                            condition=condition,
                            due=iso(due),
                        )
                        continue
                    if (now - started).total_seconds() < int(step["max_wait_s"]):
                        continue
            self.execute(step, due=due, condition_met=condition_met)
            done = finished_steps(self.events())

    def _is_first(self, step: Mapping[str, Any]) -> bool:
        return str(self.injections[str(step["item"])]["steps"][0]["id"]) == step["id"]

    def _is_last(self, step: Mapping[str, Any]) -> bool:
        steps = self.injections[str(step["item"])]["steps"]
        return len(steps) > 1 and str(steps[-1]["id"]) == step["id"]

    def mark(self, step: Mapping[str, Any], status: str, *, reason: str = "") -> None:
        """The summarizer's view: ``injection_on`` when an injection's first
        step succeeded, ``injection_off`` when its last one did,
        ``injection_skipped`` (with the reason) when its first step was missed
        or failed."""
        item = str(step["item"])
        if self._is_first(step):
            if status == "ok":
                self.event("injection_on", injection_id=item)
            else:
                self.event("injection_skipped", injection_id=item, reason=reason)
        elif self._is_last(step) and status == "ok":
            self.event("injection_off", injection_id=item)

    def execute(
        self, step: Mapping[str, Any], *, due: datetime, condition_met: bool | None
    ) -> None:
        step_id = str(step["id"])
        if step["action"] == "flag_on":
            # S-R3/E4 bookkeeping: what is still open when a new episode starts
            self.record_open_incidents(step_id)
        for attempt in range(1, STEP_ATTEMPTS + 1):
            started = self.now()
            proc = self.action(step)
            fields: dict[str, Any] = {
                "step": step_id,
                "action": step["action"],
                "attempt": attempt,
                "due": iso(due),
                "started": iso(started),
                "exit": proc.returncode,
                "output": (proc.stdout or "")[-400:],
                "stderr": (proc.stderr or "")[-400:],
            }
            if condition_met is not None:
                fields["condition"] = step["condition"]
                fields["condition_met"] = condition_met
            if proc.returncode == 0:
                self.event("step", status="ok", **fields)
                self.mark(step, "ok")
                return
            if attempt == STEP_ATTEMPTS:
                self.event("step", status="failed", **fields)
                self.mark(
                    step,
                    "failed",
                    reason=f"failed after {STEP_ATTEMPTS} attempts, exit {proc.returncode}",
                )
                return
            self.event("step", status="retry", **fields)
            self.sleep(STEP_RETRY_S)

    def record_open_incidents(self, step_id: str) -> None:
        try:
            rows = self.db.rows(pg.close_candidates_sql(self.t0))
        except RuntimeError as exc:
            self.event("error", loop="open_incidents", error=str(exc)[:300])
            return
        self.event(
            "open_incidents",
            before_step=step_id,
            incidents=[
                {"incident_id": row["incident_id"], "lifecycle": row["lifecycle"]}
                for row in rows
            ],
        )

    # the scripted operator (S-R3) ------------------------------------------

    def closer_tick(self) -> None:
        states = self.run_states()
        for run_id, state in sorted(states.items()):
            if self._last_run_states.get(run_id) != state:
                append_jsonl(
                    self.data / "run_states.jsonl",
                    {"ts": iso(self.now()), "run_id": run_id, "state": state},
                )
        self._last_run_states = states
        active = bool(flags_on(self.steps, finished_steps(self.events())))
        for row in self.db.rows(pg.close_candidates_sql(self.t0)):
            reason = pg.should_close(row, faults_active=active)
            if reason is not None:
                self.close(row, reason)

    def close(self, row: Mapping[str, Any], reason: str) -> None:
        incident = str(row["incident_id"])
        generation = int(row["control_generation"])
        user, _, password = (
            self.config.operator_credentials_file.read_text(encoding="utf-8").strip()
        ).partition(":")
        body = urllib.parse.urlencode(
            {
                "action": "close",
                "expected_generation": str(generation),
                "idempotency_key": f"soak-close-{incident}-{generation}",
            }
        ).encode()
        reply = http(
            f"{self.config.workbench}/incidents/{incident}/control",
            method="POST",
            data=body,
            headers={
                "Authorization": basic_auth(user, password),
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.config.workbench,
                "Accept": "application/json",
            },
        )
        self.event(
            "operator",
            action="close",
            actor=user,
            scripted=True,
            incident_id=incident,
            expected_generation=generation,
            reason=reason,
            status=reply.status,
            error=reply.error,
            response=reply.body[:300].decode("utf-8", "replace"),
        )

    # probes and samples ----------------------------------------------------

    def _planned_tunnel_stop(self) -> bool:
        done = finished_steps(self.events())
        stop, start = done.get("A4:stop"), done.get("A4:start")
        if stop is None or stop.get("status") != "ok":
            return False
        if start is None:
            return True
        since = (self.now() - parse_time(str(start["ts"]))).total_seconds()
        return since < TUNNEL_RECONNECT_GRACE_S

    def probe_tick(self) -> None:
        at = iso(self.now())
        rows = []
        for role, url in sorted(self.config.health.items()):
            reply = http(url, timeout=5)
            rows.append([at, role, reply.status or 0])
        append_csv(self.data / "readyz.csv", rows)
        reply, result = self.prom.query(OPSPILOT_UP, timeout=10)
        ok = reply.status == 200 and result is not None
        if ok and result is not None:
            up = {
                INSTANCE_ROLES.get(
                    str(s["metric"].get("instance", "")).rsplit(":", 1)[-1]
                ): s["value"][1]
                for s in result
            }
            append_csv(
                self.data / "prom_up.csv",
                [
                    [at, role, 1 if up.get(role) == "1" else 0]
                    for role in sorted(INSTANCE_ROLES.values())
                ],
            )
        self.tunnel_transition(ok)

    def tunnel_transition(self, ok: bool) -> None:
        """``tunnel_down``/``tunnel_up`` on change; an outage outside the
        planned A4 stop is also an environment interruption (S-R4)."""
        if self._tunnel_ok is None:
            last = [
                e
                for e in self.events()
                if e.get("type") in {"tunnel_down", "tunnel_up"}
            ]
            self._tunnel_ok = not last or last[-1]["type"] == "tunnel_up"
        if ok == self._tunnel_ok:
            return
        self._tunnel_ok = ok
        events = self.events()
        interrupted = [
            e
            for e in events
            if e.get("type") in {"env_interruption_start", "env_interruption_end"}
            and e.get("cause") == "tunnel_down"
        ]
        open_interruption = (
            bool(interrupted) and interrupted[-1]["type"] == "env_interruption_start"
        )
        if not ok:
            self.event("tunnel_down")
            if not self._planned_tunnel_stop():
                self.event("env_interruption_start", cause="tunnel_down")
        else:
            self.event("tunnel_up")
            if open_interruption:
                self.event("env_interruption_end", cause="tunnel_down")

    def alerts_tick(self) -> None:
        """New lab Alertmanager alerts (rental clock) into ``alerts.jsonl``."""
        proc = self.fault("alerts", timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"alerts exit {proc.returncode}: {proc.stderr[-200:]}")
        for alert in json.loads(proc.stdout or "[]"):
            labels = alert.get("labels") or {}
            key = (str(alert.get("fingerprint")), str(alert.get("startsAt")))
            if key in self._seen_alerts:
                continue
            self._seen_alerts.add(key)
            append_jsonl(
                self.data / "alerts.jsonl",
                {
                    "alertname": labels.get("alertname"),
                    "fingerprint": alert.get("fingerprint"),
                    "starts_at": normalize_ts(str(alert.get("startsAt"))),
                    "seen_at": iso(self.now()),
                },
            )

    def lab_tick(self) -> None:
        """S-R8(c): pods per Deployment through the KSM owner mapping only
        (no span freshness); and the S-R13 collector stall."""
        at = self.now()
        series: dict[str, list[Any] | None] = {}
        for name, expr in KSM_QUERIES.items():
            _, series[name] = self.prom.query(expr)
        counts = deployment_pods(series)
        if counts is not None:
            append_csv(
                self.data / "ksm.csv",
                [[iso(at), name, pods] for name, pods in sorted(counts.items())],
            )
        _, rate = self.prom.query(FRONTEND_RATE)
        frontend = float(rate[0]["value"][1]) if rate else None
        append_jsonl(
            self.data / "frontend_rate.jsonl", {"ts": iso(at), "rate": frontend}
        )
        if frontend == 0.0:
            self._zero_rate_since = self._zero_rate_since or at
            if (at - self._zero_rate_since).total_seconds() >= COLLECTOR_STALL_S:
                proc = self.fault("collector-restart")
                self.event(
                    "env_action",
                    action="rollout restart otel-collector",
                    reason="frontend span rate 0 for 10 min",
                    exit=proc.returncode,
                    since=iso(self._zero_rate_since),
                )
                self._zero_rate_since = None
        else:
            self._zero_rate_since = None

    def resource_tick(self) -> None:
        write_resource_sample(self.data, self.config.host_label, "shared", self.now())

    def clock_tick(self) -> None:
        self.event("clock_check", **clock_sample(self, self.now))

    def sizes(self, phase: str) -> None:
        rows: list[list[Any]] = []
        at = iso(self.now())
        try:
            db = self.db.rows(pg.export_queries(self.t0)["database"])[0]["bytes"]
            rows.append([at, self.config.host_label, "pg_database", db])
        except (RuntimeError, IndexError, KeyError) as exc:
            self.event("error", loop="sizes", error=str(exc)[:300])
        rows += docker_sizes(at, self.config.host_label)
        append_csv(self.data / "sizes.csv", rows)
        self.event("sizes", phase=phase)

    def export_tick(self, end: datetime | None = None) -> None:
        end = end or self.now()
        start = max(self.t0, end - timedelta(seconds=EXPORT_INTERVAL_S + 600))
        out: dict[str, Any] = {
            "start": iso(start),
            "end": iso(end),
            "step_s": EXPORT_STEP_S,
            "series": {},
        }
        for name, expr in EXPORT_SERIES.items():
            reply, result = self.prom.query_range(
                expr, start.timestamp(), end.timestamp(), EXPORT_STEP_S
            )
            out["series"][name] = {
                "expr": expr,
                "status": reply.status,
                "error": reply.error,
                "result": result,
            }
        path = self.data / "prom" / f"{end.strftime('%Y%m%dT%H%M%SZ')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, sort_keys=True), encoding="utf-8")
        self.event("export", path=str(path.relative_to(self.data)))

    # main ------------------------------------------------------------------

    def loop(self, name: str, interval: float, tick: Callable[[], None]) -> None:
        while not self.stop.is_set():
            self.loop_once(name, tick)
            self.stop.wait(interval)

    def loop_once(self, name: str, tick: Callable[[], None]) -> None:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 - one loop must not stop the rest
            self.event("error", loop=name, error=f"{type(exc).__name__}: {exc}"[:300])

    def run_until_end(self) -> None:
        self.begin()
        if self.stop.is_set():
            return
        if not any(e.get("type") == "sizes" for e in self.events()):
            self.loop_once("sizes", lambda: self.sizes("start"))
        self.loop_once("clock", self.clock_tick)
        loops = [
            ("plan", 5.0, self.plan_tick),
            ("closer", CLOSER_INTERVAL_S, self.closer_tick),
            ("probe", PROBE_INTERVAL_S, self.probe_tick),
            ("alerts", SAMPLE_INTERVAL_S, self.alerts_tick),
            ("lab", SAMPLE_INTERVAL_S, self.lab_tick),
            ("resources", SAMPLE_INTERVAL_S, self.resource_tick),
        ]
        threads = [
            threading.Thread(target=self.loop, args=loop, name=loop[0], daemon=True)
            for loop in loops
        ]
        for thread in threads:
            thread.start()
        next_clock = self.now() + timedelta(seconds=CLOCK_INTERVAL_S)
        next_export = self.now() + timedelta(seconds=EXPORT_INTERVAL_S)
        while not self.stop.is_set():
            now = self.now()
            if now >= soak_end(self.t0, interruption_seconds(self.events(), now)):
                break
            if now >= next_clock:
                self.loop_once("clock", self.clock_tick)
                next_clock = now + timedelta(seconds=CLOCK_INTERVAL_S)
            if now >= next_export:
                self.loop_once("export", self.export_tick)
                next_export = now + timedelta(seconds=EXPORT_INTERVAL_S)
            self.stop.wait(30)
        self.stop.set()
        for thread in threads:
            thread.join(timeout=120)
        self.loop_once("clock", self.clock_tick)
        self.loop_once("export", self.export_tick)
        self.event(
            "soak_end", interruption_s=interruption_seconds(self.events(), self.now())
        )


# -- samplers (also used on the rental host) ----------------------------------


def normalize_ts(raw: str) -> str:
    """Alertmanager's RFC 3339 (nanoseconds, offset) as ``...Z`` microseconds."""
    text = raw.strip()
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        zone = rest[len(digits) :]
        text = f"{head}.{digits[:6].ljust(6, '0')}{zone}"
    return iso(parse_time(text))


def deployment_pods(series: Mapping[str, list[Any] | None]) -> dict[str, int] | None:
    """Pods with a container per Deployment, joined in Python over the three
    KSM series (pod -> ReplicaSet -> Deployment). ``None`` when a query
    failed. Stale (zombie) series are not filtered."""
    owners, replicasets, containers = (
        series.get("pod_owner"),
        series.get("replicaset_owner"),
        series.get("container_info"),
    )
    if owners is None or replicasets is None or containers is None:
        return None
    rs_to_deploy = {
        s["metric"].get("replicaset"): s["metric"].get("owner_name")
        for s in replicasets
    }
    with_container = {s["metric"].get("pod") for s in containers}
    counts: dict[str, int] = {deploy: 0 for deploy in rs_to_deploy.values() if deploy}
    for s in owners:
        deploy = rs_to_deploy.get(s["metric"].get("owner_name"))
        if deploy and s["metric"].get("pod") in with_container:
            counts[deploy] += 1
    return counts


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return values


def write_resource_sample(
    data: Path, host: str, other_group: str, now: datetime
) -> None:
    """S-R10: one ``host_stats.csv`` row and one ``docker_stats.csv`` row per
    container. ``opspilot-*`` containers are group ``opspilot``; the rest are
    ``other_group`` (``shared`` on da-server, ``lab`` on the rental host)."""
    at = iso(now)
    mem = _meminfo()
    try:
        load1: float | None = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        load1 = None
    available = mem.get("MemAvailable")
    append_csv(
        data / "host_stats.csv",
        [
            [
                at,
                host,
                "" if available is None else available * 1024,
                "" if load1 is None else load1,
                shutil.disk_usage("/").free,
            ]
        ],
    )
    proc = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=60)
    rows = []
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("Name"))
        group = "opspilot" if name.startswith(OPSPILOT_PREFIX) else other_group
        cpu = _percent(row.get("CPUPerc"))
        mem_bytes = _bytes(str(row.get("MemUsage", "")).split("/")[0])
        rows.append(
            [
                at,
                host,
                group,
                name,
                "" if cpu is None else cpu,
                "" if mem_bytes is None else mem_bytes,
            ]
        )
    append_csv(data / "docker_stats.csv", rows)


def docker_sizes(at: str, host: str) -> list[list[Any]]:
    """``docker system df`` sizes (images, containers, local volumes, build
    cache) as ``sizes.csv`` rows."""
    proc = run(["docker", "system", "df", "--format", "{{json .}}"], timeout=120)
    rows: list[list[Any]] = []
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        size = _bytes(str(row.get("Size", "")))
        kind = "docker_" + str(row.get("Type", "")).lower().replace(" ", "_")
        if size is not None:
            rows.append([at, host, kind, size])
    return rows


def _percent(raw: Any) -> float | None:
    try:
        return float(str(raw).strip().rstrip("%"))
    except ValueError:
        return None


_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}


def _bytes(raw: str) -> int | None:
    text = raw.strip().lower()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)].strip()) * _UNITS[unit])
            except ValueError:
                return None
    return None


def clock_sample(runner: Runner, now: Callable[[], datetime]) -> dict[str, Any]:
    """S-R5: the rental host's clock over the fault key (``fault.sh clock``
    prints epoch seconds): offset = remote - midpoint of the round trip,
    plus the local NTP state."""
    local = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
    before = now()
    proc = runner.fault("clock", timeout=30)
    after = now()
    remote_epoch = None
    for line in proc.stdout.splitlines():
        if line.startswith("epoch="):
            try:
                remote_epoch = float(line.removeprefix("epoch="))
            except ValueError:
                remote_epoch = None
    midpoint = (before.timestamp() + after.timestamp()) / 2
    return {
        "host": "rental",
        "offset_s": None if remote_epoch is None else round(remote_epoch - midpoint, 3),
        "rtt_s": round(after.timestamp() - before.timestamp(), 3),
        "local_ntp_synchronized": local.stdout.strip() or None,
        "remote_exit": proc.returncode,
        "remote_output": proc.stdout[-600:],
    }
