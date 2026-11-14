"""ITBench-Lite per-scenario replay and evaluation driver (#604).

Chains the #581-#583 components into one attempt per trial (decision table
r6, L-D1 to L-D20, docs/tasks/2026-11-12-tool-expansion-contract.md):

1. the scenario's whitelisted files are copied once into a clean directory
   (names only, answer files are never opened) and re-validated with
   ``load_snapshot`` before every attempt (L-D6);
2. a fresh PostgreSQL (``LocalPgLauncher``), then in parallel: metrics ->
   OpenMetrics -> promtool -> replay Prometheus; replay Jaeger + OTLP traces;
   the fake Kubernetes API server; the web. All bind 127.0.0.1 on fixed,
   configurable ports (L-D3). Then the worker (``otel-demo-ext``);
3. one entry alert is delivered right after loading, after the 2-minute
   gap check (``delivery_gap_ok``, L-D7 as r4 words it); a late attempt is
   void and retried, at most 3 chances per trial (L-D8);
4. the Run is awaited, frozen (``scripts.run_freeze``), its report and
   evidence converted (``answer.convert``) and every started service stopped
   in reverse order, ports checked released (L-D20);
5. a separate judging process gets the answer root and this attempt's outputs
   and writes numeric scores (L-D14); this process never opens an answer.

Usage (repository root, see the rental runbook stage 4)::

    python3 -m scripts.itbench_lite.driver attempt --dataset <root> \\
        --scenario <dir> --trial 1 --round 20261120 --vocabulary <file> \\
        [--env-file <private KEY=value file>] [--no-judge] [--allow-dirty]
    python3 -m scripts.itbench_lite.driver round --dataset <root> \\
        --round 20261120 --vocabulary-dir <dir> [--scenarios 16,23] [--trials 3]
    python3 -m scripts.itbench_lite.driver judge --round 20261120 --ground-truth <root>
    python3 -m scripts.itbench_lite.driver scores --round 20261120 --out <scores.json>

Paths: evidence ``docs/evidence/itbench-lite-sre-<round>/S<id>/trial-<n>/summary.json``;
everything else under ``tmp/itbench-lite/<round>/`` (git-ignored, L-D16).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opspilot.web.auth import token_digest
from scripts import lab_evidence, run_freeze
from scripts.itbench_lite import answer, metrics, services, traces
from scripts.itbench_lite.alerts import (
    INTAKE_PATH,
    EntryTarget,
    NoEntryAlert,
    build_webhook,
    delivery_gap_ok,
    entry_webhook,
    final_alert_file,
    select_entry_alert,
)
from scripts.itbench_lite.coverage import scenario_key
from scripts.itbench_lite.official_eval import (
    DEFAULT_PROVIDER,
    EVALUATOR_DIR_ENV,
    MODEL_OVERRIDE_ENV,
    PROVIDER_ENV,
    PROVIDERS,
)
from scripts.itbench_lite.services import Started
from scripts.itbench_lite.shift import (
    check_not_future,
    compute_offset_seconds,
    from_ns,
    latest_shifted_ns,
    snapshot_end_times,
)
from scripts.itbench_lite.snapshot import (
    ALERTS_DIR,
    METRICS_DIR,
    TSV_FILES,
    AnswerFilePresent,
    Snapshot,
    UnsafeSnapshot,
    _is_answer_name,
    load_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = ROOT / "tmp" / "itbench-lite"
EVIDENCE_ROOT = ROOT / "docs" / "evidence"
TARGET_ID = "m0-otel-20260909"  # the extended profile's one target (L-D11)
NAMESPACE = "otel-demo"
HEALTH_PROFILE_ID = "otel-demo-checkout"  # required by the identity file
CLUSTER_UID_FALLBACK = "itbench-lite"
EVENT_ACTOR = "alertmanager"
FALLBACK_WORKLOAD_LABELS = ("service", "pod", "namespace")
TERMINAL_WAIT = timedelta(seconds=7200 + 600)  # RUN_WALL + 10 min (L-D10)
DEFAULT_LOAD_BUDGET = timedelta(seconds=600)
CHANCES = 3  # L-D8: at most 2 voids/infra failures per trial
JUDGE_TRIES = 3  # L-D9: the judge call is retried at most twice
SUMMARY_SCHEMA = "itbench-lite-trial-1"
_ROUND = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
_PASS_KEYS = ("DEEPSEEK_API_KEY",)
_LANGSMITH_KEYS = (
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_WORKSPACE_ID",
)


@dataclass(frozen=True)
class Ports:
    """L-D3 defaults: clear of the lab's 19090, 16686, 18080, 8080, 55431, 9464."""

    pg: int = 29110
    prometheus: int = 29190
    jaeger_query: int = 29186
    jaeger_otlp: int = 29118
    fake_k8s: int = 29143
    web: int = 29180

    def all(self) -> dict[str, int]:
        return asdict(self)


# -- failures -----------------------------------------------------------------


class DriverError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class VoidAttempt(DriverError):
    """The 2-minute line was crossed; the attempt does not count (L-D7)."""


class InfraFailure(DriverError):
    """A transient infrastructure failure; retried within the trial's chances."""


class DeterministicError(DriverError):
    """Retrying cannot help; the scenario's trials are recorded missing (L-D8)."""


class RoundAbort(DriverError):
    """Configuration error or unsafe state: the whole round stops (L-D9)."""


# -- clean copy (L-D6) ----------------------------------------------------------


def _walk_error(error: OSError) -> None:
    raise DeterministicError("UNSAFE_SNAPSHOT", "source tree cannot be listed")


def _whitelisted_rel(rel: Path) -> bool:
    """Whether a path relative to the scenario root is on the loader's list."""
    parts = rel.parts
    if len(parts) == 1:
        name = parts[0]
        return name in TSV_FILES.values() or (
            name.startswith("alerts") and name.endswith(".json")
        )
    if len(parts) == 2:
        return (parts[0] == METRICS_DIR and parts[1].endswith(".tsv")) or (
            parts[0] == ALERTS_DIR and parts[1].endswith(".json")
        )
    return False


