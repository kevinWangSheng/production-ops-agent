"""The soak runner on da-server: executes the frozen plan, closes incidents
as the scripted operator (S-R3) and samples availability, lab liveness,
resources and clocks (S-R5, S-R7, S-R8, S-R10).

One process, one thread per loop; a loop that raises logs an ``error`` event
and goes on. The runner is restartable: T0 and every finished step are read
back from ``events.jsonl``, so a restart neither moves T0 nor repeats a step.
It never patches or restarts the product outside the planned A1–A5 steps
(S-R13); the only environment action it takes by itself is the OTel
collector restart after 10 min without frontend spans.

Data directory files (formats in ``scripts/soak/README.md``):
``events.jsonl``, ``probe.jsonl``, ``runs.jsonl``, ``lab.jsonl``,
``resources-<host>.jsonl``, ``clock.jsonl``, ``prom/*.json``.
"""

from __future__ import annotations

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
#: A step whose time passed this long ago while the runner was down is
#: recorded as missed instead of run late (``flag_off`` and other restoring
#: steps are never skipped).
MISSED_AFTER_S = 30 * 60
STEP_ATTEMPTS = 3
STEP_RETRY_S = 30
#: S-R13: frontend span rate 0 for this long -> restart the OTel collector.
COLLECTOR_STALL_S = 10 * 60
RESTORING_ACTIONS = frozenset(
    {"flag_off", "worker_up", "egress_restore", "tunnel_start"}
)
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
    "checkout_error_ratio": (
        '(sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",'
        'service_name="checkout",span_kind="SPAN_KIND_SERVER",'
        'span_name=~".*CheckoutService/PlaceOrder",status_code="STATUS_CODE_ERROR"}[5m])))'
        ' / (sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",'
        'service_name="checkout",span_kind="SPAN_KIND_SERVER",'
        'span_name=~".*CheckoutService/PlaceOrder"}[5m])))'
    ),
}
KSM_QUERIES = {
    "pod_owner": 'kube_pod_owner{namespace="otel-demo",owner_kind="ReplicaSet"}',
    "replicaset_owner": (
        'kube_replicaset_owner{namespace="otel-demo",owner_kind="Deployment"}'
    ),
    "container_info": 'kube_pod_container_info{namespace="otel-demo"}',
}


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
    expected_deployments: tuple[str, ...] = ()
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
            expected_deployments=tuple(raw.get("expected_deployments", ())),
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        )


# -- step bookkeeping (pure) ---------------------------------------------------


