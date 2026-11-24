"""Round driver of the ITBench live SRE track (#564, PR 1).

Track 「ITBench 现场版 SRE（自托管，自测，不与官方榜单并列）」 (A564-10),
separate from ITBench-Lite (R8-1): own rounds, evidence and scores. One trial
of one scenario, end to end, on the rental host's ``kind-environment``
cluster (upstream ``clusters/kind``)::

    ports free -> ITBench ``deploy environment --scenario N`` (fresh environment)
    -> app Deployments available -> OpsPilot's read-only identity (the kind
    lab RBAC, get/list, no Secrets) + can-i probe (fails closed) + fresh token
    -> port-forwards (Prometheus pod, Jaeger query) -> pre-fault alert set and
    object lists -> ITBench ``inject faults --scenario N``
    -> first new firing alert (A564-1 fallback, ``alerts.py``)
    -> fresh PG, target entry (A564-2), web, worker -> webhook v4 delivery
    -> Run terminal (or deadline) -> freeze -> object lists again -> answer
    conversion (Lite ``answer.convert``) -> stop the product and forwards
    -> ITBench ``remove faults`` -> ``undeploy environment``
    -> judging process (official evaluator, Z.ai judge; diagnosis metrics)

Answer isolation: this process never opens anything under the ITBench
scenario library and never reads the CLI logs it writes (``upstream.py``);
the ground-truth root goes to the judging process as a path, whose output is
discarded. Credentials: the admin kubeconfig reaches ``kubectl`` and the
ITBench CLI only; DeepSeek/LangSmith keys reach the worker only; the judge
key is inherited by the judging process from this shell only.

Usage (repository root, on the rental host, ``set -a; . <env file>; set +a``
first so the judging process inherits ``ZAI_API_KEY`` and
``ITBENCH_EVALUATIONS_DIR``)::

    .venv/bin/python -m scripts.itbench_live.driver preflight --itbench <checkout> \\
        --kubeconfig <admin kubeconfig>
    .venv/bin/python -m scripts.itbench_live.driver round --round <label> \\
        --itbench <checkout> --kubeconfig <f> --env-file <private file> \\
        --scenarios 20,23 --trials 1
    .venv/bin/python -m scripts.itbench_live.driver judge --round <label> --itbench <checkout>
    .venv/bin/python -m scripts.itbench_live.driver scores --round <label>

Evidence ``docs/evidence/itbench-live-<round>/S<id>/trial-<n>/summary.json``
and ``scores.json``; everything else under ``tmp/itbench-live/<round>/``.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opspilot.web.auth import token_digest
from scripts import lab_evidence, run_freeze
from scripts.itbench_lite import answer, official_eval, services, traces
from scripts.itbench_lite.alerts import INTAKE_PATH
from scripts.itbench_lite.driver import (
    HEALTH_PROFILE_ID,
    TARGET_ID,
    Cleanup,
    DeterministicError,
    DriverError,
    EntryPlan,
    RoundAbort,
    _pg_start,
    _pg_stop,
    _plan_for,
    convert_run,
    deliver,
    judge_env,
    run_state,
)
from scripts.itbench_lite.services import ServiceError, Started
from scripts.itbench_live import alerts, upstream, vocabulary
from scripts.itbench_live.upstream import (
    APP_NAMESPACE,
    OBSERVABILITY_NAMESPACE,
    UpstreamError,
)
from scripts.sregym_eval import identity
from scripts.sregym_eval.boundary import BoundaryError, can_i_verdict, check_product_env
from scripts.sregym_eval.identity import IdentityError, K8sTarget

ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = ROOT / "tmp" / "itbench-live"
EVIDENCE_ROOT = ROOT / "docs" / "evidence"
EVIDENCE_PREFIX = "itbench-live-"
SUMMARY_SCHEMA = "itbench-live-trial-1"
EVENT_ACTOR = "alertmanager"
#: Upstream ``documentation/getting-started/scenarios.md`` (port-forward
#: commands): Prometheus listens on the pod's loopback only (``listenLocal``),
#: Jaeger's query port is the ``jaeger`` collector's ``query-http``.
PROMETHEUS_FORWARD = ("pod/prometheus-kube-prometheus-stack-prometheus-0", 9090)
JAEGER_FORWARD = ("svc/jaeger-collector", 16686)
JAEGER_BASES = ("", "/jaeger")
APP_READY_TIMEOUT_S = 900
ALERT_WAIT = timedelta(minutes=20)
TERMINAL_WAIT = timedelta(seconds=7200 + 600)  # RUN_WALL + 10 min, as Lite L-D10
POLL_S = 15.0
RUN_POLL_S = 5.0
JUDGE_TRIES = 3
MODEL_KEYS = ("DEEPSEEK_API_KEY",)
LANGSMITH_KEYS = ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID")
_ROUND = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
#: A564-7: ITBench gives no injection timestamp; the end of the inject
#: command is the fault-start proxy, disclosed in every summary.
FAULT_START_PROXY = "INJECT_COMMAND_END"
#: A564-1: Alertmanager is not deployed upstream; the harness forwards.
ALERT_PATH = "PROMETHEUS_ALERTS_FORWARDED_AS_WEBHOOK_V4"


@dataclass(frozen=True)
class Ports:
    pg: int = 29610
    web: int = 29680
    prometheus: int = 29690
    jaeger: int = 29686

    def all(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class Config:
    round: str
    itbench: Path
    kubeconfig: Path
    env_file: Path | None = None
    trace: str = "lab"
    judge: bool = True
    allow_dirty: bool = False
    allow_other_clusters: bool = False
    expected_context: str = upstream.EXPECTED_CONTEXT
    ports: Ports = field(default_factory=Ports)
    tmp_root: Path = TMP_ROOT
    evidence_root: Path = EVIDENCE_ROOT
    alert_wait: timedelta = ALERT_WAIT
    terminal_wait: timedelta = TERMINAL_WAIT
    poll_seconds: float = POLL_S
    run_poll_seconds: float = RUN_POLL_S
    worker_settle_seconds: float = 5.0
    git: dict[str, Any] = field(default_factory=dict)
    secrets_env: dict[str, str] = field(default_factory=dict)

    @property
    def round_dir(self) -> Path:
        return self.tmp_root / self.round

    @property
    def evidence_dir(self) -> Path:
        return self.evidence_root / f"{EVIDENCE_PREFIX}{self.round}"

    @property
    def project_name(self) -> str | None:
        if self.trace != "lab":
            return None
        return lab_evidence.lab_project(f"itbench-live-{self.round}")

    @property
    def experiment(self) -> str:
        return f"itbench-live-{self.round}"

    @property
    def ground_truth(self) -> Path:
        return upstream.ground_truth_root(self.itbench)

    def trial_dir(self, scenario: str, trial: int) -> Path:
        return self.round_dir / f"S{scenario}" / f"trial-{trial}"

    def evidence_trial(self, scenario: str, trial: int) -> Path:
        return self.evidence_dir / f"S{scenario}" / f"trial-{trial}"


# -- host --------------------------------------------------------------------------


def _kubectl(
    argv: Sequence[str], env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv), env=dict(env), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(list(argv), 127, "", "")


def start_port_forward(
    namespace: str, resource: str, local: int, remote: int,
    env: Mapping[str, str], log_path: Path,
) -> Started:  # fmt: skip
    """``kubectl port-forward -n NS <resource> L:R`` with the admin kubeconfig
    (facility infrastructure, as SREGym's forwards); ready when L answers."""
    argv = ["kubectl", "port-forward", "-n", namespace, resource, f"{local}:{remote}"]
    try:
        proc = services.spawn(argv, env, log_path)
    except OSError as error:
        raise ServiceError("PORT_FORWARD_FAILED", type(error).__name__) from None
    started = Started(
        f"port-forward:{local}",
        lambda: services.stop_process(proc, 5),
        alive=lambda: proc.poll() is None,
    )
    try:
        services.wait_tcp(local, timeout=30, alive=started.alive)
    except ServiceError as error:
        started.stop()
        raise ServiceError("PORT_FORWARD_FAILED", error.code) from None
    started.info = {"namespace": namespace, "resource": resource, "pid": proc.pid}
    return started


def get_json(url: str) -> tuple[int, Any]:
    """GET a loopback URL; ``(status, parsed body or None)``; no redirects."""
    import urllib.error
    import urllib.request

    traces.require_loopback(url)
    request = urllib.request.Request(url, method="GET")
    try:
        with traces._OPENER.open(request, timeout=traces.TIMEOUT_SECONDS) as r:
            status, raw = int(r.status), r.read()
    except urllib.error.HTTPError as error:
        status, raw = int(error.code), error.read()
    except OSError:
        return 0, None
    try:
        return status, json.loads(raw.decode() or "null")
    except ValueError:
        return status, None


def run_judge(job: Mapping[str, Any]) -> int:
    """Start the judging process; its output is discarded (answer-derived)."""
    argv = [
        sys.executable, "-m", "scripts.itbench_live.judge_trial",
        "--ground-truth", str(job["ground_truth"]),
        "--scenario", str(job["scenario"]),
        "--outputs", str(job["outputs"]),
        "--trial", str(job["trial"]),
        "--work-dir", str(job["work_dir"]),
        "--out", str(job["out"]),
    ] + (["--judge"] if job["judge"] else [])  # fmt: skip
    return subprocess.run(
        argv, cwd=ROOT, env=judge_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode  # fmt: skip


@dataclass
class Host:
    """Everything that touches the machine; tests replace any of these."""

    run: Callable[..., Any] = subprocess.run
    kubectl_fn: Callable[[Sequence[str], Mapping[str, str], float], Any] = _kubectl
    port_forward: Callable[..., Started] = start_port_forward
    get_json: Callable[[str], tuple[int, Any]] = get_json
    start_pg: Callable[[Path, int], Any] = _pg_start
    stop_pg: Callable[[Any], None] = _pg_stop
    start_web: Callable[[Mapping[str, str], int, Path], Started] = services.start_web
    start_worker: Callable[..., Started] = services.start_worker
    deliver: Callable[[str, str, Mapping[str, Any]], tuple[int, Any]] = deliver
    run_state: Callable[[str, str], str | None] = run_state
    collect: Callable[[str, str], tuple[dict[str, Any], dict[str, Any]]] = (
        run_freeze.collect
    )
    trace_roots: Callable[[str | None, str, Mapping[str, str]], dict[str, Any]] = (
        lambda project, run_id, env: run_freeze.trace_roots(
            project, run_id, client_factory=lambda: lab_evidence.langsmith_client(env)
        )
    )
    judge: Callable[[Mapping[str, Any]], int] = run_judge
    port_in_use: Callable[[int], bool] = services.port_in_use
    memory_reading: Callable[[], dict[str, Any] | None] = services.memory_reading
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def kubectl(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> Any:
        """``identity.KubectlHost``."""
        return self.kubectl_fn(argv, env, timeout)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    partial.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# -- preflight ---------------------------------------------------------------------


def check_context(host: Host, kubeconfig: Path, expected: str) -> None:
    """The admin kubeconfig is the ITBench kind cluster's, served on loopback."""
    done = host.kubectl(
        ["kubectl", "config", "current-context"],
        identity.kubectl_env(kubeconfig), identity.KUBECTL_TIMEOUT_S,
    )  # fmt: skip
    if done.returncode != 0 or (done.stdout or "").strip() != expected:
        raise IdentityError("WRONG_CLUSTER")
    server, _ = identity._cluster(host, kubeconfig)
    if not identity._loopback_server(server):
        raise IdentityError("KUBECONFIG_SERVER_UNEXPECTED")


def other_kind_clusters(host: Host, expected_context: str) -> list[str]:
    """Kind clusters other than ours: one experiment per host (§5 item 7)."""
    done = host.kubectl(["kind", "get", "clusters"], services.base_env(), 60.0)
    if done.returncode != 0:
        return ["KIND_UNAVAILABLE"]
    ours = expected_context.removeprefix("kind-")
    return [c for c in (done.stdout or "").split() if c and c != ours]


def preflight(cfg: Config, host: Host) -> dict[str, Any]:
    """Read-only checks before a round; raises ``RoundAbort`` on the first miss."""
    out: dict[str, Any] = {}
    try:
        out["itbench_commit"] = upstream.check_pinned(cfg.itbench, host.run)
        out["scenarios"] = upstream.scenario_ids(cfg.itbench)
    except UpstreamError as error:
        raise RoundAbort(error.code, str(error)) from None
    try:
        check_context(host, cfg.kubeconfig, cfg.expected_context)
    except IdentityError as error:
        raise RoundAbort(error.code) from None
    others = other_kind_clusters(host, cfg.expected_context)
    out["other_kind_clusters"] = others
    if others and not cfg.allow_other_clusters:
        raise RoundAbort("OTHER_KIND_CLUSTERS", ",".join(others))
    busy = [n for n, p in cfg.ports.all().items() if host.port_in_use(p)]
    if busy:
        raise RoundAbort("PORT_BUSY", ",".join(busy))
    if cfg.judge:
        try:
            out["evaluator_commit"] = official_eval.check_pinned(
                official_eval.evaluator_dir()
            )
        except Exception as error:  # noqa: BLE001 - ConfigError and file errors
            raise RoundAbort("EVALUATOR_NOT_READY", type(error).__name__) from None
    out["memory"] = host.memory_reading()
    return out


# -- product environments ------------------------------------------------------------


def tool_env(cfg: Config, jaeger_base: str) -> dict[str, str]:
    return {
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_OTEL_PROMETHEUS_URL": f"http://127.0.0.1:{cfg.ports.prometheus}",
        "OPSPILOT_OTEL_JAEGER_URL": f"http://127.0.0.1:{cfg.ports.jaeger}{jaeger_base}",
        "OPSPILOT_HEALTH_BIND": "",
    }


def web_env(
    cfg: Config, pg: Any, identities: Path, token: str, jaeger_base: str
) -> dict[str, str]:
    return (
        services.base_env()
        | tool_env(cfg, jaeger_base)
        | {
            "OPSPILOT_WEB_DSN": pg.web_dsn,
            "OPSPILOT_EVENT_TOKENS": f"{token_digest(token)}={EVENT_ACTOR}",
            "OPSPILOT_AUTH_REVISION": "itbench-live",
            "OPSPILOT_BIND": f"127.0.0.1:{cfg.ports.web}",
            "OPSPILOT_SELF_HEAL_INTERVAL_SECONDS": "0",
            "OPSPILOT_TARGET_IDENTITIES": str(identities),
        }
    )


def worker_env(
    cfg: Config, pg: Any, target: K8sTarget, jaeger_base: str
) -> dict[str, str]:
    env = (
        services.base_env()
        | tool_env(cfg, jaeger_base)
        | {
            "OPSPILOT_WORKER_DSN": pg.runtime_dsn,
            "OPSPILOT_K8S_API_URL": target.api_url,
            "OPSPILOT_K8S_CA_FILE": target.ca_file,
            "OPSPILOT_K8S_TOKEN": target.token,
            "OPSPILOT_K8S_NAMESPACE": APP_NAMESPACE,
            "OPSPILOT_TRACE": cfg.trace,
            "OPSPILOT_WORKER_CAPACITY": "1",
            **{k: v for k, v in cfg.secrets_env.items() if k in MODEL_KEYS},
        }
    )
    if cfg.trace == "lab":
        env.update({k: v for k, v in cfg.secrets_env.items() if k in LANGSMITH_KEYS})
        if cfg.project_name:
            env["LANGSMITH_PROJECT"] = cfg.project_name
    return env


def identities_entry(plan: EntryPlan, cluster_uid: str) -> dict[str, Any]:
    """A564-2 (Lite L-D11): kube-system uid, the app namespace, the workload
    from the entry alert's labels (``target_fallback`` when it had none)."""
    return {
        TARGET_ID: {
            "integration_id": TARGET_ID,
            "cluster_uid": cluster_uid,
            "namespace": APP_NAMESPACE,
            "workload": plan.workload,
            "health_profile_id": HEALTH_PROFILE_ID,
            "match": {"version": 1, "labels": dict(plan.match_labels)},
        }
    }


def forbidden_values(cfg: Config) -> list[str]:
    """Values no product process may hold: the admin kubeconfig (path and
    its credentials) and the ITBench checkout's path."""
    values = [str(cfg.kubeconfig), str(cfg.itbench)]
    try:
        text = cfg.kubeconfig.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if (
            key in ("token", "client-key-data", "client-certificate-data")
            and value.strip()
        ):
            values.append(value.strip().strip("\"'"))
    return [v for v in values if len(v) >= 8]


def check_envs(cfg: Config, wenv: Mapping[str, str], kenv: Mapping[str, str]) -> None:
    forbidden = forbidden_values(cfg)
    check_product_env(wenv, role="web", forbidden_values=forbidden)
    check_product_env(kenv, role="worker", forbidden_values=forbidden)
    leaked = upstream.leaks_upstream_paths(
        [*wenv.values(), *kenv.values()], cfg.itbench
    )
    if leaked:
        raise BoundaryError("ENV_ISOLATION:UPSTREAM_PATH")


# -- one trial ---------------------------------------------------------------------


class TrialStop(DriverError):
    """The trial ends here with a recorded status (no Run or no score)."""


class Trial:
    def __init__(self, cfg: Config, host: Host, scenario: str, trial: int) -> None:
        self.cfg, self.h, self.scenario, self.trial = cfg, host, scenario, trial
        self.dir = cfg.trial_dir(scenario, trial)
        self.cleanup = Cleanup()
        self.block: dict[str, Any] = {
            "round": cfg.round,
            "scenario": scenario,
            "trial": trial,
            "track": "ITBench 现场版 SRE（自托管，自测，不与官方榜单并列）",
            "itbench_commit": upstream.ITBENCH_COMMIT,
            "git": cfg.git,
            "ports": cfg.ports.all(),
            "alert_path": ALERT_PATH,
            "fault_start_proxy": FAULT_START_PROXY,
            "disclosures": [],
            "steps": {},
            "times": {},
        }

    @property
    def admin_env(self) -> dict[str, str]:
        return identity.kubectl_env(self.cfg.kubeconfig)

    def _mark(self, name: str) -> datetime:
        moment = self.h.now()
        self.block["times"][name] = _iso(moment)
        return moment

    def _cli(self, command: str, *, scenario: str | None = None) -> None:
        result = upstream.run_cli(
            self.cfg.itbench, command, self.cfg.kubeconfig,
            self.dir / f"upstream-{command}.log",
            scenario=scenario, run=self.h.run, clock=self.h.monotonic,
        )  # fmt: skip
        self.block["steps"][command] = {
            "returncode": result.returncode,
            "seconds": result.seconds,
        }
        upstream.require_ok(result)

    def _kubectl_ok(
        self, *args: str, timeout: float = 120.0, env: Mapping[str, str] | None = None
    ) -> str:
        done = self.h.kubectl(["kubectl", *args], env or self.admin_env, timeout)
        if done.returncode != 0:
            raise TrialStop("KUBECTL_FAILED", args[0] if args else "")
        return str(done.stdout or "")

    def _lists(self, kubeconfig: Path) -> dict[str, Any]:
        """Object lists with the read-only identity (``vocabulary.LIST_PLURALS``)."""
        env = identity.kubectl_env(kubeconfig)
        out: dict[str, Any] = {}
        for plural in vocabulary.LIST_PLURALS:
            done = self.h.kubectl(
                ["kubectl", "get", plural, "-A", "-o", "json"], env, 120.0
            )
            if done.returncode != 0:
                self.block["disclosures"].append(f"LIST_FAILED:{plural}")
                continue
            try:
                out[plural] = json.loads(done.stdout or "{}")
            except ValueError:
                self.block["disclosures"].append(f"LIST_UNREADABLE:{plural}")
        return out

    def _alerts(self) -> list[dict[str, Any]]:
        status, body = self.h.get_json(
            f"http://127.0.0.1:{self.cfg.ports.prometheus}{alerts.ALERTS_PATH}"
        )
        if status != 200:
            raise alerts.AlertsUnreadable(f"status {status}")
        return alerts.parse_alerts(body)

    def _jaeger_base(self) -> str:
        for base in JAEGER_BASES:
            status, body = self.h.get_json(
                f"http://127.0.0.1:{self.cfg.ports.jaeger}{base}/api/services"
            )
            if status == 200 and isinstance(body, Mapping):
                self.block["jaeger_base"] = base
                return base
        self.block["disclosures"].append("JAEGER_QUERY_UNANSWERED")
        return JAEGER_BASES[0]

    def run(self) -> dict[str, Any]:
        """Every path writes a summary; cleanup failures stop the round."""
        self.dir.mkdir(parents=True, exist_ok=True)
        status, reason = "completed", None
        try:
            self._run()
        except TrialStop as stop:
            status, reason = "stopped", stop.code
        except (UpstreamError, IdentityError, BoundaryError, ServiceError) as error:
            status, reason = "infra_failed", error.code
        except Exception as error:  # noqa: BLE001 - recorded, round decides
            status, reason = "crashed", type(error).__name__
        finally:
            errors = self.cleanup.close()
            self.block["cleanup"] = {"order": self.cleanup.order, "errors": errors}
            self._mark("cleaned_up")
        self.block["status"], self.block["reason"] = status, reason
        if "summary_path" not in self.block:
            self._status_summary(status, reason)
        _write_json(self.dir / "trial.json", self.block)
        if errors:
            raise RoundAbort("CLEANUP_FAILED", ",".join(errors))
        return self.block

    def _status_summary(self, status: str, reason: str | None) -> None:
        path = self.cfg.evidence_trial(self.scenario, self.trial) / "summary.json"
        _write_json(path, {
            "schema": SUMMARY_SCHEMA, "status": status, "reason": reason,
            "frozen_at": _iso(self.h.now()), "itbench_live": self.block,
        })  # fmt: skip
        self.block["summary_path"] = str(path)

    def _run(self) -> None:
        cfg, h = self.cfg, self.h
        self.block["memory_before"] = h.memory_reading()
        busy = [n for n, p in cfg.ports.all().items() if h.port_in_use(p)]
        if busy:
            raise TrialStop("PORT_BUSY", ",".join(busy))

        # 1. a fresh environment (§5 item 3); undeploy is pushed first so it
        # runs last, also after a failed deploy.
        self._mark("deploy_started")
        self.cleanup.push("undeploy", lambda: self._cli("undeploy"))
        self._cli("deploy", scenario=self.scenario)
        self._kubectl_ok(
            "-n", APP_NAMESPACE, "wait", "--for=condition=Available", "deployment", "--all",
            f"--timeout={APP_READY_TIMEOUT_S}s", timeout=APP_READY_TIMEOUT_S + 60,
        )  # fmt: skip
        self._mark("environment_ready")

        # 2. OpsPilot's own read-only identity (N4/R5); no ITBench agent kubeconfig.
        identity.apply_identity(h, cfg.kubeconfig)
        answers = identity.probe_can_i(h, cfg.kubeconfig)
        mismatches = can_i_verdict(identity.answers_by_check(answers))
        self.block["can_i"] = {"answers": answers, "mismatches": mismatches}
        if mismatches:
            raise TrialStop("CAN_I_MISMATCH", ",".join(mismatches))
        target = identity.issue_token(h, cfg.kubeconfig, self.dir / "identity")
        readonly = self.dir / "identity" / "readonly-kubeconfig.yaml"
        cluster_uid = (
            self._kubectl_ok(
                "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"
            ).strip()
            or "itbench-live"
        )

        # 3. observability forwards (facility side, admin kubeconfig).
        env = self.admin_env
        for name, (resource, remote), local in (
            ("prometheus", PROMETHEUS_FORWARD, cfg.ports.prometheus),
            ("jaeger", JAEGER_FORWARD, cfg.ports.jaeger),
        ):
            started = h.port_forward(
                OBSERVABILITY_NAMESPACE, resource, local, remote, env,
                self.dir / f"port-forward-{name}.log",
            )  # fmt: skip
            self.cleanup.push(f"port-forward:{name}", started.stop)
        jaeger_base = self._jaeger_base()

        # 4. pre-fault state.
        try:
            excluded = alerts.prefault_keys(self._alerts())
        except alerts.AlertsUnreadable as error:
            raise TrialStop("PROMETHEUS_ALERTS_UNREADABLE", str(error)) from None
        self.block["prefault_alert_count"] = len(excluded)
        lists_before = self._lists(readonly)

        # 5. the fault (ITBench's own tooling); removal pushed before the call.
        self._mark("inject_started")
        self.cleanup.push("remove-faults", lambda: self._cli("remove"))
        self._cli("inject", scenario=self.scenario)
        injected = self._mark("inject_done")

        # 6. the entry alert.
        entry = self._wait_entry(excluded)
        if entry is None:
            raise TrialStop("NO_ENTRY_ALERT")
        self.block["entry_alert"] = alerts.summary_of(entry)
        try:
            plan = _plan_for(entry)
        except DeterministicError as error:
            raise TrialStop(error.code) from None
        self.block["target"] = {**asdict(plan), "cluster_uid": cluster_uid}
        if plan.target_fallback:
            self.block["disclosures"].append("TARGET_FALLBACK_ALERTNAME_MATCH")

        # 7. the product.
        run_id, delivered = self._product(entry, plan, cluster_uid, target, jaeger_base)

        # 8. conversion inputs: lists after the Run, Jaeger services.
        lists_after = self._lists(readonly)
        _, body = h.get_json(
            f"http://127.0.0.1:{cfg.ports.jaeger}{jaeger_base}/api/services"
        )
        services_seen = vocabulary.jaeger_service_names(body)
        vocab = vocabulary.build([lists_before, lists_after], services_seen)
        kinds = vocabulary.build_kinds([lists_before, lists_after])
        (self.dir / "vocabulary.txt").write_text("".join(f"{n}\n" for n in vocab))
        self.block["vocabulary_size"] = len(vocab)
        self._freeze(run_id, vocab, kinds, injected, delivered)

    def _wait_entry(self, excluded: frozenset[str]) -> dict[str, Any] | None:
        deadline = self.h.now() + self.cfg.alert_wait
        polls = 0
        while True:
            polls += 1
            try:
                found = alerts.entry_alert(self._alerts(), excluded)
            except alerts.AlertsUnreadable:
                found = None
                self.block["disclosures"].append("ALERTS_POLL_UNREADABLE")
            if found is not None:
                self._mark("entry_alert_seen")
                self.block["alert_polls"] = polls
                return found
            if self.h.now() >= deadline:
                self.block["alert_polls"] = polls
                return None
            self.h.sleep(self.cfg.poll_seconds)

    def _product(
        self, entry: Mapping[str, Any], plan: EntryPlan, cluster_uid: str,
        target: K8sTarget, jaeger_base: str,
    ) -> tuple[str, datetime]:  # fmt: skip
        cfg, h = self.cfg, self.h
        try:
            pg = h.start_pg(self.dir / "pg", cfg.ports.pg)
        except Exception as error:  # noqa: BLE001 - LabError codes
            raise TrialStop(getattr(error, "code", "PG_START_FAILED")) from None
        self.cleanup.push("pg", lambda: h.stop_pg(pg))
        identities = self.dir / "target_identities.json"
        _write_json(identities, identities_entry(plan, cluster_uid))
        token = _secrets.token_urlsafe(24)
        wenv = web_env(cfg, pg, identities, token, jaeger_base)
        kenv = worker_env(cfg, pg, target, jaeger_base)
        check_envs(cfg, wenv, kenv)
        web = h.start_web(wenv, cfg.ports.web, self.dir / "web.log")
        self.cleanup.push("web", web.stop)
        worker = h.start_worker(
            kenv, self.dir / "worker.log", settle_seconds=cfg.worker_settle_seconds
        )
        self.cleanup.push("worker", worker.stop)

        body = alerts.webhook(entry, plan.match_labels)
        delivered = self._mark("delivered")
        try:
            status, reply = h.deliver(
                f"http://127.0.0.1:{cfg.ports.web}{INTAKE_PATH}", token, body
            )
        except OSError as error:
            raise TrialStop("INTAKE_UNREACHABLE", type(error).__name__) from None
        result = (
            (reply or {}).get("results", [{}])[0] if isinstance(reply, dict) else {}
        )
        self.block["intake"] = {
            "status": status, "outcome": result.get("outcome"), "reason": result.get("reason"),
        }  # fmt: skip
        run_id = result.get("run_id")
        if status != 200 or result.get("outcome") != "created" or not run_id:
            raise TrialStop("INTAKE_REFUSED", f"{status} {result.get('outcome')}")

        began = h.monotonic()
        deadline = h.now() + cfg.terminal_wait
        state: str | None = None
        while True:
            state = h.run_state(pg.owner_dsn, run_id)
            if state not in (None, "queued", "running"):
                break
            if not worker.alive():
                raise TrialStop("WORKER_EXITED")
            if h.now() >= deadline:
                break
            h.sleep(cfg.run_poll_seconds)
        self._mark("run_terminal_seen")
        self.block["run_wait"] = {
            "terminal_state": state,
            "timed_out": state in (None, "queued", "running"),
            "seconds": round(h.monotonic() - began, 3),
        }
        worker.stop()  # flushes its spans before the read-back
        self._pg, self._run_id = pg, run_id
        return str(run_id), delivered

    def _freeze(
        self, run_id: str, vocab: Sequence[str], kinds: Mapping[str, Any],
        injected: datetime, delivered: datetime,
    ) -> None:  # fmt: skip
        cfg, h = self.cfg, self.h
        run_block, ledger = h.collect(self._pg.owner_dsn, run_id)
        conversion, reason = convert_run(run_block, list(vocab), kinds)
        self.block["conversion"] = {**conversion.meta, "reason": reason}
        terminal = datetime.fromisoformat(str(self.block["times"]["run_terminal_seen"]))
        self.block["diagnosis"] = diagnosis_times(injected, delivered, terminal)
        outputs = self.dir / "outputs"
        if outputs.exists():
            shutil.rmtree(outputs)
        answer.write_trial(outputs, self.scenario, self.trial, conversion)
        trace = h.trace_roots(cfg.project_name, run_id, cfg.secrets_env)
        _, path = lab_evidence.freeze(
            ledger,
            experiment=cfg.experiment,
            run_id=run_id,
            evidence_dir=cfg.evidence_trial(self.scenario, self.trial),
            summary={
                "status": "completed", "run": run_block, "trace": trace,
                "itbench_live": self.block,
            },
        )  # fmt: skip
        self.block["summary_path"] = str(path)


def diagnosis_times(
    injected: datetime, delivered: datetime, terminal: datetime
) -> dict[str, float]:
    """Diagnosis time from the fault-start proxy and from alert delivery to the
    Run's terminal state as the driver saw it (A564-4, A564-7)."""
    return {
        "from_fault_start_proxy_seconds": round(
            (terminal - injected).total_seconds(), 3
        ),
        "from_delivery_seconds": round((terminal - delivered).total_seconds(), 3),
    }


# -- judging and scores --------------------------------------------------------------


def _numeric(values: Any) -> dict[str, float]:
    if not isinstance(values, Mapping):
        return {}
    return {
        str(k): float(v) for k, v in values.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }  # fmt: skip


def judge_one(cfg: Config, host: Host, scenario: str, trial: int) -> dict[str, Any]:
    """The judging process (retried); numeric scores into the evidence."""
    tdir = cfg.trial_dir(scenario, trial)
    out = tdir / "scores.json"
    result: dict[str, Any] = {"status": "failed", "tries": JUDGE_TRIES}
    for attempt in range(1, JUDGE_TRIES + 1):
        work = tdir / f"judge-{attempt}"
        if work.exists():
            shutil.rmtree(work)
        status = host.judge({
            "ground_truth": cfg.ground_truth, "scenario": scenario,
            "outputs": tdir / "outputs", "trial": trial, "work_dir": work,
            "out": out, "judge": cfg.judge,
        })  # fmt: skip
        data = _read_json(out)
        if status == 0 and isinstance(data, Mapping) and _numeric(data.get("det")):
            result = {
                "status": "ok",
                "judged": bool(data.get("judged")),
                "report_present": bool(data.get("report_present")),
                "det": _numeric(data.get("det")),
                "official": _numeric(data.get("official"))
                if data.get("official")
                else None,
                "labels": data.get("labels"),
            }
            break
        result["exit"] = status
    _write_json(cfg.evidence_trial(scenario, trial) / "scores.json", result)
    return result


#: Diagnosis metrics reported (A564-4); remediation metrics are not applicable.
REPORTED_OFFICIAL = (
    "root_cause_entity_f1",
    "root_cause_entity_pass",
    "propagation_chain",
    "fault_localization_component_identification",
    "root_cause_reasoning",
)
REPORTED_DET = ("det_any_hit", "det_f1")


def aggregate(evidence_dir: Path) -> dict[str, Any]:
    """Per-trial rows and per-metric means over the trials that have them;
    every trial is listed with its status (照实 x/n)."""
    rows: list[dict[str, Any]] = []
    for summary_path in sorted(evidence_dir.glob("S*/trial-*/summary.json")):
        summary = _read_json(summary_path) or {}
        block = summary.get("itbench_live") or {}
        scores = _read_json(summary_path.with_name("scores.json")) or {}
        row: dict[str, Any] = {
            "scenario": block.get("scenario"),
            "trial": block.get("trial"),
            "status": summary.get("status"),
            "reason": summary.get("reason"),
            "diagnosis": block.get("diagnosis"),
            "scored": scores.get("status") == "ok",
        }
        for key in REPORTED_OFFICIAL:
            row[key] = (scores.get("official") or {}).get(key)
        for key in REPORTED_DET:
            row[key] = (scores.get("det") or {}).get(key)
        rows.append(row)
    means: dict[str, Any] = {}
    for key in (*REPORTED_OFFICIAL, *REPORTED_DET):
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        means[key] = {
            "mean": sum(values) / len(values) if values else None,
            "n": len(values),
        }
    return {
        "track": "ITBench 现场版 SRE（自托管，自测，不与官方榜单并列）",
        "trials": len(rows),
        "scored": sum(1 for r in rows if r["scored"]),
        "rows": rows,
        "means": means,
        "not_applicable": "remediation metrics (read-only product)",
    }


# -- round ---------------------------------------------------------------------------


def git_state(allow_dirty: bool) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
        ).stdout

    commit = git("rev-parse", "HEAD").strip()
    changed = [
        line[3:]
        for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
        if not line[3:].startswith(f"docs/evidence/{EVIDENCE_PREFIX}")
    ]
    if changed and not allow_dirty:
        raise RoundAbort("WORKTREE_DIRTY", ",".join(changed[:5]))
    return {"commit": commit, "dirty": bool(changed), "allow_dirty": allow_dirty}


def load_secrets(path: Path | None) -> dict[str, str]:
    from scripts.holmes_evals.driver import load_env_file

    values = load_env_file(path)
    return {k: v for k, v in values.items() if k in (*MODEL_KEYS, *LANGSMITH_KEYS)}


def prepare_round(cfg: Config, host: Host) -> None:
    if not _ROUND.match(cfg.round):
        raise RoundAbort("ROUND_LABEL_INVALID", cfg.round)
    if not cfg.secrets_env.get("DEEPSEEK_API_KEY"):
        raise RoundAbort("DEEPSEEK_API_KEY_MISSING")
    if cfg.trace == "lab":
        if not cfg.secrets_env.get("LANGSMITH_API_KEY"):
            raise RoundAbort("LANGSMITH_API_KEY_MISSING")
        project = cfg.project_name
        assert project is not None
        lab_evidence.ensure_longlived_project(
            lab_evidence.langsmith_client(cfg.secrets_env), project
        )
    cfg.round_dir.mkdir(parents=True, exist_ok=True)
    _write_json(cfg.round_dir / "preflight.json", preflight(cfg, host))


def trial_done(cfg: Config, scenario: str, trial: int) -> bool:
    return (cfg.evidence_trial(scenario, trial) / "scores.json").is_file() or (
        cfg.evidence_trial(scenario, trial) / "summary.json"
    ).is_file()


def run_round(
    cfg: Config, host: Host, scenarios: Sequence[str], trials: Sequence[int]
) -> list[dict[str, Any]]:
    """Scenarios in order, one trial at a time (§5 item 7); done trials are
    skipped; a trial without a Run is recorded and not judged."""
    for scenario in scenarios:
        try:
            upstream.check_scenario(cfg.itbench, scenario)
        except UpstreamError as error:
            raise RoundAbort(error.code, scenario) from None
    prepare_round(cfg, host)
    results = []
    for scenario in scenarios:
        for trial in trials:
            if trial_done(cfg, scenario, trial):
                continue
            block = Trial(cfg, host, scenario, trial).run()
            entry: dict[str, Any] = {
                "scenario": scenario,
                "trial": trial,
                "status": block["status"],
            }
            if block["status"] == "completed":
                entry["scores"] = judge_one(cfg, host, scenario, trial)["status"]
            results.append(entry)
            print(json.dumps(entry), flush=True)
    return results


def resolve_trials(spec: str) -> tuple[int, ...]:
    if re.fullmatch(r"[1-9]", spec):
        return tuple(range(1, int(spec) + 1))
    match = re.fullmatch(r"([1-9])-([1-9])", spec)
    if match and int(match.group(1)) <= int(match.group(2)):
        return tuple(range(int(match.group(1)), int(match.group(2)) + 1))
    raise argparse.ArgumentTypeError("trials: N or A-B (1-9)")


def resolve_scenarios(spec: str) -> tuple[str, ...]:
    items = tuple(s.strip().removeprefix("S") for s in spec.split(",") if s.strip())
    if not items or any(not re.fullmatch(r"[1-9][0-9]{0,3}", s) for s in items):
        raise argparse.ArgumentTypeError("scenarios: comma-separated ids")
    return items


def _config(args: argparse.Namespace) -> Config:
    trace = getattr(args, "trace", "lab")
    return Config(
        round=getattr(args, "round", None) or "preflight",
        itbench=Path(args.itbench).expanduser().resolve(),
        kubeconfig=Path(
            getattr(args, "kubeconfig", None) or "/nonexistent"
        ).expanduser(),
        env_file=Path(args.env_file).expanduser()
        if getattr(args, "env_file", None)
        else None,
        trace=trace,
        judge=not getattr(args, "no_judge", False),
        allow_dirty=getattr(args, "allow_dirty", False),
        allow_other_clusters=getattr(args, "allow_other_clusters", False),
    )


def main(argv: Sequence[str] | None = None, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, cluster: bool = True) -> None:
        p.add_argument("--itbench", required=True, help="pinned ITBench checkout")
        if cluster:
            p.add_argument(
                "--kubeconfig",
                required=True,
                help="admin kubeconfig of kind-environment",
            )
            p.add_argument("--allow-other-clusters", action="store_true")
        p.add_argument("--no-judge", action="store_true")

    p_pre = sub.add_parser("preflight")
    common(p_pre)
    p_round = sub.add_parser("round")
    common(p_round)
    p_round.add_argument("--round", required=True)
    p_round.add_argument("--env-file", required=True)
    p_round.add_argument("--scenarios", required=True, type=resolve_scenarios)
    p_round.add_argument("--trials", default="1", type=resolve_trials)
    p_round.add_argument("--trace", choices=("lab", "off"), default="lab")
    p_round.add_argument("--allow-dirty", action="store_true")
    p_judge = sub.add_parser("judge")
    common(p_judge, cluster=False)
    p_judge.add_argument("--round", required=True)
    p_scores = sub.add_parser("scores")
    p_scores.add_argument("--round", required=True)
    args = parser.parse_args(argv)
    host = host or Host()

    try:
        if args.command == "scores":
            evidence = EVIDENCE_ROOT / f"{EVIDENCE_PREFIX}{args.round}"
            table = aggregate(evidence)
            _write_json(evidence / "scores.json", table)
            print(json.dumps({"trials": table["trials"], "scored": table["scored"]}))
            return 0
        cfg = _config(args)
        if args.command == "preflight":
            print(json.dumps(preflight(cfg, host), default=str))
            return 0
        if args.command == "judge":
            done = []
            for summary in sorted(cfg.evidence_dir.glob("S*/trial-*/summary.json")):
                data = _read_json(summary) or {}
                block = data.get("itbench_live") or {}
                if data.get("status") == "completed":
                    scenario, trial = str(block["scenario"]), int(block["trial"])
                    done.append({"scenario": scenario, "trial": trial,
                                 "scores": judge_one(cfg, host, scenario, trial)["status"]})  # fmt: skip
            print(json.dumps(done))
            return 0
        cfg.git = git_state(cfg.allow_dirty)
        cfg.secrets_env = load_secrets(cfg.env_file)
        run_round(cfg, host, args.scenarios, args.trials)
        return 0
    except RoundAbort as abort:
        print(json.dumps({"abort": abort.code, "detail": str(abort)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