def scan_source(source: Path) -> list[tuple[Path, Path]]:
    """The whole source tree checked by name, fail-closed; the whitelisted
    ``(source file, relative path)`` pairs.

    Lists names only (``os.walk``, not following links); opens no file.
    Refuses a symlinked root, any directory symlink anywhere in the tree and
    any listing error. Answer files are expected in the dataset and are never
    selected; a whitelisted name that is, or points to, an answer name, or
    that points outside a regular file, is refused.
    """
    if source.is_symlink():
        raise DeterministicError("UNSAFE_SNAPSHOT", "scenario root is a symlink")
    if not source.is_dir():
        raise DeterministicError("SCENARIO_NOT_FOUND", str(source.name))
    selected: list[tuple[Path, Path]] = []
    for dirpath, dirnames, filenames in os.walk(source, onerror=_walk_error):
        base = Path(dirpath)
        for name in dirnames:
            if (base / name).is_symlink():
                raise DeterministicError("UNSAFE_SNAPSHOT", "directory symlink")
        for name in filenames:
            path = base / name
            rel = path.relative_to(source)
            if not _whitelisted_rel(rel):
                continue
            if _is_answer_name(name) or _is_answer_name(path.resolve().name):
                raise DeterministicError("ANSWER_FILE_PRESENT", "whitelisted name")
            if not path.is_file():
                raise DeterministicError("UNSAFE_SNAPSHOT", "not a regular file")
            selected.append((path, rel))
    return sorted(selected, key=lambda item: str(item[1]))


def verify_copy(copy: Path) -> None:
    """A reused clean copy holds whitelisted regular files only, no links."""
    for dirpath, dirnames, filenames in os.walk(copy, onerror=_walk_error):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            path = base / name
            if path.is_symlink():
                raise DeterministicError("UNSAFE_SNAPSHOT", "link in the clean copy")
            if _is_answer_name(name):
                raise DeterministicError("ANSWER_FILE_PRESENT", "in the clean copy")
        for name in filenames:
            if not _whitelisted_rel((base / name).relative_to(copy)):
                raise DeterministicError("UNSAFE_SNAPSHOT", "unexpected file in copy")


def copy_whitelisted(source: Path, destination: Path) -> dict[str, Any]:
    """Copy the whitelisted names of ``source`` into ``destination`` once.

    The whole source tree is checked first (``scan_source``); file symlinks
    are followed (their content is copied). The copy is built beside the
    destination and renamed into place, so a half copy is never reused; a
    reused copy is verified again (``verify_copy``). Returns counts.
    """
    if destination.is_symlink():
        # ``is_dir`` is true for a link to an outside directory; never reuse it.
        raise DeterministicError("UNSAFE_SNAPSHOT", "clean copy is a symlink")
    if destination.is_dir():
        verify_copy(destination)
        return {"reused": True}
    selected = scan_source(source)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    size = 0
    for src, rel in selected:
        target = partial / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        size += target.stat().st_size
    verify_copy(partial)
    partial.rename(destination)
    return {"reused": False, "files": len(selected), "bytes": size}


# -- per-scenario preparation ---------------------------------------------------


@dataclass(frozen=True)
class EntryPlan:
    alertname: str
    match_labels: dict[str, str]
    workload: str
    target_fallback: bool


def entry_plan(snapshot: Snapshot) -> EntryPlan:
    """The target entry for the scenario's entry alert (L-D11, lead fallback)."""
    try:
        alert = select_entry_alert(snapshot.read_alert_file(final_alert_file(snapshot)))
    except NoEntryAlert as error:
        raise DeterministicError("NO_ENTRY_ALERT", str(error)) from None
    except (ValueError, TypeError, KeyError) as error:
        # Unparseable file, non-string labels, ...: this scenario's data, not
        # the round's; its trials are recorded missing and the round goes on.
        raise DeterministicError("ENTRY_ALERT_INVALID", str(error)[:200]) from None
    plan = _plan_for(alert)
    try:  # the same conversion the attempt does later, checked up front
        build_webhook(alert, offset_seconds=0, target=EntryTarget(plan.match_labels))
    except (ValueError, TypeError, KeyError) as error:
        raise DeterministicError("ENTRY_ALERT_INVALID", str(error)[:200]) from None
    return plan


def _plan_for(alert: Mapping[str, Any]) -> EntryPlan:
    labels = alert.get("labels") or {}
    name = str(labels.get("alertname") or "")
    service = labels.get("service_name")
    if isinstance(service, str) and service:
        return EntryPlan(name, {"service_name": service}, service, False)
    if not name:
        raise DeterministicError("ENTRY_ALERT_NO_MATCH_LABEL", "no alertname")
    for key in FALLBACK_WORKLOAD_LABELS:
        value = labels.get(key)
        if isinstance(value, str) and value:
            return EntryPlan(name, {"alertname": name}, value, True)
    raise DeterministicError("ENTRY_ALERT_NO_WORKLOAD_LABEL", name)


def cluster_uid(snapshot: Snapshot) -> str:
    """The kube-system namespace uid from the object rows, else a placeholder."""
    path = snapshot.tsv_path("objects")
    found = None
    if path is None:
        return CLUSTER_UID_FALLBACK
    for row in snapshot.iter_tsv(path):
        attrs, body = row.get("LogAttributes") or "", row.get("Body") or ""
        if "namespaces" not in attrs or "kube-system" not in body:
            continue
        try:
            if ast.literal_eval(attrs).get("k8s.resource.name") != "namespaces":
                continue
            meta = json.loads(body).get("metadata") or {}
        except (ValueError, SyntaxError, AttributeError):
            continue
        if meta.get("name") == "kube-system" and isinstance(meta.get("uid"), str):
            found = meta["uid"]
    return found or CLUSTER_UID_FALLBACK


@dataclass
class Scenario:
    key: str  # canonical id, e.g. "16"
    dir_name: str  # the dataset's directory name (the evaluator resolves it)
    source: Path
    copy: Path
    copy_stats: dict[str, Any] = field(default_factory=dict)
    ends: dict[str, int | None] = field(default_factory=dict)
    plan: EntryPlan | None = None
    cluster_uid: str = CLUSTER_UID_FALLBACK

    @property
    def label(self) -> str:
        return f"S{self.key}"