def finished_steps(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The final record of each step that finished (ok, failed or skipped)."""
    done: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") == "step" and event.get("status") in {
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
    return parse_time(str(before["at"])) + timedelta(seconds=int(step["delay_s"]))


def flags_on(
    steps: Sequence[Mapping[str, Any]], done: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Plan items whose flag was turned on and not yet turned off."""
    active = []
    for step in steps:
        if step["action"] != "flag_on":
            continue
        on = done.get(str(step["id"]))
        off_id = f"{step['item']}:off"
        if on is not None and on.get("status") == "ok" and off_id not in done:
            active.append(str(step["item"]))
    return active


def soak_end(t0: datetime, interruption_s: float) -> datetime:
    return t0 + timedelta(seconds=DURATION_S + extension_s(interruption_s))


# -- the runner ----------------------------------------------------------------


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
        self.steps = soak_plan.steps(self.plan)
        self.db = Database(self.config.compose, env=self.config.env)
        self.prom = Prometheus(self.config.prometheus, self.config.prometheus_env_file)
        self._lock = threading.Lock()
        self._zero_rate_since: datetime | None = None
        self._last_run_states: dict[str, str] = {}
        self._t0: datetime | None = None

    # events -----------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            append_jsonl(
                self.events_path, {"at": iso(self.now()), "kind": kind, **fields}
            )

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(read_jsonl(self.events_path))

    def t0(self) -> datetime:
        if self._t0 is not None:
            return self._t0
        for event in self.events():
            if event.get("kind") == "start":
                if event.get("plan_sha256") != self.plan_sha256:
                    raise SystemExit("events.jsonl was started with another plan")
                self._t0 = parse_time(str(event["t0"]))
                return self._t0
        start = self.now()
        self.event("start", t0=iso(start), plan_sha256=self.plan_sha256)
        self._t0 = start
        return start

    # commands ---------------------------------------------------------------

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
            done = finished_steps(self.events())
            return not busy and not flags_on(self.steps, done)
        raise ValueError(condition)

    def run_states(self) -> dict[str, str]:
        rows = self.db.rows(pg.run_states_sql(self.t0()))
        return {str(row["run_id"]): str(row["state"]) for row in rows}

    # the plan loop ----------------------------------------------------------

    def plan_tick(self) -> None:
        t0 = self.t0()
        now = self.now()
        done = finished_steps(self.events())
        waiting = {
            str(e["step"]): parse_time(str(e["at"]))
            for e in self.events()
            if e.get("kind") == "step" and e.get("status") == "waiting"
        }
        for step in self.steps:
            step_id = str(step["id"])
            if step_id in done:
                continue
            due = due_at(step, t0, done)
            if due is None or now < due:
                continue
            restoring = step["action"] in RESTORING_ACTIONS
            if not restoring and (now - due).total_seconds() > MISSED_AFTER_S + int(
                step.get("max_wait_s", 0)
            ):
                self.event(
                    "step",
                    step=step_id,
                    status="skipped",
                    reason="missed",
                    due=iso(due),
                )
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
            ok = proc.returncode == 0
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
            if ok:
                self.event("step", status="ok", **fields)
                return
            if attempt == STEP_ATTEMPTS:
                self.event("step", status="failed", **fields)
                return
            self.event("step", status="retry", **fields)
            self.sleep(STEP_RETRY_S)

    def record_open_incidents(self, step_id: str) -> None:
        try:
            rows = self.db.rows(pg.close_candidates_sql(self.t0()))
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
        t0 = self.t0()
        states = self.run_states()
        for run_id, state in sorted(states.items()):
            if self._last_run_states.get(run_id) != state:
                append_jsonl(
                    self.data / "runs.jsonl",
                    {"at": iso(self.now()), "run_id": run_id, "state": state},
                )
        self._last_run_states = states
        active = bool(flags_on(self.steps, finished_steps(self.events())))
        for row in self.db.rows(pg.close_candidates_sql(t0)):
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

    def probe_tick(self) -> None:
        at = iso(self.now())
        for role, url in sorted(self.config.health.items()):
            reply = http(url, timeout=5)
            append_jsonl(
                self.data / "probe.jsonl",
                {
                    "at": at,
                    "target": role,
                    "status": reply.status,
                    "error": reply.error,
                },
            )
        reply, result = self.prom.query(OPSPILOT_UP, timeout=10)
        up = None
        if result is not None:
            up = {
                str(series["metric"].get("instance", "")): series["value"][1]
                for series in result
            }
        append_jsonl(
            self.data / "probe.jsonl",
            {
                "at": at,
                "target": "tunnel",
                "status": reply.status,
                "error": reply.error,
                "opspilot_up": up,
            },
        )

    def lab_tick(self) -> None:
        """S-R8(c): pods under each expected Deployment through the KSM owner
        mapping only (no span freshness); and the S-R13 collector stall."""
        at = self.now()
        series: dict[str, list[Any] | None] = {}
        for name, expr in KSM_QUERIES.items():
            _, series[name] = self.prom.query(expr)
        counts = deployment_pods(series)
        _, rate = self.prom.query(FRONTEND_RATE)
        frontend = None
        if rate:
            frontend = float(rate[0]["value"][1])
        append_jsonl(
            self.data / "lab.jsonl",
            {"at": iso(at), "deployments": counts, "frontend_span_rate": frontend},
        )
        if frontend == 0.0:
            self._zero_rate_since = self._zero_rate_since or at
            if (at - self._zero_rate_since).total_seconds() >= COLLECTOR_STALL_S:
                proc = self.fault("collector-restart")
                self.event(
                    "env",
                    action="collector_restart",
                    exit=proc.returncode,
                    reason="frontend span rate 0 for 10 min",
                    since=iso(self._zero_rate_since),
                )
                self._zero_rate_since = None
        else:
            self._zero_rate_since = None

    def resource_tick(self) -> None:
        append_jsonl(
            self.data / f"resources-{self.config.host_label}.jsonl",
            resource_sample(self.config.host_label, self.now()),
        )

    def clock_tick(self) -> None:
        append_jsonl(self.data / "clock.jsonl", clock_sample(self, self.now))

    def export_tick(self, end: datetime | None = None) -> None:
        end = end or self.now()
        start = max(self.t0(), end - timedelta(seconds=EXPORT_INTERVAL_S + 600))
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
            try:
                tick()
            except Exception as exc:  # noqa: BLE001 - one loop must not stop the rest
                self.event(
                    "error", loop=name, error=f"{type(exc).__name__}: {exc}"[:300]
                )
            self.stop.wait(interval)

    def run_until_end(self, interruptions: Callable[[], float]) -> None:
        t0 = self.t0()
        self.clock_tick()
        loops = [
            ("plan", 5.0, self.plan_tick),
            ("closer", CLOSER_INTERVAL_S, self.closer_tick),
            ("probe", PROBE_INTERVAL_S, self.probe_tick),
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
            if now >= soak_end(t0, interruptions()):
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
        self.event("end", t0=iso(t0), interruption_s=interruptions())

    def loop_once(self, name: str, tick: Callable[[], None]) -> None:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001
            self.event("error", loop=name, error=f"{type(exc).__name__}: {exc}"[:300])


# -- samplers (also used on the rental host) ----------------------------------


def deployment_pods(series: Mapping[str, list[Any] | None]) -> dict[str, int] | None:
    """Pods with a container per Deployment, joined in Python over the three
    KSM series (pod -> ReplicaSet -> Deployment). ``None`` when a query
    failed. Stale (zombie) series are not filtered: README "limits"."""
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


def resource_sample(host: str, now: datetime) -> dict[str, Any]:
    """S-R10: memory, load, disk and ``docker stats`` of one host."""
    mem = _meminfo()
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        load1 = None
    disk = shutil.disk_usage("/")
    proc = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=60)
    containers = []
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        containers.append(
            {
                "name": row.get("Name"),
                "cpu_pct": _percent(row.get("CPUPerc")),
                "mem_bytes": _bytes(str(row.get("MemUsage", "")).split("/")[0]),
            }
        )
    return {
        "at": iso(now),
        "host": host,
        "mem_available_kib": mem.get("MemAvailable"),
        "swap_free_kib": mem.get("SwapFree"),
        "load1": load1,
        "disk_free_bytes": disk.free,
        "docker_exit": proc.returncode,
        "containers": containers,
    }


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
    """S-R5: local NTP state and the rental host's clock over the fault key
    (``fault.sh clock`` prints epoch seconds), offset = remote - midpoint."""
    local = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
    chrony = run(["chronyc", "-n", "tracking"], timeout=10)
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
        "at": iso(before),
        "local_ntp_synchronized": local.stdout.strip() or None,
        "local_chrony": chrony.stdout[-600:] or None,
        "remote_exit": proc.returncode,
        "remote_output": proc.stdout[-800:],
        "rtt_s": round(after.timestamp() - before.timestamp(), 3),
        "offset_s": None if remote_epoch is None else round(remote_epoch - midpoint, 3),
    }