def prepare_scenario(dataset: Path, dir_name: str, round_dir: Path) -> Scenario:
    key = scenario_key(dir_name)
    source = dataset / dir_name
    copy = round_dir / f"S{key}" / "snapshot"
    copy.parent.mkdir(parents=True, exist_ok=True)
    stats = copy_whitelisted(source, copy)
    snapshot = _load(copy)
    try:
        ends = snapshot_end_times(snapshot)
    except ValueError as error:
        raise DeterministicError("SNAPSHOT_DATA_ERROR", str(error)[:200]) from None
    if all(value is None for value in ends.values()):
        raise DeterministicError("SNAPSHOT_EMPTY")
    return Scenario(
        key=key,
        dir_name=dir_name,
        source=source,
        copy=copy,
        copy_stats=stats,
        ends=ends,
        plan=entry_plan(snapshot),
        cluster_uid=cluster_uid(snapshot),
    )


def _load(copy: Path) -> Snapshot:
    try:
        return load_snapshot(copy)
    except AnswerFilePresent:
        raise DeterministicError("ANSWER_FILE_PRESENT") from None
    except UnsafeSnapshot:
        raise DeterministicError("UNSAFE_SNAPSHOT") from None


# -- configuration and injectable backends ---------------------------------------


@dataclass
class Config:
    round: str
    dataset: Path
    ground_truth: Path
    ports: Ports = field(default_factory=Ports)
    load_budget: timedelta = DEFAULT_LOAD_BUDGET
    trace: str = "lab"  # or "off"
    judge: bool = True
    allow_dirty: bool = False
    env_file: Path | None = None
    prometheus_account: Path | None = None
    tmp_root: Path = TMP_ROOT
    evidence_root: Path = EVIDENCE_ROOT
    poll_seconds: float = 5.0
    terminal_wait: timedelta = TERMINAL_WAIT
    worker_settle_seconds: float = 5.0
    git: dict[str, Any] = field(default_factory=dict)
    secrets_env: dict[str, str] = field(default_factory=dict)
    project: dict[str, Any] | None = None

    @property
    def round_dir(self) -> Path:
        return self.tmp_root / self.round

    @property
    def evidence_dir(self) -> Path:
        return self.evidence_root / f"itbench-lite-sre-{self.round}"

    @property
    def project_name(self) -> str | None:
        if self.trace != "lab":
            return None
        return lab_evidence.lab_project(f"itbench-lite-{self.round}")

    @property
    def experiment(self) -> str:
        return f"itbench-lite-{self.round}"


def _pg_start(directory: Path, port: int) -> Any:
    from scripts.eval_live_lab import LocalPgLauncher

    return LocalPgLauncher().start(directory, port)


def _pg_stop(instance: Any) -> None:
    from scripts.eval_live_lab import LocalPgLauncher

    LocalPgLauncher().stop(instance)


_NO_REDIRECT_OPENER = traces._OPENER  # no proxies, no redirects (#581)


def deliver(url: str, token: str, body: Mapping[str, Any]) -> tuple[int, Any]:
    """POST the webhook to the loopback intake; ``(status, parsed JSON body)``."""
    import urllib.error
    import urllib.request

    traces.require_loopback(url)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=traces.TIMEOUT_SECONDS) as r:
            status, raw = int(r.status), r.read()
    except urllib.error.HTTPError as error:
        status, raw = int(error.code), error.read()
    try:
        return status, json.loads(raw.decode() or "null")
    except ValueError:
        return status, None


def run_state(dsn: str, run_id: str) -> str | None:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(
            "SELECT state FROM opspilot_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def judge_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The judging process's environment, narrowed as r4 narrows the
    evaluator's: base paths, ``PYTHON_DOTENV_DISABLED=1``, the evaluator
    location, the provider choice and only that provider's three variables."""
    env = os.environ if environ is None else environ
    names = [EVALUATOR_DIR_ENV, PROVIDER_ENV, MODEL_OVERRIDE_ENV]
    provider = (env.get(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().casefold()
    spec = PROVIDERS.get(provider)
    if spec is not None:
        names += [spec.key_env, spec.base_env]
        if spec.model_env:
            names.append(spec.model_env)
    child = services.base_env(env) | {n: env[n] for n in names if env.get(n)}
    child["PYTHON_DOTENV_DISABLED"] = "1"
    return child


def run_judge(job: Mapping[str, Any]) -> int:
    """Start the judging process; returns its exit status.

    Its output is discarded: the evaluator it runs may print judge text that
    derives from the answer, and nothing answer-derived goes to a log.
    """
    argv = [
        sys.executable, "-m", "scripts.itbench_lite.judge_trial",
        "--ground-truth", str(job["ground_truth"]),
        "--scenario-dir", str(job["scenario_dir"]),
        "--outputs", str(job["outputs"]),
        "--trial", str(job["trial"]),
        "--work-dir", str(job["work_dir"]),
        "--out", str(job["out"]),
    ] + (["--judge"] if job["judge"] else [])  # fmt: skip
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=judge_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


@dataclass
class Backends:
    """Everything that touches the host; tests replace any of these."""

    start_pg: Callable[[Path, int], Any] = _pg_start
    stop_pg: Callable[[Any], None] = _pg_stop
    start_prometheus: Callable[[str, Path, Path, int], Started] = (
        services.start_prometheus
    )
    start_jaeger: Callable[[str, int, int], Started] = services.start_jaeger
    post_json: Callable[[str, Mapping[str, Any]], int] = traces.post_json_loopback
    start_fake_k8s: Callable[..., Started] = services.start_fake_k8s
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
    sleep: Callable[[float], None] = time.sleep


# -- one attempt -----------------------------------------------------------------


class Cleanup:
    """Reverse-order stop of everything an attempt started (L-D20)."""

    def __init__(self) -> None:
        self._items: list[tuple[str, Callable[[], None]]] = []
        self._lock = threading.Lock()
        self.order: list[str] = []

    def push(self, name: str, stop: Callable[[], None]) -> None:
        with self._lock:
            self._items.append((name, stop))

    def close(self) -> list[str]:
        errors = []
        with self._lock:
            items, self._items = self._items, []
        for name, stop in reversed(items):
            self.order.append(name)
            try:
                stop()
            except BaseException as error:  # noqa: BLE001 - clean up to the end
                errors.append(f"{name}:{type(error).__name__}")
        return errors


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _env_file_values(path: Path | None, names: Sequence[str]) -> dict[str, str]:
    if path is None:
        return {}
    values = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in names and value:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _prometheus_auth(path: Path | None) -> dict[str, str]:
    """L16: the replay Prometheus checks nothing; an account file is optional."""
    if path is None:
        return {}
    account = json.loads(path.read_text())
    return {
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": str(account["username"]),
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": str(account["password"]),
    }


def tool_env(cfg: Config) -> dict[str, str]:
    """The tool profile's addresses: same for web and worker (L-D3: fixed
    ports keep the registry revision equal across attempts)."""
    return {
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_OTEL_PROMETHEUS_URL": f"http://127.0.0.1:{cfg.ports.prometheus}",
        "OPSPILOT_OTEL_JAEGER_URL": f"http://127.0.0.1:{cfg.ports.jaeger_query}",
        "OPSPILOT_HEALTH_BIND": "",
        **_prometheus_auth(cfg.prometheus_account),
    }


def web_env(cfg: Config, pg: Any, identities: Path, token: str) -> dict[str, str]:
    return (
        services.base_env()
        | tool_env(cfg)
        | {
            "OPSPILOT_WEB_DSN": pg.web_dsn,
            "OPSPILOT_EVENT_TOKENS": f"{token_digest(token)}={EVENT_ACTOR}",
            "OPSPILOT_AUTH_REVISION": "itbench-lite",
            "OPSPILOT_BIND": f"127.0.0.1:{cfg.ports.web}",
            "OPSPILOT_SELF_HEAL_INTERVAL_SECONDS": "0",
            "OPSPILOT_TARGET_IDENTITIES": str(identities),
        }
    )


def worker_env(cfg: Config, pg: Any, k8s: Started, k8s_token: str) -> dict[str, str]:
    env = (
        services.base_env()
        | tool_env(cfg)
        | {
            "OPSPILOT_WORKER_DSN": pg.runtime_dsn,
            "OPSPILOT_K8S_API_URL": str(k8s.info["api_url"]),
            "OPSPILOT_K8S_CA_FILE": str(k8s.info["ca_file"]),
            "OPSPILOT_K8S_TOKEN": k8s_token,
            "OPSPILOT_K8S_NAMESPACE": NAMESPACE,
            "OPSPILOT_TRACE": cfg.trace,
            **cfg.secrets_env,
        }
    )
    if cfg.project_name:
        env["LANGSMITH_PROJECT"] = cfg.project_name
    return env


def write_identities(path: Path, scenario: Scenario) -> None:
    plan = scenario.plan
    assert plan is not None
    entry = {
        "integration_id": TARGET_ID,
        "cluster_uid": scenario.cluster_uid,
        "namespace": NAMESPACE,
        "workload": plan.workload,
        "health_profile_id": HEALTH_PROFILE_ID,
        "match": {"version": 1, "labels": plan.match_labels},
    }
    path.write_text(json.dumps({TARGET_ID: entry}, indent=1) + "\n")


@dataclass
class AttemptResult:
    run_id: str
    summary_path: Path
    outputs_root: Path
    report_present: bool


class Attempt:
    """One chance of one trial: start, deliver, wait, freeze, convert, stop."""

    def __init__(
        self,
        cfg: Config,
        backends: Backends,
        scenario: Scenario,
        trial: int,
        chance: int,
        vocabulary: list[str],
        history: list[dict[str, Any]],
    ) -> None:
        self.cfg, self.b, self.s = cfg, backends, scenario
        self.trial, self.chance, self.vocabulary = trial, chance, vocabulary
        self.history = history
        self.dir = (
            cfg.round_dir / scenario.label / f"trial-{trial}" / f"chance-{chance}"
        )
        self.id = f"{cfg.round}-s{scenario.key}-t{trial}-c{chance}"
        self.cleanup = Cleanup()
        self.owned_ports: dict[str, int] = {}
        self.block: dict[str, Any] = {
            "round": cfg.round,
            "scenario": scenario.key,
            "scenario_dir": scenario.dir_name,
            "trial": trial,
            "chance": chance,
            "ports": cfg.ports.all(),
            "load_budget_seconds": cfg.load_budget.total_seconds(),
            "git": cfg.git,
            "copy": scenario.copy_stats,
            "ends": {
                k: None if v is None else from_ns(v).isoformat()
                for k, v in scenario.ends.items()
            },
            "entry_alert": asdict(scenario.plan) if scenario.plan else None,
            "target_fallback": bool(scenario.plan and scenario.plan.target_fallback),
            "load": {},
            "images": {},
        }

    # The steps -----------------------------------------------------------

    def _started(self, item: Started) -> Started:
        self.cleanup.push(item.name, item.stop)
        return item

    def _check_ports(self) -> None:
        busy = [n for n, p in self.cfg.ports.all().items() if self.b.port_in_use(p)]
        if busy:
            raise InfraFailure("PORT_BUSY", ",".join(busy))
        # Only ports found free here can be ours to see released afterwards.
        self.owned_ports = self.cfg.ports.all()

    def _load_metrics(self, snapshot: Snapshot, offset: int) -> Started:
        began = time.monotonic()
        work = self.dir / "prometheus"
        work.mkdir(parents=True, exist_ok=True)
        om = work / "metrics.om"
        try:
            with om.open("w", encoding="utf-8") as out:
                stats = metrics.convert_metrics(snapshot, out, offset_seconds=offset)
        except ValueError as error:
            raise DeterministicError("SNAPSHOT_DATA_ERROR", str(error)[:200]) from None
        item = self._started(
            self.b.start_prometheus(
                f"itbl-{self.id}-prom", om, work, self.cfg.ports.prometheus
            )
        )
        self.block["load"]["metrics"] = {
            **asdict(stats),
            "seconds": round(time.monotonic() - began, 3),
        }
        self.block["images"]["prometheus"] = item.info
        return item

    def _load_traces(self, snapshot: Snapshot, offset: int) -> Started:
        began = time.monotonic()
        item = self._started(
            self.b.start_jaeger(
                f"itbl-{self.id}-jaeger",
                self.cfg.ports.jaeger_query,
                self.cfg.ports.jaeger_otlp,
            )
        )
        endpoint = f"http://127.0.0.1:{self.cfg.ports.jaeger_otlp}/v1/traces"
        stats = traces.TraceStats()
        try:
            for body in traces.iter_otlp_requests(
                snapshot, offset_seconds=offset, stats=stats
            ):
                self.b.post_json(endpoint, body)
        except ValueError as error:
            raise DeterministicError("SNAPSHOT_DATA_ERROR", str(error)[:200]) from None
        except OSError as error:
            raise InfraFailure("TRACE_LOAD_FAILED", type(error).__name__) from None
        self.block["load"]["traces"] = {
            **asdict(stats),
            "seconds": round(time.monotonic() - began, 3),
        }
        self.block["images"]["jaeger"] = item.info
        return item

    def _fake_k8s(self, offset: int, token: str) -> Started:
        began = time.monotonic()
        work = self.dir / "fake_k8s"
        try:
            item = self.b.start_fake_k8s(
                self.s.copy,
                offset_seconds=offset,
                token=token,
                work_dir=work,
                port=self.cfg.ports.fake_k8s,
            )
        except services.ServiceError as error:
            log = work / "fake_k8s.log"
            if log.is_file() and "SnapshotRowError" in log.read_text(errors="replace"):
                raise DeterministicError("SNAPSHOT_ROW_ERROR") from None
            raise InfraFailure(error.code, str(error)) from None
        self._started(item)
        self.block["load"]["fake_k8s"] = {"seconds": round(time.monotonic() - began, 3)}
        return item

    def _parallel(
        self, tasks: Mapping[str, Callable[[], Started]]
    ) -> dict[str, Started]:
        results: dict[str, Started] = {}
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {name: pool.submit(task) for name, task in tasks.items()}
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except BaseException as error:  # noqa: BLE001 - reraised below
                    errors.append(error)
        if errors:
            # Deterministic outranks transient: retrying cannot fix the data.
            errors.sort(key=lambda e: not isinstance(e, DeterministicError))
            raise errors[0]
        return results

    def run(self) -> AttemptResult:
        try:
            return self._run()
        finally:
            failures = self.cleanup.close()
            failures += self._ports_released()
            if failures:
                raise RoundAbort("CLEANUP_FAILED", ",".join(failures))

    def _ports_released(self) -> list[str]:
        busy: list[str] = []
        for _ in range(20):
            busy = [n for n, p in self.owned_ports.items() if self.b.port_in_use(p)]
            if not busy:
                return []
            self.b.sleep(0.5)
        return [f"port:{n}" for n in busy]

    def _run(self) -> AttemptResult:
        cfg, b = self.cfg, self.b
        self.dir.mkdir(parents=True, exist_ok=True)
        snapshot = _load(self.s.copy)  # re-validated every attempt (L-D6)
        self.block["memory_before"] = b.memory_reading()
        self._check_ports()

        # r4 as worded (lead 2026-11-13): deliver right after loading, void
        # when loading-to-delivery exceeds 2 minutes. The offset is fixed
        # before loading and anchored on this moment, so the shifted data
        # always ends before the (later) delivery; ``--load-budget-seconds``
        # is only the ceiling on loading.
        started_at = b.now()
        offset = compute_offset_seconds(self.s.ends, started_at)
        check_not_future(self.s.ends, offset, started_at)
        self.block.update(started_at=_iso(started_at), offset_seconds=offset)

        try:
            pg = b.start_pg(self.dir / "pg", cfg.ports.pg)
        except Exception as error:  # noqa: BLE001 - LabError codes
            raise InfraFailure(getattr(error, "code", "PG_START_FAILED")) from None
        self.cleanup.push("pg", lambda: b.stop_pg(pg))

        k8s_token = secrets.token_urlsafe(24)
        event_token = secrets.token_urlsafe(24)
        identities = self.dir / "target_identities.json"
        write_identities(identities, self.s)
        try:
            loaded = self._parallel(
                {
                    "prometheus": lambda: self._load_metrics(snapshot, offset),
                    "jaeger": lambda: self._load_traces(snapshot, offset),
                    "fake_k8s": lambda: self._fake_k8s(offset, k8s_token),
                    "web": lambda: self._started(
                        b.start_web(
                            web_env(cfg, pg, identities, event_token),
                            cfg.ports.web,
                            self.dir / "web.log",
                        )
                    ),
                }
            )
            loaded_at = b.now()
            if loaded_at - started_at > cfg.load_budget:
                raise InfraFailure(
                    "LOAD_BUDGET_EXCEEDED",
                    f"{(loaded_at - started_at).total_seconds():.0f}s",
                )
            worker = self._started(
                b.start_worker(
                    worker_env(cfg, pg, loaded["fake_k8s"], k8s_token),
                    self.dir / "worker.log",
                    settle_seconds=cfg.worker_settle_seconds,
                )
            )
        except services.ServiceError as error:
            raise InfraFailure(error.code, str(error)) from None
        self.block["loaded_at"] = _iso(loaded_at)

        # Delivery (L-D7): immediately, checked before POST so a void costs
        # no model call.
        now = b.now()
        self.block["delivered_at"] = _iso(now)
        self.block["load_seconds"] = (loaded_at - started_at).total_seconds()
        self.block["gap_seconds"] = (now - loaded_at).total_seconds()
        self.block["data_end_to_delivery_seconds"] = (
            now - from_ns(latest_shifted_ns(self.s.ends, offset))
        ).total_seconds()
        if not delivery_gap_ok(loaded_at, now):
            raise VoidAttempt(
                "DELIVERY_GAP_EXCEEDED", f"{self.block['gap_seconds']:.1f}s"
            )
        try:
            check_not_future(self.s.ends, offset, now)
        except ValueError:
            raise VoidAttempt("DATA_AFTER_DELIVERY") from None
        assert self.s.plan is not None
        body = entry_webhook(
            snapshot,
            offset_seconds=offset,
            target=EntryTarget(match_labels=self.s.plan.match_labels),
        )
        url = f"http://127.0.0.1:{cfg.ports.web}{INTAKE_PATH}"
        try:
            status, reply = b.deliver(url, event_token, body)
        except OSError as error:
            raise InfraFailure("INTAKE_UNREACHABLE", type(error).__name__) from None
        responded = b.now()
        self.block["responded_at"] = _iso(responded)
        result = (
            (reply or {}).get("results", [{}])[0] if isinstance(reply, dict) else {}
        )
        self.block["intake"] = {
            "status": status,
            "outcome": result.get("outcome"),
            "reason": result.get("reason"),
        }
        if result.get("reason") in ("TARGET_UNRESOLVED", "TARGET_AMBIGUOUS"):
            raise RoundAbort(str(result["reason"]), self.s.label)
        run_id = result.get("run_id")
        if status != 200 or result.get("outcome") != "created" or not run_id:
            raise InfraFailure("INTAKE_REFUSED", f"{status} {result.get('outcome')}")
        if not delivery_gap_ok(loaded_at, responded):
            raise VoidAttempt("DELIVERY_GAP_EXCEEDED_AFTER_POST")

        # Wait for a terminal state (L-D10); the worker must stay alive.
        began = time.monotonic()
        deadline = b.now() + cfg.terminal_wait
        state = None
        while True:
            state = b.run_state(pg.owner_dsn, run_id)
            if state not in (None, "queued", "running"):
                break
            if not worker.alive():
                raise InfraFailure("WORKER_EXITED")
            if b.now() >= deadline:
                break
            b.sleep(cfg.poll_seconds)
        self.block["run_wait"] = {
            "terminal_state": state,
            "timed_out": state in (None, "queued", "running"),
            "seconds": round(time.monotonic() - began, 3),
        }
        worker.stop()  # flushes its spans before the read-back

        run_block, ledger = b.collect(pg.owner_dsn, run_id)
        conversion, reason = self._convert(run_block)
        self.block["conversion"] = {**conversion.meta, "reason": reason}
        trace = b.trace_roots(cfg.project_name, run_id, cfg.secrets_env)
        self.block["history"] = list(self.history)
        summary = {
            "schema": SUMMARY_SCHEMA,
            "status": "completed",
            "run": run_block,
            "trace": trace,
            "itbench_lite": self.block,
        }
        # Outputs first, summary second, completion marker last (after judging,
        # ``run_trial``): a crash in between is never mistaken for a done trial.
        outputs_root = self.dir.parent / "outputs"
        if outputs_root.exists():
            shutil.rmtree(outputs_root)
        answer.write_trial(outputs_root, self.s.dir_name, self.trial, conversion)
        evidence = cfg.evidence_dir / self.s.label / f"trial-{self.trial}"
        _, summary_path = lab_evidence.freeze(
            ledger,
            experiment=cfg.experiment,
            run_id=run_id,
            evidence_dir=evidence,
            summary=summary,
        )
        return AttemptResult(
            run_id, summary_path, outputs_root, conversion.meta["report_present"]
        )

    def _convert(
        self, run_block: Mapping[str, Any]
    ) -> tuple[answer.Conversion, str | None]:
        report = run_block.get("report")
        evidence = run_freeze.flat_evidence(run_block)
        if not isinstance(report, Mapping):
            return answer.convert(None, [], self.vocabulary), (
                None if report is None else "REPORT_INVALID_FOR_CONVERSION"
            )
        try:
            return answer.convert(report, evidence, self.vocabulary), None
        except ValueError:
            return answer.convert(
                None, [], self.vocabulary
            ), "REPORT_INVALID_FOR_CONVERSION"


# -- trials, rounds, judging --------------------------------------------------------


def _numeric(values: Any) -> dict[str, float]:
    if not isinstance(values, Mapping):
        return {}
    return {
        str(k): float(v)
        for k, v in values.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def judge_trial(
    cfg: Config,
    backends: Backends,
    scenario_dir: str,
    label: str,
    trial: int,
    *,
    judge: bool,
) -> dict[str, Any]:
    """Run the judging process (retried); return its numeric scores only."""
    trial_dir = cfg.round_dir / label / f"trial-{trial}"
    out = trial_dir / "scores.json"
    status: int | str | None = None
    for attempt in range(1, JUDGE_TRIES + 1):
        work = trial_dir / f"judge-{attempt}"
        if work.exists():
            shutil.rmtree(work)
        status = backends.judge(
            {
                "ground_truth": cfg.ground_truth,
                "scenario_dir": scenario_dir,
                "outputs": trial_dir / "outputs",
                "trial": trial,
                "work_dir": work,
                "out": out,
                "judge": judge,
            }
        )
        if status == 0:
            data = _read_json(out)
            if isinstance(data, Mapping) and _numeric(data.get("det")):
                break
            status = "SCORES_INVALID"  # exit 0 but a truncated or bad file
    if status != 0:
        return {"status": "failed", "tries": JUDGE_TRIES, "exit": status}
    return {
        "status": "ok",
        "judged": bool(data.get("judged")),
        "det": _numeric(data.get("det")),
        "official": _numeric(data.get("official")) if data.get("official") else None,
    }


def _write_status_summary(
    path: Path, status: str, reason: str, block: Mapping[str, Any]
) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / "summary.json").write_text(
        json.dumps(
            {
                "schema": SUMMARY_SCHEMA,
                "status": status,
                "reason": reason,
                "frozen_at": datetime.now(UTC).isoformat(),
                "itbench_lite": block,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n"
    )


MARKER = "complete.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_marker(
    cfg: Config, label: str, trial: int, record: Mapping[str, Any]
) -> None:
    """The trial's completion marker, written atomically and last."""
    path = cfg.round_dir / label / f"trial-{trial}" / MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(record, indent=1, default=str) + "\n")
    os.replace(partial, path)


def trial_state(cfg: Config, label: str, scenario_dir: str, trial: int) -> str:
    """``done``, ``missing`` (a terminal missing record), ``judge`` (Run frozen
    and converted, scoring not finished) or ``rerun``.

    ``done`` needs the marker, a completed summary, the evaluator output and a
    scores file with the deterministic score.
    """
    summary = _read_json(cfg.evidence_dir / label / f"trial-{trial}" / "summary.json")
    if not isinstance(summary, Mapping):
        return "rerun"
    status = summary.get("status")
    if status == "missing" and summary.get("reason"):
        return "missing"
    if status != "completed":
        return "rerun"
    trial_dir = cfg.round_dir / label / f"trial-{trial}"
    output = trial_dir / "outputs" / scenario_dir / str(trial) / "outputs"
    if not (output / "agent_output.json").is_file():
        return "rerun"
    scores = _read_json(trial_dir / "scores.json")
    marker = _read_json(trial_dir / MARKER)
    if (
        isinstance(marker, Mapping)
        and marker.get("status") == "completed"
        and isinstance(scores, Mapping)
        and _numeric(scores.get("det"))
    ):
        return "done"
    return "judge"


def trial_done(cfg: Config, label: str, scenario_dir: str, trial: int) -> bool:
    return trial_state(cfg, label, scenario_dir, trial) in ("done", "missing")


def _set_aside(cfg: Config, label: str, trial: int) -> None:
    """Move an unfinished trial's evidence directory into the ignored tree so
    the rerun can freeze afresh; nothing is deleted."""
    evidence = cfg.evidence_dir / label / f"trial-{trial}"
    if evidence.exists():
        stale = cfg.round_dir / label / f"trial-{trial}" / "stale"
        stale.mkdir(parents=True, exist_ok=True)
        shutil.move(
            str(evidence), stale / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        )


def finish_judging(
    cfg: Config,
    backends: Backends,
    scenario_dir: str,
    label: str,
    trial: int,
    *,
    judge: bool | None = None,
) -> dict[str, Any]:
    """Judge an already frozen trial and write its completion marker once the
    deterministic score exists (a failed judge call is re-run by ``judge``)."""
    use = cfg.judge if judge is None else judge
    outcome = judge_trial(cfg, backends, scenario_dir, label, trial, judge=use)
    scores = _read_json(cfg.round_dir / label / f"trial-{trial}" / "scores.json")
    if isinstance(scores, Mapping) and _numeric(scores.get("det")):
        write_marker(cfg, label, trial, {"status": "completed", "judge": outcome})
    return outcome


def run_trial(
    cfg: Config,
    backends: Backends,
    scenario: Scenario,
    trial: int,
    vocabulary: list[str],
) -> dict[str, Any]:
    """Up to ``CHANCES`` attempts; returns a small record for the caller."""
    history: list[dict[str, Any]] = []
    evidence = cfg.evidence_dir / scenario.label / f"trial-{trial}"
    for chance in range(1, CHANCES + 1):
        attempt = Attempt(cfg, backends, scenario, trial, chance, vocabulary, history)
        try:
            result = attempt.run()
        except (VoidAttempt, InfraFailure) as error:
            history.append(
                {
                    "chance": chance,
                    "kind": "void" if isinstance(error, VoidAttempt) else "infra",
                    "code": error.code,
                    "detail": error.detail[:300],
                    "at": _iso(backends.now()),
                    "cleanup_order": attempt.cleanup.order,
                }
            )
            continue
        except DeterministicError:
            raise
        except KeyboardInterrupt:
            history.append({"chance": chance, "kind": "interrupted"})
            raise
        record: dict[str, Any] = {
            "status": "completed",
            "run_id": result.run_id,
            "history": history,
        }
        record["judge"] = finish_judging(
            cfg, backends, scenario.dir_name, scenario.label, trial
        )
        return record
    block = {
        "scenario": scenario.key,
        "trial": trial,
        "history": history,
        "git": cfg.git,
    }
    _write_status_summary(evidence, "missing", "CHANCES_EXHAUSTED", block)
    return {"status": "missing", "history": history}


def run_scenario(
    cfg: Config,
    backends: Backends,
    dir_name: str,
    vocabulary_file: Path,
    trials: Sequence[int],
) -> dict[str, Any]:
    key = scenario_key(dir_name)
    label = f"S{key}"
    states = {t: trial_state(cfg, label, dir_name, t) for t in trials}
    records: dict[int, Any] = {}
    for trial, state in states.items():
        if state == "judge":  # Run already frozen: finish scoring only
            records[trial] = {
                "status": "completed",
                "resumed": "judge",
                "judge": finish_judging(cfg, backends, dir_name, label, trial),
            }
    pending = [t for t, state in states.items() if state == "rerun"]
    if not pending:
        skipped = [t for t, state in states.items() if state in ("done", "missing")]
        if records:
            return {"scenario": key, "trials": records, "skipped": skipped}
        return {"scenario": key, "skipped": skipped}
    for trial in pending:
        _set_aside(cfg, label, trial)
    try:
        check_input_file(vocabulary_file, "vocabulary")
        if not vocabulary_file.is_file():
            raise DeterministicError("VOCABULARY_MISSING", str(vocabulary_file.name))
        vocabulary = vocabulary_file.read_text().splitlines()
        scenario = prepare_scenario(cfg.dataset, dir_name, cfg.round_dir)
    except DeterministicError as error:
        missing = _all_missing(cfg, label, key, pending, error)
        return {"scenario": key, "trials": {**records, **missing["trials"]}}
    for trial in pending:
        try:
            records[trial] = run_trial(cfg, backends, scenario, trial, vocabulary)
        except DeterministicError as error:
            remaining = [t for t in pending if t not in records]
            records.update(_all_missing(cfg, label, key, remaining, error)["trials"])
            break
    return {"scenario": key, "trials": records}


def _all_missing(
    cfg: Config, label: str, key: str, trials: Sequence[int], error: DriverError
) -> dict[str, Any]:
    out = {}
    for trial in trials:
        block = {
            "scenario": key,
            "trial": trial,
            "detail": error.detail[:300],
            "git": cfg.git,
        }
        _write_status_summary(
            cfg.evidence_dir / label / f"trial-{trial}", "missing", error.code, block
        )
        out[trial] = {"status": "missing", "reason": error.code}
    return {"scenario": key, "trials": out}


def aggregate_scores(cfg: Config) -> dict[str, list[dict[str, float]]]:
    """``{scenario id: [trial scores]}`` for ``coverage.py``; a missing or
    unscored trial is absent and counts 0 there (L5)."""
    table: dict[str, list[dict[str, float]]] = {}
    if not cfg.round_dir.is_dir():
        return table
    for scenario in sorted(cfg.round_dir.glob("S*")):
        key = scenario_key(scenario.name)
        rows = table.setdefault(key, [])
        for trial in sorted(scenario.glob("trial-*")):
            path = trial / "scores.json"
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
            rows.append({**_numeric(data.get("det")), **_numeric(data.get("official"))})
    return table


def git_state(allow_dirty: bool) -> dict[str, Any]:
    """L-D18: tracked changes outside this round's evidence count as dirty."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
        ).stdout

    commit = git("rev-parse", "HEAD").strip()
    changed = [
        line[3:]
        for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
        if not line[3:].startswith("docs/evidence/itbench-lite-sre-")
    ]
    if changed and not allow_dirty:
        raise RoundAbort("WORKTREE_DIRTY", ",".join(changed[:5]))
    return {"commit": commit, "dirty": bool(changed), "allow_dirty": allow_dirty}


def check_input_file(path: Path | None, what: str) -> None:
    """A driver-side input never names, or points at, an answer file."""
    if path is None:
        return
    names = [path.name]
    if path.exists() or path.is_symlink():
        names.append(path.resolve().name)
    if any(_is_answer_name(name) for name in names):
        raise RoundAbort("ANSWER_FILE_AS_INPUT", what)


def prepare_round(cfg: Config, backends: Backends) -> None:
    if not _ROUND.match(cfg.round):
        raise RoundAbort("BAD_ROUND", cfg.round)
    check_input_file(cfg.env_file, "env-file")
    check_input_file(cfg.prometheus_account, "prometheus-account")
    cfg.git = git_state(cfg.allow_dirty)
    cfg.secrets_env = _env_file_values(cfg.env_file, (*_PASS_KEYS, *_LANGSMITH_KEYS))
    for name in (*_PASS_KEYS, *_LANGSMITH_KEYS):
        if name not in cfg.secrets_env and os.environ.get(name):
            cfg.secrets_env[name] = os.environ[name]
    if "DEEPSEEK_API_KEY" not in cfg.secrets_env:
        raise RoundAbort("DEEPSEEK_API_KEY_MISSING")
    if cfg.trace == "lab":
        if "LANGSMITH_API_KEY" not in cfg.secrets_env:
            raise RoundAbort("LANGSMITH_API_KEY_MISSING")
        client = lab_evidence.langsmith_client(cfg.secrets_env)
        project = cfg.project_name
        assert project is not None
        cfg.project = lab_evidence.ensure_longlived_project(client, project)


# -- CLI --------------------------------------------------------------------------


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--round", required=True, help="label, e.g. 20261120")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument(
        "--ground-truth", type=Path, help="answer root; default --dataset"
    )
    parser.add_argument(
        "--load-budget-seconds",
        type=float,
        default=DEFAULT_LOAD_BUDGET.total_seconds(),
        help="ceiling on loading; delivery follows loading at once",
    )
    parser.add_argument("--trace", choices=("lab", "off"), default="lab")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--env-file", type=Path, help="private KEY=value file")
    parser.add_argument(
        "--prometheus-account", type=Path, help="JSON username/password"
    )
    parser.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    parser.add_argument("--tmp-root", type=Path, default=TMP_ROOT)
    for name, default in Ports().all().items():
        parser.add_argument(
            f"--{name.replace('_', '-')}-port", type=int, default=default
        )


def _config(args: argparse.Namespace) -> Config:
    return Config(
        round=args.round,
        dataset=args.dataset.resolve(),
        ground_truth=(args.ground_truth or args.dataset).resolve(),
        ports=Ports(**{n: getattr(args, f"{n}_port") for n in Ports().all()}),
        load_budget=timedelta(seconds=args.load_budget_seconds),
        trace=args.trace,
        judge=not args.no_judge,
        allow_dirty=args.allow_dirty,
        env_file=args.env_file,
        prometheus_account=args.prometheus_account,
        tmp_root=args.tmp_root.resolve(),
        evidence_root=args.evidence_root.resolve(),
    )


def _scenario_dirs(dataset: Path, only: str | None) -> list[str]:
    names = []
    for entry in os.scandir(dataset):
        if not entry.is_dir():
            continue
        try:
            key = scenario_key(entry.name)
        except ValueError:
            continue
        names.append((int(key), entry.name))
    wanted = None if not only else {scenario_key(x) for x in only.split(",")}
    return [n for k, n in sorted(names) if wanted is None or str(k) in wanted]


def main(argv: Sequence[str] | None = None, backends: Backends | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("attempt", help="one scenario, one trial")
    _common(one)
    one.add_argument("--scenario", required=True, help="scenario directory name")
    one.add_argument("--trial", type=int, required=True)
    one.add_argument("--vocabulary", type=Path, required=True)
    many = sub.add_parser("round", help="all scenarios, sorted, 3 trials each")
    _common(many)
    many.add_argument(
        "--vocabulary-dir", type=Path, required=True, help="S<id>.txt files"
    )
    many.add_argument("--scenarios", help="comma-separated ids, default all")
    many.add_argument("--trials", type=int, default=3)
    judge = sub.add_parser("judge", help="judge trials whose scores are not judged")
    judge.add_argument("--round", required=True)
    judge.add_argument("--ground-truth", required=True, type=Path)
    judge.add_argument("--tmp-root", type=Path, default=TMP_ROOT)
    scores = sub.add_parser("scores", help="coverage.py scores file of a round")
    scores.add_argument("--round", required=True)
    scores.add_argument("--out", required=True, type=Path)
    scores.add_argument("--tmp-root", type=Path, default=TMP_ROOT)
    args = parser.parse_args(argv)
    backends = backends or Backends()

    if args.command in ("judge", "scores"):
        cfg = Config(
            round=args.round,
            dataset=Path(),
            ground_truth=getattr(args, "ground_truth", None) or Path(),
            tmp_root=args.tmp_root.resolve(),
        )
        if args.command == "scores":
            table = aggregate_scores(cfg)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
            print(json.dumps({"out": str(args.out), "scenarios": len(table)}))
            return 0
        results = []
        for trial_dir in sorted(cfg.round_dir.glob("S*/trial-*")):
            meta = trial_dir / "outputs"
            scored = trial_dir / "scores.json"
            if not meta.is_dir():
                continue
            if scored.is_file() and json.loads(scored.read_text()).get("judged"):
                continue
            scenario_dir = next(p.name for p in meta.iterdir() if p.is_dir())
            trial = int(trial_dir.name.split("-")[1])
            outcome = finish_judging(
                cfg, backends, scenario_dir, trial_dir.parent.name, trial, judge=True
            )
            results.append(
                {"trial": str(trial_dir.relative_to(cfg.round_dir)), **outcome}
            )
        print(json.dumps(results, indent=1))
        return 0 if all(r["status"] == "ok" for r in results) else 1

    cfg = _config(args)
    try:
        prepare_round(cfg, backends)
        if args.command == "attempt":
            report = run_scenario(
                cfg, backends, args.scenario, args.vocabulary, [args.trial]
            )
            print(json.dumps(report, indent=1, default=str))
            return 0
        reports = []
        for dir_name in _scenario_dirs(cfg.dataset, args.scenarios):
            key = scenario_key(dir_name)
            reports.append(
                run_scenario(
                    cfg,
                    backends,
                    dir_name,
                    args.vocabulary_dir / f"S{key}.txt",
                    range(1, args.trials + 1),
                )
            )
            print(json.dumps(reports[-1], default=str), flush=True)
        return 0
    except RoundAbort as error:
        print(
            json.dumps(
                {"status": "aborted", "code": error.code, "detail": error.detail}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
